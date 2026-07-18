import pytest
from app.tools import execute_tool

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
