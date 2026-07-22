import sys
import asyncio

# Windows: psycopg requires SelectorEventLoop, not the default ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from .agent import AgentEvent, run_agent
from .context import ContextManager
from .memory import (
    GLOBAL_MEMORY_SCOPE_ID,
    MEMORY_TYPES,
    MemoryCandidate,
    MemoryService,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from .config import get_settings
from .database import Base, engine, get_db, SessionLocal
from .models import (
    AgentEventRecord,
    AgentRun,
    Approval,
    AuditLog,
    Message,
    Memory,
    MemoryAudit,
    ConversationSummary,
    Plan,
    Task,
    Project,
    Session,
)
from .executor import (
    mark_task_started,
    mark_task_success,
    mark_task_failure,
    next_task,
    plan_finished,
)
from .llm import complete
from .registry import get_tool
from .security import PathGuard

settings = get_settings()
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # create_all does not alter existing tables; keep local/dev databases compatible.
        await connection.execute(
            text(
                "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE approvals ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE approvals ADD COLUMN IF NOT EXISTS task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE"
            )
        )
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


class MemoryCreateRequest(BaseModel):
    project_id: int
    session_id: int | None = None
    scope_type: str = Field(default="project", pattern="^(project|session)$")
    memory_type: str = Field(default="fact", pattern="^(fact|preference|decision|constraint|task|summary)$")
    content: str = Field(min_length=6, max_length=4000)
    importance: int = Field(default=5, ge=1, le=10)
    confidence: float = Field(default=0.9, ge=0, le=1)
    expires_at: datetime | None = None


class MemoryPatchRequest(BaseModel):
    content: str | None = Field(default=None, min_length=6, max_length=4000)
    memory_type: str | None = Field(default=None, pattern="^(fact|preference|decision|constraint|task|summary)$")
    importance: int | None = Field(default=None, ge=1, le=10)
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: datetime | None = None
    status: str | None = Field(default=None, pattern="^(active|deleted)$")


@app.get("/api/projects")
async def list_projects(db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Project).order_by(Project.id.desc()))).all()


