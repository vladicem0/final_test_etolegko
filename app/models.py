from sqlalchemy import JSON, ForeignKey, String, false, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    keitaro_id: Mapped[int | None] = mapped_column(unique=True)
    name: Mapped[str] = mapped_column(String(255))
    alias: Mapped[str | None] = mapped_column(String(255), unique=True)
    geo_code: Mapped[str | None] = mapped_column(String(2))
    offer_id: Mapped[int | None]
    domain_id: Mapped[int | None]
    group_id: Mapped[int | None]
    traffic_source_id: Mapped[int | None]
    creation_state: Mapped[str] = mapped_column(String(32))
    # Creator requests retain their original name and alias for idempotent retries.
    snapshot_name: Mapped[str | None] = mapped_column(String(255))
    snapshot_alias: Mapped[str | None] = mapped_column(String(255))
    snapshot_revision: Mapped[int] = mapped_column(server_default=text("0"))


class Stream(Base):
    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    position: Mapped[int]
    type: Mapped[str] = mapped_column(String(32))
    schema: Mapped[str] = mapped_column(String(32))
    present: Mapped[bool]
    edit_revision: Mapped[int] = mapped_column(server_default=text("0"))
    push_pending: Mapped[bool] = mapped_column(server_default=false())
    draft_base: Mapped[list[dict] | None] = mapped_column(JSON(none_as_null=True))
    draft_add_origin: Mapped[list[list[int]] | None] = mapped_column(JSON(none_as_null=True))


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255))


class StreamOffer(Base):
    __tablename__ = "stream_offers"

    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"), primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), primary_key=True)
    position: Mapped[int]
    share: Mapped[int]
    state: Mapped[str] = mapped_column(String(32))
    removed: Mapped[bool]
    pinned: Mapped[bool] = mapped_column(server_default=false())


class StreamOfferDraft(Base):
    __tablename__ = "stream_offer_drafts"

    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"), primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), primary_key=True)
    position: Mapped[int]
    share: Mapped[int]
    state: Mapped[str] = mapped_column(String(32))
    removed: Mapped[bool]
    pinned: Mapped[bool]
