import json
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .models import Plan, Task, TaskDependency

def now(): return datetime.now(timezone.utc)

async def next_task(db: AsyncSession, plan_id: int) -> Task | None:
    tasks = list((await db.scalars(select(Task).where(Task.plan_id == plan_id).order_by(Task.id))).all())
    for task in tasks:
        if task.status not in {"PENDING", "READY"}: continue
        deps = list((await db.scalars(select(TaskDependency).where(TaskDependency.task_id == task.id))).all())
        states = [await db.get(Task, dep.depends_on_task_id) for dep in deps]
        if all(dep and dep.status == "SUCCEEDED" for dep in states): return task
        task.status = "BLOCKED"
    return None

async def plan_finished(db: AsyncSession, plan: Plan) -> bool:
    tasks = list((await db.scalars(select(Task).where(Task.plan_id == plan.id))).all())
    if tasks and all(task.status == "SUCCEEDED" for task in tasks):
        plan.status = "SUCCEEDED"; await db.commit(); return True
    return False

async def mark_task_started(db: AsyncSession, task: Task):
    task.status = "RUNNING"; task.started_at = now(); await db.commit()

async def mark_task_success(db: AsyncSession, task: Task, output: str):
    task.status = "SUCCEEDED"; task.output_data = output[:12000]; task.finished_at = now(); await db.commit()

async def mark_task_failure(db: AsyncSession, task: Task, error_type: str, message: str):
    task.error_type = error_type; task.error_message = message[:4000]; task.retry_count += 1
    if task.retry_count <= task.max_retries and error_type in {"LLM_ERROR", "TOOL_ERROR", "COMMAND_TIMEOUT"}:
        task.status = "RETRYING"
    else:
        task.status = "FAILED"; task.finished_at = now()
    await db.commit()
