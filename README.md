# AI Agent System (OpenClaw Control)

This app provides a Control API and worker services that integrate with OpenClaw.

## Local dev

1) Copy `.env.example` to `.env` and fill values.
2) Start infra:

```bash
docker compose up -d
```

3) Run API:

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

4) Run migrations:

```bash
alembic upgrade head
```

5) Run worker:

```bash
celery -A app.worker.celery_app worker --loglevel=info
```

6) Optional scheduler:

```bash
celery -A app.worker.celery_app beat --loglevel=info
```

## Tool server for OpenClaw

The Control API exposes tool endpoints at:

- `GET /tools/manifest`
- `POST /tools/execute`

Set `TOOL_API_TOKEN` in `.env` and update the header in `openclaw-tools.json`.

## Endpoints

- `GET /health`
- `GET /agents`
