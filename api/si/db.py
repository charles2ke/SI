import uuid
from datetime import datetime, timezone
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from si.config import settings


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Identified:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class Project(Base, Identified):
    __tablename__ = "projects"
    name: Mapped[str] = mapped_column(String(200))


class Scoped(Identified):
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)


class APIKey(Base, Scoped):
    __tablename__ = "api_keys"
    name: Mapped[str] = mapped_column(String(200), default="API key")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(20), default="developer")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Agent(Base, Scoped):
    __tablename__ = "agents"
    name: Mapped[str] = mapped_column(String(200))
    system_prompt: Mapped[str] = mapped_column(Text, default="You are a helpful assistant.")
    model: Mapped[str] = mapped_column(String(200), default="mock")
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    collection_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_steps: Mapped[int] = mapped_column(Integer, default=8)


class Collection(Base, Scoped):
    __tablename__ = "collections"
    name: Mapped[str] = mapped_column(String(200))


class Chunk(Base, Scoped):
    __tablename__ = "chunks"
    collection_id: Mapped[str] = mapped_column(ForeignKey("collections.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(JSON().with_variant(Vector(64), "postgresql"))


class Memory(Base, Scoped):
    __tablename__ = "memories"
    session_id: Mapped[str] = mapped_column(String(200), index=True)
    role: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    long_term: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base, Scoped):
    __tablename__ = "runs"
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    pending_tool: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PromptVersion(Base, Scoped):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("project_id", "name", "version"),)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    template: Mapped[str] = mapped_column(Text)


class Span(Base, Scoped):
    __tablename__ = "spans"
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EvalRun(Base, Scoped):
    __tablename__ = "eval_runs"
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    score: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(30), default="running")


class Audit(Base, Scoped):
    __tablename__ = "audits"
    key_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(200))
    resource_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


engine = create_engine(settings.database_url, pool_pre_ping=True, **(
    {"connect_args": {"check_same_thread": False}} if settings.database_url.startswith("sqlite") else {}))
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_session():
    with SessionLocal() as session:
        yield session
