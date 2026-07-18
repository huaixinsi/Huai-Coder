from dataclasses import dataclass
from typing import AsyncIterator

from .tools import execute_tool


@dataclass
class AgentEvent:
    type: str
    content: str = ""
    tool: str | None = None


async def run_agent(prompt: str) -> AsyncIterator[AgentEvent]:
    yield AgentEvent("run.started")
    if prompt.startswith("/list "):
        path = prompt.removeprefix("/list ").strip() or "."
        yield AgentEvent("tool.started", tool="list_dir")
        result = await execute_tool("list_dir", {"path": path})
        yield AgentEvent("tool.finished", content=result, tool="list_dir")
        yield AgentEvent("message.delta", content=result)
    elif prompt.startswith("/read "):
        path = prompt.removeprefix("/read ").strip()
        yield AgentEvent("tool.started", tool="read_file")
        result = await execute_tool("read_file", {"path": path})
        yield AgentEvent("tool.finished", content=result, tool="read_file")
        yield AgentEvent("message.delta", content=result)
    else:
        yield AgentEvent("message.delta", content=f"Received: {prompt}\n\nAvailable tools: /list <path>, /read <path>")
    yield AgentEvent("run.finished")
