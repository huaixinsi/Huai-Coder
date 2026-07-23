from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any
from urllib.parse import urljoin

import httpx

from ..config import get_settings
from .models import (
    McpCallResult,
    McpServerConfig,
    McpToolDescriptor,
    classify_mcp_tool_risk,
    content_to_text,
)


class McpProtocolError(RuntimeError):
    pass


class _JsonRpcSession:
    async def initialize(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    async def list_tools(self) -> list[dict[str, Any]]:  # pragma: no cover - interface
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class PythonSdkMcpSession(_JsonRpcSession):
    """MCP Python SDK transport adapter with the local wire client as fallback.

    The SDK owns the transport lifecycle and JSON-RPC details.  Keeping this
    adapter behind the same small session interface lets Huai-Coder preserve
    its risk, allowlist, retry, and audit layers independent of the transport.
    """

    def __init__(self, config: McpServerConfig):
        self.config = config
        self._stack: AsyncExitStack | None = None
        self._session: Any = None

    @staticmethod
    def available() -> bool:
        try:
            import mcp  # noqa: F401
        except ImportError:
            return False
        return True

    async def initialize(self) -> dict[str, Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as error:  # pragma: no cover - exercised in minimal installs
            raise McpProtocolError(
                "MCP Python SDK is not installed; install the backend dependencies"
            ) from error

        stack = AsyncExitStack()
        try:
            if self.config.transport == "stdio":
                if not self.config.command:
                    raise McpProtocolError(f"MCP server '{self.config.server_id}' has no command")
                params: dict[str, Any] = {
                    "command": self.config.command,
                    "args": list(self.config.args),
                    "env": {**os.environ, **self.config.env},
                }
                if self.config.cwd:
                    params["cwd"] = self.config.cwd
                try:
                    server_params = StdioServerParameters(**params)
                except TypeError:
                    # Older SDK releases do not expose cwd on the parameter
                    # model; the server still works with the inherited cwd.
                    params.pop("cwd", None)
                    server_params = StdioServerParameters(**params)
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(server_params)
                )
            elif self.config.transport == "streamable_http":
                try:
                    from mcp.client.streamable_http import streamable_http_client
                except ImportError:  # pragma: no cover - compatibility with older SDK names
                    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client
                try:
                    http_context = streamable_http_client(
                        self.config.url or "", headers=self.config.headers
                    )
                except TypeError:  # pragma: no cover - older SDK compatibility
                    http_context = streamable_http_client(self.config.url or "")
                read_stream, write_stream, _ = await stack.enter_async_context(http_context)
            else:
                raise McpProtocolError("Python SDK adapter does not support legacy SSE transport")

            self._session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            result = await self._session.initialize()
            self._stack = stack
            return self._jsonable(result)
        except Exception:
            await stack.aclose()
            raise

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return {str(key): PythonSdkMcpSession._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [PythonSdkMcpSession._jsonable(item) for item in value]
        return value

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._session is None:
            raise McpProtocolError(f"MCP server '{self.config.server_id}' is not running")
        result = await self._session.list_tools()
        return [
            {
                "name": str(getattr(tool, "name", "")),
                "description": str(getattr(tool, "description", "") or ""),
                "inputSchema": self._jsonable(
                    getattr(tool, "inputSchema", None)
                    or getattr(tool, "input_schema", None)
                    or {"type": "object", "properties": {}}
                ),
            }
            for tool in getattr(result, "tools", [])
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise McpProtocolError(f"MCP server '{self.config.server_id}' is not running")
        result = await self._session.call_tool(name, arguments=arguments)
        return self._jsonable(result)

    async def close(self) -> None:
        if self._stack is not None:
            stack, self._stack = self._stack, None
            self._session = None
            await stack.aclose()


class StdioMcpSession(_JsonRpcSession):
    def __init__(self, config: McpServerConfig):
        self.config = config
        # Use blocking Popen behind asyncio.to_thread. Windows' SelectorEventLoop
        # is required by psycopg in this project but cannot own subprocess pipes;
        # this keeps stdio MCP working in both Docker and the Windows host runner.
        self.process: subprocess.Popen[bytes] | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self.stderr_lines: list[str] = []

    async def start(self) -> None:
        if not self.config.command:
            raise McpProtocolError(f"MCP server '{self.config.server_id}' has no command")
        env = os.environ.copy()
        env.update(self.config.env)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = await asyncio.to_thread(
            subprocess.Popen,
            [self.config.command, *self.config.args],
            cwd=self.config.cwd or None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=creationflags,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        while True:
            line = await asyncio.to_thread(self.process.stderr.readline)
            if not line:
                return
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded:
                self.stderr_lines.append(decoded[-1000:])
                del self.stderr_lines[:-100:]

    async def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise McpProtocolError(f"MCP server '{self.config.server_id}' is not running")
        async with self._lock:
            self._request_id += 1
            request_id = self._request_id
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            await asyncio.to_thread(self.process.stdin.write, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await asyncio.to_thread(self.process.stdin.flush)
            while True:
                line = await asyncio.wait_for(asyncio.to_thread(self.process.stdout.readline), timeout=timeout)
                if not line:
                    detail = "; ".join(self.stderr_lines[-5:])
                    raise McpProtocolError(f"MCP server '{self.config.server_id}' closed stdout. {detail}")
                try:
                    response = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as error:
                    raise McpProtocolError(f"Invalid JSON-RPC response from '{self.config.server_id}'") from error
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response["error"]
                    raise McpProtocolError(f"{method} failed: {error}")
                return response.get("result") or {}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.process or not self.process.stdin:
            raise McpProtocolError(f"MCP server '{self.config.server_id}' is not running")
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await asyncio.to_thread(self.process.stdin.write, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await asyncio.to_thread(self.process.stdin.flush)

    async def initialize(self) -> dict[str, Any]:
        await self.start()
        result = await self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "huai-coder", "version": "0.1.0"},
            },
            timeout=self.config.connect_timeout_seconds,
        )
        await self._notify("notifications/initialized", {})
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        cursor: str | None = None
        tools: list[dict[str, Any]] = []
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params, timeout=self.config.call_timeout_seconds)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=self.config.call_timeout_seconds,
        )

    async def close(self) -> None:
        if not self.process:
            return
        process = self.process
        self.process = None
        if process.stdin:
            await asyncio.to_thread(process.stdin.close)
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait, 2), timeout=3)
        except (asyncio.TimeoutError, subprocess.TimeoutExpired):
            if process.returncode is None:
                if os.name == "nt":
                    await asyncio.to_thread(process.terminate)
                else:
                    await asyncio.to_thread(process.send_signal, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait, 2), timeout=3)
            except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                if process.returncode is None:
                    await asyncio.to_thread(process.kill)
                await asyncio.to_thread(process.wait)
        if self._stderr_task:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None


