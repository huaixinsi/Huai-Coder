from dataclasses import dataclass
from pathlib import Path
import asyncio
import os
import tempfile
from typing import Any, Callable
from .security import PathGuard, Risk, WorkspaceViolation, analyze_command

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk: Risk
    handler: Callable[..., Any]

def _list_dir(path: str, guard: PathGuard) -> str:
    target = guard.resolve(path)
    if not target.is_dir(): return f"Not a directory: {path}"
    return "\n".join(sorted(item.name for item in target.iterdir())) or "(empty)"

def _read_file(path: str, guard: PathGuard) -> str:
    target = guard.resolve(path)
    if not target.is_file(): return f"Not a file: {path}"
    return target.read_text(encoding="utf-8")[:12000]

def _grep_code(query: str, path: str, guard: PathGuard) -> str:
    target = guard.resolve(path or ".")
    matches = []
    for file in target.rglob("*"):
        if not file.is_file() or any(part in {".git", "node_modules", "__pycache__"} for part in file.parts): continue
        try:
            for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                if query.lower() in line.lower(): matches.append(f"{file.relative_to(guard.root)}:{number}:{line[:300]}")
        except (OSError, UnicodeDecodeError): pass
    return "\n".join(matches[:200]) or "No matches"

def _write_file(path: str, content: str, guard: PathGuard) -> str:
    target = guard.resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".huai-coder-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output: output.write(content)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return f"Wrote {target.relative_to(guard.root)} ({len(content)} bytes)"

async def _execute_command(command: str, guard: PathGuard) -> str:
    process = await asyncio.create_subprocess_shell(command, cwd=guard.root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={"PATH": os.getenv("PATH", "")})
    try: stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        process.kill(); await process.wait(); return "Command timed out after 30 seconds"
    output = (stdout + stderr).decode("utf-8", errors="replace")[:12000]
    return f"exit_code={process.returncode}\n{output}"

TOOLS = {
    "list_dir": ToolSpec("list_dir", "List files", Risk("low", "read-only", False), _list_dir),
    "read_file": ToolSpec("read_file", "Read a file", Risk("low", "read-only", False), _read_file),
    "grep_code": ToolSpec("grep_code", "Search source", Risk("low", "read-only", False), _grep_code),
    "write_file": ToolSpec("write_file", "Write a workspace file", Risk("high", "changes project contents", True), _write_file),
    "execute_command": ToolSpec("execute_command", "Run a command in the workspace", Risk("medium", "command is not guaranteed read-only", True), _execute_command),
}

def get_tool(name: str) -> ToolSpec:
    if name not in TOOLS: raise WorkspaceViolation(f"Unknown tool: {name}")
    return TOOLS[name]

def command_risk(command: str) -> Risk:
    return analyze_command(command)
