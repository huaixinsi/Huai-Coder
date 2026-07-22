from pathlib import Path
from typing import AsyncIterator, TypedDict
from dataclasses import dataclass
from hashlib import sha256
import json

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from .config import get_settings
from .registry import get_tool, TOOLS
from .llm import complete_with_tools
from .agents.registry import list_subagents
from .context import estimate_messages

SENSITIVE_REQUEST_MARKERS = (
    ".env",
    "密钥",
    "密码",
    "token",
    "api_key",
    "apikey",
    "secret",
    "凭证",
    "access key",
)

_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    "coverage",
    ".idea",
    ".vscode",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
}
_SKIP_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".mp3",
    ".zip",
    ".tar",
    ".gz",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".war",
    ".pdf",
    ".lock",
    ".pyc",
    ".pyo",
    ".o",
    ".obj",
    ".bin",
    ".dat",
    ".db",
    ".sqlite3",
}
_CODE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".md",
    ".yml",
    ".yaml",
    ".java",
    ".go",
    ".rs",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".vue",
    ".toml",
    ".cfg",
    ".ini",
    ".txt",
    ".sh",
    ".bat",
}

def _bound_react_messages(messages: list[dict], max_tokens: int) -> list[dict]:
    """Keep the tool loop inside the model budget without breaking tool pairs."""
    if estimate_messages(messages) <= int(max_tokens * 0.8):
        return messages
    if len(messages) <= 4:
        return messages
    prefix = messages[:2]
    tail = messages[2:]
    # Tool observations are appended as assistant(tool_calls) + tool pairs.
    keep = tail[-8:]
    while keep and keep[0].get("role") == "tool":
        keep = keep[1:]
    compacted = prefix + [
        {
            "role": "system",
            "content": "Earlier tool observations were compacted. Use the recent observations and re-check files when necessary.",
        }
    ] + keep
    return compacted


@dataclass
class _RepeatRecord:
    attempts: int = 0
    stale_streak: int = 0
    last_signature: str | None = None
    last_result: str | None = None
    last_workspace: str | None = None
    circuit_broken: bool = False


def _normalize_call_value(value, key: str | None = None):
    if isinstance(value, dict):
        return {name: _normalize_call_value(value[name], name) for name in sorted(value)}
    if isinstance(value, list):
        return [_normalize_call_value(item, key) for item in value]
    if isinstance(value, str):
        normalized = value.strip()
        if key in {"path", "directory", "cwd"}:
            normalized = normalized.replace("\\", "/")
            while "//" in normalized:
                normalized = normalized.replace("//", "/")
            if normalized.startswith("./"):
                normalized = normalized[2:]
            normalized = normalized or "."
        return normalized
    return value


def _call_signature(tool_name: str, arguments: dict) -> str:
    normalized = _normalize_call_value(arguments)
    return f"{tool_name}:{json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=( ',', ':' ))}"


def _result_fingerprint(result: str) -> str:
    try:
        normalized = json.dumps(json.loads(result), ensure_ascii=False, sort_keys=True, separators=( ',', ':' ))
    except (TypeError, json.JSONDecodeError):
        normalized = " ".join(str(result).split())
    return sha256(normalized.encode("utf-8")).hexdigest()


def _workspace_fingerprint(root: Path) -> str:
    """Hash semantic workspace contents for stateful duplicate-call detection."""
    entries: list[tuple[str, str, str]] = []
    if not root.exists():
        return sha256(b"missing").hexdigest()
    for path in sorted(root.rglob("*")):
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in path.parts):
            continue
        relative = str(path.relative_to(root)).replace("\\", "/")
        if path.is_dir():
            entries.append((relative, "dir", ""))
            continue
        if path.is_file():
            try:
                digest = sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            entries.append((relative, "file", digest))
    payload = json.dumps(entries, ensure_ascii=False, separators=( ',', ':' )).encode("utf-8")
    return sha256(payload).hexdigest()


def _repeat_action(record: _RepeatRecord, signature: str) -> str:
    """Return execute/reject/circuit for a repeated tool proposal."""
    if record.circuit_broken or record.attempts >= 4:
        record.attempts += 1
        record.circuit_broken = True
        return "circuit"
    if record.last_signature == signature and record.stale_streak >= 3:
        record.attempts += 1
        return "reject"
    return "execute"


