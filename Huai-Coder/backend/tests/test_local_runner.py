import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

from app.local_runner import DependencyPlan, LocalRunner
from app.runner_server import create_runner_server


class LocalRunnerTests(unittest.TestCase):
    def test_environment_keeps_runner_python_on_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = LocalRunner(temporary)
            path_entries = runner._environment()["PATH"].split(os.pathsep)

            self.assertIn(str(Path(sys.executable).resolve().parent), path_entries)

    def test_detects_python_and_node_projects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("colorama==0.4.6\n", encoding="utf-8")
            (root / "package.json").write_text("{\"scripts\": {\"test\": \"node -e \\\"console.log('ok')\\\"\"}}", encoding="utf-8")
            runner = LocalRunner(root)

            with patch("app.local_runner.shutil.which", return_value="tool"):
                plans = runner.detect_dependencies()

            self.assertEqual({plan.ecosystem for plan in plans}, {"python", "node"})
            self.assertTrue(any(plan.manifest == "requirements.txt" for plan in plans))
            self.assertTrue(any(plan.manifest == "package.json" for plan in plans))

    def test_blocks_destructive_commands_before_process_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = LocalRunner(temporary)
            with patch.object(runner, "_run_process") as run_process:
                result = runner.run("rm -rf .")

            self.assertFalse(result["ok"])
            self.assertEqual(result["error_type"], "unsafe_command")
            run_process.assert_not_called()

    def test_install_plan_retries_fallback_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = LocalRunner(temporary, max_retries=3)
            results = [
                {"ok": False, "exit_code": 1, "output": "peer dependency conflict", "duration_ms": 1},
                {"ok": True, "exit_code": 0, "output": "installed", "duration_ms": 1},
            ]
            plan = DependencyPlan("node", "package.json", "npm ci", ("npm install",))
            with patch.object(runner, "_run_process", side_effect=results) as run_process:
                result = runner._install_plan(plan)

            self.assertTrue(result["ok"])
            self.assertEqual([call.args[0] for call in run_process.call_args_list], ["npm ci", "npm install"])
            self.assertEqual(len(result["attempts"]), 2)

    def test_manifest_changes_reprepare_existing_node_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package.json"
            package.write_text('{"dependencies":{"is-number":"7.0.0"}}', encoding="utf-8")
            (root / "node_modules").mkdir()
            runner = LocalRunner(root)
            install_result = {"ok": True, "ecosystem": "node", "manifest": "package.json", "attempts": []}

            with patch("app.local_runner.shutil.which", return_value="npm"), patch.object(
                runner, "_install_plan", return_value=install_result
            ) as install_plan:
                first = runner.prepare_dependencies()
                package.write_text('{"dependencies":{"is-number":"7.1.0"}}', encoding="utf-8")
                second = runner.prepare_dependencies()

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(install_plan.call_count, 2)

    def test_command_result_contains_dependency_and_command_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("dummy\n", encoding="utf-8")
            runner = LocalRunner(root)
            with patch.object(runner, "prepare_dependencies", return_value={"ok": True, "steps": [{"ecosystem": "python", "ok": True}]}), patch.object(
                runner,
                "_run_process",
                return_value={"ok": True, "exit_code": 0, "output": "hello", "duration_ms": 2},
            ):
                result = runner.run("python -c \"print('hello')\"")

            self.assertTrue(result["ok"])
            self.assertEqual(result["dependency_steps"][0]["ecosystem"], "python")
            self.assertEqual(result["attempts"][0]["exit_code"], 0)
            json.dumps(result)

    def test_runner_http_api_executes_in_bound_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = LocalRunner(temporary)
            server = create_runner_server(runner, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                health = urlopen(f"{base_url}/health")
                request = Request(
                    f"{base_url}/v1/execute",
                    data=json.dumps({"command": "python -c 'print(\"runner-ok\")'", "auto_prepare": False}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                response = urlopen(request)
                response_body = json.loads(response.read())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            self.assertEqual(health.status, 200)
            self.assertEqual(response.status, 200)
            self.assertTrue(response_body["ok"])
            self.assertIn("runner-ok", response_body["result"])


if __name__ == "__main__":
    unittest.main()
