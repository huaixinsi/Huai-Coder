import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent import _bound_react_messages, _execute, _run_react_stream, resolve_client_tool_result
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

        settings = SimpleNamespace(
            context_max_tokens=100000,
            tool_approval_enabled=False,
            react_max_turns=128,
            context_compaction_threshold=0.75,
            client_tool_timeout_seconds=1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = {
                "prompt": "test",
                "user_prompt": "test",
                "workspace": temporary,
                "response": "",
                "events": [],
                "local_workspace": local_workspace,
                "run_id": None,
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
        self.assertEqual(len([event for event in result["events"] if event.type == "tool.started"]), 5)
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

    def test_multiple_tool_calls_are_observed_as_one_assistant_turn(self):
        responses = [
            LLMResponse(
                content="",
                tool_calls=[
                    ParsedToolCall("call-a", "list_dir", {"path": "a"}, {"id": "call-a"}),
                    ParsedToolCall("call-b", "list_dir", {"path": "b"}, {"id": "call-b"}),
                ],
            ),
            LLMResponse(content="done"),
        ]

        result, calls = self._run_agent(responses=responses)

        self.assertEqual(len(calls), 2)
        self.assertEqual(len([event for event in result["events"] if event.type == "tool.started"]), 2)
        self.assertEqual(len([event for event in result["events"] if event.type == "tool.finished"]), 2)
        assistant = next(message for message in calls[1] if message.get("role") == "assistant")
        self.assertEqual([call["id"] for call in assistant["tool_calls"]], ["call-a", "call-b"])
        self.assertEqual([message["tool_call_id"] for message in calls[1] if message.get("role") == "tool"], ["call-a", "call-b"])

    def test_local_client_tool_waits_for_result_before_continuing(self):
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

        async def consume(state):
            events = []
            async for event in _run_react_stream(state):
                events.append(event)
                if event.type == "tool.request":
                    for call in json.loads(event.content)["calls"]:
                        self.assertTrue(resolve_client_tool_result(call["invocation_id"], {"ok": True, "result": "Wrote src/app.py"}, 99))
            return events

        settings = SimpleNamespace(
            context_max_tokens=100000,
            tool_approval_enabled=False,
            react_max_turns=128,
            context_compaction_threshold=0.75,
            client_tool_timeout_seconds=1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = {"prompt": "test", "user_prompt": "test", "workspace": temporary, "response": "", "events": [], "local_workspace": True, "run_id": 99}
            with patch("app.agent.get_settings", return_value=settings), patch(
                "app.agent._workspace_context", return_value="context"
            ), patch("app.agent._build_system_prompt", return_value="system"), patch(
                "app.agent.get_tool", return_value=tool
            ), patch("app.agent.complete_with_tools", side_effect=responses):
                events = asyncio.run(consume(state))

        self.assertEqual(writes, [])
        self.assertTrue(any(event.type == "tool.request" for event in events))
        self.assertTrue(any(event.type == "tool.finished" for event in events))
        self.assertEqual(events[-1].type, "run.finished")

    def test_local_execute_command_is_delegated_to_runner(self):
        tool = ToolSpec(
            "execute_command",
            "run local command",
            Risk("medium", "local command", True),
            lambda guard, command: "server execution must not run",
        )
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id="local-command",
                    name="execute_command",
                    arguments={"command": "python --version", "auto_prepare": True},
                    raw={"id": "local-command"},
                ),
            ),
            LLMResponse(content="done"),
        ]

        async def consume(state):
            events = []
            async for event in _run_react_stream(state):
                events.append(event)
                if event.type == "tool.request":
                    calls = json.loads(event.content)["calls"]
                    self.assertEqual(calls[0]["tool"], "execute_command")
                    resolve_client_tool_result(calls[0]["invocation_id"], {"ok": True, "result": "Python 3.12"})
            return events

        settings = SimpleNamespace(context_max_tokens=100000, tool_approval_enabled=False, react_max_turns=128, context_compaction_threshold=0.75, client_tool_timeout_seconds=1)
        with tempfile.TemporaryDirectory() as temporary:
            state = {"prompt": "test", "user_prompt": "test", "workspace": temporary, "response": "", "events": [], "local_workspace": True}
            with patch("app.agent.get_settings", return_value=settings), patch("app.agent._workspace_context", return_value="context"), patch("app.agent._build_system_prompt", return_value="system"), patch("app.agent.get_tool", return_value=tool), patch("app.agent.complete_with_tools", side_effect=responses):
                events = asyncio.run(consume(state))

        self.assertEqual(events[-1].type, "run.finished")
        self.assertFalse(any("server execution" in event.content for event in events))

    def test_compaction_keeps_tool_call_result_groups_intact(self):
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "goal"}]
        for index in range(8):
            call_id = f"call-{index}"
            messages.extend([
                {"role": "assistant", "content": None, "tool_calls": [{"id": call_id}]},
                {"role": "tool", "tool_call_id": call_id, "content": "result"},
            ])

        compacted, was_compacted = _bound_react_messages(messages, 80, 0.5)

        self.assertTrue(was_compacted)
        for index, message in enumerate(compacted):
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue
            call_id = message["tool_calls"][0]["id"]
            self.assertTrue(any(next_message.get("role") == "tool" and next_message.get("tool_call_id") == call_id for next_message in compacted[index + 1:]))

    def test_react_loop_emits_limited_terminal_state(self):
        responses = [
            LLMResponse(
                content="",
                tool_call=ParsedToolCall(
                    id="limited-call",
                    name="list_dir",
                    arguments={"path": "."},
                    raw={"id": "limited-call"},
                ),
            )
        ]
        calls = []

        async def fake_complete(messages, tools, timeout=120):
            calls.append(messages)
            return responses[0]

        settings = SimpleNamespace(context_max_tokens=100000, tool_approval_enabled=False, react_max_turns=1, context_compaction_threshold=0.75, client_tool_timeout_seconds=1)
        with tempfile.TemporaryDirectory() as temporary:
            state = {"prompt": "test", "user_prompt": "test", "workspace": temporary, "response": "", "events": [], "local_workspace": False}
            with patch("app.agent.get_settings", return_value=settings), patch("app.agent._workspace_context", return_value="context"), patch("app.agent._build_system_prompt", return_value="system"), patch("app.agent.get_tool", return_value=ToolSpec("list_dir", "test", Risk("low", "test", False), lambda guard, path: "ok")), patch("app.agent.complete_with_tools", side_effect=fake_complete):
                result = asyncio.run(_execute(state))

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["events"][-1].type, "run.limited")

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

        settings = SimpleNamespace(context_max_tokens=100000, tool_approval_enabled=False, react_max_turns=128, context_compaction_threshold=0.75, client_tool_timeout_seconds=1)
        async def consume(state):
            events = []
            async for event in _run_react_stream(state):
                events.append(event)
                if event.type == "tool.request":
                    for call in json.loads(event.content)["calls"]:
                        resolve_client_tool_result(call["invocation_id"], {"ok": True, "result": "Wrote src/app.py"})
            return events

        with tempfile.TemporaryDirectory() as temporary:
            state = {"prompt": "test", "user_prompt": "test", "workspace": temporary, "response": "", "events": [], "local_workspace": True}
            with patch("app.agent.get_settings", return_value=settings), patch(
                "app.agent._workspace_context", return_value="context"
            ), patch("app.agent._build_system_prompt", return_value="system"), patch(
                "app.agent.get_tool", return_value=tool
            ), patch("app.agent.complete_with_tools", side_effect=responses):
                events = asyncio.run(consume(state))

        self.assertEqual(writes, [])
        self.assertEqual(len([event for event in events if event.type == "tool.request"]), 1)
        self.assertEqual(events[-1].type, "run.finished")

if __name__ == "__main__":
    unittest.main()
