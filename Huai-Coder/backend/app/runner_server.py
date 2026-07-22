"""Self-contained HTTP server for the user-side LocalRunner.

It intentionally uses only the Python standard library so users do not need
to install Huai-Coder's FastAPI backend just to run local commands.

Start it with:
    python -m app.runner_server --workspace C:\\path\\to\\project
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .local_runner import LocalRunner, runner_metadata


class _RunnerHandler(BaseHTTPRequestHandler):
    runner: LocalRunner

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if status != 204:
            self.send_header("Content-Length", str(len(encoded)))
        origin = self.headers.get("Origin")
        if origin in {
            "http://localhost",
            "http://localhost:5173",
            "http://127.0.0.1",
            "http://127.0.0.1:5173",
        }:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        if status != 204:
            self.wfile.write(encoded)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(204, {})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"status": "ok", **runner_metadata(self.runner)})
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/v1/execute", "/v1/prepare"}:
            self._send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/v1/prepare":
                self._send_json(200, self.runner.prepare_dependencies())
                return
            command = body.get("command")
            if not isinstance(command, str) or not command.strip():
                self._send_json(400, {"ok": False, "error_type": "invalid_command", "result": "command is required"})
                return
            timeout_seconds = max(5, min(int(body.get("timeout_seconds", 120)), 900))
            result = self.runner.run(
                command,
                auto_prepare=body.get("auto_prepare", True) is not False,
                timeout_seconds=timeout_seconds,
            )
            self._send_json(200, result)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._send_json(400, {"ok": False, "error_type": "invalid_request", "result": str(error)})
        except Exception as error:  # keep the client-facing protocol alive
            self._send_json(500, {"ok": False, "error_type": "runner_error", "result": str(error)})


def create_runner_server(
    runner: LocalRunner, host: str = "127.0.0.1", port: int = 8765
) -> ThreadingHTTPServer:
    handler = type("RunnerHandler", (_RunnerHandler,), {"runner": runner})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Huai-Coder commands in a local project")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_runner_server(LocalRunner(args.workspace), args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
