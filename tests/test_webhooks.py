import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.dependencies import get_orchestrator
from app.main import app

WEBHOOK_SECRET = "test-secret"


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _pr_payload(action: str = "opened", pr_number: int = 42, sha: str = "abc123") -> dict:
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "head": {
                "ref": "feature-branch",
                "sha": sha,
                "repo": {"clone_url": "https://github.com/forker/repo.git"},
            },
            "base": {"ref": "main"},
        },
        "repository": {
            "name": "repo",
            "owner": {"login": "owner"},
        },
    }


@pytest.fixture(autouse=True)
def _configure_secret():
    original = settings.GITHUB_WEBHOOK_SECRET
    settings.GITHUB_WEBHOOK_SECRET = WEBHOOK_SECRET
    yield
    settings.GITHUB_WEBHOOK_SECRET = original


@pytest.fixture(autouse=True)
def _override_orchestrator():
    app.dependency_overrides[get_orchestrator] = lambda: MagicMock()
    yield
    app.dependency_overrides.pop(get_orchestrator, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_valid_signature_accepted(client):
    body = json.dumps(_pr_payload(action="opened")).encode()

    with patch("app.services.github_service.run_scoped_pr_analysis") as mock_run:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    mock_run.assert_called_once()


async def test_invalid_signature_rejected(client):
    body = json.dumps(_pr_payload(action="opened")).encode()

    response = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=deadbeef",
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401


async def test_missing_signature_rejected(client):
    body = json.dumps(_pr_payload(action="opened")).encode()

    response = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
    )

    assert response.status_code == 401


async def test_non_pull_request_event_is_noop(client):
    body = json.dumps({"zen": "Keep it logically awesome."}).encode()

    with patch("app.services.github_service.run_scoped_pr_analysis") as mock_run:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "ping",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    mock_run.assert_not_called()


async def test_unhandled_pr_action_is_noop(client):
    body = json.dumps(_pr_payload(action="closed")).encode()

    with patch("app.services.github_service.run_scoped_pr_analysis") as mock_run:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    mock_run.assert_not_called()


@pytest.mark.parametrize("action", ["opened", "synchronize"])
async def test_handled_actions_trigger_background_analysis(client, action):
    body = json.dumps(_pr_payload(action=action, sha=f"sha-{action}")).encode()

    with patch("app.services.github_service.run_scoped_pr_analysis") as mock_run:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": _sign(body),
                "X-GitHub-Event": "pull_request",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 202
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs["owner"] == "owner"
    assert kwargs["repo"] == "repo"
    assert kwargs["pr_number"] == 42
    assert kwargs["head_ref"] == "feature-branch"
    assert kwargs["head_clone_url"] == "https://github.com/forker/repo.git"


async def test_same_pr_and_sha_is_idempotent(client):
    body = json.dumps(_pr_payload(action="opened", sha="same-sha")).encode()
    headers = {
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Event": "pull_request",
        "Content-Type": "application/json",
    }

    with patch("app.services.github_service.run_scoped_pr_analysis") as mock_run:
        first = await client.post("/api/v1/webhooks/github", content=body, headers=headers)
        assert first.status_code == 202
        mock_run.assert_called_once()

        # Simulate the background task having completed and cached its result.
        from app.services import github_service

        run_id = first.json()["run_id"]
        github_service.save_run(run_id, {"status": "completed", "markdown": "cached report"})

        second = await client.post("/api/v1/webhooks/github", content=body, headers=headers)

    assert second.status_code == 200
    assert second.json()["status"] == "cached"
    assert second.json()["run_id"] == first.json()["run_id"]
    # No second background run was scheduled for the identical PR + SHA.
    mock_run.assert_called_once()
