import asyncio
import logging
import time
from collections import defaultdict
from datetime import timezone
from sqlalchemy import asc, desc, func, select
from database import async_session_factory
from models import ItemMeta, MarketTick, SnapshotRun
from .constants import (
    HIST_WINDOW_SNAPSHOTS,
    LATEST_CHECK_TTL_SEC,
    MODEL_VERSION,
    RAID_WATCH_ID_SET,
    min_snapshot_id,
)
from .types import HistPoint, MetaLite, SnapshotCache, TickLite

logger = logging.getLogger("analyzer")

_cache = SnapshotCache()
_cache_lock = asyncio.Lock()


def invalidate_analyzer_cache() -> None:
    _cache.snapshot_id = -1
    _cache.checked_at = 0.0
    _cache.scored_cache_key = None
    _cache.raid_radar_cache = None


def get_cached_items_map() -> dict[int, MetaLite]:
    return _cache.items_map


async def load_snapshot_context(snapshot_id: int | None = None, force_reload: bool = False) -> SnapshotCache | None:
    """Кэширует срез + 120ч чистой истории Blizzard (строго начиная с ANALYZER_MIN_SNAPSHOT_ID)."""
    now_mono = time.monotonic()

    if (
        not force_reload
        and snapshot_id is None
        and _cache.snapshot_id > 0
        and (now_mono - _cache.checked_at) < LATEST_CHECK_TTL_SEC
    ):
        return _cache

    async with _cache_lock:
        now_mono = time.monotonic()
        if (
            not force_reload
            and snapshot_id is None
            and _cache.snapshot_id > 0
            and (now_mono - _cache.checked_at) < LATEST_CHECK_TTL_SEC
        ):
            return _cache

        cutoff_snap_id = min_snapshot_id()

        async with async_session_factory() as session:
            if snapshot_id is None:
                target_run = (
                    await session.execute(
                        select(SnapshotRun)
                        .where(SnapshotRun.id >= cutoff_snap_id)
                        .order_by(desc(SnapshotRun.id))
                        .limit(1)
                    )
                ).scalar_one_or_none()
            else:
                target_run = await session.get(SnapshotRun, snapshot_id)

            if not target_run:
                return None
            if cutoff_snap_id > 0 and target_run.id < cutoff_snap_id:
                return None

            if not force_reload and target_run.id == _cache.snapshot_id:
                _cache.checked_at = now_mono
                return _cache

            raw_snap_ts = target_run.snapshot_ts
            snap_ts_utc = raw_snap_ts if raw_snap_ts.tzinfo else raw_snap_ts.replace(tzinfo=timezone.utc)

            runs_count = int(
                (
                    await session.execute(
                        select(func.count(func.distinct(MarketTick.snapshot_id))).where(
                            MarketTick.source == "blizzard",
                            MarketTick.snapshot_id >= cutoff_snap_id,
                        )
                    )
                ).scalar()
                or 0
            )

            meta_rows = (
                await session.execute(
                    select(
                        ItemMeta.item_id,
                        ItemMeta.name,
                        ItemMeta.category,
                        ItemMeta.is_hidden,
                        ItemMeta.vendor_sell_price,
                        ItemMeta.max_stack,
                        ItemMeta.icon_name,
                    )
                )
            ).all()
            items_map = {
                int(iid): MetaLite(
                    item_id=int(iid),
                    name=str(name or f"Item_{iid}"),
                    category=str(cat or ""),
                    is_hidden=bool(hidden),
                    vendor_sell_price=int(vsp or 0),
                    max_stack=int(ms or 20),
                    icon_name=str(icon or "inv_misc_questionmark"),
                )
                for iid, name, cat, hidden, vsp, ms, icon in meta_rows
            }

            has_v4_cols = hasattr(MarketTick, "cheap_qty")
            cur_cols = [
                MarketTick.item_id,
                MarketTick.min_buyout,
                MarketTick.entry_price,
                MarketTick.wall_price,
                MarketTick.quantity,
                MarketTick.num_auctions,
                MarketTick.hour_of_day,
                MarketTick.day_of_week,
            ]
            if has_v4_cols:
                cur_cols.extend([
                    MarketTick.cheap_qty,
                    MarketTick.gap_qty,
                    MarketTick.book_p25_price,
                    MarketTick.est_sold_qty,
                    MarketTick.est_sold_vwap,
                ])

            cur_rows = (
                await session.execute(
                    select(*cur_cols).where(
                        MarketTick.snapshot_id == target_run.id,
                        MarketTick.source == "blizzard",
                    )
                )
            ).all()

            latest_ticks: dict[int, TickLite] = {}
            for row in cur_rows:
                ep = int(row[2] or 0)
                if ep <= 0:
                    continue
                iid = int(row[0])
                latest_ticks[iid] = TickLite(
                    item_id=iid,
                    min_buyout=int(row[1] or ep),
                    entry_price=ep,
                    wall_price=int(row[3] or 0),
                    quantity=int(row[4] or 0),
                    num_auctions=int(row[5] or 0),
                    hour_of_day=int(row[6] if row[6] is not None else snap_ts_utc.hour),
                    day_of_week=int(row[7] if row[7] is not None else snap_ts_utc.weekday()),
                    cheap_qty=int(row[8] or 0) if has_v4_cols else 0,
                    gap_qty=int(row[9] or 0) if has_v4_cols else 0,
                    book_p25_price=int(row[10] or 0) if has_v4_cols else 0,
                    est_sold_qty=int(row[11] or 0) if has_v4_cols else 0,
                    est_sold_vwap=int(row[12] or 0) if has_v4_cols else 0,
                )

            latest_tape_active = any(t.est_sold_qty > 0 for t in latest_ticks.values())

            # История строго по монотонному id: >= cutoff_snap_id и строго < target_run.id
            hist_runs = (
                await session.execute(
                    select(SnapshotRun.id, SnapshotRun.snapshot_ts)
                    .where(
                        SnapshotRun.id >= cutoff_snap_id,
                        SnapshotRun.id < target_run.id,
                    )
                    .order_by(desc(SnapshotRun.id))
                    .limit(HIST_WINDOW_SNAPSHOTS)
                )
            ).all()

            prev_snapshot_id: int | None = None
            prev_snap_gap_min: float | None = None
            if hist_runs:
                prev_snapshot_id = int(hist_runs[0][0])
                raw_prev_ts = hist_runs[0][1]
                prev_ts_utc = raw_prev_ts if raw_prev_ts.tzinfo else raw_prev_ts.replace(tzinfo=timezone.utc)
                prev_snap_gap_min = (snap_ts_utc - prev_ts_utc).total_seconds() / 60.0

            hist_run_ids = [int(r[0]) for r in hist_runs]
            hist_by_item: dict[int, list[HistPoint]] = defaultdict(list)

            if hist_run_ids:
                h_cols = [
                    MarketTick.snapshot_id,   # 0
                    MarketTick.item_id,       # 1
                    MarketTick.entry_price,   # 2
                    MarketTick.quantity,      # 3
                    MarketTick.num_auctions,  # 4
                    MarketTick.hour_of_day,   # 5
                    MarketTick.day_of_week,   # 6
                ]
                if has_v4_cols:
                    h_cols.append(MarketTick.est_sold_qty)  # 7

                h_rows = (
                    await session.execute(
                        select(*h_cols)
                        .where(
                            MarketTick.source == "blizzard",
                            MarketTick.snapshot_id.in_(hist_run_ids),
                            MarketTick.entry_price > 0,
                        )
                        .order_by(asc(MarketTick.snapshot_id))
                    )
                ).all()

                # Срезы, где синтетическая лента реально отработала (отличаем 0 продаж от отсутствия ленты)
                tape_snap_ids = (
                    {int(row[0]) for row in h_rows if int(row[7] or 0) > 0}
                    if has_v4_cols
                    else set()
                )

                for row in h_rows:
                    iid_int = int(row[1])
                    if iid_int in latest_ticks or iid_int in RAID_WATCH_ID_SET:
                        sid = int(row[0])
                        sold_q = int(row[7] or 0) if (has_v4_cols and sid in tape_snap_ids) else -1
                        hist_by_item[iid_int].append(
                            HistPoint(
                                snapshot_id=sid,
                                entry_price=int(row[2]),
                                quantity=int(row[3] or 0),
                                num_auctions=int(row[4] or 0),
                                hour_of_day=int(row[5] or 0),
                                day_of_week=int(row[6] or 0),
                                est_sold_qty=sold_q,
                            )
                        )

            _cache.snapshot_id = target_run.id
            _cache.snap_ts = snap_ts_utc
            _cache.prev_snapshot_id = prev_snapshot_id
            _cache.prev_snap_gap_min = prev_snap_gap_min
            _cache.runs_count = runs_count
            _cache.tape_active = latest_tape_active
            _cache.checked_at = now_mono
            _cache.items_map = items_map
            _cache.latest_ticks = latest_ticks
            _cache.hist_by_item = dict(hist_by_item)
            _cache.scored_cache_key = None
            _cache.scored_ops = []
            _cache.raid_radar_cache = None

            logger.info(
                "Кэш анализатора %s прогрет: snapshot_id=%d (prev=%s, min=%d), blizzard_snaps=%d, позиций=%d, лента=%s",
                MODEL_VERSION,
                target_run.id,
                prev_snapshot_id,
                cutoff_snap_id,
                runs_count,
                len(latest_ticks),
                "ON" if latest_tape_active else "OFF",
            )
            return _cache