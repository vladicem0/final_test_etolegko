"""Remember selected catalog references for resumable creation."""

from alembic import op
import sqlalchemy as sa

revision = "0002_creation_defaults"
down_revision = "0001_campaigns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("domain_id", "group_id", "traffic_source_id"):
        op.add_column("campaigns", sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("campaigns") as table:
        for name in ("traffic_source_id", "group_id", "domain_id"):
            table.drop_column(name)
