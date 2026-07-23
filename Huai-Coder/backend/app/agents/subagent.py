import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .base import SubAgentConfig
from .registry import get_subagent_config


@dataclass
class ToolCall:
    name: str
    arguments: dict


@dataclass
class SubAgentResult:
    agent_name: str
    output: str
    turns_used: int
    approval_required: bool = False
    pending_tool: ToolCall | None = None


class _SubAgentGraphState(TypedDict, total=False):
    """State passed through the LangGraph sub-agent boundary.

    The runner keeps its own message history, so the parent Agent never shares
    conversation messages with a child Agent.  Keeping the runner in graph
    state also makes the boundary explicit for future per-turn streaming.
    """

    runner: Any
    result: SubAgentResult | None


class _SubAgentResourceLimiter:
    """Process-wide concurrency and per-run quota for delegated Agents."""

    def __init__(self, max_parallel: int, max_per_run: int, queue_timeout: float):
        self.max_parallel = max(1, max_parallel)
        self.max_per_run = max(1, max_per_run)
        self.queue_timeout = max(0.1, queue_timeout)
        self._semaphore = asyncio.Semaphore(self.max_parallel)
        self._lock = asyncio.Lock()
        self._active_by_run: dict[str, int] = {}

    async def acquire(self, run_id: int | str | None) -> bool:
        run_key = str(run_id) if run_id is not None else "anonymous"
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.queue_timeout)
        except asyncio.TimeoutError:
            return False
        async with self._lock:
            active = self._active_by_run.get(run_key, 0)
            if active >= self.max_per_run:
                self._semaphore.release()
                return False
            self._active_by_run[run_key] = active + 1
        return True

    async def release(self, run_id: int | str | None) -> None:
        run_key = str(run_id) if run_id is not None else "anonymous"
        async with self._lock:
            active = self._active_by_run.get(run_key, 0)
            if active <= 1:
                self._active_by_run.pop(run_key, None)
            else:
                self._active_by_run[run_key] = active - 1
        self._semaphore.release()


_resource_limiter: _SubAgentResourceLimiter | None = None
_resource_limiter_key: tuple[int, int, float] | None = None


def _get_resource_limiter() -> _SubAgentResourceLimiter:
    from ..config import get_settings

    global _resource_limiter, _resource_limiter_key
    settings = get_settings()
    key = (
        max(1, int(getattr(settings, "subagent_max_parallel", 4))),
        max(1, int(getattr(settings, "subagent_max_per_run", 4))),
        max(0.1, float(getattr(settings, "subagent_queue_timeout_seconds", 5))),
    )
    if _resource_limiter is None or _resource_limiter_key != key:
        _resource_limiter = _SubAgentResourceLimiter(*key)
        _resource_limiter_key = key
    return _resource_limiter


