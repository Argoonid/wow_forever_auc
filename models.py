from datetime import datetime
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ItemMeta(Base):
    __tablename__ = "items_meta"

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    icon_name: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    category: Mapped[str] = mapped_column(String(32), default="shadow")
    max_stack: Mapped[int] = mapped_column(Integer, default=20)
    vendor_sell_price: Mapped[int] = mapped_column(Integer, default=0)

    tsm_market_value: Mapped[int] = mapped_column(Integer, default=0)
    tsm_historical_value: Mapped[int] = mapped_column(Integer, default=0)
    tsm_sale_rate: Mapped[float] = mapped_column(Float, default=0.0)

    is_active_pool: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    velocity_score: Mapped[float] = mapped_column(Float, default=0.0)
    meta_resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    meta_retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SnapshotRun(Base):
    __tablename__ = "snapshot_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16), default="tsm")
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    gap_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_continuous: Mapped[bool] = mapped_column(Boolean, default=True)
    tracked_items_count: Mapped[int] = mapped_column(Integer, default=0)


class MarketTick(Base):
    __tablename__ = "market_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, ForeignKey("snapshot_runs.id", ondelete="CASCADE"), index=True)
    snapshot_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    day_of_week: Mapped[int] = mapped_column(Integer)
    hour_of_day: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16), default="tsm")

    item_id: Mapped[int] = mapped_column(Integer, index=True)
    min_buyout: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[int] = mapped_column(Integer)
    wall_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market_value: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    num_auctions: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_tick_item_season", "item_id", "day_of_week", "hour_of_day"),
    )


class ChartDrawing(Base):
    """Пользовательские ценовые уровни и текстовые комментарии на графике."""
    __tablename__ = "chart_drawings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(Integer, index=True)
    tool_type: Mapped[str] = mapped_column(String(24))
    points_json: Mapped[str] = mapped_column(Text)
    note_text: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TradePosition(Base):
    """Журнал покупок, выставлений на аукцион, залогов и закрытых сделок."""
    __tablename__ = "trade_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(Integer, index=True)
    qty: Mapped[int] = mapped_column(Integer, default=1)

    buy_price_copper: Mapped[int] = mapped_column(Integer)
    list_price_copper: Mapped[int] = mapped_column(Integer, default=0)
    sell_price_copper: Mapped[int] = mapped_column(Integer, default=0)
    vendor_unit_copper: Mapped[int] = mapped_column(Integer, default=0)

    deposit_hours: Mapped[int] = mapped_column(Integer, default=8)
    active_deposit_copper: Mapped[int] = mapped_column(Integer, default=0)
    burned_deposit_copper: Mapped[int] = mapped_column(Integer, default=0)
    ah_fee_copper: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl_copper: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(16), default="holding", index=True)
    comment: Mapped[str] = mapped_column(String(256), default="")

    bought_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SignalCallLog(Base):
    """История торговых коллов и проверка точности предсказанных целей."""
    __tablename__ = "signal_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, index=True)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    item_id: Mapped[int] = mapped_column(Integer, index=True)

    tag: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(128), default="")

    buy_price_copper: Mapped[int] = mapped_column(Integer)
    predicted_sell_copper: Mapped[int] = mapped_column(Integer)
    market_value_copper: Mapped[int] = mapped_column(Integer, default=0)
    local_median_copper: Mapped[int] = mapped_column(Integer, default=0)

    predicted_roi: Mapped[float] = mapped_column(Float, default=0.0)
    z_score: Mapped[float] = mapped_column(Float, default=0.0)
    recommended_qty: Mapped[int] = mapped_column(Integer, default=1)
    predicted_profit_copper: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    max_observed_copper: Mapped[int] = mapped_column(Integer, default=0)
    actual_roi: Mapped[float] = mapped_column(Float, default=0.0)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)