def _record_tool_result(
    record: _RepeatRecord,
    signature: str,
    result: str,
    workspace_fingerprint: str | None,
    repeat_policy: str,
) -> bool:
    """Record an execution and report whether it was a stale duplicate."""
    result_fingerprint = _result_fingerprint(result)
    same_result = record.last_result == result_fingerprint
    same_workspace = repeat_policy != "stateful" or record.last_workspace == workspace_fingerprint
    stale = (
        repeat_policy != "polling"
        and record.last_signature == signature
        and same_result
        and same_workspace
    )
    # Only stale, no-progress executions belong to the duplicate budget. A
    # stateful call that changed the workspace starts a fresh baseline.
    record.attempts = record.attempts + 1 if stale else 1
    record.stale_streak = record.stale_streak + 1 if stale else 1
    record.last_signature = signature
    record.last_result = result_fingerprint
    record.last_workspace = workspace_fingerprint
    return stale


def _workspace_context(root: Path) -> str:
    """Give the model a bounded, useful snapshot of the selected project."""
    files: list[str] = []
    excerpts: list[str] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.name.startswith(".") and path.name not in {
            ".env.example",
            ".gitignore",
            ".eslintrc",
            ".prettierrc",
        }:
            continue
        ext = path.suffix.lower()
        if ext in _SKIP_EXTENSIONS:
            continue
        relative = str(path.relative_to(root))
        files.append(relative)
        if len(excerpts) < 40 and ext in _CODE_EXTENSIONS:
            try:
                content = path.read_text(encoding="utf-8")[:4000]
                excerpts.append(f"--- {relative} ---\n{content}")
                total += len(content)
            except (OSError, UnicodeDecodeError):
                pass
        if total >= 60000:
            break
    return (
        "Project files:\n"
        + ("\n".join(files) or "(empty)")
        + "\n\nRelevant excerpts:\n"
        + ("\n\n".join(excerpts) or "(none)")
    )


def _build_system_prompt(
    context: str, tool_approval_enabled: bool = True, local_workspace: bool = False
) -> str:
    """Build the main agent system prompt with tool descriptions and sub-agent list."""
    agents_desc = json.dumps(list_subagents(), ensure_ascii=False, indent=2)
    approval_note = (
        "Project tool approvals are temporarily disabled. Execute tools directly within the workspace."
        if not tool_approval_enabled
        else "High-risk tools require explicit user approval before execution."
    )
    workspace_note = (
        "Write operations are local-file proposals: emit write_file calls and verify them with read_file; execute_command and task are unavailable in local mode."
        if local_workspace
        else "Tools operate in the selected project workspace."
    )
    return (
        "You are a coding assistant working inside a project workspace. "
        "You have tools to inspect and modify the project. Use them when needed.\n\n"
        "## Available Tools\n"
        "- list_dir(path): List files in a directory\n"
        "- read_file(path): Read a file's content\n"
        "- grep_code(query, path): Search for text in source files\n"
        "- write_file(path, content): Create or overwrite source code and other workspace files\n"
        "- execute_command(command): Run a shell command\n"
        "- task(agent_name, task): Delegate to a sub-agent\n\n"
        "## Sub-Agents\n"
        f"{agents_desc}\n\n"
        "## Rules\n"
        "1. Use read-only tools first to understand the project before making changes.\n"
        "2. For complex tasks, delegate to sub-agents via the 'task' tool.\n"
        "3. Never output secrets, passwords, tokens, or credentials.\n"
        f"4. {approval_note}\n"
        "5. Keep all file paths inside the selected project workspace.\n"
        "6. When the user asks to create or modify code, use write_file and verify the result with read_file or a test command.\n"
        "7. When done, provide a clear final answer without tool calls.\n\n"
        f"## Workspace Mode\n{workspace_note}\n\n"
        f"## Project Context\n{context}"
    )


def _build_tool_schemas() -> list[dict]:
    """OpenAI-compatible tool schemas for all main agent tools."""
    schemas = []
    PARAMS = {
        "list_dir": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path relative to workspace root",
                }
            },
            "required": ["path"],
        },
        "read_file": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to workspace root",
                }
            },
            "required": ["path"],
        },
        "grep_code": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default '.')",
                },
            },
            "required": ["query"],
        },
        "write_file": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to workspace root",
                },
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
        "execute_command": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"}
            },
            "required": ["command"],
        },
        "task": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Sub-agent name: explorer, planner, coder, or tester",
                },
                "task": {
                    "type": "string",
                    "description": "Task description for the sub-agent",
                },
            },
            "required": ["agent_name", "task"],
        },
    }
    for name, spec in TOOLS.items():
        schemas.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": PARAMS.get(name, {"type": "object", "properties": {}}),
            },
        })
    return schemas


class AgentState(TypedDict):
    prompt: str
    user_prompt: str
    workspace: str
    response: str
    events: list["AgentEvent"]
    local_workspace: bool