def _parse_sse_json(body: str) -> dict[str, Any]:
    data_lines = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
    for data in reversed(data_lines):
        if data and data != "[DONE]":
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                continue
    raise McpProtocolError("Streamable HTTP returned no JSON-RPC data event")


class StreamableHttpMcpSession(_JsonRpcSession):
    def __init__(self, config: McpServerConfig):
        if not config.url:
            raise McpProtocolError(f"MCP server '{config.server_id}' has no URL")
        self.config = config
        self.client = httpx.AsyncClient(timeout=config.call_timeout_seconds)
        self.session_id: str | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def _post(self, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        headers = {
            **self.config.headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = await self.client.post(self.config.url, json=payload, headers=headers, timeout=timeout)
        if response.status_code >= 400:
            raise McpProtocolError(f"HTTP MCP request failed ({response.status_code}): {response.text[:500]}")
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self.session_id = session_id
        if not response.content:
            return {}
        content_type = response.headers.get("content-type", "")
        return _parse_sse_json(response.text) if "text/event-stream" in content_type else response.json()

    async def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        async with self._lock:
            self._request_id += 1
            payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
            if params is not None:
                payload["params"] = params
            response = await self._post(payload, timeout=timeout)
            if "error" in response:
                raise McpProtocolError(f"{method} failed: {response['error']}")
            return response.get("result") or {}

    async def initialize(self) -> dict[str, Any]:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "huai-coder", "version": "0.1.0"},
            },
            timeout=self.config.connect_timeout_seconds,
        )
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        cursor: str | None = None
        tools: list[dict[str, Any]] = []
        while True:
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {}, self.config.call_timeout_seconds)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments}, self.config.call_timeout_seconds)

    async def close(self) -> None:
        if self.session_id and self.config.url:
            try:
                await self.client.delete(
                    self.config.url,
                    headers={**self.config.headers, "Mcp-Session-Id": self.session_id},
                )
            except Exception:
                pass
        await self.client.aclose()


