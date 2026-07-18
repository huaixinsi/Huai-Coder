# Huai-Coder

Huai-Coder is a Python web project for intelligent coding workflows.

## Current Results

- **Phase 01 - Foundation:** FastAPI, React/Vite, PostgreSQL, Docker Compose, health check API, and secret-file protection.
- **Phase 02 - ReAct Agent:** Agent run events, SSE streaming, workspace-safe file tools, and a React chat timeline.

## Start

```powershell
cd Huai-Coder
Copy-Item .env.example .env
docker compose up -d --build
```

Endpoints:

- Frontend: http://localhost
- Health check: http://localhost:8000/health
- Agent run stream: `POST http://localhost:8000/api/runs`

Example request:

```json
{"prompt":"/list ."}
```

The demo currently supports `/list <path>` and `/read <path>`. File access is restricted to the workspace root. Real LLM calls, persistent run events, and authentication are planned for later phases.

## Git Workflow

Each phase or feature uses an independent branch, is verified, submitted as a Pull Request, and merged into `main` after review. Secrets must never be committed.