@dataclass
class AgentEvent:
    type: str
    content: str = ""
    tool: str | None = None


async def _execute(state: AgentState) -> AgentState:
    """ReAct loop: LLM thinks -> calls tool -> observes -> repeats until done."""
    import asyncio
    from .security import PathGuard

    user_prompt = state["user_prompt"]
    agent_prompt = state.get("prompt") or user_prompt
    workspace = state.get("workspace", ".")
    events: list[AgentEvent] = [AgentEvent("run.started")]
    settings = get_settings()
    local_workspace = bool(state.get("local_workspace", False))

    # Sensitive request guard
    if settings.tool_approval_enabled and any(marker in user_prompt.lower() for marker in SENSITIVE_REQUEST_MARKERS):
        msg = "敏感配置不会被自动读取或回显。若需要访问，请使用 read_file 工具读取 .env，系统会先请求你的明确批准。"
        events.append(AgentEvent("message.delta", msg))
        events.append(AgentEvent("run.finished"))
        return {**state, "response": msg, "events": events}

    guard = PathGuard(Path(workspace))
    context = _workspace_context(Path(workspace))
    system_prompt = _build_system_prompt(
        context, settings.tool_approval_enabled, local_workspace
    )
    tool_schemas = _build_tool_schemas()

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": agent_prompt},
    ]

    final_answer = ""
    repeat_records: dict[str, _RepeatRecord] = {}
    pending_local_writes: dict[str, str] = {}
    while True:
        messages = _bound_react_messages(messages, settings.context_max_tokens)
        response = await complete_with_tools(messages, tool_schemas, timeout=120)

        # No tool call -> final answer
        if response.tool_call is None:
            final_answer = response.content
            events.append(AgentEvent("message.delta", final_answer))
            break

        tc = response.tool_call
        repeat_signature = _call_signature(tc.name, tc.arguments)
        try:
            repeat_tool_spec = get_tool(tc.name, caller="main")
        except Exception:
            repeat_tool_spec = None
        repeat_record = repeat_records.setdefault(repeat_signature, _RepeatRecord()) if repeat_tool_spec else None
        repeat_policy = repeat_tool_spec.repeat_policy if repeat_tool_spec else "guarded"
        repeat_action = (
            "execute"
            if repeat_record is None or repeat_policy == "polling"
            else _repeat_action(repeat_record, repeat_signature)
        )
        if repeat_action != "execute":
            if repeat_action == "circuit":
                repeat_message = (
                    "TOOL_CIRCUIT_BROKEN: 该工具与标准化参数组合累计尝试已达到 5 次，"
                    "本组合已熔断，禁止继续执行。请更换工具或参数并重新规划。"
                )
                repeat_event = "tool.circuit_broken"
            else:
                repeat_message = (
                    "DUPLICATE_CALL_REJECTED: 相同工具、参数和有效结果连续重复，"
                    "第 4 次起拒绝执行。请重新规划，不要再次提交相同调用。"
                )
                repeat_event = "tool.repeat_rejected"
            events.append(AgentEvent("tool.blocked", repeat_message, tc.name))
            events.append(AgentEvent(repeat_event, repeat_message, tc.name))
            messages.append({"role": "assistant", "content": None, "tool_calls": [tc.raw]})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": repeat_message})
            if repeat_action == "circuit":
                final_answer = repeat_message
                events.append(AgentEvent("message.delta", final_answer))
                break
            messages.append({
                "role": "system",
                "content": "A duplicate tool call was rejected. Re-plan now and choose a different tool or normalized argument set.",
            })
            continue
        repeat_workspace_before = (
            _workspace_fingerprint(Path(workspace))
            if repeat_record is not None and repeat_policy == "stateful"
            else None
        )
        events.append(
            AgentEvent(
                "tool.started",
                content=json.dumps({"arguments": tc.arguments}, ensure_ascii=False),
                tool=tc.name,
            )
        )

        # Execute the tool
        try:
            tool_spec = get_tool(tc.name, caller="main")
            handler = tool_spec.handler

            # Approval gate for high-risk tools
            if (
                settings.tool_approval_enabled
                and not local_workspace
                and tool_spec.risk.requires_approval
            ):
                events.append(
                    AgentEvent(
                        "approval.required",
                        content=json.dumps({
                            "tool": tc.name,
                            "arguments": tc.arguments,
                            "reason": tool_spec.risk.reason,
                        }),
                        tool=tc.name,
                    )
                )
                # In first version: report and stop (terminate-resume pattern)
                final_answer = f"需要审批: 工具 '{tc.name}' 需要你的确认。原因: {tool_spec.risk.reason}\n参数: {json.dumps(tc.arguments, ensure_ascii=False)}"
                events.append(AgentEvent("message.delta", final_answer))
                break

            if local_workspace and tc.name == "write_file":
                target = guard.resolve(tc.arguments["path"])
                relative = str(target.relative_to(guard.root)).replace("\\", "/")
                content = str(tc.arguments["content"])
                pending_local_writes[relative] = content
                events.append(
                    AgentEvent(
                        "file.write",
                        json.dumps(
                            {"path": relative, "content": content}, ensure_ascii=False
                        ),
                        tc.name,
                    )
                )
                result = f"LOCAL_FILE_WRITE_PENDING: {relative} ({len(content)} bytes)"
            elif local_workspace and tc.name in {"execute_command", "task"}:
                result = (
                    "LOCAL_WORKSPACE_ONLY: this operation is disabled because the bound folder is local. "
                    "Use write_file for code changes and read_file to verify them."
                )
            elif local_workspace and tc.name == "read_file":
                target = guard.resolve(tc.arguments["path"])
                relative = str(target.relative_to(guard.root)).replace("\\", "/")
                if relative in pending_local_writes:
                    result = pending_local_writes[relative]
                elif asyncio.iscoroutinefunction(handler):
                    result = await handler(guard=guard, **tc.arguments)
                else:
                    result = handler(guard=guard, **tc.arguments)
            elif asyncio.iscoroutinefunction(handler):
                result = await handler(guard=guard, **tc.arguments)
            else:
                result = handler(guard=guard, **tc.arguments)
        except Exception as exc:
            result = f"Error: {exc}"

        if repeat_record is not None and repeat_policy != "polling":
            repeat_workspace_after = (
                _workspace_fingerprint(Path(workspace))
                if repeat_policy == "stateful"
                else None
            )
            was_stale = _record_tool_result(
                repeat_record,
                repeat_signature,
                str(result),
                repeat_workspace_after if repeat_policy == "stateful" else repeat_workspace_before,
                repeat_policy,
            )
        else:
            was_stale = False

        events.append(AgentEvent("tool.finished", str(result)[:4000], tc.name))

        # Feed observation back to LLM
        messages.append({"role": "assistant", "content": None, "tool_calls": [tc.raw]})
        observation = str(result)[:8000]
        if was_stale and repeat_record.stale_streak == 3:
            replan_message = (
                "DUPLICATE_CALL_REPLAN: 已连续 3 次执行相同工具调用，且有效结果一致、工作区没有新进展。"
                "这是重复调用错误。请立即重新规划，改用不同工具或参数，不要重复当前调用。"
            )
            events.append(AgentEvent("tool.repeat_warning", replan_message, tc.name))
            observation = f"{observation}\n\n{replan_message}"
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": observation,
        })
        if was_stale and repeat_record.stale_streak == 3:
            messages.append({
                "role": "system",
                "content": "The duplicate-call error requires a fresh plan. Select another tool or materially different normalized arguments before continuing.",
            })
    events.append(AgentEvent("run.finished"))
    return {**state, "response": final_answer, "events": events}


