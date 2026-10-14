"""Explicitly project/session-scoped conversation and long-term memory."""

from typing import Any

from sqlalchemy import or_, select

from si.db import Memory


class MemoryStore:
    def __init__(self, session: Any):
        self.session = session

    def load(self, project_id: str, session_id: str, limit: int = 20) -> list[Any]:
        if not session_id or not 1 <= limit <= 100:
            raise ValueError("Valid session and memory limit required")
        rows = self.session.scalars(
            select(Memory)
            .where(
                Memory.project_id == project_id,
                or_(Memory.session_id == session_id, Memory.long_term.is_(True)),
            )
            .order_by(Memory.created_at.desc(), Memory.id.desc())
            .limit(limit)
        ).all()
        return list(reversed(rows))

    def remember(
        self, project_id: str, session_id: str, content: str,
        role: str = "user", long_term: bool = False,
    ) -> Any:
        if not session_id or len(session_id) > 200 or role not in {"user", "assistant"}:
            raise ValueError("Valid session and memory role required")
        if not content or len(content) > 100_000:
            raise ValueError("Memory must contain 1 to 100000 characters")
        memory = Memory(
            project_id=project_id, session_id=session_id, role=role, content=content,
            long_term=long_term,
        )
        self.session.add(memory)
        return memory
