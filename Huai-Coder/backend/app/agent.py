from typing import AsyncIterator, TypedDict
from dataclasses import dataclass
from langgraph.graph import END, START, StateGraph
from .tools import execute_tool
from .llm import complete

class AgentState(TypedDict):
    prompt: str
    response: str
    events: list["AgentEvent"]

@dataclass
class AgentEvent:
    type: str
    content: str = ""
    tool: str | None = None

async def _execute(state: AgentState) -> AgentState:
    prompt = state["prompt"]
    events = [AgentEvent("run.started")]
    if prompt.startswith("/list"):
        path = prompt.removeprefix("/list").strip() or "."
        events.append(AgentEvent("tool.started", tool="list_dir"))
        result = await execute_tool("list_dir", {"path": path})
        events.extend([AgentEvent("tool.finished", result, "list_dir"), AgentEvent("message.delta", result)])
    elif prompt.startswith("/read"):
        path = prompt.removeprefix("/read").strip()
        events.append(AgentEvent("tool.started", tool="read_file"))
        result = await execute_tool("read_file", {"path": path})
        events.extend([AgentEvent("tool.finished", result, "read_file"), AgentEvent("message.delta", result)])
    elif prompt.startswith("/grep"):
        query, _, path = prompt.removeprefix("/grep").strip().partition(" ")
        events.append(AgentEvent("tool.started", tool="grep_code"))
        result = await execute_tool("grep_code", {"query": query, "path": path or "."})
        events.extend([AgentEvent("tool.finished", result, "grep_code"), AgentEvent("message.delta", result)])
    else:
        events.append(AgentEvent("message.delta", await complete(prompt)))
    events.append(AgentEvent("run.finished"))
    return {**state, "response": events[-2].content, "events": events}

_graph = StateGraph(AgentState)
_graph.add_node("execute", _execute)
_graph.add_edge(START, "execute")
_graph.add_edge("execute", END)
agent_graph = _graph.compile()

async def run_agent(prompt: str) -> AsyncIterator[AgentEvent]:
    result = await agent_graph.ainvoke({"prompt": prompt, "response": "", "events": []})
    for event in result["events"]:
        yield event
