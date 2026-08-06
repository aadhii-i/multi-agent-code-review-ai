# MARC-AI — Multi-Agent Review & Consolidation

Cloud-native FastAPI application that orchestrates LangGraph agents to run static linters & scanners (Ruff, ESLint, Bandit, Semgrep, Radon) and consolidates findings into explainable markdown reports using Azure OpenAI.

## Features

-   **Multi-agent orchestration** with LangGraph
-   **Static analysis only** — no code execution
-   **Parallel agent execution** for style, security, and performance checks
-   **AI-powered explanations** via Azure OpenAI (GPT-4)
-   **Async REST API** with FastAPI
-   **Containerized** with multi-stage Docker builds

## Quick Start

### Prerequisites

-   Python 3.11+
-   Docker & Docker Compose
-   Node.js & npm (for ESLint)

### Local Development

1. Install dependencies:

```bash
uv sync # for python specific dependencies
npm i   # for nodejs specific dependencies
```

2. Copy environment template:

```bash
cp .env.example .env
# Edit .env with your Azure OpenAI credentials
```

3. Run the application:

```bash
uvicorn app.main:app --reload
```

4. Access API docs at `http://localhost:8000/docs`

### Docker Development

Build and run the development environment:

```bash
# Build and start in foreground
docker-compose -f docker-compose.dev.yml up --build

# Or run in background
docker-compose -f docker-compose.dev.yml up -d --build

# View logs
docker-compose -f docker-compose.dev.yml logs -f

# Stop
docker-compose -f docker-compose.dev.yml down
```

### Docker Production

Build and run the production environment:

```bash
# Build and start in background
docker-compose -f docker-compose.prod.yml up -d --build

# View logs
docker-compose -f docker-compose.prod.yml logs -f

# Stop
docker-compose -f docker-compose.prod.yml down
```

### Build Specific Stage Only

```bash
# Build dev stage
docker build --target dev -t marc-ai:dev .

# Build prod stage
docker build --target prod -t marc-ai:prod .

# Run manually (without docker-compose)
docker run -p 8000:8000 --env-file .env marc-ai:prod
```

## API Endpoints

-   `GET /api/v1/health/` — Health check
-   `POST /api/v1/review/analyze` — Trigger code analysis
-   `GET /api/v1/review/status/{run_id}` — Check analysis status
-   `GET /api/v1/review/report/{run_id}` — Retrieve analysis report
-   `POST /api/v1/webhooks/github` — GitHub PR webhook (auto-triggers a scoped analysis on `opened`/`synchronize` and posts results back to the PR)

## GitHub Integration

Instead of calling `/api/v1/review/analyze` manually, MARC-AI can review pull requests automatically via a GitHub webhook.

### Setup

1. **Register the webhook** — in your repo, go to *Settings → Webhooks → Add webhook*:
    -   Payload URL: `https://<your-marc-ai-host>/api/v1/webhooks/github`
    -   Content type: `application/json`
    -   Secret: a random string — set the same value as `GITHUB_WEBHOOK_SECRET` in MARC-AI's `.env`
    -   Events: select **Pull requests** only
2. **Generate a token** MARC-AI can use to call the GitHub API back — a classic PAT with the `repo` scope (or a GitHub App installation token with `pull_requests: read & write` and `contents: read` permissions) — and set it as `GITHUB_TOKEN`.
3. Set both in `.env` (see `.env.example`).

### What happens on a PR

-   `opened` and `synchronize` (new commits pushed) trigger a run; every other event/action is acknowledged and ignored.
-   Only the PR's changed files are analyzed (filtered to the extensions the existing agents understand: `.py`, `.js`, `.jsx`, `.ts`, `.tsx`), via the same LangGraph workflow used by `/api/v1/review/analyze` — no separate analysis path.
-   The consolidated result is posted back as a single PR review comment. Re-delivering a webhook for a PR/commit that's already been analyzed returns the cached result instead of re-running.
-   Inline, per-finding comments (option (b) in the design) are not implemented yet — see the `TODO` in `app/services/github_service.py::post_summary_comment`.

## Project Structure

```
marc-ai/
├── app/
│   ├── agents/          # Analysis agents (style, security, performance)
│   ├── core/            # Config, logging, dependencies
│   ├── models/          # Pydantic schemas
│   ├── routers/         # API endpoints
│   ├── services/        # Business logic
│   ├── utils/           # Utilities (subprocess, parsers)
│   └── workflows/       # LangGraph workflows
├── tests/               # Test suite
├── Dockerfile           # Multi-stage container build
└── requirements.txt     # Python dependencies
```

## Security

-   All static analysis tools run via safe subprocess calls (`shell=False`)
-   Timeouts and resource limits enforced
-   No repository code execution
-   Isolated working directories per analysis run