"""Initial SI schema."""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "api_keys",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_project_id", "api_keys", ["project_id"])
    op.create_table(
        "agents",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=False),
        sa.Column("collection_ids", sa.JSON(), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agents_project_id", "agents", ["project_id"])
    op.create_table(
        "collections",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collections_project_id", "collections", ["project_id"])
    op.create_table(
        "chunks",
        sa.Column("collection_id", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.JSON().with_variant(Vector(64), "postgresql"), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chunks_collection_id", "chunks", ["collection_id"])
    op.create_index("ix_chunks_project_id", "chunks", ["project_id"])
    op.create_table(
        "memories",
        sa.Column("session_id", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("long_term", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memories_project_id", "memories", ["project_id"])
    op.create_index("ix_memories_session_id", "memories", ["session_id"])
    op.create_table(
        "runs",
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=200), nullable=True),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("pending_tool", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_agent_id", "runs", ["agent_id"])
    op.create_index("ix_runs_project_id", "runs", ["project_id"])
    op.create_table(
        "prompt_versions",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", "version"),
    )
    op.create_index("ix_prompt_versions_project_id", "prompt_versions", ["project_id"])
    op.create_table(
        "spans",
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("outputs", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_spans_project_id", "spans", ["project_id"])
    op.create_index("ix_spans_run_id", "spans", ["run_id"])
    op.create_table(
        "eval_runs",
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_eval_runs_project_id", "eval_runs", ["project_id"])
    op.create_table(
        "audits",
        sa.Column("key_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("resource_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audits_project_id", "audits", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_audits_project_id", table_name="audits")
    op.drop_table("audits")
    op.drop_index("ix_eval_runs_project_id", table_name="eval_runs")
    op.drop_table("eval_runs")
    op.drop_index("ix_spans_run_id", table_name="spans")
    op.drop_index("ix_spans_project_id", table_name="spans")
    op.drop_table("spans")
    op.drop_index("ix_prompt_versions_project_id", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index("ix_runs_project_id", table_name="runs")
    op.drop_index("ix_runs_agent_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_memories_session_id", table_name="memories")
    op.drop_index("ix_memories_project_id", table_name="memories")
    op.drop_table("memories")
    op.drop_index("ix_chunks_project_id", table_name="chunks")
    op.drop_index("ix_chunks_collection_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("ix_collections_project_id", table_name="collections")
    op.drop_table("collections")
    op.drop_index("ix_agents_project_id", table_name="agents")
    op.drop_table("agents")
    op.drop_index("ix_api_keys_project_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("projects")
