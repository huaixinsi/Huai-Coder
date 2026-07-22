from pathlib import Path
from typing import AsyncIterator, TypedDict
from dataclasses import dataclass
from hashlib import sha256
import asyncio
import json
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from .config import get_settings
from .registry import get_tool, TOOLS
from .llm import ParsedToolCall, complete_with_tools
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

def _bound_react_messages(
    messages: list[dict], max_tokens: int, threshold: float = 0.75
) -> tuple[list[dict], bool]:
    """Compact complete assistant/tool groups without breaking the API contract."""
    if estimate_messages(messages) <= int(max_tokens * threshold) or len(messages) <= 4:
        return messages, False

    prefix = messages[:2]
    groups: list[list[dict]] = []
    index = 2
    while index < len(messages):
        message = messages[index]
        group = [message]
        index += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            while index < len(messages) and messages[index].get("role") == "tool":
                group.append(messages[index])
                index += 1
        groups.append(group)

    keep_groups: list[list[dict]] = []
    estimated = estimate_messages(prefix)
    for group in reversed(groups):
        group_tokens = estimate_messages(group)
        if keep_groups and estimated + group_tokens > int(max_tokens * threshold):
            break
        keep_groups.insert(0, group)
        estimated += group_tokens

    dropped = groups[: len(groups) - len(keep_groups)]
    summary = {
        "role": "system",
        "content": (
            f"Earlier context was compacted: {len(dropped)} complete message groups were removed. "
            "Preserve the original goal and acceptance criteria; re-check files when necessary."
        ),
    }
    return prefix + [summary] + [message for group in keep_groups for message in group], True


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
        "Write operations are local-file proposals: emit write_file calls and verify them with read_file; execute_command is delegated to the user's Local Runner, which can install detected dependencies and run tests/builds; task is unavailable in local mode."
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
                "command": {"type": "string", "description": "Command to run in the local project workspace"},
                "auto_prepare": {"type": "boolean", "description": "Install detected project dependencies before running", "default": True},
                "timeout_seconds": {"type": "integer", "description": "Maximum command time in seconds", "minimum": 5, "maximum": 900, "default": 120},
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
    run_id: int | None


@dataclass
class AgentEvent:
    type: str
    content: str = ""
    tool: str | None = None


CLIENT_TOOL_NAMES = {"list_dir", "read_file", "grep_code", "write_file", "execute_command"}
_CLIENT_TOOL_WAITERS: dict[str, tuple[int | None, asyncio.Future[dict]]] = {}


def resolve_client_tool_result(
    invocation_id: str, result: dict, run_id: int | None = None
) -> bool:
    """Resume a paused local tool call from the browser client."""
    waiter = _CLIENT_TOOL_WAITERS.get(invocation_id)
    if waiter is None:
        return False
    expected_run_id, future = waiter
    if run_id is not None and expected_run_id is not None and expected_run_id != run_id:
        return False
    if not future.done():
        future.set_result(result)
    return True


def _register_client_tool_waiter(
    invocation_id: str, run_id: int | None
) -> asyncio.Future[dict]:
    future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
    _CLIENT_TOOL_WAITERS[invocation_id] = (run_id, future)
    return future


async def _wait_for_client_tool(
    invocation_id: str,
    request: dict,
    future: asyncio.Future[dict],
    timeout_seconds: int,
) -> dict:
    try:
        return await asyncio.wait_for(future, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error_type": "client_tool_timeout",
            "result": f"Client tool timed out: {request.get('tool')}",
        }
    finally:
        _CLIENT_TOOL_WAITERS.pop(invocation_id, None)


