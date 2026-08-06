"""
GitHub integration for the PR webhook flow.

Responsibilities:
    - verify inbound webhook signatures
    - pull the PR's changed files and filter to what the existing agents
      understand (mirrors AuditorAgent's py/js_ts extension patterns)
    - scope a cloned checkout down to just those files, then hand off to
      the *existing* orchestrator/workflow unchanged
    - post the consolidated result back to the PR
    - a minimal idempotency cache keyed on (owner, repo, pr, head_sha)

None of this touches app/agents or app/workflows — it only calls the
public surface those modules already expose (RepoClonerService, the
orchestrator returned by get_orchestrator()).
"""

import hashlib
import hmac
import os
from pathlib import Path
from typing import Any

import requests

from app.core.logger import logger
from app.services.code_review_service import RepoClonerService

GITHUB_API_BASE = "https://api.github.com"

# Mirrors AuditorAgent.js_ts_patterns / py_patterns (app/agents/auditor_agent.py) —
# these are the only file types any existing agent actually does anything with.
RELEVANT_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx"}

# ---------------------------------------------------------------------------
# Idempotency cache
#
# Deliberately minimal: an in-memory dict, matching the fact that the rest of
# this app has no persistence layer yet either (see the TODOs in
# app/routers/code_review.py — /status and /report don't actually look
# anything up). This is fine for a single process but will NOT survive a
# restart and will NOT be shared across replicas. Swap for Redis/a DB table
# here (and nowhere else) once that lands.
# ---------------------------------------------------------------------------
_RUN_CACHE: dict[str, dict[str, Any]] = {}


def compute_run_id(owner: str, repo: str, pr_number: int, head_sha: str) -> str:
    """Deterministic id for a (PR, commit) pair — doubles as the idempotency key."""
    key = f"{owner}/{repo}#{pr_number}@{head_sha}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def get_cached_run(run_id: str) -> dict[str, Any] | None:
    return _RUN_CACHE.get(run_id)


def save_run(run_id: str, data: dict[str, Any]) -> None:
    _RUN_CACHE[run_id] = data


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    """
    Verify GitHub's X-Hub-Signature-256 header via HMAC-SHA256.

    Fails closed: a missing secret, missing header, or malformed header all
    verify as False rather than being treated as "no verification requested".
    """
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# PR payload parsing
# ---------------------------------------------------------------------------


class PRContext:
    """Everything pulled out of a pull_request webhook payload that we need downstream."""

    def __init__(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        head_ref: str,
        head_clone_url: str,
    ):
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.head_sha = head_sha
        self.head_ref = head_ref
        self.head_clone_url = head_clone_url


def parse_pr_context(payload: dict[str, Any]) -> PRContext:
    """
    Raises KeyError if the payload is missing fields we need — callers should
    treat that as a malformed/unexpected payload (400), not a 500.
    """
    pr = payload["pull_request"]
    head = pr["head"]

    return PRContext(
        owner=payload["repository"]["owner"]["login"],
        repo=payload["repository"]["name"],
        pr_number=pr["number"],
        head_sha=head["sha"],
        head_ref=head["ref"],
        # Use the head repo's clone URL (not the base repo's) so PRs from
        # forks — where the branch only exists in the fork — clone correctly.
        head_clone_url=head["repo"]["clone_url"],
    )


# ---------------------------------------------------------------------------
# GitHub REST calls
#
# Uses `requests` (sync), matching the existing HTTP client already used in
# app/services/code_review_service.py (RepoClonerService._repo_exists). This
# whole flow runs inside a FastAPI BackgroundTasks callable the same way
# orchestrator.run already does, i.e. off the event loop via threadpool, so
# a sync HTTP client here is consistent rather than a shortcut.
# ---------------------------------------------------------------------------


def _github_headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_pr_files(owner: str, repo: str, pr_number: int, token: str) -> list[dict[str, Any]]:
    """GET /repos/{owner}/{repo}/pulls/{pr}/files, following pagination."""
    files: list[dict[str, Any]] = []
    url: str | None = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    params: dict[str, Any] | None = {"per_page": 100}

    while url:
        response = requests.get(url, headers=_github_headers(token), params=params, timeout=30)
        response.raise_for_status()
        files.extend(response.json())
        url = response.links.get("next", {}).get("url")
        params = None  # `next` URLs from the Link header already carry query params

    return files


