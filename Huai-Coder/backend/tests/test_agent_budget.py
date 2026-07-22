import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent import AgentEvent, _execute
from app.llm import LLMResponse, ParsedToolCall
from app.registry import ToolSpec
from app.security import Risk


class AgentBudgetTests(unittest.TestCase):
    def _run_agent(self, *, budget: int, responses: list[LLMResponse], tool: ToolSpec | None = None):
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

        settings = SimpleNamespace(context_max_tokens=100000, agent_token_budget=budget, tool_approval_enabled=False)
        with tempfile.TemporaryDirectory() as temporary:
            state = {
                "prompt": "test",
                "user_prompt": "test",
                "workspace": temporary,
                "response": "",
                "events": [],
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

        result, calls = self._run_agent(budget=100000, responses=responses)

        self.assertEqual(len(calls), 17)
        self.assertEqual(
            len([event for event in result["events"] if event.type == "tool.started"]),
            16,
        )
        self.assertEqual(result["response"], "done")

    def test_token_budget_stops_before_next_model_call(self):
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id="call-1",
                    name="list_dir",
                    arguments={"path": "."},
                    raw={"id": "call-1"},
                ),
            ),
            LLMResponse(content="should not be called"),
        ]

        result, calls = self._run_agent(budget=20, responses=responses)

        self.assertEqual(len(calls), 1)
        self.assertIn("Token 预算", result["response"])
        self.assertTrue(any(event.type == "tool.finished" for event in result["events"]))


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

        result, calls = self._run_agent(budget=100000, responses=responses, tool=tool)

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["response"], "done")
        self.assertTrue(any(event.type == "tool.finished" for event in result["events"]))
        self.assertFalse(any(event.type == "approval.required" for event in result["events"]))


if __name__ == "__main__":
    unittest.main()
