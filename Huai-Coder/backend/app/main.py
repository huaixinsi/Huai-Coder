from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import json
from .agent import run_agent
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from .config import get_settings
from .database import Base, engine, get_db
from .models import AgentEventRecord, AgentRun, Message, Project, Session

settings = get_settings()
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

@app.get("/api/projects/{project_id}/sessions")
async def list_sessions(project_id: int, db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Session).where(Session.project_id == project_id).order_by(Session.id.desc()))).all()

@app.post("/api/sessions", status_code=201)
async def create_session(request: SessionRequest, db: AsyncSession = Depends(get_db)):
    if await db.get(Project, request.project_id) is None: raise HTTPException(404, "Project not found")
    session = Session(**request.model_dump()); db.add(session); await db.commit(); await db.refresh(session)
    return session

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

@app.post("/api/runs")
async def create_run(request: RunRequest, db: AsyncSession = Depends(get_db)):
    run = AgentRun(prompt=request.prompt, status="running")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    async def events():
        async for event in run_agent(request.prompt):
            db.add(AgentEventRecord(run_id=run.id, event_type=event.type, content=event.content, tool=event.tool))
            if event.type in {"run.finished", "run.failed"}:
                run.status = "completed" if event.type == "run.finished" else "failed"
            await db.commit()
            yield f"data: {json.dumps({'type': event.type, 'content': event.content, 'tool': event.tool}, ensure_ascii=False)}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/api/runs/{run_id}/events")
async def list_run_events(run_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    return (await db.scalars(select(AgentEventRecord).where(AgentEventRecord.run_id == run_id).order_by(AgentEventRecord.id))).all()
