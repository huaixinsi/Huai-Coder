from dataclasses import dataclass
from pathlib import Path
import re
import shlex

SENSITIVE_NAMES = {".env", ".ssh", "credentials", "secrets", "secret", "token", "id_rsa"}
SENSITIVE_SUFFIXES = {".pem", ".key"}

class WorkspaceViolation(ValueError):
    pass

class PathGuard:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def resolve(self, value: str) -> Path:
        if not value or Path(value).is_absolute():
            raise WorkspaceViolation("absolute or empty paths are not allowed")
        candidate = (self.root / value).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceViolation("path is outside the workspace")
        return candidate

    def is_sensitive(self, path: Path) -> bool:
        relative = path.resolve().relative_to(self.root)
        for part in relative.parts:
            lower = part.lower()
            if lower in SENSITIVE_NAMES or lower.startswith(".env.") or lower.endswith(tuple(SENSITIVE_SUFFIXES)):
                return True
        return False

@dataclass(frozen=True)
class Risk:
    level: str
    reason: str
    requires_approval: bool

def analyze_command(command: str) -> Risk:
    lowered = command.lower()
    if not command.strip():
        return Risk("critical", "empty command", True)
    dangerous = (r"\brm\b", r"\bdel\b", r"remove-item", r"git\s+(reset|checkout|clean)", r"(npm|pip|pnpm|yarn)\s+install", r"(docker|systemctl)\s+(run|start|stop)", r"\bformat\b")
    if any(re.search(pattern, lowered) for pattern in dangerous):
        return Risk("high", "command can modify, delete, install, or control services", True)
    if any(token in lowered for token in ("git status", "git diff", "--version", "pwd", "ls", "dir", "find", "cat", "type")):
        return Risk("low", "read-only inspection command", False)
    return Risk("medium", "command is not in the read-only allowlist", True)

def scrub(value: object) -> str:
    text = str(value)
    return re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*[^\s,]+", r"\1=[REDACTED]", text)[:2000]
