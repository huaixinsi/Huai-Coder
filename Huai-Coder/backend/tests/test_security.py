from pathlib import Path
import pytest
from app.security import PathGuard, WorkspaceViolation, analyze_command

def test_path_guard_rejects_escape(tmp_path: Path):
    guard = PathGuard(tmp_path)
    with pytest.raises(WorkspaceViolation): guard.resolve("../outside.txt")

def test_sensitive_path_requires_review(tmp_path: Path):
    assert PathGuard(tmp_path).is_sensitive((tmp_path / ".env").resolve())

def test_command_risk_classification():
    assert not analyze_command("git status").requires_approval
    assert analyze_command("rm -rf build").requires_approval
