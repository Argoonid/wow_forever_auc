from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple


class HistPoint(NamedTuple):
    snapshot_id: int
    entry_price: int
    quantity: int
    num_auctions: int
    hour_of_day: int
    day_of_week: int
    est_sold_qty: int  # -1 если на этом срезе лента сделок ещё не работала


@dataclass(slots=True)
class MetaLite:
    item_id: int
    name: str
    category: str
    is_hidden: bool
    vendor_sell_price: int
    max_stack: int
    icon_name: str


@dataclass(slots=True)
class TickLite:
    item_id: int
    min_buyout: int
    entry_price: int
    wall_price: int
    quantity: int
    num_auctions: int
    hour_of_day: int
    day_of_week: int
    cheap_qty: int = 0
    gap_qty: int = 0
    book_p25_price: int = 0
    est_sold_qty: int = 0
    est_sold_vwap: int = 0


@dataclass
class SnapshotCache:
    snapshot_id: int = -1
    snap_ts: datetime | None = None
    prev_snapshot_id: int | None = None
    prev_snap_gap_min: float | None = None
    runs_count: int = 0
    tape_active: bool = False
    checked_at: float = 0.0
    items_map: dict[int, MetaLite] = field(default_factory=dict)
    latest_ticks: dict[int, TickLite] = field(default_factory=dict)
    hist_by_item: dict[int, list[HistPoint]] = field(default_factory=dict)
    scored_cache_key: tuple[int, float, float] | None = None
    scored_ops: list[dict] = field(default_factory=list)
    raid_radar_cache: dict | None = None