_builder = StateGraph(AgentState)
_builder.add_node("execute", _execute)
_builder.add_edge(START, "execute")
_builder.add_edge("execute", END)


async def run_agent(
    prompt: str,
    workspace: str = ".",
    history: list[tuple[str, str]] | None = None,
    thread_id: str = "default",
    context_text: str | None = None,
    local_workspace: bool = True,
) -> AsyncIterator[AgentEvent]:
    settings = get_settings()
    connection_string = settings.database_url.replace("+asyncpg", "")
    async with AsyncPostgresSaver.from_conn_string(connection_string) as checkpointer:
        await checkpointer.setup()
        graph = _builder.compile(checkpointer=checkpointer)
        previous = "\n".join(f"{role}: {content}" for role, content in (history or [])[-20:])
        enriched_prompt = prompt
        if context_text:
            enriched_prompt = f"{context_text}\n\nUser request:\n{prompt}"
        elif previous:
            enriched_prompt = (
                f"Conversation history:\n{previous}\n\nUser request:\n{prompt}"
            )
        result = await graph.ainvoke(
            {
                "prompt": enriched_prompt,
                "user_prompt": prompt,
                "response": "",
                "events": [],
                "workspace": workspace,
                "local_workspace": local_workspace,
            },
            config={"configurable": {"thread_id": thread_id}},
        )
    for event in result["events"]:
        yield event
