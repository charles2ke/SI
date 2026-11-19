"""Record evaluation configuration and creation time."""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "eval_runs",
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column("eval_runs", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("eval_runs") as batch:
        batch.drop_column("created_at")
        batch.drop_column("config")