async def _run_subagent_react(
    agent_name: str,
    task_prompt: str,
    workspace: str,
    approved_tools: set[str] | None = None,
) -> SubAgentResult:
    """Execute a sub-agent ReAct loop with hard permission constraints.

    Args:
        agent_name: One of the keys in SUBAGENT_CONFIGS.
        task_prompt: The specific task for this sub-agent.
        workspace: Root path of the project workspace.
        approved_tools: Tools the user has pre-approved (for resume after approval).

    Returns:
        SubAgentResult with output or an approval request.
    """
    # Lazy import to avoid circular dependency with registry.py
    from ..llm import complete_with_tools
    from ..registry import get_tool
    from ..security import PathGuard
    from ..config import get_settings

    config = get_subagent_config(agent_name)
    if config is None:
        return SubAgentResult(
            agent_name=agent_name, output=f"Unknown agent: {agent_name}", turns_used=0
        )

    guard = PathGuard(Path(workspace))
    tool_approval_enabled = get_settings().tool_approval_enabled
    approved = approved_tools or set()
    messages: list[dict] = [
        {"role": "system", "content": config.system_prompt},
        {"role": "user", "content": task_prompt},
    ]

    # Build tool schemas for the LLM (only tools this agent is allowed to use)
    tool_schemas = _build_tool_schemas(config)

    start = time.monotonic()
    for turn in range(config.max_turns):
        if time.monotonic() - start > config.timeout:
            return SubAgentResult(
                agent_name=agent_name,
                output=f"Timed out after {config.timeout}s ({turn} turns used)",
                turns_used=turn,
            )

        response = await complete_with_tools(messages, tool_schemas)

        # No tool call means the agent is done
        if response.tool_call is None:
            return SubAgentResult(
                agent_name=agent_name,
                output=response.content,
                turns_used=turn + 1,
            )

        tc = response.tool_call

        # Hard permission check (defense in depth, LLM schema already restricts)
        if not config.can_use(tc.name):
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [tc.raw],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": f"DENIED: '{tc.name}' is not in your permission set.",
            })
            continue

        # Approval gate for high-risk tools
        tool_spec = get_tool(tc.name, caller=agent_name)
        if tool_approval_enabled and tool_spec.risk.requires_approval and tc.name not in approved:
            return SubAgentResult(
                agent_name=agent_name,
                output=f"Approval required for tool '{tc.name}': {tool_spec.risk.reason}",
                turns_used=turn + 1,
                approval_required=True,
                pending_tool=ToolCall(name=tc.name, arguments=tc.arguments),
            )

        # Execute the tool
        try:
            handler = tool_spec.handler
            if asyncio.iscoroutinefunction(handler):
                result = await handler(guard=guard, **tc.arguments)
            else:
                result = handler(guard=guard, **tc.arguments)
        except Exception as exc:
            result = f"Error: {exc}"

        # Feed observation back
        messages.append({"role": "assistant", "content": None, "tool_calls": [tc.raw]})
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": str(result)[:8000],
        })

    return SubAgentResult(
        agent_name=agent_name,
        output=f"Reached max turns ({config.max_turns}) without completing.",
        turns_used=config.max_turns,
    )


async def run_subagent(
    agent_name: str,
    task_prompt: str,
    workspace: str,
    approved_tools: set[str] | None = None,
    run_id: int | str | None = None,
) -> SubAgentResult:
    """Run a permission-isolated child Agent inside a LangGraph subgraph.

    The subgraph currently contains one ReAct node so the existing streaming
    implementation remains compatible.  The explicit graph boundary gives
    each child its own state and makes per-turn streaming/checkpointing a safe
    follow-up.  A process-wide semaphore and per-run quota prevent a large
    parent task from exhausting model or tool resources.
    """

    limiter = _get_resource_limiter()
    if not await limiter.acquire(run_id):
        return SubAgentResult(
            agent_name=agent_name,
            output=(
                "SUBAGENT_RESOURCE_LIMIT: concurrent sub-agent capacity or "
                "per-run quota has been reached; retry after an active task finishes."
            ),
            turns_used=0,
        )

    async def run_node(state: _SubAgentGraphState) -> dict[str, SubAgentResult]:
        return {"result": await state["runner"]()}

    graph = StateGraph(_SubAgentGraphState)
    graph.add_node("react", run_node)
    graph.set_entry_point("react")
    graph.add_edge("react", END)
    compiled = graph.compile()
    try:
        result_state = await compiled.ainvoke(
            {
                "runner": lambda: _run_subagent_react(
                    agent_name=agent_name,
                    task_prompt=task_prompt,
                    workspace=workspace,
                    approved_tools=approved_tools,
                )
            }
        )
        return result_state["result"]
    finally:
        await limiter.release(run_id)


def _build_tool_schemas(config: SubAgentConfig) -> list[dict]:
    """Build OpenAI-compatible tool schemas for allowed tools only."""
    # Lazy import to avoid circular dependency
    from ..registry import TOOLS

    schemas = []
    for tool_name in config.tools:
        spec = TOOLS.get(tool_name)
        if spec is None:
            continue
        schemas.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": _infer_parameters(spec.name),
            },
        })
    return schemas


def _infer_parameters(tool_name: str) -> dict:
    """Static parameter schemas for known tools."""
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
    }
    return PARAMS.get(tool_name, {"type": "object", "properties": {}})
