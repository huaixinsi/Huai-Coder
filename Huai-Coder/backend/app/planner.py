import json
from dataclasses import dataclass
from .llm import complete

MAX_TASKS = 20
ALLOWED_TYPES = {"inspect", "edit", "test", "command", "report"}

@dataclass
class ValidatedPlan:
    goal: str
    summary: str
    tasks: list[dict]

def validate_plan(data: dict) -> ValidatedPlan:
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list): raise ValueError("plan.tasks must be an array")
    if not data.get("goal"): raise ValueError("plan.goal is required")
    tasks = data["tasks"]
    if not tasks or len(tasks) > MAX_TASKS: raise ValueError("plan must contain 1-20 tasks")
    keys = {task.get("task_key") for task in tasks}
    if len(keys) != len(tasks) or None in keys: raise ValueError("task_key must be unique")
    for task in tasks:
        if not task.get("title") or not task.get("description"): raise ValueError("task title and description are required")
        if task.get("task_type", "inspect") not in ALLOWED_TYPES: raise ValueError("unsupported task type")
        for dependency in task.get("depends_on", []):
            if dependency not in keys or dependency == task["task_key"]: raise ValueError("invalid task dependency")
    graph = {task["task_key"]: set(task.get("depends_on", [])) for task in tasks}
    visited: set[str] = set(); active: set[str] = set()
    def visit(key: str):
        if key in active: raise ValueError("cyclic task dependency")
        if key in visited: return
        active.add(key)
        for dependency in graph[key]: visit(dependency)
        active.remove(key); visited.add(key)
    for key in graph: visit(key)
    return ValidatedPlan(str(data["goal"]), str(data.get("summary", "")), tasks)

async def create_plan(prompt: str, context: str = "") -> ValidatedPlan:
    instruction = f"Create a JSON execution plan only. Maximum 20 tasks. Allowed task_type: inspect, edit, test, command, report. Each task needs task_key,title,description,task_type,depends_on,success_criteria. Never include secrets. User goal: {prompt}\nProject context:\n{context}"
    raw = await complete(instruction)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return validate_plan({"goal": prompt, "summary": "先检查项目结构并分析用户目标", "tasks": [{"task_key": "inspect_project", "title": "检查项目结构", "description": prompt, "task_type": "inspect", "depends_on": [], "success_criteria": "返回项目分析结果"}]})
    return validate_plan(json.loads(raw[start:end + 1]))