def filter_relevant_files(files: list[dict[str, Any]]) -> list[str]:
    """
    Repo-relative paths of changed files that at least one existing agent
    can do something with. Removed files are skipped — there's nothing left
    to check out and analyze.
    """
    relevant = []
    for f in files:
        if f.get("status") == "removed":
            continue
        filename = f.get("filename", "")
        if Path(filename).suffix.lower() in RELEVANT_EXTENSIONS:
            relevant.append(filename)
    return relevant


def post_summary_comment(owner: str, repo: str, pr_number: int, markdown: str, token: str) -> None:
    """
    POST /repos/{owner}/{repo}/pulls/{pr}/reviews with the consolidated
    markdown as a single summary comment (option (a) from the spec).

    TODO (option (b)): post inline comments per finding instead of/in
    addition to this. That needs each finding's file+line mapped against the
    PR diff (GitHub only accepts inline comments on lines that appear in the
    diff hunk) — requires parsing the `patch` field from fetch_pr_files()
    output. Deferred for simplicity; the consolidated summary covers the
    same findings today.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    body = {"body": markdown, "event": "COMMENT"}
    response = requests.post(url, headers=_github_headers(token), json=body, timeout=30)
    response.raise_for_status()


# ---------------------------------------------------------------------------
# Scoping the clone to changed files
# ---------------------------------------------------------------------------


def _prune_to_files(repo_path: str, relevant_files: list[str]) -> None:
    """
    Delete everything under repo_path except `relevant_files`, so that the
    *unmodified* AuditorAgent (which always os.walk()s the whole repo_path —
    see app/agents/auditor_agent.py) and every downstream tool (all of which
    are invoked with repo_path as their scan root — see style/security/
    performance agents) end up only ever seeing the PR's changed files.

    This is intentionally done at the filesystem level rather than by adding
    a file-list parameter to the agents/workflow, per "do not duplicate
    orchestration logic" / "don't refactor unrelated existing code": the
    agents already accept a repo_path and nothing else, so controlling what
    exists at repo_path is the only non-invasive way to scope the run.

    Trade-off worth knowing: linters lose full-project context (e.g. cross-
    file import resolution). Acceptable for a diff-scoped PR check; not a
    replacement for the full-repo /api/v1/review/analyze run.
    """
    keep = {str((Path(repo_path) / rel).resolve()) for rel in relevant_files}

    for root, _dirs, files in os.walk(repo_path, topdown=False):
        if ".git" in Path(root).parts:
            continue  # leave version control metadata untouched

        for name in files:
            full_path = Path(root) / name
            if str(full_path.resolve()) not in keep:
                try:
                    full_path.unlink()
                except OSError as exc:
                    logger.debug(f"Could not remove {full_path} while pruning: {exc}")

        # Best-effort cleanup of now-empty directories (skip repo root and .git)
        if root != repo_path and ".git" not in Path(root).parts:
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def build_markdown_summary(merged_findings: list[Any] | None, analyzed_files: list[str]) -> str:
    """
    Build the PR comment body from orchestrator.state["merged_findings"].

    Note: we read merged_findings directly rather than
    state["markdown_report"] / ExplainerAgent's output, because
    ExplainerAgent.run() (app/agents/explainer_agent.py) currently writes to
    a hardcoded "explanation_report.md" in the process CWD and returns a
    placeholder string — it never populates state["markdown_report"] (that
    assignment is commented out in code_review_workflow.py). That file path
    also isn't safe under concurrent webhook-triggered runs. Once the
    explainer is wired to actually return a report, swap this function's
    body for `state["markdown_report"]` and delete the formatting below.

    merged_findings, per conflict_resolver() in code_review_workflow.py, is
    always the 3-element list [style_findings, security_findings,
    performance_findings] — a list[dict] and two differently-shaped pydantic
    models. Handled defensively since that shape isn't itself documented
    anywhere.
    """
    lines = ["## 🤖 MARC-AI Automated Review", ""]
    lines.append(f"Analyzed **{len(analyzed_files)}** changed file(s):")
    for f in analyzed_files[:20]:
        lines.append(f"- `{f}`")
    if len(analyzed_files) > 20:
        lines.append(f"- ...and {len(analyzed_files) - 20} more")
    lines.append("")

    if not merged_findings:
        lines.append("No findings were produced by the analysis pipeline.")
        lines.append("")
        lines.append("---")
        lines.append("_Posted automatically by MARC-AI._")
        return "\n".join(lines)

    padded = list(merged_findings) + [None, None, None]
    style_findings, security_findings, performance_findings = padded[:3]

    lines.append("### Style")
    if style_findings:
        for entry in style_findings:
            tool = entry.get("tool", "unknown")
            output = entry.get("output")
            if isinstance(output, list):
                count = len(output)
            elif isinstance(output, dict):
                count = sum(
                    r.get("errorCount", 0) + r.get("warningCount", 0)
                    for r in output.get("results", [])
                )
            else:
                count = 0
            lines.append(f"- **{tool}**: {count} issue(s)")
    else:
        lines.append("- No style findings.")
    lines.append("")

    lines.append("### Security")
    bandit_results = getattr(getattr(security_findings, "Bandit", None), "results", None) or []
    semgrep_results = getattr(getattr(security_findings, "Semgrep", None), "results", None) or []
    lines.append(f"- **Bandit**: {len(bandit_results)} issue(s)")
    lines.append(f"- **Semgrep**: {len(semgrep_results)} issue(s)")
    lines.append("")

    lines.append("### Performance")
    summary = getattr(performance_findings, "summary", None) or {}
    if summary:
        for key, value in summary.items():
            lines.append(f"- **{key.replace('_', ' ').title()}**: {value}")
    else:
        lines.append("- No performance metrics produced.")
    lines.append("")

    lines.append("---")
    lines.append("_Posted automatically by MARC-AI. Full findings available via the review API._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Background task entry point
# ---------------------------------------------------------------------------


def run_scoped_pr_analysis(
    orchestrator: Any,
    run_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    head_ref: str,
    head_clone_url: str,
    token: str,
) -> None:
    """
    The actual webhook work, run as a FastAPI background task — mirrors
    exactly how POST /api/v1/review/analyze already schedules
    `orchestrator.run` as a plain sync callable (app/routers/code_review.py),
    which Starlette runs off the event loop via a threadpool. Kept sync end
    to end (including the `requests` calls) for that same reason.
    """
    try:
        pr_files = fetch_pr_files(owner, repo, pr_number, token)
        relevant_files = filter_relevant_files(pr_files)

        if not relevant_files:
            logger.info(f"{owner}/{repo}#{pr_number}: no agent-relevant files changed, skipping.")
            save_run(run_id, {"status": "skipped", "reason": "no relevant files changed"})
            post_summary_comment(
                owner,
                repo,
                pr_number,
                "## 🤖 MARC-AI Automated Review\n\n"
                "No Python/JS/TS files changed in this PR — nothing to analyze.",
                token,
            )
            return

        tmpdir = RepoClonerService(repo_url=head_clone_url, ref=head_ref, scan_id=run_id).clone()
        _prune_to_files(tmpdir, relevant_files)

        # Reuses the existing orchestrator/workflow unchanged — cleanup=True
        # matches the manual endpoint's default and removes tmpdir when done.
        orchestrator.run(tmpdir=tmpdir, log_all_audit=False, cleanup=True)

        markdown = build_markdown_summary(
            merged_findings=orchestrator.state.get("merged_findings"),
            analyzed_files=relevant_files,
        )

        save_run(
            run_id,
            {"status": "completed", "markdown": markdown, "files": relevant_files},
        )
        post_summary_comment(owner, repo, pr_number, markdown, token)

    except Exception as exc:
        # Background task boundary: must not raise, or FastAPI just logs an
        # unhandled exception with no way for anyone to find out the PR
        # analysis failed.
        logger.error(f"PR analysis failed for {owner}/{repo}#{pr_number}: {exc}")
        save_run(run_id, {"status": "failed", "error": str(exc)})
        try:
            post_summary_comment(
                owner,
                repo,
                pr_number,
                f"## 🤖 MARC-AI Automated Review\n\nAnalysis failed: `{exc}`",
                token,
            )
        except Exception as post_exc:
            logger.error(f"Also failed to post the failure comment to the PR: {post_exc}")
