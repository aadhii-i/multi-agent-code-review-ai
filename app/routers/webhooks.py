import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.dependencies import get_orchestrator
from app.core.logger import logger
from app.services import github_service

router = APIRouter()

HANDLED_ACTIONS = {"opened", "synchronize"}


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    orchestrator=Depends(get_orchestrator),
):
    """
    GitHub PR webhook. Verifies the signature, filters to pull_request
    opened/synchronize events, and schedules the existing agent workflow as
    a background task scoped to the PR's changed files.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not github_service.verify_signature(raw_body, signature, settings.GITHUB_WEBHOOK_SECRET):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body") from exc

    action = payload.get("action")

    # Non pull_request events (ping, issue_comment, push, ...) and PR actions
    # we don't care about (closed, labeled, ...) are a no-op, not an error.
    if event_type != "pull_request" or action not in HANDLED_ACTIONS:
        logger.debug(f"Ignoring webhook: event={event_type} action={action}")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ignored", "event": event_type, "action": action},
        )

    try:
        ctx = github_service.parse_pr_context(payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Malformed pull_request payload: {exc}"
        ) from exc

    run_id = github_service.compute_run_id(ctx.owner, ctx.repo, ctx.pr_number, ctx.head_sha)

    cached = github_service.get_cached_run(run_id)
    if cached is not None:
        logger.info(f"{ctx.owner}/{ctx.repo}#{ctx.pr_number}@{ctx.head_sha[:7]} already analyzed, skipping.")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"run_id": run_id, "status": "cached", "result": cached},
        )

    background_tasks.add_task(
        github_service.run_scoped_pr_analysis,
        orchestrator=orchestrator,
        run_id=run_id,
        owner=ctx.owner,
        repo=ctx.repo,
        pr_number=ctx.pr_number,
        head_sha=ctx.head_sha,
        head_ref=ctx.head_ref,
        head_clone_url=ctx.head_clone_url,
        token=settings.GITHUB_TOKEN,
    )

    return {"run_id": run_id, "status": "accepted"}
