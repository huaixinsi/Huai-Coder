"""Safe, read-only MCP acceptance probe for a running deployment.

Examples:
    python -m app.mcp_smoke --config /workspace/backend/mcp.json --github
    python -m app.mcp_smoke --config /workspace/backend/mcp.json --browser

The probe only invokes explicitly selected low-risk tools. It never calls a
tool that requires approval, and it never creates or modifies remote data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .mcp import McpManager, McpToolDescriptor, namespaced_tool_name


def _pick_tool(
    tools: list[McpToolDescriptor], server_id: str, names: tuple[str, ...]
) -> McpToolDescriptor | None:
    for name in names:
        for tool in tools:
            if (
                tool.server_id == server_id
                and tool.original_name == name
                and tool.risk_level == "low"
                and not tool.requires_approval
            ):
                return tool
    return None


def _github_arguments(tool_name: str, repository: str, path: str) -> dict[str, Any]:
    if tool_name in {"get_me", "search_user"}:
        return {}
    if tool_name in {"search_repositories", "search_code"}:
        return {"query": repository}
    owner, _, repo = repository.partition("/")
    return {"owner": owner, "repo": repo, "path": path}


def _browser_arguments(tool_name: str) -> dict[str, Any]:
    return {"action": "list"} if tool_name == "browser_tabs" else {}


async def _run(args: argparse.Namespace) -> int:
    manager = McpManager(args.config)
    report: dict[str, Any] = {"servers": [], "tools": [], "checks": [], "errors": []}
    try:
        tools = await manager.list_tools()
        report["servers"] = manager.server_statuses()
        report["tools"] = [
            {
                "name": tool.model_name,
                "server": tool.server_id,
                "risk": tool.risk_level,
                "requires_approval": tool.requires_approval,
            }
            for tool in tools
        ]

        if args.github:
            tool = _pick_tool(
                tools,
                "github",
                ("get_me", "search_repositories", "get_file_contents"),
            )
            if tool is None:
                report["errors"].append(
                    "github: no low-risk approved read tool was discovered"
                )
            else:
                result = await manager.call_tool(
                    namespaced_tool_name("github", tool.original_name),
                    _github_arguments(tool.original_name, args.repository, args.path),
                )
                report["checks"].append(
                    {
                        "name": "github.read",
                        "tool": tool.original_name,
                        "ok": result.ok,
                        "text": result.bounded_text(2000),
                    }
                )
                if not result.ok:
                    report["errors"].append(f"github: {result.text[:500]}")

        if args.browser:
            tool = _pick_tool(tools, "playwright", ("browser_tabs", "browser_snapshot"))
            if tool is None:
                tool = _pick_tool(
                    tools, "playwright-host", ("browser_tabs", "browser_snapshot")
                )
            if tool is None:
                report["errors"].append(
                    "browser: no low-risk browser_tabs/browser_snapshot tool was discovered"
                )
            else:
                result = await manager.call_tool(
                    tool.model_name, _browser_arguments(tool.original_name)
                )
                report["checks"].append(
                    {
                        "name": "browser.read",
                        "tool": tool.model_name,
                        "ok": result.ok,
                        "text": result.bounded_text(2000),
                    }
                )
                if not result.ok:
                    report["errors"].append(f"browser: {result.text[:500]}")
    except Exception as error:  # pragma: no cover - exercised by deployment failures
        report["errors"].append(str(error))
    finally:
        await manager.close_all()

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if report["errors"] else 0


def main() -> int:
    # Windows PowerShell may expose a GBK stdout even though MCP content is
    # Unicode. Keep the probe's JSON output machine-readable and lossless.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Read-only MCP deployment smoke test")
    parser.add_argument("--config", default=None, help="MCP config path; defaults to MCP_CONFIG_PATH")
    parser.add_argument("--github", action="store_true", help="run a low-risk GitHub read probe")
    parser.add_argument("--browser", action="store_true", help="run a low-risk browser tabs/snapshot probe")
    parser.add_argument("--repository", default="huaixinsi/Huai-Coder", help="owner/repo for GitHub read tools")
    parser.add_argument("--path", default="README.md", help="repository path for get_file_contents")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
