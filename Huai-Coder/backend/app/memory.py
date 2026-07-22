"""Scoped long-term memory extraction, ranking, and lifecycle management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import Memory, MemoryAudit

MEMORY_TYPES = {"fact", "preference", "decision", "constraint", "task", "summary"}
MEMORY_SCOPES = {"user", "project", "session"}
ACTIVE_STATUSES = {"active"}

_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(api[_ -]?key|access[_ -]?token|authorization|password|passwd|secret)\s*[:=]"),
    re.compile(r"(?i)-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:sk|ghp|github_pat|xoxb|xoxp)-[a-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql)://[^\s]+:[^\s@]+@"),
)

_STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "项目", "使用", "需要",
    "请", "记住", "一下", "我们", "当前", "一个", "可以", "不要", "已经",
}


@dataclass(slots=True)
class MemoryCandidate:
    scope_type: str
    scope_id: int
    memory_type: str
    content: str
    importance: int = 5
    confidence: float = 0.7
    source_session_id: int | None = None
    source_message_ids: list[int] | None = None
    source_run_id: int | None = None
    expires_at: datetime | None = None


def contains_sensitive(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS)


def normalize_content(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value[:4000]


def _terms(value: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]", value.lower()))
    return {word for word in words if word not in _STOP_WORDS}


def similarity(left: str, right: str) -> float:
    a, b = _terms(left), _terms(right)
    if not a or not b:
        return 1.0 if normalize_content(left) == normalize_content(right) else 0.0
    return len(a & b) / max(1, len(a | b))


def _memory_type(text: str) -> str:
    lower = text.lower()
    if any(marker in lower for marker in ("偏好", "喜欢", "习惯", "prefer", "prefered", "always")):
        return "preference"
    if any(marker in lower for marker in ("约束", "禁止", "不允许", "不得", "must not", "不要")):
        return "constraint"
    if any(marker in lower for marker in ("待办", "todo", "还需要", "接下来", "need to", "next")):
        return "task"
    if any(marker in lower for marker in ("决定", "选择", "采用", "使用", "we use", "decided")):
        return "decision"
    return "fact"


def extract_candidates(
    text: str,
    *,
    project_id: int,
    session_id: int | None = None,
    source_message_ids: list[int] | None = None,
    source_run_id: int | None = None,
    settings: Settings | None = None,
) -> list[MemoryCandidate]:
    """Extract safe, high-signal candidates without requiring an LLM call.

    Explicit "remember" requests are always considered. A small set of stable
    decision/preference/constraint phrases is also recognized so normal project
    conversations can create useful project memory deterministically.
    """
    if not text.strip() or contains_sensitive(text):
        return []
    settings = settings or Settings()
    candidates: list[MemoryCandidate] = []
    explicit_request = any(
        marker in text.lower() for marker in ("记住", "remember", "请保存", "保存这个")
    )
    chunks = re.split(r"[\n。！？.!?，,]+", text)
    for chunk in chunks:
        sentence = chunk.strip(" \t:-：")
        if len(sentence) < 6 or contains_sensitive(sentence):
            continue
        lower = sentence.lower()
        explicit = any(marker in lower for marker in ("记住", "remember", "请保存", "保存这个"))
        stable = any(
            marker in lower
            for marker in (
                "偏好", "喜欢", "习惯", "决定", "选择", "采用", "使用",
                "约束", "禁止", "不允许", "不得", "待办", "todo", "还需要",
                "prefer", "decided", "must not", "we use",
            )
        )
        if not explicit and not stable:
            continue
        content = sentence
        if explicit:
            content = re.sub(r"(?i)^(请)?(记住|remember|请保存|保存这个)\s*[:：,，]?\s*", "", content).strip()
        if len(content) < 6 or contains_sensitive(content):
            continue
        memory_type = _memory_type(content)
        importance = 8 if explicit_request else settings.memory_default_importance
        if memory_type == "constraint":
            importance = max(importance, 8)
        expires_at = None
        if memory_type == "task":
            expires_at = datetime.now(timezone.utc) + timedelta(days=settings.memory_retention_days)
        candidates.append(
            MemoryCandidate(
                scope_type="project",
                scope_id=project_id,
                memory_type=memory_type,
                content=content[:4000],
                importance=min(10, max(1, importance)),
                confidence=0.95 if explicit else 0.75,
                source_session_id=session_id,
                source_message_ids=source_message_ids or [],
                source_run_id=source_run_id,
                expires_at=expires_at,
            )
        )
    return candidates


class MemoryService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()

    async def list_project_memories(
        self, db: AsyncSession, project_id: int, *, include_expired: bool = False
    ) -> list[Memory]:
        query = select(Memory).where(
            Memory.scope_type == "project", Memory.scope_id == project_id
        )
        if not include_expired:
            query = query.where(Memory.status == "active")
        return list((await db.scalars(query.order_by(Memory.importance.desc(), Memory.id.desc()))).all())

    async def search(
        self,
        db: AsyncSession,
        *,
        project_id: int,
        query: str,
        session_id: int | None = None,
        limit: int | None = None,
    ) -> list[Memory]:
        now = datetime.now(timezone.utc)
        scopes = [("project", project_id)]
        if session_id is not None:
            scopes.append(("session", session_id))
        scope_filter = or_(*[Memory.scope_type == scope and Memory.scope_id == scope_id for scope, scope_id in scopes])
        rows = list(
            (
                await db.scalars(
                    select(Memory).where(
                        scope_filter,
                        Memory.status == "active",
                        or_(Memory.expires_at.is_(None), Memory.expires_at > now),
                    ).limit(200)
                )
            ).all()
        )
        query_terms = _terms(query)
        ranked: list[tuple[float, Memory]] = []
        for memory in rows:
            memory_terms = _terms(memory.content)
            overlap = len(query_terms & memory_terms) / max(1, len(query_terms))
            phrase_bonus = 0.25 if normalize_content(query) in normalize_content(memory.content) else 0.0
            scope_bonus = 0.2 if memory.scope_type == "session" else 0.1
            score = overlap * 0.6 + phrase_bonus + scope_bonus + (int(memory.importance) / 10) * 0.15
            if overlap or phrase_bonus or not query_terms:
                ranked.append((score, memory))
        ranked.sort(key=lambda item: (item[0], int(item[1].importance), item[1].id), reverse=True)
        result = [memory for _, memory in ranked[: limit or self.settings.memory_max_retrieved]]
        for memory in result:
            memory.access_count += 1
            memory.last_accessed_at = now
        return result

    async def upsert(
        self, db: AsyncSession, candidate: MemoryCandidate, *, reason: str = "automatic extraction"
    ) -> Memory | None:
        if candidate.scope_type not in MEMORY_SCOPES or candidate.memory_type not in MEMORY_TYPES:
            raise ValueError("Unsupported memory scope or type")
        if contains_sensitive(candidate.content):
            return None
        normalized = normalize_content(candidate.content)
        existing = list(
            (
                await db.scalars(
                    select(Memory).where(
                        Memory.scope_type == candidate.scope_type,
                        Memory.scope_id == candidate.scope_id,
                        Memory.memory_type == candidate.memory_type,
                        Memory.status == "active",
                    ).limit(100)
                )
            ).all()
        )
        match = max(existing, key=lambda item: similarity(item.content, candidate.content), default=None)
        if match is not None and similarity(match.content, candidate.content) >= 0.55:
            before = match.content
            if len(candidate.content) > len(match.content) or candidate.confidence > float(match.confidence):
                match.content = candidate.content
                match.normalized_content = normalized
            match.importance = max(match.importance, candidate.importance)
            match.confidence = max(float(match.confidence), candidate.confidence)
            match.source_session_id = candidate.source_session_id or match.source_session_id
            match.source_run_id = candidate.source_run_id or match.source_run_id
            match.source_message_ids = sorted(set((match.source_message_ids or []) + (candidate.source_message_ids or [])))
            if candidate.expires_at is not None:
                match.expires_at = candidate.expires_at
            db.add(MemoryAudit(memory_id=match.id, action="update", before_content=before, after_content=match.content, reason=reason, source_run_id=candidate.source_run_id))
            return match
        memory = Memory(
            scope_type=candidate.scope_type,
            scope_id=candidate.scope_id,
            memory_type=candidate.memory_type,
            content=candidate.content,
            normalized_content=normalized,
            importance=candidate.importance,
            confidence=candidate.confidence,
            status="active",
            source_session_id=candidate.source_session_id,
            source_message_ids=candidate.source_message_ids or [],
            source_run_id=candidate.source_run_id,
            expires_at=candidate.expires_at,
        )
        db.add(memory)
        await db.flush()
        db.add(MemoryAudit(memory_id=memory.id, action="create", after_content=memory.content, reason=reason, source_run_id=candidate.source_run_id))
        return memory

    async def extract_and_persist(
        self,
        db: AsyncSession,
        text: str,
        *,
        project_id: int,
        session_id: int | None = None,
        source_message_ids: list[int] | None = None,
        source_run_id: int | None = None,
    ) -> list[Memory]:
        if not self.settings.memory_enabled or not self.settings.memory_extraction_enabled:
            return []
        saved: list[Memory] = []
        for candidate in extract_candidates(
            text,
            project_id=project_id,
            session_id=session_id,
            source_message_ids=source_message_ids,
            source_run_id=source_run_id,
            settings=self.settings,
        ):
            memory = await self.upsert(db, candidate)
            if memory is not None:
                saved.append(memory)
        return saved

    async def delete(self, db: AsyncSession, memory: Memory) -> None:
        memory.status = "deleted"
        db.add(MemoryAudit(memory_id=memory.id, action="delete", before_content=memory.content, reason="user requested deletion"))
