from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import json
import os
import shutil
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from .agent import run_agent
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from .config import get_settings
from .database import Base, engine, get_db
from .models import AgentEventRecord, AgentRun, Approval, AuditLog, Message, Plan, Task, TaskDependency, Project, Session
from .registry import get_tool, command_risk
from .security import PathGuard, scrub, WorkspaceViolation
from .planner import create_plan
from .executor import mark_task_started, mark_task_success, mark_task_failure, next_task, plan_finished
from .llm import complete

settings = get_settings()
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/workspace")).resolve()
@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # create_all does not alter existing tables; keep local/dev databases compatible.
        await connection.execute(text("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL"))
        await connection.execute(text("ALTER TABLE approvals ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE"))
        await connection.execute(text("ALTER TABLE approvals ADD COLUMN IF NOT EXISTS task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE"))
        await connection.execute(text("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE"))
        await connection.execute(text("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE"))
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
            if not request.prompt.startswith(("/write ", "/exec ", "/read ", "/list", "/grep")):
                try:
                    planned = await create_plan(request.prompt)
                    plan = Plan(project_id=request.project_id, session_id=session.id, run_id=run.id, goal=planned.goal, summary=planned.summary)
                    db.add(plan); await db.flush(); run.plan_id = plan.id
                    task_ids = {}
                    for item in planned.tasks:
                        task = Task(plan_id=plan.id, task_key=item["task_key"], title=item["title"], description=item["description"], task_type=item.get("task_type", "inspect"), input_data=json.dumps(item, ensure_ascii=False))
                        db.add(task); await db.flush(); task_ids[task.task_key] = task.id
                    for item in planned.tasks:
                        for dependency in item.get("depends_on", []): db.add(TaskDependency(plan_id=plan.id, task_id=task_ids[item["task_key"]], depends_on_task_id=task_ids[dependency]))
                    await db.commit()
                    yield f"data: {json.dumps({'run_id': run.id, 'plan_id': plan.id, 'type': 'plan.created', 'content': planned.summary, 'status': plan.status}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'run_id': run.id, 'plan_id': plan.id, 'type': 'plan.confirmation_required', 'content': planned.goal}, ensure_ascii=False)}\n\n"; return
                except Exception as error:
                    run.status = "failed"; await db.commit()
                    yield f"data: {json.dumps({'run_id': run.id, 'type': 'run.failed', 'content': f'Plan validation failed: {error}'}, ensure_ascii=False)}\n\n"; return
            if request.prompt.startswith("/write ") or request.prompt.startswith("/exec ") or request.prompt.startswith("/read "):
                if request.prompt.startswith("/write "):
                    _, path, content = request.prompt.split(" ", 2)
                    tool_name, arguments, risk, target = "write_file", {"path": path, "content": content}, get_tool("write_file").risk, path
                elif request.prompt.startswith("/read "):
                    path = request.prompt.removeprefix("/read ").strip()
                    guard = PathGuard((WORKSPACE_ROOT / "projects" / str(request.project_id)).resolve())
                    if not guard.is_sensitive(guard.resolve(path)):
                        # Non-sensitive reads continue through the normal Agent tool path.
                        history = [(message.role, message.content) for message in previous_messages]
                        async for event in run_agent(request.prompt, workspace, history, f"session-{session.id}"):
                            db.add(AgentEventRecord(run_id=run.id, event_type=event.type, content=event.content, tool=event.tool))
                            await db.commit()
                            yield f"data: {json.dumps({'run_id': run.id, 'type': event.type, 'content': event.content, 'tool': event.tool}, ensure_ascii=False)}\n\n"
                        return
                    tool_name, arguments, risk, target = "read_file", {"path": path}, get_tool("read_file").risk, path
                    risk = type(risk)("high", "sensitive file access requires explicit approval", True)
                else:
                    command = request.prompt.removeprefix("/exec ").strip()
                    tool_name, arguments, risk, target = "execute_command", {"command": command}, command_risk(command), "."
                approval = Approval(run_id=run.id, session_id=session.id, tool_name=tool_name, arguments=json.dumps(arguments), risk_level=risk.level, risk_reason=risk.reason, target_path=target)
                db.add(approval)
                db.add(AuditLog(project_id=request.project_id, session_id=session.id, run_id=run.id, event_type="approval.requested", tool_name=tool_name, details=scrub(arguments)))
                await db.commit(); await db.refresh(approval)
                yield f"data: {json.dumps({'run_id': run.id, 'type': 'approval.required', 'approval_id': approval.id, 'tool': tool_name, 'content': risk.reason, 'risk_level': risk.level, 'arguments': scrub(arguments), 'target_path': target}, ensure_ascii=False)}\n\n"
                while True:
                    await asyncio.sleep(1)
                    await db.refresh(approval)
                    if approval.status != "PENDING": break
                if approval.status != "APPROVED":
                    run.status = "cancelled" if approval.status == "CANCELLED" else "failed"
                    db.add(AuditLog(project_id=request.project_id, session_id=session.id, run_id=run.id, event_type=f"approval.{approval.status.lower()}", tool_name=tool_name, details=approval.resolution_reason or ""))
                    await db.commit()
                    yield f"data: {json.dumps({'run_id': run.id, 'type': 'run.failed', 'content': f'Approval {approval.status.lower()}'}, ensure_ascii=False)}\n\n"; return
                guard = PathGuard(Path(workspace))
                if tool_name == "write_file": result = get_tool(tool_name).handler(arguments["path"], arguments["content"], guard)
                elif tool_name == "read_file":
                    get_tool(tool_name).handler(arguments["path"], guard)
                    result = "已获批准读取敏感配置，具体内容已隐藏，不会显示在聊天或审计日志中。"
                else: result = await get_tool(tool_name).handler(arguments["command"], guard)
                run.status = "completed"; db.add(AuditLog(project_id=request.project_id, session_id=session.id, run_id=run.id, event_type="tool.executed", tool_name=tool_name, details=scrub(result)))
                await db.commit()
                yield f"data: {json.dumps({'run_id': run.id, 'type': 'message.delta', 'content': result, 'tool': tool_name}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'run_id': run.id, 'type': 'run.finished', 'content': ''}, ensure_ascii=False)}\n\n"; return
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

