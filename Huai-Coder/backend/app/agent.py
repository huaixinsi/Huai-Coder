from pathlib import Path
from typing import AsyncIterator, TypedDict
from dataclasses import dataclass
import json

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from .config import get_settings
from .registry import get_tool, TOOLS
from .llm import complete_with_tools
from .agents.registry import list_subagents
from .context import estimate_messages, estimate_tokens

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


def _agent_token_budget(settings) -> int:
    """Allow several context windows of ReAct work while keeping a hard cap."""
    ratio = max(0.0, float(getattr(settings, "agent_token_budget_ratio", 4.0)))
    return max(1, int(settings.context_max_tokens * ratio))


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


def _build_system_prompt(context: str, tool_approval_enabled: bool = True) -> str:
    """Build the main agent system prompt with tool descriptions and sub-agent list."""
    agents_desc = json.dumps(list_subagents(), ensure_ascii=False, indent=2)
    approval_note = (
        "Project tool approvals are temporarily disabled. Execute tools directly within the workspace."
        if not tool_approval_enabled
        else "High-risk tools require explicit user approval before execution."
    )
    return (
        "You are a coding assistant working inside a project workspace. "
        "You have tools to inspect and modify the project. Use them when needed.\n\n"
        "## Available Tools\n"
        "- list_dir(path): List files in a directory\n"
        "- read_file(path): Read a file's content\n"
        "- grep_code(query, path): Search for text in source files\n"
        "- write_file(path, content): Write content to a file\n"
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
        "6. When done, provide a clear final answer without tool calls.\n\n"
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

    # Sensitive request guard
    if settings.tool_approval_enabled and any(marker in user_prompt.lower() for marker in SENSITIVE_REQUEST_MARKERS):
        msg = "敏感配置不会被自动读取或回显。若需要访问，请使用 read_file 工具读取 .env，系统会先请求你的明确批准。"
        events.append(AgentEvent("message.delta", msg))
        events.append(AgentEvent("run.finished"))
        return {**state, "response": msg, "events": events}

    guard = PathGuard(Path(workspace))
    context = _workspace_context(Path(workspace))
    system_prompt = _build_system_prompt(context, settings.tool_approval_enabled)
    tool_schemas = _build_tool_schemas()

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": agent_prompt},
    ]

    final_answer = ""
    repeated_call: tuple[str, str] | None = None
    repeated_count = 0
    token_budget = _agent_token_budget(settings)
    tokens_used = 0
    while True:
        messages = _bound_react_messages(messages, settings.context_max_tokens)
        request_tokens = estimate_messages(messages)
        if tokens_used and tokens_used + request_tokens > token_budget:
            final_answer = (
                f"本轮已达到 Agent Token 预算（{token_budget}），已停止继续调用工具。\n\n"
                "已保留当前会话上下文；你可以继续发送下一步要求，或将任务拆分成更小的步骤。"
            )
            events.append(AgentEvent("message.delta", final_answer))
            break
        tokens_used += request_tokens
        response = await complete_with_tools(messages, tool_schemas, timeout=120)
        response_tokens = estimate_tokens(response.content or "")
        if response.tool_call is not None:
            response_tokens += estimate_tokens(
                json.dumps(response.tool_call.arguments, ensure_ascii=False)
            )
        tokens_used += response_tokens

        # No tool call -> final answer
        if response.tool_call is None:
            final_answer = response.content
            events.append(AgentEvent("message.delta", final_answer))
            break

        tc = response.tool_call
        call_signature = (tc.name, json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False))
        if call_signature == repeated_call:
            repeated_count += 1
        else:
            repeated_call = call_signature
            repeated_count = 1
        if repeated_count >= 3:
            final_answer = (
                "本轮连续进行了相同的工具调用，Agent 已主动停止，避免重复执行。\n\n"
                "你可以缩小任务范围，或直接告诉我下一步要检查的文件。"
            )
            events.append(AgentEvent("message.delta", final_answer))
            break
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
            if settings.tool_approval_enabled and tool_spec.risk.requires_approval:
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

            if asyncio.iscoroutinefunction(handler):
                result = await handler(guard=guard, **tc.arguments)
            else:
                result = handler(guard=guard, **tc.arguments)
        except Exception as exc:
            result = f"Error: {exc}"

        events.append(AgentEvent("tool.finished", str(result)[:4000], tc.name))

        # Feed observation back to LLM
        messages.append({"role": "assistant", "content": None, "tool_calls": [tc.raw]})
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": str(result)[:8000],
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
            },
            config={"configurable": {"thread_id": thread_id}},
        )
    for event in result["events"]:
        yield event