class SseMcpSession(_JsonRpcSession):
    """Compatibility client for MCP servers exposing the legacy SSE transport."""

    def __init__(self, config: McpServerConfig):
        if not config.url:
            raise McpProtocolError(f"MCP server '{config.server_id}' has no URL")
        self.config = config
        self.client = httpx.AsyncClient(timeout=config.call_timeout_seconds)
        self.message_url: str | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stream_context: Any = None
        self._stream_response: httpx.Response | None = None
        self._reader_task: asyncio.Task[None] | None = None

    async def _consume_stream(self) -> None:
        if self._stream_response is None:
            return
        event_name = "message"
        data_lines: list[str] = []
        try:
            async for line in self._stream_response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line.strip():
                    if data_lines:
                        data = "\n".join(data_lines).strip()
                        if event_name == "endpoint":
                            self.message_url = urljoin(self.config.url or "", data)
                        elif data and data != "[DONE]":
                            try:
                                payload = json.loads(data)
                                if isinstance(payload, dict):
                                    await self._responses.put(payload)
                            except json.JSONDecodeError:
                                pass
                    event_name = "message"
                    data_lines = []
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._responses.put({"__error__": str(error)})

    async def _start_stream(self) -> None:
        self._stream_context = self.client.stream(
            "GET",
            self.config.url,
            headers={**self.config.headers, "Accept": "text/event-stream"},
            timeout=self.config.connect_timeout_seconds,
        )
        self._stream_response = await self._stream_context.__aenter__()
        if self._stream_response.status_code >= 400:
            detail = (await self._stream_response.aread()).decode("utf-8", errors="replace")[:500]
            raise McpProtocolError(f"SSE MCP connection failed ({self._stream_response.status_code}): {detail}")
        self._reader_task = asyncio.create_task(self._consume_stream())
        for _ in range(100):
            if self.message_url:
                return
            if self._reader_task.done():
                await self._reader_task
                break
            await asyncio.sleep(0.01)
        if not self.message_url:
            raise McpProtocolError("SSE MCP server did not publish a message endpoint")

    async def _post_message(self, payload: dict[str, Any]) -> None:
        response = await self.client.post(
            self.message_url or "",
            json=payload,
            headers={
                **self.config.headers,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=self.config.call_timeout_seconds,
        )
        if response.status_code >= 400:
            raise McpProtocolError(f"SSE MCP request failed ({response.status_code}): {response.text[:500]}")

    async def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        async with self._lock:
            self._request_id += 1
            payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
            if params is not None:
                payload["params"] = params
            await self._post_message(payload)
            while True:
                response = await asyncio.wait_for(self._responses.get(), timeout=timeout)
                if response.get("__error__"):
                    raise McpProtocolError(str(response["__error__"]))
                if response.get("id") != self._request_id:
                    continue
                if "error" in response:
                    raise McpProtocolError(f"{method} failed: {response['error']}")
                return response.get("result") or {}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._post_message(payload)

    async def initialize(self) -> dict[str, Any]:
        await self._start_stream()
        result = await self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "huai-coder", "version": "0.1.0"},
            },
            timeout=self.config.connect_timeout_seconds,
        )
        await self._notify("notifications/initialized", {})
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        cursor: str | None = None
        tools: list[dict[str, Any]] = []
        while True:
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {}, self.config.call_timeout_seconds)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments}, self.config.call_timeout_seconds)

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._stream_context:
            await self._stream_context.__aexit__(None, None, None)
            self._stream_context = None
            self._stream_response = None
        await self.client.aclose()


@dataclass
class _ServerState:
    config: McpServerConfig
    session: _JsonRpcSession | None = None
    status: str = "configured"
    error: str | None = None
    tools: list[McpToolDescriptor] | None = None


