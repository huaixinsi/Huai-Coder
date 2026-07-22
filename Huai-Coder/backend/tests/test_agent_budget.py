import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent import _execute
from app.llm import LLMResponse, ParsedToolCall
from app.registry import ToolSpec
from app.security import Risk


class AgentBudgetTests(unittest.TestCase):
    def _run_agent(self, *, responses: list[LLMResponse], tool: ToolSpec | None = None, local_workspace: bool = False):
        calls = []
        fake_tool = tool or ToolSpec(
            "list_dir",
            "test tool",
            Risk("low", "test", False),
            lambda guard, path: "ok",
        )

        async def fake_complete(messages, tools, timeout=120):
            calls.append(messages)
            return responses[len(calls) - 1]

        settings = SimpleNamespace(context_max_tokens=100000, tool_approval_enabled=False)
        with tempfile.TemporaryDirectory() as temporary:
            state = {
                "prompt": "test",
                "user_prompt": "test",
                "workspace": temporary,
                "response": "",
                "events": [],
                "local_workspace": local_workspace,
            }
            with patch("app.agent.get_settings", return_value=settings), patch(
                "app.agent._workspace_context", return_value="context"
            ), patch("app.agent._build_system_prompt", return_value="system"), patch(
                "app.agent.get_tool", return_value=fake_tool
            ), patch("app.agent.complete_with_tools", side_effect=fake_complete):
                result = asyncio.run(_execute(state))
        return result, calls

    def test_more_than_fifteen_calls_are_allowed_within_budget(self):
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id=f"call-{index}",
                    name="list_dir",
                    arguments={"path": f"path-{index}"},
                    raw={"id": f"call-{index}"},
                ),
            )
            for index in range(16)
        ] + [LLMResponse(content="done")]

        result, calls = self._run_agent(responses=responses)

        self.assertEqual(len(calls), 17)
        self.assertEqual(
            len([event for event in result["events"] if event.type == "tool.started"]),
            16,
        )
        self.assertEqual(result["response"], "done")

    def test_agent_has_no_cumulative_token_cutoff(self):
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id=f"call-{index}",
                    name="list_dir",
                    arguments={"path": f"path-{index}"},
                    raw={"id": f"call-{index}"},
                ),
            )
            for index in range(40)
        ] + [LLMResponse(content="done")]

        result, calls = self._run_agent(responses=responses)

        self.assertEqual(len(calls), 41)
        self.assertEqual(result["response"], "done")

    def test_repeated_call_replans_rejects_then_circuits(self):
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id=f"repeat-{index}",
                    name="list_dir",
                    arguments={"path": " ./ "},
                    raw={"id": f"repeat-{index}"},
                ),
            )
            for index in range(5)
        ]

        result, calls = self._run_agent(responses=responses)

        self.assertEqual(len(calls), 5)
        self.assertEqual(len([event for event in result["events"] if event.type == "tool.started"]), 3)
        self.assertEqual(len([event for event in result["events"] if event.type == "tool.blocked"]), 2)
        self.assertTrue(any(event.type == "tool.repeat_warning" for event in result["events"]))
        self.assertTrue(any(event.type == "tool.repeat_rejected" for event in result["events"]))
        self.assertTrue(any(event.type == "tool.circuit_broken" for event in result["events"]))
        self.assertIn("TOOL_CIRCUIT_BROKEN", result["response"])

    def test_stateful_progress_is_not_treated_as_stale(self):
        counter = {"value": 0}

        def stateful_tool(guard, path, content):
            counter["value"] += 1
            target = guard.resolve(path)
            target.write_text(str(counter["value"]), encoding="utf-8")
            return "updated"

        tool = ToolSpec(
            "write_file",
            "test stateful tool",
            Risk("high", "test write", True),
            stateful_tool,
            "stateful",
        )
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id=f"state-{index}",
                    name="write_file",
                    arguments={"path": "state.txt", "content": "same"},
                    raw={"id": f"state-{index}"},
                ),
            )
            for index in range(5)
        ] + [LLMResponse(content="done")]

        result, calls = self._run_agent(responses=responses, tool=tool)

        self.assertEqual(len(calls), 6)
        self.assertEqual(result["response"], "done")
        self.assertFalse(any(event.type == "tool.repeat_warning" for event in result["events"]))

    def test_polling_tool_is_exempt(self):
        tool = ToolSpec(
            "poll_status",
            "test polling tool",
            Risk("low", "test polling", False),
            lambda guard: "waiting",
            "polling",
        )
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id=f"poll-{index}",
                    name="poll_status",
                    arguments={},
                    raw={"id": f"poll-{index}"},
                ),
            )
            for index in range(5)
        ] + [LLMResponse(content="done")]

        result, calls = self._run_agent(responses=responses, tool=tool)

        self.assertEqual(len(calls), 6)
        self.assertEqual(result["response"], "done")
        self.assertFalse(any(event.type == "tool.repeat_warning" for event in result["events"]))

    def test_high_risk_tool_runs_without_approval_when_disabled(self):
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id="write-call",
                    name="write_file",
                    arguments={"path": "note.txt", "content": "updated"},
                    raw={"id": "write-call"},
                ),
            ),
            LLMResponse(content="done"),
        ]
        tool = ToolSpec(
            "write_file",
            "test write tool",
            Risk("high", "test write", True),
            lambda guard, path, content: "written",
        )

        result, calls = self._run_agent(responses=responses, tool=tool)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["response"], "done")
        self.assertTrue(any(event.type == "tool.finished" for event in result["events"]))
        self.assertFalse(any(event.type == "approval.required" for event in result["events"]))

    def test_local_workspace_emits_file_write_without_calling_container_handler(self):
        writes = []

        def container_write(guard, path, content):
            writes.append((path, content))
            return "container write should not run"

        tool = ToolSpec(
            "write_file",
            "test write tool",
            Risk("high", "test write", True),
            container_write,
            "stateful",
        )
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id="local-write",
                    name="write_file",
                    arguments={"path": "src/app.py", "content": "print('ok')\n"},
                    raw={"id": "local-write"},
                ),
            ),
            LLMResponse(content="done"),
        ]

        result, calls = self._run_agent(
            responses=responses, tool=tool, local_workspace=True
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(writes, [])
        write_events = [event for event in result["events"] if event.type == "file.write"]
        self.assertEqual(len(write_events), 1)
        self.assertIn('"path": "src/app.py"', write_events[0].content)
        self.assertEqual(result["response"], "done")

if __name__ == "__main__":
    unittest.main()
