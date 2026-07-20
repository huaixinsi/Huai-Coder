from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import json
import os
import shutil
from pathlib import Path
from .agent import run_agent
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from .config import get_settings
from .database import Base, engine, get_db
from .models import AgentEventRecord, AgentRun, Message, Project, Session

settings = get_settings()
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()

app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None

class SessionRequest(BaseModel):
    project_id: int
    title: str = Field(default="New session", max_length=200)

class MessageRequest(BaseModel):
    session_id: int
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str = Field(min_length=1)

@app.get("/api/projects")
async def list_projects(db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Project).order_by(Project.id.desc()))).all()

@app.post("/api/projects", status_code=201)
async def create_project(request: ProjectRequest, db: AsyncSession = Depends(get_db)):
    project = Project(**request.model_dump()); db.add(project); await db.commit(); await db.refresh(project)
    return project

@app.delete("/api/projects/{project_id}", status_code=204)
async def delete_project(project_id: int, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    await db.delete(project)
    await db.commit()
    workspace = (WORKSPACE_ROOT / "projects" / str(project_id)).resolve()
    if WORKSPACE_ROOT in workspace.parents and workspace.exists():
        shutil.rmtree(workspace)

@app.post("/api/projects/{project_id}/files")
async def upload_project_files(project_id: int, files: list[UploadFile] = File(...), relative_paths: list[str] = Form(default=[]), db: AsyncSession = Depends(get_db)):
    if await db.get(Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    root = (WORKSPACE_ROOT / "projects" / str(project_id)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for index, upload in enumerate(files):
        relative = relative_paths[index] if index < len(relative_paths) else upload.filename or f"file-{index}"
        target = (root / relative).resolve()
        if target == root or root not in target.parents:
            raise HTTPException(400, "Invalid file path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await upload.read())
    return {"project_id": project_id, "files": len(files)}

@app.get("/api/projects/{project_id}/sessions")
async def list_sessions(project_id: int, db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Session).where(Session.project_id == project_id).order_by(Session.id.desc()))).all()

@app.post("/api/sessions", status_code=201)
async def create_session(request: SessionRequest, db: AsyncSession = Depends(get_db)):
    if await db.get(Project, request.project_id) is None: raise HTTPException(404, "Project not found")
    session = Session(**request.model_dump()); db.add(session); await db.commit(); await db.refresh(session)
    return session

@app.delete("/api/sessions/{session_id}", status_code=204)
async def delete_session(session_id: int, db: AsyncSession = Depends(get_db)):
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    await db.delete(session)
    await db.commit()

@app.get("/api/sessions/{session_id}/messages")
async def list_messages(session_id: int, db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id))).all()

@app.post("/api/messages", status_code=201)
async def create_message(request: MessageRequest, db: AsyncSession = Depends(get_db)):
    if await db.get(Session, request.session_id) is None: raise HTTPException(404, "Session not found")
    message = Message(**request.model_dump()); db.add(message); await db.commit(); await db.refresh(message)
    return message

@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    await db.execute(text("SELECT 1"))
    return {"status": "ok"}

class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    project_id: int
    session_id: int

@app.post("/api/runs")
async def create_run(request: RunRequest, db: AsyncSession = Depends(get_db)):
    session = await db.get(Session, request.session_id)
    if session is None or session.project_id != request.project_id:
        raise HTTPException(400, "Session does not belong to project")
    run = AgentRun(prompt=request.prompt, status="running")
    db.add(run)
    previous_messages = list((await db.scalars(select(Message).where(Message.session_id == session.id).order_by(Message.id))).all())
    db.add(Message(session_id=session.id, role="user", content=request.prompt))
    await db.commit()
    await db.refresh(run)
    async def events():
        try:
            workspace = str((WORKSPACE_ROOT / "projects" / str(request.project_id)).resolve()) if request.project_id else str(WORKSPACE_ROOT)
            history = [(message.role, message.content) for message in previous_messages]
            async for event in run_agent(request.prompt, workspace, history, f"session-{session.id}"):
                db.add(AgentEventRecord(run_id=run.id, event_type=event.type, content=event.content, tool=event.tool))
                if event.type in {"run.finished", "run.failed"}:
                    run.status = "completed" if event.type == "run.finished" else "failed"
                if event.type == "message.delta":
                    db.add(Message(session_id=session.id, role="assistant", content=event.content))
                await db.commit()
                yield f"data: {json.dumps({'run_id': run.id, 'type': event.type, 'content': event.content, 'tool': event.tool}, ensure_ascii=False)}\n\n"
        except Exception as error:
            run.status = "failed"
            failure = AgentEventRecord(run_id=run.id, event_type="run.failed", content=str(error))
            db.add(failure); await db.commit()
            yield f"data: {json.dumps({'run_id': run.id, 'type': 'run.failed', 'content': 'Agent run failed'}, ensure_ascii=False)}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/api/runs/{run_id}/events")
async def list_run_events(run_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    return (await db.scalars(select(AgentEventRecord).where(AgentEventRecord.run_id == run_id).order_by(AgentEventRecord.id))).all()
