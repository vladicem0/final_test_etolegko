"""Persist stream edit versions, original published state, and draft offer bindings."""

from alembic import op
import sqlalchemy as sa


revision = "0004_stream_drafts"
down_revision = "0003_campaign_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("streams", sa.Column("edit_revision", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("streams", sa.Column("draft_base", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("streams", sa.Column("draft_add_origin", sa.JSON(none_as_null=True), nullable=True))
    op.add_column("stream_offers", sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "stream_offer_drafts",
        sa.Column("stream_id", sa.Integer(), sa.ForeignKey("streams.id"), primary_key=True),
        sa.Column("offer_id", sa.Integer(), sa.ForeignKey("offers.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("share", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stream_offer_drafts")
    with op.batch_alter_table("stream_offers") as table:
        table.drop_column("pinned")
    with op.batch_alter_table("streams") as table:
        table.drop_column("draft_add_origin")
        table.drop_column("draft_base")
        table.drop_column("edit_revision")
