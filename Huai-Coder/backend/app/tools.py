import os
from pathlib import Path

WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", Path.cwd())).resolve()


def _safe_path(value: str, root: Path = WORKSPACE_ROOT) -> Path:
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path is outside the workspace")
    return candidate


async def execute_tool(name: str, arguments: dict[str, str], workspace: Path = WORKSPACE_ROOT) -> str:
    try:
        path = _safe_path(arguments.get("path", "."), workspace)
        if name == "list_dir":
            if not path.is_dir():
                return f"Not a directory: {arguments.get('path', '.') }"
            return "\n".join(sorted(item.name for item in path.iterdir())) or "(empty)"
        if name == "read_file":
            if not path.is_file():
                return f"Not a file: {arguments.get('path', '')}"
            return path.read_text(encoding="utf-8")[:12000]
        if name == "grep_code":
            query = arguments.get("query", "")
            if not query:
                return "Tool error: query is required"
            if not path.is_dir():
                return f"Not a directory: {arguments.get('path', '.') }"
            matches: list[str] = []
            for file in path.rglob("*"):
                if not file.is_file() or any(part in {".git", "node_modules", "__pycache__"} for part in file.parts):
                    continue
                try:
                    for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                        if query.lower() in line.lower():
                            matches.append(f"{file.relative_to(workspace)}:{number}:{line[:300]}")
                except (OSError, UnicodeDecodeError):
                    continue
            return "\n".join(matches[:200]) or "No matches"
        return f"Unknown tool: {name}"
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return f"Tool error: {error}"
