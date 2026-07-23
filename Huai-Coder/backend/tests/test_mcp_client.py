import asyncio
import json
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agent import _execute
from app.agent import _build_system_prompt
from app.llm import LLMResponse, ParsedToolCall
from app.mcp import McpManager, McpServerConfig, namespaced_tool_name
from app.mcp.client import SseMcpSession, StdioMcpSession, StreamableHttpMcpSession
from app.mcp.models import McpCallResult, McpToolDescriptor


def test_system_prompt_requires_browser_mcp_when_connected():
    browser = McpToolDescriptor(
        "playwright",
        "browser_navigate",
        "browser_navigate",
        "Navigate the browser",
        {"type": "object", "properties": {}},
        "low",
        "read-only browser operation",
        False,
    )
    prompt = _build_system_prompt("context", mcp_tools=[browser])
    assert "Browser MCP is connected" in prompt
    assert "MUST use the listed browser MCP tools" in prompt
    assert "Do not claim that you cannot operate a browser" in prompt


def test_system_prompt_explains_unconfigured_browser_mcp():
    prompt = _build_system_prompt("context", mcp_tools=[])
    assert "Browser MCP is not connected in this run" in prompt
    assert "no browser_* MCP tool is currently connected" in prompt


FAKE_SERVER = r'''
import json
import sys

TOOLS = [{
    "name": "browser_wait_for",
    "description": "Wait for text",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"]
    }
}, {
    "name": "create_pull_request",
    "description": "Create a pull request",
    "inputSchema": {"type": "object", "properties": {}}
}]

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if not request.get("id"):
        continue
    result = {}
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        arguments = request.get("params", {}).get("arguments", {})
        if request.get("params", {}).get("name") == "browser_wait_for":
            result = {"content": [{"type": "text", "text": "waited for " + arguments.get("text", "")}]}
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "write denied"}]}
    else:
        result = {"content": [{"type": "text", "text": "unknown method"}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''

GITHUB_READ_SERVER = r'''
import json
import sys

