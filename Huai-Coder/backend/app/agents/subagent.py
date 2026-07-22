import asyncio
import time
from dataclasses import dataclass, field

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


async def run_subagent(
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

    config = get_subagent_config(agent_name)
    if config is None:
        return SubAgentResult(
            agent_name=agent_name, output=f"Unknown agent: {agent_name}", turns_used=0
        )

    guard = PathGuard(workspace)
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
        if tool_spec.risk.requires_approval and tc.name not in approved:
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
