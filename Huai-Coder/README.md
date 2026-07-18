# Huai-Coder

Phase 01 foundation and Phase 02 ReAct Agent demonstration.

## Docker

Create a local `.env` from `.env.example`, set `POSTGRES_PASSWORD`, then run:

```powershell
docker compose up -d --build
```

## Phase 02

The backend exposes `POST /api/runs` as an SSE stream. The frontend renders run status, tool calls, and streamed agent messages.

Supported demo commands:

- `/list .` - list files in the workspace
- `/read README.md` - read a workspace file

The file tools reject paths outside the workspace root. The current phase uses a deterministic agent demonstration; OpenAI-compatible model calls, persistent event history, and authentication are not implemented yet.

## Phase Results

- **Phase 01:** FastAPI, React/Vite, PostgreSQL, Docker Compose, health check, and Git secret protection.
- **Phase 02:** Agent event model, SSE run endpoint, safe file tools, and React chat timeline.
