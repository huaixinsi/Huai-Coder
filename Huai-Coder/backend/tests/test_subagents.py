import pytest

from app.agents.registry import get_subagent_config, list_subagents
from app.agents.subagent import (
    _SubAgentResourceLimiter,
    _build_tool_schemas,
    run_subagent,
)
from app.main import subagent_catalog
from app.llm import LLMResponse


def test_subagent_registry_exposes_four_isolated_roles():
    names = {item["name"] for item in list_subagents()}
    assert names == {"explorer", "planner", "coder", "tester"}
    assert "execute_command" not in get_subagent_config("coder").tools
    assert "write_file" not in get_subagent_config("tester").tools


def test_subagent_tool_schema_is_limited_to_declared_permissions():
    schemas = _build_tool_schemas(get_subagent_config("explorer"))
    names = {item["function"]["name"] for item in schemas}
    assert names == {"list_dir", "read_file", "grep_code"}
    assert "write_file" not in names
    assert "execute_command" not in names


@pytest.mark.asyncio
async def test_subagent_catalog_exposes_graph_and_roles():
    payload = await subagent_catalog()
    assert payload["graph"]["root"] == "react"
    assert payload["graph"]["child"] == "subagent.react"
    assert {agent["name"] for agent in payload["agents"]} == {
        "explorer", "planner", "coder", "tester"
    }


@pytest.mark.asyncio
async def test_subagent_runs_inside_graph_with_private_context(monkeypatch, tmp_path):
    async def fake_complete(messages, tools, timeout=60):
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert {item["function"]["name"] for item in tools} == {
            "list_dir",
            "read_file",
            "grep_code",
        }
        return LLMResponse(content="explorer finished")

    monkeypatch.setattr("app.llm.complete_with_tools", fake_complete)
    result = await run_subagent("explorer", "inspect the project", str(tmp_path), run_id=7)
    assert result.output == "explorer finished"
    assert result.turns_used == 1
    assert result.approval_required is False


@pytest.mark.asyncio
async def test_subagent_resource_limiter_enforces_parallel_and_per_run_quota():
    limiter = _SubAgentResourceLimiter(max_parallel=1, max_per_run=1, queue_timeout=0.01)
    assert await limiter.acquire("run-1") is True
    assert await limiter.acquire("run-1") is False
    assert await limiter.acquire("run-2") is False
    await limiter.release("run-1")
    assert await limiter.acquire("run-2") is True
    await limiter.release("run-2")
