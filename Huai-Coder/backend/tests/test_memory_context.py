from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.context import ContextManager, estimate_tokens
from app.database import Base
from app.agent import _bound_react_messages
from app.main import MemoryCreateRequest, MemoryPatchRequest, create_memory, delete_memory, list_memory_audit, list_project_memory_audit, update_memory
from app.memory import GLOBAL_MEMORY_SCOPE_ID, MemoryCandidate, MemoryService, extract_candidates
from app.models import Memory, MemoryAudit, Message, Project, Session


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def test_extracts_explicit_memory_and_classifies_it():
    candidates = extract_candidates(
        "请记住：项目决定使用 PostgreSQL，用户偏好中文回复。",
        project_id=12,
        session_id=3,
        source_message_ids=[7],
    )
    assert len(candidates) == 2
    assert {candidate.memory_type for candidate in candidates} == {"decision", "preference"}
    assert all(candidate.importance >= 8 for candidate in candidates)


def test_rejects_credentials_from_memory():
    assert extract_candidates(
        "请记住：API_KEY=super-secret-value",
        project_id=12,
    ) == []


def test_react_context_compacts_complete_tool_pairs():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect project"},
    ]
    for index in range(8):
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{"id": str(index)}]},
            {"role": "tool", "tool_call_id": str(index), "content": "x" * 500},
        ])
    compacted, was_compacted = _bound_react_messages(messages, 300)
    assert was_compacted is True
    assert len(compacted) < len(messages)
    assert compacted[0]["role"] == "system"
    assert compacted[1]["role"] == "user"
    assert all(
        not (index > 1 and message["role"] == "tool" and compacted[index - 1]["role"] != "assistant")
        for index, message in enumerate(compacted)
    )


@pytest.mark.asyncio
async def test_memory_upsert_deduplicates_and_searches(db_session):
    settings = Settings(memory_max_retrieved=8)
    service = MemoryService(settings)
    first = extract_candidates("请记住：项目使用 PostgreSQL 数据库。", project_id=1)[0]
    second = extract_candidates("请记住：项目使用 PostgreSQL 数据库作为主库。", project_id=1)[0]
    await service.upsert(db_session, first)
    await service.upsert(db_session, second)
    await db_session.commit()
    rows = await service.list_project_memories(db_session, 1)
    assert len(rows) == 1
    found = await service.search(db_session, project_id=1, query="PostgreSQL 主库")
    assert len(found) == 1
    assert found[0].access_count == 1


@pytest.mark.asyncio
async def test_memory_delete_keeps_auditable_history(db_session):
    service = MemoryService()
    memory = await service.upsert(
        db_session,
        MemoryCandidate(
            scope_type="project",
            scope_id=3,
            memory_type="fact",
            content="Python backend",
            importance=5,
            confidence=0.9,
        ),
        reason="test create",
    )
    await db_session.commit()
    await service.delete(db_session, memory)
    await db_session.commit()

    audits = list(
        (
            await db_session.scalars(
                select(MemoryAudit).where(MemoryAudit.memory_id == memory.id)
            )
        ).all()
    )
    assert [audit.action for audit in audits] == ["create", "delete"]
    assert memory.status == "deleted"


@pytest.mark.asyncio
async def test_memory_audit_routes_return_single_and_project_history(db_session):
    project = Project(name="audit-route-project")
    db_session.add(project)
    await db_session.flush()
    service = MemoryService()
    memory = await service.upsert(
        db_session,
        MemoryCandidate(
            scope_type="project",
            scope_id=project.id,
            memory_type="fact",
            content="Audit route memory",
            importance=5,
            confidence=0.9,
        ),
    )
    await db_session.commit()

    single = await list_memory_audit(memory.id, db_session)
    project_history = await list_project_memory_audit(project.id, False, db_session)
    assert single[0]["memory_id"] == memory.id
    assert project_history[0]["action"] == "create"


@pytest.mark.asyncio
async def test_memory_update_and_delete_routes_keep_audit_history(db_session):
    service = MemoryService()
    memory = await service.upsert(
        db_session,
        MemoryCandidate(
            scope_type="project",
            scope_id=8,
            memory_type="fact",
            content="Original project fact",
            importance=5,
            confidence=0.9,
        ),
        reason="route test create",
    )
    await db_session.commit()

    updated = await update_memory(
        memory.id,
        MemoryPatchRequest(content="Updated project fact"),
        db_session,
    )
    assert updated["content"] == "Updated project fact"
    await delete_memory(memory.id, db_session)
    await db_session.refresh(memory)

    assert memory.status == "deleted"
    audits = list(
        (
            await db_session.scalars(
                select(MemoryAudit)
                .where(MemoryAudit.memory_id == memory.id)
                .order_by(MemoryAudit.id)
            )
        ).all()
    )
    assert [audit.action for audit in audits] == ["create", "update", "delete"]
    assert audits[1].before_content == "Original project fact"
    assert audits[1].after_content == "Updated project fact"