@app.post("/api/projects", status_code=201)
async def create_project(request: ProjectRequest, db: AsyncSession = Depends(get_db)):
    project = Project(**request.model_dump())
    db.add(project)
    await db.commit()
    await db.refresh(project)
    (WORKSPACE_ROOT / "projects" / str(project.id)).resolve().mkdir(
        parents=True, exist_ok=True
    )
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
async def upload_project_files(
    project_id: int,
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    if await db.get(Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    root = (WORKSPACE_ROOT / "projects" / str(project_id)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for index, upload in enumerate(files):
        relative = (
            relative_paths[index]
            if index < len(relative_paths)
            else upload.filename or f"file-{index}"
        )
        target = (root / relative).resolve()
        if target == root or root not in target.parents:
            raise HTTPException(400, "Invalid file path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await upload.read())
    return {"project_id": project_id, "files": len(files)}


@app.get("/api/projects/{project_id}/sessions")
async def list_sessions(project_id: int, db: AsyncSession = Depends(get_db)):
    return (
        await db.scalars(
            select(Session)
            .where(Session.project_id == project_id)
            .order_by(Session.id.desc())
        )
    ).all()


@app.post("/api/sessions", status_code=201)
async def create_session(request: SessionRequest, db: AsyncSession = Depends(get_db)):
    if await db.get(Project, request.project_id) is None:
        raise HTTPException(404, "Project not found")
    session = Session(**request.model_dump())
    db.add(session)
    await db.commit()
    await db.refresh(session)
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
    return (
        await db.scalars(
            select(Message).where(Message.session_id == session_id).order_by(Message.id)
        )
    ).all()


def _memory_payload(memory: Memory) -> dict:
    return {
        "id": memory.id,
        "scope_type": memory.scope_type,
        "scope_id": memory.scope_id,
        "memory_type": memory.memory_type,
        "content": memory.content,
        "importance": memory.importance,
        "confidence": float(memory.confidence),
        "status": memory.status,
        "source_session_id": memory.source_session_id,
        "source_message_ids": memory.source_message_ids or [],
        "source_run_id": memory.source_run_id,
        "access_count": memory.access_count,
        "expires_at": memory.expires_at,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
    }


async def _validate_memory_scope(
    project_id: int, session_id: int | None, scope_type: str, db: AsyncSession
) -> None:
    if await db.get(Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    if scope_type == "session":
        if session_id is None:
            raise HTTPException(400, "session_id is required for session memory")
        session = await db.get(Session, session_id)
        if session is None or session.project_id != project_id:
            raise HTTPException(400, "Session does not belong to project")


@app.get("/api/projects/{project_id}/memories")
async def list_project_memories(
    project_id: int,
    scope_type: str | None = None,
    memory_type: str | None = None,
    status: str = "active",
    db: AsyncSession = Depends(get_db),
):
    if await db.get(Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    query = select(Memory).where(Memory.scope_type == "project", Memory.scope_id == project_id)
    if status:
        query = query.where(Memory.status == status)
    if memory_type:
        if memory_type not in MEMORY_TYPES:
            raise HTTPException(400, "Unsupported memory type")
        query = query.where(Memory.memory_type == memory_type)
    rows = list((await db.scalars(query.order_by(Memory.importance.desc(), Memory.id.desc()))).all())
    if scope_type == "session":
        sessions = list((await db.scalars(select(Session.id).where(Session.project_id == project_id))).all())
        if sessions:
            session_rows = list((await db.scalars(select(Memory).where(Memory.scope_type == "session", Memory.scope_id.in_(sessions), Memory.status == status))).all())
            rows.extend(session_rows)
    return [_memory_payload(memory) for memory in rows]


@app.get("/api/projects/{project_id}/memories/overview")
async def list_project_memory_overview(
    project_id: int, db: AsyncSession = Depends(get_db)
):
    if await db.get(Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    grouped = await MemoryService(settings).list_project_and_global_memories(
        db, project_id, global_scope_id=GLOBAL_MEMORY_SCOPE_ID
    )
    return {
        "project_id": project_id,
        "global_scope": {
            "scope_type": "user",
            "scope_id": GLOBAL_MEMORY_SCOPE_ID,
        },
        "project": [_memory_payload(memory) for memory in grouped["project"]],
        "global": [_memory_payload(memory) for memory in grouped["global"]],
    }


@app.get("/api/sessions/{session_id}/memories/overview")
async def list_session_memory_overview(
    session_id: int, db: AsyncSession = Depends(get_db)
):
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    grouped = await MemoryService(settings).list_session_project_user_memories(
        db,
        session_id=session.id,
        project_id=session.project_id,
        user_scope_id=GLOBAL_MEMORY_SCOPE_ID,
    )
    return {
        "session_id": session.id,
        "project_id": session.project_id,
        "user_scope": {
            "scope_type": "user",
            "scope_id": GLOBAL_MEMORY_SCOPE_ID,
        },
        "session": [_memory_payload(memory) for memory in grouped["session"]],
        "project": [_memory_payload(memory) for memory in grouped["project"]],
        "user": [_memory_payload(memory) for memory in grouped["user"]],
    }


@app.post("/api/memories", status_code=201)
async def create_memory(request: MemoryCreateRequest, db: AsyncSession = Depends(get_db)):
    await _validate_memory_scope(request.project_id, request.session_id, request.scope_type, db)
    service = MemoryService(settings)
    candidate = MemoryCandidate(
        scope_type=request.scope_type,
        scope_id=request.session_id if request.scope_type == "session" else request.project_id,
        memory_type=request.memory_type,
        content=request.content,
        importance=request.importance,
        confidence=request.confidence,
        source_session_id=request.session_id,
        expires_at=request.expires_at,
    )
    memory = await service.upsert(db, candidate, reason="manual creation")
    if memory is None:
        raise HTTPException(400, "Sensitive information cannot be stored as memory")
    await db.commit()
    await db.refresh(memory)
    return _memory_payload(memory)


@app.patch("/api/memories/{memory_id}")
async def update_memory(memory_id: int, request: MemoryPatchRequest, db: AsyncSession = Depends(get_db)):
    memory = await db.get(Memory, memory_id)
    if memory is None:
        raise HTTPException(404, "Memory not found")
    if request.content is not None:
        from .memory import contains_sensitive, normalize_content
        if contains_sensitive(request.content):
            raise HTTPException(400, "Sensitive information cannot be stored as memory")
        memory.content = request.content
        memory.normalized_content = normalize_content(request.content)
    if request.memory_type is not None:
        memory.memory_type = request.memory_type
    if request.importance is not None:
        memory.importance = request.importance
    if request.confidence is not None:
        memory.confidence = request.confidence
    if request.expires_at is not None:
        memory.expires_at = request.expires_at
    if request.status is not None:
        memory.status = request.status
    db.add(MemoryAudit(memory_id=memory.id, action="update", after_content=memory.content, reason="manual update"))
    await db.commit()
    await db.refresh(memory)
    return _memory_payload(memory)


@app.delete("/api/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: int, db: AsyncSession = Depends(get_db)):
    memory = await db.get(Memory, memory_id)
    if memory is None:
        raise HTTPException(404, "Memory not found")
    await MemoryService(settings).delete(db, memory)
    await db.commit()


@app.get("/api/sessions/{session_id}/summary")
async def get_session_summary(session_id: int, db: AsyncSession = Depends(get_db)):
    if await db.get(Session, session_id) is None:
        raise HTTPException(404, "Session not found")
    summary = await ContextManager(settings).latest_summary(db, session_id)
    return summary or {"summary": "", "covered_until_message_id": None, "token_count": 0}


@app.post("/api/sessions/{session_id}/compact")
async def compact_session(session_id: int, db: AsyncSession = Depends(get_db)):
    if await db.get(Session, session_id) is None:
        raise HTTPException(404, "Session not found")
    messages = list((await db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id))).all())
    summary = await ContextManager(settings).compact_session(db, session_id, messages)
    await db.commit()
    if summary is None:
        return {"summary": "", "covered_until_message_id": None, "token_count": 0}
    return {
        "id": summary.id,
        "summary": summary.summary,
        "covered_until_message_id": summary.covered_until_message_id,
        "summary_version": summary.summary_version,
        "token_count": summary.token_count,
    }


@app.post("/api/messages", status_code=201)
async def create_message(request: MessageRequest, db: AsyncSession = Depends(get_db)):
    if await db.get(Session, request.session_id) is None:
        raise HTTPException(404, "Session not found")
    message = Message(**request.model_dump())
    db.add(message)
    await db.commit()
    await db.refresh(message)
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
    previous_messages = list(
        (
            await db.scalars(
                select(Message)
                .where(Message.session_id == session.id)
                .order_by(Message.id)
            )
        ).all()
    )
    user_message = Message(session_id=session.id, role="user", content=request.prompt)
    db.add(user_message)
    await db.commit()
    await db.refresh(run)
    await db.refresh(user_message)

    async def events():
        try:
            workspace = str(
                (WORKSPACE_ROOT / "projects" / str(request.project_id)).resolve()
            )
            prepared_context = await ContextManager(settings).build_context(
                db,
                project_id=request.project_id,
                session_id=session.id,
                prompt=request.prompt,
                history=previous_messages,
            )
            if prepared_context.compacted:
                compact_event = AgentEvent(
                    "context.compacted",
                    content=(
                        "已自动压缩较早的会话历史，保留结构化摘要和最近对话后继续执行。"
                    ),
                )
                db.add(
                    AgentEventRecord(
                        run_id=run.id,
                        event_type=compact_event.type,
                        content=compact_event.content,
                    )
                )
                await db.commit()
                yield f"data: {json.dumps({'run_id': run.id, 'type': compact_event.type, 'content': compact_event.content}, ensure_ascii=False)}\n\n"
            async for event in run_agent(
                request.prompt,
                workspace,
                history=None,
                thread_id=f"session-{session.id}",
                context_text=prepared_context.render(),
            ):
                event_payload = {
                    "run_id": run.id,
                    "type": event.type,
                    "content": event.content,
                    "tool": event.tool,
                }
                if event.type == "approval.required":
                    details: dict = {}
                    try:
                        details = json.loads(event.content or "{}")
                    except json.JSONDecodeError:
                        details = {}
                    tool_name = event.tool or details.get("tool") or "unknown"
                    arguments = details.get("arguments") or {}
                    try:
                        tool_spec = get_tool(tool_name)
                        risk_level = tool_spec.risk.level
                        risk_reason = details.get("reason") or tool_spec.risk.reason
                    except Exception:
                        risk_level = details.get("risk_level") or "medium"
                        risk_reason = details.get("reason") or "需要人工确认后才能继续执行"
                    approval = Approval(
                        run_id=run.id,
                        session_id=session.id,
                        tool_name=tool_name,
                        arguments=json.dumps(arguments, ensure_ascii=False),
                        risk_level=risk_level,
                        risk_reason=risk_reason,
                        target_path=arguments.get("path") if isinstance(arguments, dict) else None,
                    )
                    db.add(approval)
                    await db.flush()
                    event_payload.update(
                        {
                            "approval_id": approval.id,
                            "risk_level": approval.risk_level,
                            "arguments": approval.arguments,
                            "target_path": approval.target_path,
                            "content": approval.risk_reason,
                        }
                    )
                db.add(
                    AgentEventRecord(
                        run_id=run.id,
                        event_type=event.type,
                        content=event.content,
                        tool=event.tool,
                    )
                )
                if event.type in {"run.finished", "run.failed"}:
                    run.status = (
                        "completed" if event.type == "run.finished" else "failed"
                )
                if event.type == "message.delta":
                    db.add(
                        Message(
                            session_id=session.id,
                            role="assistant",
                            content=event.content,
                        )
                    )
                await db.commit()
                yield f"data: {json.dumps(event_payload, ensure_ascii=False)}\n\n"
            if run.status == "completed":
                await MemoryService(settings).extract_and_persist(
                    db,
                    request.prompt,
                    project_id=request.project_id,
                    session_id=session.id,
                    source_message_ids=[user_message.id],
                    source_run_id=run.id,
                )
                await db.commit()
        except Exception as error:
            run.status = "failed"
            failure = AgentEventRecord(
                run_id=run.id, event_type="run.failed", content=str(error)
            )
            db.add(failure)
            await db.commit()
            yield f"data: {json.dumps({'run_id': run.id, 'type': 'run.failed', 'content': 'Agent run failed'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs/{run_id}/events")
async def list_run_events(run_id: int, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select

    return (
        await db.scalars(
            select(AgentEventRecord)
            .where(AgentEventRecord.run_id == run_id)
            .order_by(AgentEventRecord.id)
        )
    ).all()


async def _get_approval(approval_id: int, db: AsyncSession) -> Approval:
    approval = await db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "Approval not found")
    return approval


@app.get("/api/runs/{run_id}/approvals")
async def list_approvals(run_id: int, db: AsyncSession = Depends(get_db)):
    return (
        await db.scalars(
            select(Approval).where(Approval.run_id == run_id).order_by(Approval.id)
        )
    ).all()


@app.get("/api/approvals/{approval_id}")
async def get_approval(approval_id: int, db: AsyncSession = Depends(get_db)):
    return await _get_approval(approval_id, db)


async def _execute_approved_tool(approval: Approval, db: AsyncSession) -> str:
    session = await db.get(Session, approval.session_id)
    if session is None:
        return "执行失败：关联会话不存在。"
    try:
        tool_spec = get_tool(approval.tool_name)
        arguments = json.loads(approval.arguments or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("审批参数必须是 JSON 对象")
        workspace = (WORKSPACE_ROOT / "projects" / str(session.project_id)).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        result = tool_spec.handler(guard=PathGuard(workspace), **arguments)
        if asyncio.iscoroutine(result):
            result = await result
        output = str(result)[:12000]
        event_type = "tool.finished"
    except Exception as error:
        output = f"审批后执行失败：{error}"
        event_type = "tool.failed"

    db.add(
        AgentEventRecord(
            run_id=approval.run_id,
            event_type=event_type,
            content=output,
            tool=approval.tool_name,
        )
    )
    db.add(
        AuditLog(
            project_id=session.project_id,
            session_id=session.id,
            run_id=approval.run_id,
            event_type="approval.executed" if event_type == "tool.finished" else "approval.execution_failed",
            tool_name=approval.tool_name,
            details=json.dumps({"approval_id": approval.id, "result": output}, ensure_ascii=False),
        )
    )
    db.add(
        Message(
            session_id=session.id,
            role="system",
            content=f"审批已通过，工具 {approval.tool_name} 已执行。\n\n执行结果：\n{output}",
        )
    )
    await db.commit()
    return output


async def _continue_after_approval(
    approval: Approval, execution_result: str, db: AsyncSession
) -> list[dict]:
    """Resume the agent after an approved tool and return the follow-up events."""
    session = await db.get(Session, approval.session_id)
    run = await db.get(AgentRun, approval.run_id)
    if session is None or run is None:
        return []

    run.status = "running"
    await db.commit()
    messages = list(
        (
            await db.scalars(
                select(Message)
                .where(Message.session_id == session.id)
                .order_by(Message.id)
            )
        ).all()
    )
    prepared_context = await ContextManager(settings).build_context(
        db,
        project_id=session.project_id,
        session_id=session.id,
        prompt=run.prompt,
        history=messages,
    )
    continuation_prompt = (
        "Continue the original task from the approved tool result below. "
        "The approved tool has already been executed; do not repeat that same call. "
        "Inspect or modify the workspace as needed, then provide the final answer "
        "for the user when the task is complete.\n\n"
        f"Original user request:\n{run.prompt}\n\n"
        f"Approved tool: {approval.tool_name}\n"
        f"Approved tool result:\n{execution_result}"
    )
    workspace = str((WORKSPACE_ROOT / "projects" / str(session.project_id)).resolve())
    continuation_events: list[dict] = []
    waiting_for_approval = False

    try:
        async for event in run_agent(
            continuation_prompt,
            workspace,
            history=None,
            thread_id=f"session-{session.id}-approval-{approval.id}",
            context_text=prepared_context.render(),
        ):
            event_payload = {
                "run_id": run.id,
                "type": event.type,
                "content": event.content,
                "tool": event.tool,
            }
            if event.type == "approval.required":
                details: dict = {}
                try:
                    details = json.loads(event.content or "{}")
                except json.JSONDecodeError:
                    details = {}
                tool_name = event.tool or details.get("tool") or "unknown"
                arguments = details.get("arguments") or {}
                try:
                    tool_spec = get_tool(tool_name)
                    risk_level = tool_spec.risk.level
                    risk_reason = details.get("reason") or tool_spec.risk.reason
                except Exception:
                    risk_level = details.get("risk_level") or "medium"
                    risk_reason = details.get("reason") or "需要人工确认后才能继续执行"
                next_approval = Approval(
                    run_id=run.id,
                    session_id=session.id,
                    tool_name=tool_name,
                    arguments=json.dumps(arguments, ensure_ascii=False),
                    risk_level=risk_level,
                    risk_reason=risk_reason,
                    target_path=arguments.get("path") if isinstance(arguments, dict) else None,
                )
                db.add(next_approval)
                await db.flush()
                waiting_for_approval = True
                event_payload.update(
                    {
                        "approval_id": next_approval.id,
                        "risk_level": next_approval.risk_level,
                        "arguments": next_approval.arguments,
                        "target_path": next_approval.target_path,
                        "content": next_approval.risk_reason,
                    }
                )
            db.add(
                AgentEventRecord(
                    run_id=run.id,
                    event_type=event.type,
                    content=event.content,
                    tool=event.tool,
                )
            )
            if event.type == "message.delta":
                db.add(
                    Message(
                        session_id=session.id,
                        role="assistant",
                        content=event.content,
                    )
                )
            if event.type == "run.finished":
                run.status = "waiting_approval" if waiting_for_approval else "completed"
            elif event.type == "run.failed":
                run.status = "failed"
            await db.commit()
            continuation_events.append(event_payload)
    except Exception as error:
        run.status = "failed"
        db.add(
            AgentEventRecord(
                run_id=run.id,
                event_type="run.failed",
                content=str(error),
            )
        )
        await db.commit()
        continuation_events.append(
            {"run_id": run.id, "type": "run.failed", "content": str(error)}
        )

    if run.status == "completed":
        await MemoryService(settings).extract_and_persist(
            db,
            run.prompt,
            project_id=session.project_id,
            session_id=session.id,
            source_message_ids=[message.id for message in messages if message.role == "user"],
            source_run_id=run.id,
        )
        await db.commit()
    return continuation_events


def _approval_response(
    approval: Approval,
    execution_result: str | None = None,
    continuation_events: list[dict] | None = None,
) -> dict:
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "session_id": approval.session_id,
        "tool_name": approval.tool_name,
        "arguments": approval.arguments,
        "risk_level": approval.risk_level,
        "risk_reason": approval.risk_reason,
        "target_path": approval.target_path,
        "status": approval.status,
        "resolution_reason": approval.resolution_reason,
        "execution_result": execution_result,
        "continuation_events": continuation_events or [],
    }


async def resolve_approval(
    approval_id: int, status: str, reason: str | None, db: AsyncSession
):
    approval = (
        await db.scalars(
            select(Approval).where(Approval.id == approval_id).with_for_update()
        )
    ).one_or_none()
    if approval is None:
        raise HTTPException(404, "Approval not found")
    if approval.status != "PENDING":
        raise HTTPException(409, "Approval is already resolved")
    approval.status = status
    approval.resolution_reason = reason
    approval.resolved_at = datetime.now(timezone.utc)
    if status != "APPROVED":
        session = await db.get(Session, approval.session_id)
        if session is not None:
            decision_label = "拒绝" if status == "REJECTED" else "取消"
            db.add(
                Message(
                    session_id=session.id,
                    role="system",
                    content=f"已{decision_label}工具 {approval.tool_name} 的审批。",
                )
            )
    await db.commit()
    await db.refresh(approval)
    execution_result = None
    continuation_events: list[dict] = []
    if status == "APPROVED":
        execution_result = await _execute_approved_tool(approval, db)
        continuation_events = await _continue_after_approval(
            approval, execution_result, db
        )
    return _approval_response(approval, execution_result, continuation_events)


class ApprovalDecision(BaseModel):
    reason: str | None = None


@app.post("/api/approvals/{approval_id}/approve")
async def approve_approval(
    approval_id: int, decision: ApprovalDecision, db: AsyncSession = Depends(get_db)
):
    return await resolve_approval(approval_id, "APPROVED", decision.reason, db)


@app.post("/api/approvals/{approval_id}/reject")
async def reject_approval(
    approval_id: int, decision: ApprovalDecision, db: AsyncSession = Depends(get_db)
):
    return await resolve_approval(approval_id, "REJECTED", decision.reason, db)


@app.post("/api/approvals/{approval_id}/cancel")
async def cancel_approval(
    approval_id: int, decision: ApprovalDecision, db: AsyncSession = Depends(get_db)
):
    return await resolve_approval(approval_id, "CANCELLED", decision.reason, db)


@app.get("/api/runs/{run_id}/audit-events")
async def list_audit_events(run_id: int, db: AsyncSession = Depends(get_db)):
    return (
        await db.scalars(
            select(AuditLog).where(AuditLog.run_id == run_id).order_by(AuditLog.id)
        )
    ).all()


async def get_plan(plan_id: int, db: AsyncSession) -> Plan:
    plan = await db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    return plan


@app.get("/api/plans/{plan_id}")
async def read_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    return await get_plan(plan_id, db)


@app.get("/api/plans/{plan_id}/tasks")
async def list_plan_tasks(plan_id: int, db: AsyncSession = Depends(get_db)):
    await get_plan(plan_id, db)
    return (
        await db.scalars(select(Task).where(Task.plan_id == plan_id).order_by(Task.id))
    ).all()


async def execute_plan(plan: Plan, db: AsyncSession):
    from .agent import _workspace_context

    plan.status = "RUNNING"
    await db.commit()
    workspace = str((WORKSPACE_ROOT / "projects" / str(plan.project_id)).resolve())
    context = _workspace_context(Path(workspace))
    while True:
        task = await next_task(db, plan.id)
        if task is None:
            if await plan_finished(db, plan):
                return
            plan.status = "FAILED"
            await db.commit()
            return
        await mark_task_started(db, task)
        try:
            criteria = json.loads(task.input_data).get("success_criteria", "")
            result = await complete(
                f"你正在分析一个项目。以下是项目上下文：\n\n{context}\n\n请执行任务：{task.description}\n成功标准：{criteria}\n\n请根据项目上下文给出具体分析结果。"
            )
            await mark_task_success(db, task, result)
        except Exception as error:
            await mark_task_failure(db, task, "LLM_ERROR", str(error))
            if task.status == "FAILED":
                plan.status = "FAILED"
                await db.commit()
                return


async def _run_plan_background(plan_id: int):
    async with SessionLocal() as db:
        plan = await db.get(Plan, plan_id)
        if plan is None:
            return
        await execute_plan(plan, db)


@app.post("/api/plans/{plan_id}/confirm")
async def confirm_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await get_plan(plan_id, db)
    if plan.status != "WAITING_CONFIRMATION":
        raise HTTPException(409, "Plan is not awaiting confirmation")
    plan.status = "READY"
    plan.confirmed_at = datetime.now(timezone.utc)
    await db.commit()
    asyncio.create_task(_run_plan_background(plan_id))
    return plan


@app.post("/api/plans/{plan_id}/cancel")
async def cancel_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await get_plan(plan_id, db)
    plan.status = "CANCELLED"
    plan.cancelled_at = datetime.now(timezone.utc)
    await db.execute(
        text(
            "UPDATE tasks SET status='CANCELLED' WHERE plan_id=:id AND status NOT IN ('SUCCEEDED','FAILED')"
        ),
        {"id": plan_id},
    )
    await db.commit()
    return plan


@app.post("/api/plans/{plan_id}/pause")
async def pause_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await get_plan(plan_id, db)
    plan.status = "PAUSED"
    await db.commit()
    return plan


@app.post("/api/plans/{plan_id}/resume")
async def resume_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await get_plan(plan_id, db)
    if plan.status != "PAUSED":
        raise HTTPException(409, "Plan is not paused")
    asyncio.create_task(_run_plan_background(plan_id))
    return plan


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int, db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if task.status != "FAILED":
        raise HTTPException(409, "Task is not failed")
    task.status = "PENDING"
    await db.commit()
    return task
