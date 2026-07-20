from pathlib import Path
from typing import AsyncIterator, TypedDict
from dataclasses import dataclass
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from .config import get_settings
from .registry import get_tool
from .llm import complete

class AgentState(TypedDict):
    prompt: str
    user_prompt: str
    workspace: str
    response: str
    events: list["AgentEvent"]

def _workspace_context(root: Path) -> str:
    """Give the model a bounded, useful snapshot of the selected project."""
    files: list[str] = []
    excerpts: list[str] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in {".git", "node_modules", "__pycache__"} for part in path.parts):
            continue
        relative = str(path.relative_to(root))
        files.append(relative)
        if len(excerpts) < 40 and path.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".json", ".md", ".yml", ".yaml", ".java", ".go", ".rs", ".sql", ".html", ".css"}:
            try:
                content = path.read_text(encoding="utf-8")[:4000]
                excerpts.append(f"--- {relative} ---\n{content}")
                total += len(content)
            except (OSError, UnicodeDecodeError):
                pass
        if total >= 60000:
            break
    return "Project files:\n" + ("\n".join(files) or "(empty)") + "\n\nRelevant excerpts:\n" + ("\n\n".join(excerpts) or "(none)")

@dataclass
class AgentEvent:
    type: str
    content: str = ""
    tool: str | None = None

async def _execute(state: AgentState) -> AgentState:
    user_prompt = state["user_prompt"]
    prompt = state["prompt"]
    events = [AgentEvent("run.started")]
    if user_prompt.startswith("/list"):
        path = user_prompt.removeprefix("/list").strip() or "."
        events.append(AgentEvent("tool.started", tool="list_dir"))
        result = get_tool("list_dir").handler(path, Path(state.get("workspace", ".")))
        events.extend([AgentEvent("tool.finished", result, "list_dir"), AgentEvent("message.delta", result)])
    elif user_prompt.startswith("/read"):
        path = user_prompt.removeprefix("/read").strip()
        events.append(AgentEvent("tool.started", tool="read_file"))
        result = get_tool("read_file").handler(path, Path(state.get("workspace", ".")))
        events.extend([AgentEvent("tool.finished", result, "read_file"), AgentEvent("message.delta", result)])
    elif user_prompt.startswith("/grep"):
        query, _, path = user_prompt.removeprefix("/grep").strip().partition(" ")
        events.append(AgentEvent("tool.started", tool="grep_code"))
        result = get_tool("grep_code").handler(query, path or ".", Path(state.get("workspace", ".")))
        events.extend([AgentEvent("tool.finished", result, "grep_code"), AgentEvent("message.delta", result)])
    else:
        events.append(AgentEvent("message.delta", await complete(prompt)))
    events.append(AgentEvent("run.finished"))
    return {**state, "response": events[-2].content, "events": events}

_builder = StateGraph(AgentState)
_builder.add_node("execute", _execute)
_builder.add_edge(START, "execute")
_builder.add_edge("execute", END)

async def run_agent(prompt: str, workspace: str = ".", history: list[tuple[str, str]] | None = None, thread_id: str = "default") -> AsyncIterator[AgentEvent]:
    settings = get_settings()
    connection_string = settings.database_url.replace("+asyncpg", "")
    async with AsyncPostgresSaver.from_conn_string(connection_string) as checkpointer:
        await checkpointer.setup()
        graph = _builder.compile(checkpointer=checkpointer)
        context = _workspace_context(Path(workspace))
        previous = "\n".join(f"{role}: {content}" for role, content in (history or [])[-20:])
        enriched_prompt = f"You are working in the selected project. Use the project context below to answer. If exact details are needed, inspect files with the available workspace tools.\n\nConversation history:\n{previous or '(none)'}\n\n{context}\n\nUser request:\n{prompt}"
        result = await graph.ainvoke({"prompt": enriched_prompt, "user_prompt": prompt, "response": "", "events": [], "workspace": workspace}, config={"configurable": {"thread_id": thread_id}})
    for event in result["events"]:
        yield event