class McpManager:
    """Own configured MCP processes and expose namespaced tools to the Agent."""

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or ""
        self._states: dict[str, _ServerState] = {}
        self._tools: dict[str, McpToolDescriptor] = {}
        self._raw_server_configs: dict[str, dict[str, Any]] = {}
        self._config_document: dict[str, Any] = {"mcpServers": {}}
        self._config_from_env = False
        self._loaded = False
        self._lock = asyncio.Lock()

    def _resolve_config_path(self) -> Path | None:
        configured = self.config_path or os.getenv("MCP_CONFIG_PATH", "")
        if not configured:
            return None
        return Path(configured).expanduser().resolve()

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw_json = os.getenv("MCP_SERVERS_JSON", "").strip()
        path = self._resolve_config_path()
        try:
            if raw_json:
                document = json.loads(raw_json)
                self._config_from_env = True
            elif path and path.exists():
                document = json.loads(path.read_text(encoding="utf-8"))
            else:
                document = {"mcpServers": {}}
            if not isinstance(document, dict):
                raise ValueError("MCP config root must be an object")
            self._config_document = document
            servers = document.get("mcpServers") or document.get("mcp_servers") or {}
            for server_id, raw in servers.items():
                try:
                    if isinstance(raw, dict):
                        self._raw_server_configs[str(server_id)] = dict(raw)
                    normalized = dict(raw)
                    normalized.setdefault("call_timeout_seconds", getattr(get_settings(), "mcp_tool_timeout_seconds", 120))
                    config = McpServerConfig.from_mapping(str(server_id), normalized)
                    self._states[config.server_id] = _ServerState(config=config, status="disabled" if not config.enabled else "configured")
                except (TypeError, ValueError) as error:
                    self._states[str(server_id)] = _ServerState(
                        config=McpServerConfig(server_id=str(server_id), enabled=False),
                        status="failed",
                        error=str(error),
                    )
        except (OSError, json.JSONDecodeError, ValueError) as error:
            self._states["__config__"] = _ServerState(
                config=McpServerConfig(server_id="__config__", enabled=False),
                status="failed",
                error=f"Invalid MCP config: {error}",
            )

    @staticmethod
    def _safe_runtime_config(server_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", server_id):
            raise ValueError("MCP server id may contain only letters, numbers, '_' and '-'")
        if not isinstance(raw, dict):
            raise ValueError("MCP server config must be an object")
        sanitized = json.loads(json.dumps(raw, ensure_ascii=False))
        env = sanitized.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError("MCP env must be an object")
        for key, value in env.items():
            if value and not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", str(value)):
                raise ValueError(f"MCP env '{key}' must use an environment placeholder such as ${{TOKEN}}")
        sanitized["env"] = {str(key): str(value) for key, value in env.items()}
        headers = sanitized.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError("MCP headers must be an object")
        sensitive_header_names = {"authorization", "proxy-authorization"}
        for key, value in headers.items():
            name = str(key).lower()
            if (
                name in sensitive_header_names
                or "token" in name
                or "secret" in name
                or "api-key" in name
            ) and value and not re.fullmatch(r".*\$\{[A-Za-z_][A-Za-z0-9_]*\}.*", str(value)):
                raise ValueError(
                    f"MCP header '{key}' must use an environment placeholder such as ${{TOKEN}}"
                )
        sanitized["headers"] = {str(key): str(value) for key, value in headers.items()}
        return sanitized

    def _persist_config(self) -> None:
        if self._config_from_env:
            raise McpProtocolError("MCP_SERVERS_JSON is read-only; use MCP_CONFIG_PATH for API-managed configuration")
        path = self._resolve_config_path()
        if path is None:
            raise McpProtocolError("MCP_CONFIG_PATH is not configured")
        document = dict(self._config_document)
        document["mcpServers"] = self._raw_server_configs
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _status_for(self, server_id: str) -> dict[str, Any]:
        for item in self.server_statuses():
            if item["id"] == server_id:
                return item
        raise McpProtocolError(f"Unknown MCP server: {server_id}")

    def server_config_mapping(self, server_id: str) -> dict[str, Any]:
        self.load()
        raw = self._raw_server_configs.get(server_id)
        if raw is None:
            raise McpProtocolError(f"Unknown MCP server: {server_id}")
        return json.loads(json.dumps(raw, ensure_ascii=False))

    async def upsert_server(self, server_id: str, raw: dict[str, Any], persist: bool = True) -> dict[str, Any]:
        self.load()
        sanitized = self._safe_runtime_config(server_id, raw)
        normalized = dict(sanitized)
        normalized.setdefault("call_timeout_seconds", getattr(get_settings(), "mcp_tool_timeout_seconds", 120))
        config = McpServerConfig.from_mapping(server_id, normalized)
        async with self._lock:
            old = self._states.get(server_id)
            if old and old.session:
                await old.session.close()
            self._states[server_id] = _ServerState(
                config=config,
                status="disabled" if not config.enabled else "configured",
            )
            self._raw_server_configs[server_id] = sanitized
            self._tools = {name: tool for name, tool in self._tools.items() if tool.server_id != server_id}
            if persist:
                self._persist_config()
        return self._status_for(server_id)

    async def remove_server(self, server_id: str, persist: bool = True) -> None:
        self.load()
        async with self._lock:
            state = self._states.pop(server_id, None)
            if state is None:
                raise McpProtocolError(f"Unknown MCP server: {server_id}")
            if state.session:
                await state.session.close()
            self._raw_server_configs.pop(server_id, None)
            self._tools = {name: tool for name, tool in self._tools.items() if tool.server_id != server_id}
            if persist:
                self._persist_config()

    async def reconnect(self, server_id: str) -> dict[str, Any]:
        await self.disconnect(server_id)
        return await self.connect(server_id)

    async def list_server_tools(self, server_id: str) -> list[McpToolDescriptor]:
        await self.connect(server_id)
        state = self._states.get(server_id)
        return list(state.tools or []) if state else []

    def runtime_info(self, server_id: str) -> dict[str, Any]:
        self.load()
        state = self._states.get(server_id)
        if state is None:
            raise McpProtocolError(f"Unknown MCP server: {server_id}")
        session = state.session
        process = getattr(session, "process", None)
        return {
            "process_id": getattr(process, "pid", None),
            "gateway_session_id": getattr(session, "session_id", None),
            "status": state.status,
        }

    async def _ensure_state(self, state: _ServerState) -> None:
        if state.session is not None and state.status == "ready":
            return
        if state.session is not None:
            await state.session.close()
            state.session = None
        if not state.config.enabled:
            state.status = "disabled"
            return
        state.status = "starting"
        state.error = None
        try:
            def legacy_session() -> _JsonRpcSession:
                if state.config.transport == "stdio":
                    return StdioMcpSession(state.config)
                if state.config.transport == "sse":
                    return SseMcpSession(state.config)
                return StreamableHttpMcpSession(state.config)

            use_sdk = (
                state.config.client in {"auto", "sdk", "python_sdk"}
                and state.config.transport in {"stdio", "streamable_http"}
                and PythonSdkMcpSession.available()
            )
            state.session = PythonSdkMcpSession(state.config) if use_sdk else legacy_session()
            try:
                await asyncio.wait_for(
                    state.session.initialize(),
                    timeout=state.config.connect_timeout_seconds,
                )
            except Exception:
                if not use_sdk or state.config.client in {"sdk", "python_sdk"}:
                    raise
                # Auto mode is resilient to SDK/runtime differences (notably
                # Windows event-loop and subprocess combinations); fall back
                # to the built-in protocol client without changing policy.
                await state.session.close()
                state.session = legacy_session()
                await asyncio.wait_for(
                    state.session.initialize(),
                    timeout=state.config.connect_timeout_seconds,
                )
            raw_tools = await state.session.list_tools()
            state.tools = self._descriptor_list(state.config, raw_tools)
            state.status = "ready"
        except Exception as error:
            state.status = "failed"
            state.error = str(error)
            if state.session:
                await state.session.close()
            state.session = None
            state.tools = []

    @staticmethod
    def _descriptor_list(config: McpServerConfig, raw_tools: list[dict[str, Any]]) -> list[McpToolDescriptor]:
        allowed = set(config.allowed_tools)
        descriptors: list[McpToolDescriptor] = []
        for raw in raw_tools:
            original = str(raw.get("name", "")).strip()
            if not original or (allowed and "*" not in allowed and original not in allowed):
                continue
            description = str(raw.get("description", ""))
            level, reason, requires = classify_mcp_tool_risk(original, description)
            configured_approval = config.approval.get(original, "")
            if configured_approval in {"confirm", "approval", "manual", "required"}:
                requires = True
                reason = "MCP 配置要求人工确认后才能执行"
            policy = "polling" if any(marker in original.lower() for marker in ("wait", "status", "poll")) else "guarded"
            descriptors.append(McpToolDescriptor(config.server_id, original, original, description, raw.get("inputSchema") or {"type": "object", "properties": {}}, level, reason, requires, policy))
        return descriptors

    async def list_tools(self) -> list[McpToolDescriptor]:
        self.load()
        async with self._lock:
            self._tools = {}
            for state in self._states.values():
                if state.config.server_id == "__config__":
                    continue
                await self._ensure_state(state)
                for descriptor in state.tools or []:
                    self._tools[descriptor.model_name] = descriptor
            return list(self._tools.values())

    def find_tool(self, model_name: str) -> McpToolDescriptor | None:
        return self._tools.get(model_name)

    def server_statuses(self) -> list[dict[str, Any]]:
        self.load()
        result: list[dict[str, Any]] = []
        for state in self._states.values():
            tools = state.tools or []
            result.append({
                **state.config.public_dict(),
                "status": state.status,
                "error": state.error,
                "tool_count": len(tools),
                "tools": [tool.public_dict() for tool in tools],
            })
        return result

    async def connect(self, server_id: str) -> dict[str, Any]:
        self.load()
        state = self._states.get(server_id)
        if state is None:
            raise McpProtocolError(f"Unknown MCP server: {server_id}")
        async with self._lock:
            await self._ensure_state(state)
            if state.status == "failed":
                raise McpProtocolError(state.error or f"MCP server '{server_id}' failed")
            self._tools.update({tool.model_name: tool for tool in state.tools or []})
        return self._status_for(server_id)

    async def disconnect(self, server_id: str) -> dict[str, Any]:
        self.load()
        state = self._states.get(server_id)
        if state is None:
            raise McpProtocolError(f"Unknown MCP server: {server_id}")
        async with self._lock:
            if state.session:
                await state.session.close()
            state.session = None
            state.status = "stopped"
            state.tools = []
            self._tools = {name: tool for name, tool in self._tools.items() if tool.server_id != server_id}
        return self._status_for(server_id)

    async def call_tool(self, model_name: str, arguments: dict[str, Any]) -> McpCallResult:
        descriptor = self.find_tool(model_name)
        if descriptor is None:
            await self.list_tools()
            descriptor = self.find_tool(model_name)
        if descriptor is None:
            return McpCallResult(False, f"MCP tool not found: {model_name}", True, server_id=None, tool_name=model_name)
        state = self._states.get(descriptor.server_id)
        if state is None:
            return McpCallResult(False, f"MCP server not found: {descriptor.server_id}", True, server_id=descriptor.server_id, tool_name=descriptor.original_name)
        try:
            async with self._lock:
                await self._ensure_state(state)
                if state.status != "ready" or state.session is None:
                    raise McpProtocolError(state.error or f"MCP server '{descriptor.server_id}' is not ready")
                try:
                    raw = await state.session.call_tool(descriptor.original_name, arguments)
                except Exception as first_error:
                    # Retry only a read/wait tool after a transport-level failure.
                    # Never replay a tool that may create an external side effect.
                    retryable = descriptor.risk_level == "low" and any(
                        marker in str(first_error).lower()
                        for marker in ("timeout", "closed", "not running", "connection", "session")
                    )
                    if not retryable:
                        raise
                    state.status = "degraded"
                    state.error = str(first_error)
                    await self._ensure_state(state)
                    if state.status != "ready" or state.session is None:
                        raise
                    raw = await state.session.call_tool(descriptor.original_name, arguments)
            is_error = bool(raw.get("isError", False))
            return McpCallResult(not is_error, content_to_text(raw), is_error, raw.get("structuredContent"), descriptor.server_id, descriptor.original_name)
        except Exception as error:
            state.status = "degraded"
            state.error = str(error)
            return McpCallResult(False, f"MCP tool error ({descriptor.server_id}/{descriptor.original_name}): {error}", True, server_id=descriptor.server_id, tool_name=descriptor.original_name)

    async def close_all(self) -> None:
        self.load()
        async with self._lock:
            for state in self._states.values():
                if state.session:
                    await state.session.close()
                state.session = None
                if state.config.enabled:
                    state.status = "stopped"
            self._tools = {}

    async def reload_config(self) -> None:
        """Close current sessions and reload the MCP configuration from disk."""
        async with self._lock:
            for state in self._states.values():
                if state.session:
                    await state.session.close()
            self._states = {}
            self._tools = {}
            self._raw_server_configs = {}
            self._config_document = {"mcpServers": {}}
            self._config_from_env = False
            self._loaded = False
        self.load()


_MANAGER: McpManager | None = None
_MANAGER_KEY: str | None = None


def _settings_config_path() -> str:
    try:
        return str(getattr(get_settings(), "mcp_config_path", "") or "")
    except Exception:
        return ""


def get_mcp_manager() -> McpManager:
    global _MANAGER, _MANAGER_KEY
    key = _settings_config_path() or os.getenv("MCP_CONFIG_PATH", "")
    if _MANAGER is None or _MANAGER_KEY != key:
        _MANAGER = McpManager(key)
        _MANAGER_KEY = key
    return _MANAGER


def reset_mcp_manager() -> None:
    global _MANAGER, _MANAGER_KEY
    _MANAGER = None
    _MANAGER_KEY = None
