from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from typing import Any


def _expand_env(value: str) -> str:
    """Expand ${NAME} without exposing or persisting the resolved secret."""
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    return pattern.sub(lambda match: os.getenv(match.group(1), ""), value)


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


@dataclass(frozen=True)
class McpServerConfig:
    server_id: str
    transport: str = "stdio"
    client: str = "legacy"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    enabled: bool = True
    scope: str = "user"
    allowed_tools: tuple[str, ...] = ()
    connect_timeout_seconds: float = 30.0
    call_timeout_seconds: float = 120.0
    approval: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, server_id: str, raw: dict[str, Any]) -> "McpServerConfig":
        if not isinstance(raw, dict):
            raise ValueError(f"MCP server '{server_id}' must be an object")
        url = raw.get("url") or raw.get("endpoint")
        transport = str(raw.get("transport") or ("streamable_http" if url else "stdio")).lower()
        if transport in {"http", "streamable-http", "streamablehttp"}:
            transport = "streamable_http"
        if transport in {"sse_http", "http_sse"}:
            transport = "sse"
        if transport not in {"stdio", "streamable_http", "sse"}:
            raise ValueError(f"MCP server '{server_id}' has unsupported transport: {transport}")
        command = raw.get("command")
        if command is not None:
            command = _expand_env(str(command))
        args = tuple(_expand_env(str(item)) for item in _as_string_list(raw.get("args")))
        env = {
            str(key): _expand_env(str(value))
            for key, value in (raw.get("env") or {}).items()
            if value is not None
        }
        headers = {
            str(key): _expand_env(str(value))
            for key, value in (raw.get("headers") or {}).items()
            if value is not None
        }
        approval = {
            str(key): str(value).lower()
            for key, value in (raw.get("approval") or {}).items()
        }
        return cls(
            server_id=server_id,
            transport=transport,
            client=str(raw.get("client", "legacy")).lower(),
            command=command,
            args=args,
            env=env,
            headers=headers,
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
            url=str(url) if url else None,
            enabled=bool(raw.get("enabled", True)),
            scope=str(raw.get("scope", "user")),
            allowed_tools=tuple(_as_string_list(raw.get("allowedTools") or raw.get("allowed_tools"))),
            connect_timeout_seconds=(
                float(raw["connectTimeoutMs"]) / 1000
                if "connectTimeoutMs" in raw
                else float(raw.get("connect_timeout_seconds", 30))
            ),
            call_timeout_seconds=(
                float(raw["callTimeoutMs"]) / 1000
                if "callTimeoutMs" in raw
                else float(raw.get("call_timeout_seconds", 120))
            ),
            approval=approval,
        )

    def public_dict(self) -> dict[str, Any]:
        """Return safe configuration metadata; never return environment values."""
        return {
            "id": self.server_id,
            "transport": self.transport,
            "client": self.client,
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "url": self.url,
            "enabled": self.enabled,
            "scope": self.scope,
            "allowed_tools": list(self.allowed_tools),
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "call_timeout_seconds": self.call_timeout_seconds,
            "has_env": bool(self.env),
            "header_names": sorted(self.headers),
        }


def namespaced_tool_name(server_id: str, tool_name: str) -> str:
    safe_server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)
    safe_tool = re.sub(r"[^A-Za-z0-9_.-]", "_", tool_name)
    return f"mcp__{safe_server}__{safe_tool}"


def classify_mcp_tool_risk(tool_name: str, description: str = "") -> tuple[str, str, bool]:
    """Return (level, reason, requires_approval) for a discovered MCP tool."""
    normalized = f"{tool_name} {description}".lower()
    external_markers = (
        "delete", "remove", "destroy", "merge", "publish", "send", "submit",
        "create_pull_request", "pull_request_create", "issue_write", "release",
        "approve", "payment", "pay", "push", "write", "update", "create_",
    )
    if any(marker in normalized for marker in external_markers):
        return "high", "可能改变远程服务或产生外部副作用", True
    if any(marker in normalized for marker in ("read", "get", "list", "search", "find", "snapshot", "tabs", "wait", "status", "view")):
        return "low", "读取或等待操作", False
    return "medium", "外部工具操作，需要根据目标判断风险", False


@dataclass(frozen=True)
class McpToolDescriptor:
    server_id: str
    name: str
    original_name: str
    description: str
    input_schema: dict[str, Any]
    risk_level: str
    risk_reason: str
    requires_approval: bool
    repeat_policy: str = "guarded"

    @property
    def model_name(self) -> str:
        return namespaced_tool_name(self.server_id, self.original_name)

    def model_schema(self) -> dict[str, Any]:
        schema = self.input_schema if isinstance(self.input_schema, dict) else {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": self.model_name,
                "description": f"[MCP:{self.server_id}] {self.description}"[:4000],
                "parameters": schema,
            },
        }

    def public_dict(self, enabled: bool = True) -> dict[str, Any]:
        return {
            "name": self.model_name,
            "server": self.server_id,
            "original_name": self.original_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk_level": self.risk_level,
            "risk_reason": self.risk_reason,
            "requires_approval": self.requires_approval,
            "enabled": enabled,
        }


@dataclass(frozen=True)
class McpCallResult:
    ok: bool
    text: str
    is_error: bool = False
    structured_content: Any = None
    server_id: str | None = None
    tool_name: str | None = None

    def bounded_text(self, limit: int = 12000) -> str:
        return self.text[:limit]


def content_to_text(result: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            chunks.append(str(item))
            continue
        kind = item.get("type")
        if kind == "text":
            chunks.append(str(item.get("text", "")))
        elif kind == "image":
            chunks.append("[MCP image content omitted from text context]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            chunks.append(str(resource.get("text") or resource.get("uri") or "[resource]"))
        else:
            chunks.append(json.dumps(item, ensure_ascii=False, default=str))
    structured = result.get("structuredContent")
    if structured is not None:
        chunks.append(json.dumps(structured, ensure_ascii=False, default=str))
    return "\n".join(chunk for chunk in chunks if chunk).strip() or "(empty MCP result)"