async def _run_react_stream(state: AgentState) -> AsyncIterator[AgentEvent]:
    """Stream a resumable ReAct loop one event at a time."""
    from .security import PathGuard

    user_prompt = state["user_prompt"]
    agent_prompt = state.get("prompt") or user_prompt
    workspace = state.get("workspace", ".")
    settings = get_settings()
    local_workspace = bool(state.get("local_workspace", False))
    run_id = state.get("run_id")
    yield AgentEvent("run.started")

    if settings.tool_approval_enabled and any(
        marker in user_prompt.lower() for marker in SENSITIVE_REQUEST_MARKERS
    ):
        msg = "敏感配置不会被自动读取或回显。若需要访问，请使用 read_file 工具读取 .env，系统会先请求你的明确批准。"
        yield AgentEvent("message.delta", msg)
        yield AgentEvent("run.finished")
        return

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
    repeat_records: dict[str, _RepeatRecord] = {}

    for turn_no in range(1, getattr(settings, "react_max_turns", 128) + 1):
        messages, compacted = _bound_react_messages(
            messages,
            settings.context_max_tokens,
            getattr(settings, "context_compaction_threshold", 0.75),
        )
        if compacted:
            yield AgentEvent(
                "context.compacted",
                f"上下文已压缩（第 {turn_no} 轮），保留目标、规则和最近工具结果。",
            )

        try:
            response = await complete_with_tools(messages, tool_schemas, timeout=120)
        except Exception as error:
            yield AgentEvent("run.failed", str(error))
            return

        calls = response.tool_calls or ([response.tool_call] if response.tool_call else [])
        if not calls:
            if response.content:
                yield AgentEvent("message.delta", response.content)
            yield AgentEvent("run.finished")
            return

        if response.content:
            yield AgentEvent("message.delta", response.content)

        assistant_message = {
            "role": "assistant",
            "content": response.content or None,
            "tool_calls": [call.raw for call in calls],
        }
        messages.append(assistant_message)
        tool_results: list[tuple[ParsedToolCall, str, bool]] = []
        client_requests: list[tuple[ParsedToolCall, str, dict]] = []
        terminal_message: str | None = None
        blocked_call_ids: set[str] = set()

        for call in calls:
            yield AgentEvent(
                "tool.started",
                json.dumps({"arguments": call.arguments}, ensure_ascii=False),
                call.name,
            )
            try:
                tool_spec = get_tool(call.name, caller="main")
            except Exception as error:
                tool_results.append((call, f"Error: {error}", False))
                continue

            signature = _call_signature(call.name, call.arguments)
            record = repeat_records.setdefault(signature, _RepeatRecord())
            repeat_action = (
                "execute"
                if tool_spec.repeat_policy == "polling"
                else _repeat_action(record, signature)
            )
            if repeat_action != "execute":
                message = (
                    "TOOL_CIRCUIT_BROKEN: repeated tool call circuit opened; choose a different action."
                    if repeat_action == "circuit"
                    else "DUPLICATE_CALL_REJECTED: the same call made no progress; re-plan before retrying."
                )
                event_type = "tool.circuit_broken" if repeat_action == "circuit" else "tool.repeat_rejected"
                yield AgentEvent("tool.blocked", message, call.name)
                yield AgentEvent(event_type, message, call.name)
                tool_results.append((call, message, False))
                blocked_call_ids.add(call.id)
                if repeat_action == "circuit":
                    terminal_message = message
                continue

            if call.argument_error:
                tool_results.append((call, f"Invalid tool arguments: {call.argument_error}", False))
                continue

            if (
                settings.tool_approval_enabled
                and not local_workspace
                and tool_spec.risk.requires_approval
            ):
                yield AgentEvent(
                    "approval.required",
                    json.dumps(
                        {
                            "tool": call.name,
                            "arguments": call.arguments,
                            "reason": tool_spec.risk.reason,
                        },
                        ensure_ascii=False,
                    ),
                    call.name,
                )
                yield AgentEvent("run.waiting", "等待工具审批")
                return

            if local_workspace and call.name in CLIENT_TOOL_NAMES:
                invocation_id = f"client-{uuid4().hex}"
                request = {
                    "invocation_id": invocation_id,
                    "tool": call.name,
                    "arguments": call.arguments,
                    "execution": "client",
                }
                client_requests.append((call, invocation_id, request))
                continue

            if local_workspace and call.name == "task":
                tool_results.append(
                    (
                        call,
                        "LOCAL_WORKSPACE_ONLY: task delegation is unavailable in browser-local mode.",
                        False,
                    )
                )
                continue

            try:
                handler = tool_spec.handler
                if asyncio.iscoroutinefunction(handler):
                    result = await handler(guard=guard, **call.arguments)
                else:
                    result = handler(guard=guard, **call.arguments)
                tool_results.append((call, str(result)[:12000], True))
            except Exception as error:
                tool_results.append((call, f"Error: {error}", False))

        if client_requests:
            client_futures = {
                invocation_id: _register_client_tool_waiter(invocation_id, run_id)
                for _, invocation_id, _ in client_requests
            }
            yield AgentEvent(
                "tool.request",
                json.dumps(
                    {"calls": [request for _, _, request in client_requests]},
                    ensure_ascii=False,
                ),
            )
            client_results = await asyncio.gather(
                *(
                    _wait_for_client_tool(
                        invocation_id,
                        request,
                        client_futures[invocation_id],
                        getattr(settings, "client_tool_timeout_seconds", 300),
                    )
                    for _, invocation_id, request in client_requests
                )
            )
            for (call, _, _), client_result in zip(client_requests, client_results):
                ok = bool(client_result.get("ok", False))
                value = str(client_result.get("result") or client_result.get("error") or "")[:12000]
                tool_results.append((call, value, ok))

        # A single assistant tool-call message must be followed by one result
        # per call. This keeps OpenAI-compatible providers and Claude-style
        # adapters aligned even when calls were executed in different places.
        for call, result, ok in tool_results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result if ok else f"Tool error: {result}",
                }
            )
            yield AgentEvent("tool.finished" if ok else "tool.failed", result, call.name)

            if call.id in blocked_call_ids:
                continue

            try:
                tool_spec = get_tool(call.name, caller="main")
                record = repeat_records[_call_signature(call.name, call.arguments)]
                result_hash = _result_fingerprint(result)
                workspace_after = (
                    f"client:{result_hash}"
                    if local_workspace and call.name in CLIENT_TOOL_NAMES and ok
                    else _workspace_fingerprint(Path(workspace))
                    if tool_spec.repeat_policy == "stateful"
                    else None
                )
                stale = _record_tool_result(
                    record,
                    _call_signature(call.name, call.arguments),
                    result,
                    workspace_after,
                    tool_spec.repeat_policy,
                )
                if stale and record.stale_streak == 3:
                    yield AgentEvent(
                        "tool.repeat_warning",
                        "DUPLICATE_CALL_REPLAN: repeated result made no observable progress; choose a different tool or argument.",
                        call.name,
                    )
            except Exception:
                pass

        if terminal_message:
            yield AgentEvent("message.delta", terminal_message)
            yield AgentEvent("run.finished")
            return

    yield AgentEvent("run.limited", "达到 Agent 最大轮次限制，任务尚未完成。")


async def _execute(state: AgentState) -> AgentState:
    """Collect the streaming ReAct loop for unit tests and internal callers."""
    events: list[AgentEvent] = []
    async for event in _run_react_stream(state):
        events.append(event)
    response = next(
        (event.content for event in reversed(events) if event.type == "message.delta"),
        "",
    )
    return {**state, "response": response, "events": events}


async def _execute_legacy(state: AgentState) -> AgentState:
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
        messages, _ = _bound_react_messages(messages, settings.context_max_tokens)
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
    run_id: int | None = None,
) -> AsyncIterator[AgentEvent]:
    previous = "\n".join(
        f"{role}: {content}" for role, content in (history or [])[-20:]
    )
    enriched_prompt = prompt
    if context_text:
        enriched_prompt = f"{context_text}\n\nUser request:\n{prompt}"
    elif previous:
        enriched_prompt = f"Conversation history:\n{previous}\n\nUser request:\n{prompt}"
    state: AgentState = {
        "prompt": enriched_prompt,
        "user_prompt": prompt,
        "response": "",
        "events": [],
        "workspace": workspace,
        "local_workspace": local_workspace,
        "run_id": run_id,
    }
    async for event in _run_react_stream(state):
        yield event
