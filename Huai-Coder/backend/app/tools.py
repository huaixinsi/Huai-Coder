from pathlib import Path

WORKSPACE_ROOT = Path.cwd().resolve()


def _safe_path(value: str) -> Path:
    candidate = (WORKSPACE_ROOT / value).resolve()
    if candidate != WORKSPACE_ROOT and WORKSPACE_ROOT not in candidate.parents:
        raise ValueError("path is outside the workspace")
    return candidate


async def execute_tool(name: str, arguments: dict[str, str]) -> str:
    try:
        path = _safe_path(arguments.get("path", "."))
        if name == "list_dir":
            if not path.is_dir():
                return f"Not a directory: {arguments.get('path', '.') }"
            return "\n".join(sorted(item.name for item in path.iterdir())) or "(empty)"
        if name == "read_file":
            if not path.is_file():
                return f"Not a file: {arguments.get('path', '')}"
            return path.read_text(encoding="utf-8")[:12000]
        return f"Unknown tool: {name}"
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return f"Tool error: {error}"
