"""Store fetched streams, published offer weights and archived bindings."""

from alembic import op
import sqlalchemy as sa


revision = "0003_campaign_import"
down_revision = "0002_creation_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as table:
        table.alter_column("alias", existing_type=sa.String(255), nullable=True)
        table.add_column(sa.Column("snapshot_name", sa.String(255)))
        table.add_column(sa.Column("snapshot_alias", sa.String(255)))
        table.add_column(sa.Column(
            "snapshot_revision", sa.Integer(), nullable=False, server_default=sa.text("0"),
        ))
    op.create_table(
        "streams",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("schema", sa.String(32), nullable=False),
        sa.Column("present", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_streams_campaign_id", "streams", ["campaign_id"])
    op.create_table(
        "offers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(255), nullable=False),
    )
    op.create_table(
        "stream_offers",
        sa.Column("stream_id", sa.Integer(), sa.ForeignKey("streams.id"), primary_key=True),
        sa.Column("offer_id", sa.Integer(), sa.ForeignKey("offers.id"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("share", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stream_offers")
    op.drop_table("offers")
    op.drop_table("streams")
    # The old schema requires an alias on every row, including imported campaigns.
    db = op.get_bind()
    used = set(db.scalars(sa.text("SELECT alias FROM campaigns WHERE alias IS NOT NULL")))
    imported = db.execute(sa.text("SELECT id, snapshot_alias FROM campaigns WHERE alias IS NULL")).all()
    for campaign_id, remote_alias in imported:
        alias = remote_alias or f"imported-{campaign_id}"
        suffix = 0
        while alias in used:
            suffix += 1
            alias = f"imported-{campaign_id}-{suffix}"
        db.execute(sa.text("UPDATE campaigns SET alias = :alias WHERE id = :id"), {"id": campaign_id, "alias": alias})
        used.add(alias)
    with op.batch_alter_table("campaigns") as table:
        table.alter_column("alias", existing_type=sa.String(255), nullable=False)
        table.drop_column("snapshot_revision")
        table.drop_column("snapshot_alias")
        table.drop_column("snapshot_name")