TOOLS = [{
    "name": "get_file_contents",
    "description": "Read a repository file",
    "inputSchema": {"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "path": {"type": "string"}}}
}, {
    "name": "create_pull_request",
    "description": "Create a pull request",
    "inputSchema": {"type": "object", "properties": {}}
}]

for line in sys.stdin:
    request = json.loads(line)
    if not request.get("id"):
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "github-fake"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call" and request.get("params", {}).get("name") == "get_file_contents":
        result = {"content": [{"type": "text", "text": "package.json from github"}]}
    else:
        result = {"isError": True, "content": [{"type": "text", "text": "github write blocked"}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


@pytest.mark.asyncio
async def test_stdio_session_initializes_lists_and_calls_tools():
    config = McpServerConfig(
        server_id="fake",
        command=sys.executable,
        args=("-u", "-c", FAKE_SERVER),
        call_timeout_seconds=5,
    )
    session = StdioMcpSession(config)
    try:
        result = await session.initialize()
        assert result["serverInfo"]["name"] == "fake"
        tools = await session.list_tools()
        assert [tool["name"] for tool in tools] == ["browser_wait_for", "create_pull_request"]
        call = await session.call_tool("browser_wait_for", {"text": "loaded"})
        assert call["content"][0]["text"] == "waited for loaded"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_manager_filters_tools_namespaces_and_preserves_mcp_errors(tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({
            "mcpServers": {
                "fake": {
                    "command": sys.executable,
                    "args": ["-u", "-c", FAKE_SERVER],
                    "allowedTools": ["browser_wait_for", "create_pull_request"]
                }
            }
        }),
        encoding="utf-8",
    )
    manager = McpManager(str(config_path))
    try:
        tools = await manager.list_tools()
        assert {tool.model_name for tool in tools} == {
            namespaced_tool_name("fake", "browser_wait_for"),
            namespaced_tool_name("fake", "create_pull_request"),
        }
        wait = await manager.call_tool("mcp__fake__browser_wait_for", {"text": "ready"})
        assert wait.ok is True
        assert "waited for ready" in wait.text
        denied = await manager.call_tool("mcp__fake__create_pull_request", {})
        assert denied.ok is False
        assert denied.is_error is True
        assert "write denied" in denied.text
        statuses = manager.server_statuses()
        assert statuses[0]["status"] == "ready"
        assert statuses[0]["tool_count"] == 2
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_manager_reload_config_replaces_cached_servers(tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({
            "mcpServers": {
                "fake": {
                    "command": sys.executable,
                    "args": ["-u", "-c", FAKE_SERVER],
                    "allowedTools": ["browser_wait_for"],
                }
            }
        }),
        encoding="utf-8",
    )
    manager = McpManager(str(config_path))
    try:
        assert len(await manager.list_tools()) == 1
        config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        await manager.reload_config()
        assert await manager.list_tools() == []
        assert manager.server_statuses() == []
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_github_mcp_simulation_discovers_read_tool_and_preserves_namespace(tmp_path):
    config_path = tmp_path / "github-mcp.json"
    config_path.write_text(
        json.dumps({
            "mcpServers": {
                "github": {
                    "command": sys.executable,
                    "args": ["-u", "-c", GITHUB_READ_SERVER],
                    "allowedTools": ["get_file_contents", "create_pull_request"],
                }
            }
        }),
        encoding="utf-8",
    )
    manager = McpManager(str(config_path))
    try:
        tools = await manager.list_tools()
        names = {tool.model_name for tool in tools}
        assert names == {
            namespaced_tool_name("github", "get_file_contents"),
            namespaced_tool_name("github", "create_pull_request"),
        }
        read = await manager.call_tool(
            namespaced_tool_name("github", "get_file_contents"),
            {"owner": "octo", "repo": "demo", "path": "package.json"},
        )
        assert read.ok is True
        assert "package.json" in read.text
    finally:
        await manager.close_all()


def test_mcp_server_config_expands_timeout_and_hides_env_values():
    config = McpServerConfig.from_mapping(
        "github",
        {
            "command": "docker",
            "args": ["run"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "secret"},
            "headers": {
                "Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}",
                "X-MCP-Readonly": "true",
            },
            "connectTimeoutMs": 1500,
            "callTimeoutMs": 2500,
        },
    )
    assert config.connect_timeout_seconds == 1.5
    assert config.call_timeout_seconds == 2.5
    public = config.public_dict()
    assert public["has_env"] is True
    assert public["header_names"] == ["Authorization", "X-MCP-Readonly"]
    assert "secret" not in json.dumps(public)


@pytest.mark.asyncio
async def test_manager_config_crud_persists_placeholders_and_rejects_secrets(tmp_path):
    config_path = tmp_path / "mcp.json"
    manager = McpManager(str(config_path))
    raw = {
        "enabled": True,
        "command": sys.executable,
        "args": ["-u", "-c", FAKE_SERVER],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"},
        "allowedTools": ["browser_wait_for"],
    }
    try:
        status = await manager.upsert_server("fake", raw)
        assert status["id"] == "fake"
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["mcpServers"]["fake"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${GITHUB_PERSONAL_ACCESS_TOKEN}"
        assert manager.server_config_mapping("fake")["command"] == sys.executable
        tools = await manager.list_server_tools("fake")
        assert [tool.original_name for tool in tools] == ["browser_wait_for"]
        with pytest.raises(ValueError):
            await manager.upsert_server("unsafe", {"command": sys.executable, "env": {"TOKEN": "plain-secret"}})
        with pytest.raises(ValueError):
            await manager.upsert_server(
                "unsafe-header",
                {
                    "transport": "streamable_http",
                    "url": "http://mcp.test",
                    "headers": {"Authorization": "Bearer plain-secret"},
                },
            )
        await manager.remove_server("fake")
        assert not config_path.read_text(encoding="utf-8").__contains__("fake")
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_configured_mcp_approval_can_raise_risk_for_read_like_tool(tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps({"mcpServers": {"fake": {
        "command": sys.executable,
        "args": ["-u", "-c", FAKE_SERVER],
        "allowedTools": ["browser_wait_for"],
        "approval": {"browser_wait_for": "confirm"},
    }}}, ensure_ascii=False), encoding="utf-8")
    manager = McpManager(str(config_path))
    try:
        tools = await manager.list_tools()
        descriptor = next(tool for tool in tools if tool.original_name == "browser_wait_for")
        assert descriptor.requires_approval is True
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_sse_session_handles_endpoint_and_jsonrpc_events():
    import httpx

    stream_body = "\n".join([
        "event: endpoint",
        "data: /messages?session_id=test",
        "",
        "event: message",
        'data: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"sse-fake"}}}',
        "",
        "event: message",
        'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"browser_tabs"}]}}',
        "",
        "event: message",
        'data: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"ok"}]}}',
        "",
    ]) + "\n"

    async def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream_body.encode())
        return httpx.Response(202)

    config = McpServerConfig(server_id="sse", transport="sse", url="http://mcp.test/sse", call_timeout_seconds=2)
    session = SseMcpSession(config)
    await session.client.aclose()
    session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        initialized = await session.initialize()
        assert initialized["serverInfo"]["name"] == "sse-fake"
        assert [tool["name"] for tool in await session.list_tools()] == ["browser_tabs"]
        assert (await session.call_tool("browser_tabs", {}))["content"][0]["text"] == "ok"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_streamable_http_session_round_trip():
    import httpx

    async def handler(request: httpx.Request):
        if request.method == "DELETE":
            return httpx.Response(200)
        assert request.headers.get("Authorization") == "Bearer test"
        payload = json.loads(request.content.decode())
        if "id" not in payload:
            return httpx.Response(202)
        method = payload["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "http-fake"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "browser_snapshot", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": "http-ok"}]}
        return httpx.Response(200, headers={"Mcp-Session-Id": "http-session"}, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    config = McpServerConfig(
        server_id="http",
        transport="streamable_http",
        url="http://mcp.test/mcp",
        headers={"Authorization": "Bearer test"},
        call_timeout_seconds=2,
    )
    session = StreamableHttpMcpSession(config)
    await session.client.aclose()
    session.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        initialized = await session.initialize()
        assert initialized["serverInfo"]["name"] == "http-fake"
        assert (await session.list_tools())[0]["name"] == "browser_snapshot"
        assert (await session.call_tool("browser_snapshot", {}))["content"][0]["text"] == "http-ok"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_react_routes_mcp_tool_and_returns_observation_to_model():
    descriptor = McpToolDescriptor(
        server_id="fake",
        name="browser_wait_for",
        original_name="browser_wait_for",
        description="Wait for text",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        risk_level="low",
        risk_reason="读取或等待操作",
        requires_approval=False,
        repeat_policy="polling",
    )

    class FakeManager:
        def __init__(self):
            self.calls = []

        async def list_tools(self):
            return [descriptor]

        def find_tool(self, name):
            return descriptor if name == descriptor.model_name else None

        def server_statuses(self):
            return [{"id": "fake", "status": "ready", "error": None}]

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return McpCallResult(True, "页面已出现：loaded", server_id="fake", tool_name="browser_wait_for")

    manager = FakeManager()
    responses = [
        LLMResponse(content="", tool_call=ParsedToolCall("mcp-call", descriptor.model_name, {"text": "loaded"}, {"id": "mcp-call"})),
        LLMResponse(content="已确认页面加载完成"),
    ]
    calls = []

    async def fake_complete(messages, tools, timeout=120):
        calls.append((messages, tools))
        return responses[len(calls) - 1]

    settings = SimpleNamespace(
        context_max_tokens=100000,
        tool_approval_enabled=False,
        mcp_enabled=True,
        mcp_approval_enabled=True,
        react_max_turns=8,
        context_compaction_threshold=0.75,
        client_tool_timeout_seconds=1,
    )
    with tempfile.TemporaryDirectory() as workspace:
        state = {"prompt": "wait", "user_prompt": "wait", "workspace": workspace, "response": "", "events": [], "local_workspace": True, "run_id": None}
        with patch("app.agent.get_settings", return_value=settings), patch("app.agent.get_mcp_manager", return_value=manager), patch("app.agent._workspace_context", return_value="context"), patch("app.agent._build_system_prompt", return_value="system"), patch("app.agent.complete_with_tools", side_effect=fake_complete):
            result = await _execute(state)

    assert manager.calls == [(descriptor.model_name, {"text": "loaded"})]
    assert any(event.type == "mcp.tool.completed" for event in result["events"])
    assert any(event.type == "tool.finished" and event.tool == descriptor.model_name for event in result["events"])
    assert result["response"] == "已确认页面加载完成"
    assert any(tool["function"]["name"] == descriptor.model_name for tool in calls[0][1])


@pytest.mark.asyncio
async def test_mcp_external_effect_requires_approval_even_in_local_workspace():
    descriptor = McpToolDescriptor(
        server_id="github",
        name="create_pull_request",
        original_name="create_pull_request",
        description="Create a pull request",
        input_schema={"type": "object", "properties": {}},
        risk_level="high",
        risk_reason="可能改变远程服务或产生外部副作用",
        requires_approval=True,
    )

    class FakeManager:
        async def list_tools(self): return [descriptor]
        def find_tool(self, name): return descriptor if name == descriptor.model_name else None
        def server_statuses(self): return [{"id": "github", "status": "ready", "error": None}]

    settings = SimpleNamespace(
        context_max_tokens=100000,
        tool_approval_enabled=False,
        mcp_enabled=True,
        mcp_approval_enabled=True,
        react_max_turns=8,
        context_compaction_threshold=0.75,
        client_tool_timeout_seconds=1,
    )
    response = LLMResponse(content="", tool_call=ParsedToolCall("pr-call", descriptor.model_name, {}, {"id": "pr-call"}))
    with tempfile.TemporaryDirectory() as workspace:
        state = {"prompt": "create pr", "user_prompt": "create pr", "workspace": workspace, "response": "", "events": [], "local_workspace": True, "run_id": None}
        with patch("app.agent.get_settings", return_value=settings), patch("app.agent.get_mcp_manager", return_value=FakeManager()), patch("app.agent._workspace_context", return_value="context"), patch("app.agent._build_system_prompt", return_value="system"), patch("app.agent.complete_with_tools", return_value=response):
            result = await _execute(state)

    approval = next(event for event in result["events"] if event.type == "approval.required")
    assert approval.tool == descriptor.model_name
    assert "github" in approval.content
    assert result["events"][-1].type == "run.waiting"


@pytest.mark.asyncio
async def test_react_emits_terminal_cancelled_event():
    class EmptyManager:
        async def list_tools(self): return []
        def server_statuses(self): return []

    settings = SimpleNamespace(
        context_max_tokens=100000,
        tool_approval_enabled=False,
        mcp_enabled=True,
        mcp_approval_enabled=True,
        react_max_turns=8,
        context_compaction_threshold=0.75,
        client_tool_timeout_seconds=1,
    )
    cancel_event = asyncio.Event()
    cancel_event.set()
    with tempfile.TemporaryDirectory() as workspace:
        state = {"prompt": "cancel", "user_prompt": "cancel", "workspace": workspace, "response": "", "events": [], "local_workspace": True, "run_id": 1, "cancel_event": cancel_event}
        with patch("app.agent.get_settings", return_value=settings), patch("app.agent.get_mcp_manager", return_value=EmptyManager()), patch("app.agent._workspace_context", return_value="context"):
            result = await _execute(state)
    assert [event.type for event in result["events"]][-1] == "run.cancelled"
