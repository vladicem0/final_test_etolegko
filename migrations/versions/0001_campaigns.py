"""Store imported campaigns and recoverable campaign creation requests."""

from alembic import op
import sqlalchemy as sa


revision = "0001_campaigns"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("keitaro_id", sa.Integer(), nullable=True, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("alias", sa.String(255), nullable=False, unique=True),
        sa.Column("geo_code", sa.String(2), nullable=True),
        sa.Column("offer_id", sa.Integer(), nullable=True),
        sa.Column("creation_state", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("campaigns")
