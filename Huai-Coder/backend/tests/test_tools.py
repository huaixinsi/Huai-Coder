import pytest
from app.tools import execute_tool
from app.registry import get_tool
from app.security import PathGuard

@pytest.mark.asyncio
async def test_list_workspace():
    result = await execute_tool("list_dir", {"path": "."})
    assert "app" in result

@pytest.mark.asyncio
async def test_rejects_path_escape():
    result = await execute_tool("read_file", {"path": "../secret.txt"})
    assert "outside the workspace" in result

@pytest.mark.asyncio
async def test_grep_code():
    result = await execute_tool("grep_code", {"query": "WORKSPACE_ROOT", "path": "app"})
    assert "tools.py" in result

@pytest.mark.asyncio
async def test_write_file(tmp_path):
    result = await execute_tool("write_file", {"path": "src/example.py", "content": "print('ok')\n"}, workspace=tmp_path)
    assert "Wrote src/example.py" in result
    assert (tmp_path / "src/example.py").read_text(encoding="utf-8") == "print('ok')\n"

def test_agent_write_file_tool(tmp_path):
    tool = get_tool("write_file")
    result = tool.handler(guard=PathGuard(tmp_path), path="src/agent.py", content="x = 1\n")
    assert "Wrote src/agent.py" in result
    assert (tmp_path / "src/agent.py").read_text(encoding="utf-8") == "x = 1\n"