async def _get_approval(approval_id: int, db: AsyncSession) -> Approval:
    approval = await db.get(Approval, approval_id)
    if approval is None: raise HTTPException(404, "Approval not found")
    return approval

@app.get("/api/runs/{run_id}/approvals")
async def list_approvals(run_id: int, db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Approval).where(Approval.run_id == run_id).order_by(Approval.id))).all()

@app.get("/api/approvals/{approval_id}")
async def get_approval(approval_id: int, db: AsyncSession = Depends(get_db)):
    return await _get_approval(approval_id, db)

async def resolve_approval(approval_id: int, status: str, reason: str | None, db: AsyncSession):
    approval = await _get_approval(approval_id, db)
    if approval.status != "PENDING": raise HTTPException(409, "Approval is already resolved")
    approval.status = status; approval.resolution_reason = reason; approval.resolved_at = datetime.now(timezone.utc)
    await db.commit(); await db.refresh(approval); return approval

class ApprovalDecision(BaseModel):
    reason: str | None = None

@app.post("/api/approvals/{approval_id}/approve")
async def approve_approval(approval_id: int, decision: ApprovalDecision, db: AsyncSession = Depends(get_db)):
    return await resolve_approval(approval_id, "APPROVED", decision.reason, db)

@app.post("/api/approvals/{approval_id}/reject")
async def reject_approval(approval_id: int, decision: ApprovalDecision, db: AsyncSession = Depends(get_db)):
    return await resolve_approval(approval_id, "REJECTED", decision.reason, db)

@app.post("/api/approvals/{approval_id}/cancel")
async def cancel_approval(approval_id: int, decision: ApprovalDecision, db: AsyncSession = Depends(get_db)):
    return await resolve_approval(approval_id, "CANCELLED", decision.reason, db)

@app.get("/api/runs/{run_id}/audit-events")
async def list_audit_events(run_id: int, db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(AuditLog).where(AuditLog.run_id == run_id).order_by(AuditLog.id))).all()

async def get_plan(plan_id: int, db: AsyncSession) -> Plan:
    plan = await db.get(Plan, plan_id)
    if plan is None: raise HTTPException(404, "Plan not found")
    return plan

@app.get("/api/plans/{plan_id}")
async def read_plan(plan_id: int, db: AsyncSession = Depends(get_db)): return await get_plan(plan_id, db)

@app.get("/api/plans/{plan_id}/tasks")
async def list_plan_tasks(plan_id: int, db: AsyncSession = Depends(get_db)):
    await get_plan(plan_id, db); return (await db.scalars(select(Task).where(Task.plan_id == plan_id).order_by(Task.id))).all()

async def execute_plan(plan: Plan, db: AsyncSession):
    plan.status = "RUNNING"; await db.commit()
    while True:
        task = await next_task(db, plan.id)
        if task is None:
            if await plan_finished(db, plan): return
            plan.status = "FAILED"; await db.commit(); return
        await mark_task_started(db, task)
        try:
            result = await complete(f"Execute this task in the project and return a concise result. Task: {task.description}. Success criteria: {json.loads(task.input_data).get('success_criteria', '')}")
            await mark_task_success(db, task, result)
        except Exception as error:
            await mark_task_failure(db, task, "LLM_ERROR", str(error))
            if task.status == "FAILED": plan.status = "FAILED"; await db.commit(); return

@app.post("/api/plans/{plan_id}/confirm")
async def confirm_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await get_plan(plan_id, db)
    if plan.status != "WAITING_CONFIRMATION": raise HTTPException(409, "Plan is not awaiting confirmation")
    plan.status = "READY"; plan.confirmed_at = datetime.now(timezone.utc); await db.commit()
    await execute_plan(plan, db); return plan

@app.post("/api/plans/{plan_id}/cancel")
async def cancel_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await get_plan(plan_id, db); plan.status = "CANCELLED"; plan.cancelled_at = datetime.now(timezone.utc)
    await db.execute(text("UPDATE tasks SET status='CANCELLED' WHERE plan_id=:id AND status NOT IN ('SUCCEEDED','FAILED')"), {"id": plan_id}); await db.commit(); return plan

@app.post("/api/plans/{plan_id}/pause")
async def pause_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await get_plan(plan_id, db); plan.status = "PAUSED"; await db.commit(); return plan

@app.post("/api/plans/{plan_id}/resume")
async def resume_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await get_plan(plan_id, db)
    if plan.status != "PAUSED": raise HTTPException(409, "Plan is not paused")
    await execute_plan(plan, db); return plan

@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int, db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task is None: raise HTTPException(404, "Task not found")
    if task.status != "FAILED": raise HTTPException(409, "Task is not failed")
    task.status = "PENDING"; await db.commit(); return task
