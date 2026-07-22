import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.main import AgentEvent, _execute_approved_tool, resolve_approval
from app.models import AgentRun, Approval, Session


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def one_or_none(self):
        return self.value


class _Rows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _FakeDb:
    def __init__(self, approval, session, run=None, messages=None):
        self.approval = approval
        self.session = session
        self.run = run
        self.messages = messages or []
        self.scalars_calls = 0
        self.added = []
        self.commits = 0

    async def scalars(self, _statement):
        self.scalars_calls += 1
        if self.run is not None and self.scalars_calls > 1:
            return _Rows(self.messages)
        return _ScalarResult(self.approval)

    async def get(self, model, _identifier):
        if model is Session:
            return self.session
        if model is AgentRun:
            return self.run
        return None

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _value):
        return None


def _approval(tool_name="list_dir", status="PENDING"):
    return Approval(
        id=7,
        run_id=11,
        session_id=13,
        tool_name=tool_name,
        arguments=json.dumps({"path": "."}),
        risk_level="high",
        risk_reason="test approval",
        status=status,
    )


class ApprovalFlowTests(unittest.TestCase):
    def test_approved_tool_executes_and_persists_result(self):
        with self.subTest("execute approved tool"):
            session = SimpleNamespace(id=13, project_id=3)
            approval = _approval(status="APPROVED")
            db = _FakeDb(approval, session)
            with tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary) / "projects" / "3"
                workspace.mkdir(parents=True)
                with patch("app.main.WORKSPACE_ROOT", Path(temporary)):
                    result = asyncio.run(_execute_approved_tool(approval, db))

            self.assertEqual(result, "(empty)")
            self.assertEqual(db.commits, 1)
            self.assertTrue(any(item.__class__.__name__ == "AgentEventRecord" for item in db.added))
            self.assertTrue(any(item.__class__.__name__ == "AuditLog" for item in db.added))
            self.assertTrue(any(item.__class__.__name__ == "Message" for item in db.added))

    def test_approval_is_single_use_and_rejection_is_persisted(self):
        approval = _approval()
        session = SimpleNamespace(id=13, project_id=3)
        db = _FakeDb(approval, session)

        result = asyncio.run(resolve_approval(approval.id, "REJECTED", None, db))

        self.assertEqual(result["status"], "REJECTED")
        self.assertTrue(any(item.__class__.__name__ == "Message" for item in db.added))
        with self.assertRaises(HTTPException) as error:
            asyncio.run(resolve_approval(approval.id, "APPROVED", None, db))
        self.assertEqual(error.exception.status_code, 409)

    def test_approval_resumes_agent_and_returns_follow_up_events(self):
        approval = _approval()
        session = SimpleNamespace(id=13, project_id=3)
        run = SimpleNamespace(id=11, prompt="分析项目并给出结论", status="completed")
        db = _FakeDb(approval, session, run=run)

        class _Context:
            async def build_context(self, *_args, **_kwargs):
                return SimpleNamespace(render=lambda: "context")

        async def fake_run_agent(*_args, **_kwargs):
            yield AgentEvent("run.started")
            yield AgentEvent("message.delta", "审批后的最终结论")
            yield AgentEvent("run.finished")

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "projects" / "3"
            workspace.mkdir(parents=True)
            with patch("app.main.WORKSPACE_ROOT", Path(temporary)), patch(
                "app.main.ContextManager", return_value=_Context()
            ), patch("app.main.run_agent", fake_run_agent):
                result = asyncio.run(resolve_approval(approval.id, "APPROVED", None, db))

        self.assertEqual(result["status"], "APPROVED")
        self.assertTrue(any(event["type"] == "message.delta" for event in result["continuation_events"]))
        self.assertEqual(run.status, "completed")
        self.assertTrue(any(getattr(item, "content", "") == "审批后的最终结论" for item in db.added))


if __name__ == "__main__":
    unittest.main()