@pytest.mark.asyncio
async def test_memory_create_route_supports_global_user_scope(db_session):
    project = Project(name="user-memory-route-project")
    db_session.add(project)
    await db_session.flush()
    payload = await create_memory(
        MemoryCreateRequest(
            project_id=project.id,
            scope_type="user",
            memory_type="preference",
            content="用户偏好中文回复",
        ),
        db_session,
    )
    assert payload["scope_type"] == "user"
    assert payload["scope_id"] == GLOBAL_MEMORY_SCOPE_ID


@pytest.mark.asyncio
async def test_project_memory_overview_includes_global_memory(db_session):
    db_session.add_all(
        [
            Memory(
                scope_type="project",
                scope_id=7,
                memory_type="decision",
                content="项目使用 PostgreSQL",
                normalized_content="项目使用 postgresql",
                importance=8,
                confidence=0.9,
                status="active",
            ),
            Memory(
                scope_type="user",
                scope_id=GLOBAL_MEMORY_SCOPE_ID,
                memory_type="preference",
                content="用户偏好中文回复",
                normalized_content="用户偏好中文回复",
                importance=7,
                confidence=0.9,
                status="active",
            ),
            Memory(
                scope_type="project",
                scope_id=99,
                memory_type="fact",
                content="其他项目记忆",
                normalized_content="其他项目记忆",
                importance=10,
                confidence=0.9,
                status="active",
            ),
        ]
    )
    await db_session.commit()

    grouped = await MemoryService().list_project_and_global_memories(db_session, 7)

    assert [memory.content for memory in grouped["project"]] == ["项目使用 PostgreSQL"]
    assert [memory.content for memory in grouped["global"]] == ["用户偏好中文回复"]


@pytest.mark.asyncio
async def test_session_memory_overview_groups_session_project_and_user_memory(db_session):
    project = Project(name="memory-overview-project")
    db_session.add(project)
    await db_session.flush()
    session = Session(project_id=project.id, title="memory-overview-session")
    db_session.add(session)
    await db_session.flush()
    db_session.add_all(
        [
            Memory(
                scope_type="session",
                scope_id=session.id,
                memory_type="task",
                content="当前会话需要补充登录测试",
                normalized_content="当前会话需要补充登录测试",
                importance=8,
                confidence=0.9,
                status="active",
            ),
            Memory(
                scope_type="project",
                scope_id=project.id,
                memory_type="decision",
                content="项目采用 PostgreSQL",
                normalized_content="项目采用 postgresql",
                importance=7,
                confidence=0.9,
                status="active",
            ),
            Memory(
                scope_type="user",
                scope_id=GLOBAL_MEMORY_SCOPE_ID,
                memory_type="preference",
                content="用户偏好中文回复",
                normalized_content="用户偏好中文回复",
                importance=6,
                confidence=0.9,
                status="active",
            ),
            Memory(
                scope_type="session",
                scope_id=session.id + 100,
                memory_type="fact",
                content="其他会话记忆",
                normalized_content="其他会话记忆",
                importance=10,
                confidence=0.9,
                status="active",
            ),
        ]
    )
    await db_session.commit()

    grouped = await MemoryService().list_session_project_user_memories(
        db_session, session.id, project.id
    )

    assert [memory.content for memory in grouped["session"]] == [
        "当前会话需要补充登录测试"
    ]
    assert [memory.content for memory in grouped["project"]] == ["项目采用 PostgreSQL"]
    assert [memory.content for memory in grouped["user"]] == ["用户偏好中文回复"]


@pytest.mark.asyncio
async def test_context_compaction_keeps_recent_messages_and_summary(db_session):
    project = Project(name="context-test")
    db_session.add(project)
    await db_session.flush()
    session = Session(project_id=project.id, title="long session")
    db_session.add(session)
    await db_session.flush()
    messages = []
    for index in range(10):
        message = Message(
            session_id=session.id,
            role="user" if index % 2 == 0 else "assistant",
            content=f"第 {index} 轮：项目决定使用 PostgreSQL，并完成文件 file{index}.py。",
        )
        db_session.add(message)
        messages.append(message)
    await db_session.flush()
    manager = ContextManager(
        Settings(
            context_max_tokens=300,
            context_compaction_threshold=0.8,
            context_recent_turns=2,
        )
    )
    prepared = await manager.build_context(
        db_session,
        project_id=project.id,
        session_id=session.id,
        prompt="下一步继续处理数据库",
        history=messages,
    )
    await db_session.commit()
    assert prepared.compacted is True
    assert len(prepared.recent_messages) == 4
    assert "关键决策" in prepared.summary_context
    assert estimate_tokens(prepared.render()) < estimate_tokens("\n".join(m.content for m in messages))
