"""Record a stream publication awaiting verification in Keitaro."""

from alembic import op
import sqlalchemy as sa


revision = "0005_stream_push_pending"
down_revision = "0004_stream_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("streams", sa.Column("push_pending", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("streams") as table:
        table.drop_column("push_pending")
