import asyncio
import logging
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from sqlalchemy import asc, desc, func, select
from config import settings
from database import async_session_factory, personal_session_factory
from models import ItemMeta, MarketTick, SignalCallLog, SnapshotRun, TradePosition

logger = logging.getLogger("analyzer")

MODEL_VERSION = "v4"

# Глубина кэша: до 120 чистых часовых срезов Blizzard (~5 суток для персональных часов)
HIST_WINDOW_SNAPSHOTS = 120
REGIME_WINDOW_TICKS = 72
MIN_HIST_TICKS_FLIP = 14
MIN_TICKS_FOR_CLOCK_BOOST = 96
MIN_AUCTIONS_FLIP = 6
MIN_TOTAL_QTY_FLIP = 15
MIN_QTY_CHURN_FLIP = 2.0

# Интервал перепроверки нового snapshot_id в Neon (секунды)
LATEST_CHECK_TTL_SEC = 30.0

# Открытие рейдов WoW: Forever (Barrow Deeps 10, Hyjal Summit 20, Onyxia 40)
RAID_LAUNCH_UTC = datetime(2026, 12, 9, 18, 0, tzinfo=timezone.utc)

# Чёрный список дешёвых NPC-товаров
VENDOR_JUNK_ITEM_IDS: set[int] = {
    2320, 2321, 2324, 2325, 2604, 2605, 2678, 2880,
    3371, 3372, 3466, 3857, 4289, 4291, 4340, 4341,
    4342, 5042, 5048, 6260, 6261, 8343, 8925, 14341, 18256,
}

# Долгосрочный радар инвестиций под рейды 9 декабря: (item_id, заметка, тир)
RAID_WATCHLIST: list[tuple[int, str, str]] = [
    (248822, "Death Lotus [Forever фласки]", "T1"),
    (248821, "Demonsage [Forever фласки]", "T1"),
    (248823, "Marefoil [Forever фласки/крафт]", "T1"),
    (7068, "Elemental Fire [GFPP Ониксия]", "T1"),
    (13457, "Greater Fire Protection Potion", "T1"),
    (13463, "Dreamfoil [GFPP/Эликсиры/Фласки]", "T1"),
    (8846, "Gromsblood [Титан/Grizzly/Ferocity]", "T1"),
    (13466, "Plaguebloom [Frenzy/Mongoose]", "T1"),
    (8839, "Blindweed [Frenzy/Grizzly]", "T1"),
    (13465, "Mountain Silversage [Mongoose/Mana]", "T1"),
    (8838, "Sungrass [Fortitude/Nature Power]", "T1"),
    (13464, "Golden Sansam [Sages/Nature Power]", "T1"),
    (13452, "Elixir of the Mongoose [Готовый]", "T1"),
    (13444, "Major Mana Potion [Готовый]", "T1"),
    (13468, "Black Lotus [Титан/СПД Ониксия]", "T2"),
    (13422, "Stonescale Eel [Угорь -> Масло]", "T2"),
    (13423, "Stonescale Oil [Титан/Защита]", "T2"),
    (8845, "Ghost Mushroom [Ferocity/Shadow]", "T2"),
    (12363, "Arcane Crystal [Arcanite CD]", "T2"),
    (12360, "Arcanite Bar [Готовый слиток]", "T2"),
    (248815, "Heavy Thorium Ore [Legionite CD]", "T2"),
    (248819, "Fel Crystal [Legionite CD]", "T2"),
    (251290, "Legionite Bar [Пре-рейд BiS]", "T2"),
    (7067, "Elemental Earth [GNPP спекуляция]", "T3"),
    (3824, "Shadow Oil [GSPP спекуляция]", "T3"),
    (3369, "Grave Moss [Сырьё Shadow Oil]", "T3"),
]
RAID_WATCHLIST_IDS: list[tuple[int, str]] = [(iid, note) for iid, note, _ in RAID_WATCHLIST]
_RAID_WATCH_ID_SET: set[int] = {iid for iid, _, _ in RAID_WATCHLIST}


@dataclass(slots=True)
class _MetaLite:
    item_id: int
    name: str
    category: str
    is_hidden: bool
    vendor_sell_price: int
    max_stack: int
    icon_name: str


@dataclass(slots=True)
class _TickLite:
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
class _SnapshotCache:
    snapshot_id: int = -1
    snap_ts: datetime | None = None
    runs_count: int = 0
    checked_at: float = 0.0
    items_map: dict[int, _MetaLite] = field(default_factory=dict)
    latest_ticks: dict[int, _TickLite] = field(default_factory=dict)
    # item_id -> [(entry_price, qty, num_auctions, hour_of_day, day_of_week, est_sold_qty), ...]
    hist_by_item: dict[int, list[tuple[int, int, int, int, int, int]]] = field(default_factory=dict)
    scored_cache_key: tuple[int, float, float] | None = None
    scored_ops: list[dict] = field(default_factory=list)
    raid_radar_cache: dict | None = None


_cache = _SnapshotCache()
_cache_lock = asyncio.Lock()


def invalidate_analyzer_cache() -> None:
    _cache.snapshot_id = -1
    _cache.checked_at = 0.0
    _cache.scored_cache_key = None
    _cache.raid_radar_cache = None


def _is_market_read_only() -> bool:
    if bool(getattr(settings, "MARKET_READ_ONLY", False)):
        return True
    return os.getenv("MARKET_READ_ONLY", "").strip().lower() in {"1", "true", "yes"}


def _min_snapshot_id() -> int:
    val = getattr(settings, "ANALYZER_MIN_SNAPSHOT_ID", None)
    if val is None:
        val = os.getenv("ANALYZER_MIN_SNAPSHOT_ID", "0")
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 0


def format_wow_money(copper: int) -> str:
    sign = "-" if copper < 0 else ""
    c = abs(int(copper))
    gold = c // 10000
    silver = (c % 10000) // 100
    rem_copper = c % 100
    if gold > 0:
        return f"{sign}{gold}g {silver:02d}s {rem_copper:02d}c"
    return f"{sign}{silver}s {rem_copper:02d}c"


def calculate_deposit_copper(vendor_sell_price: int, quantity: int, hours: int = 8) -> int:
    rate = {2: 0.15, 8: 0.30, 24: 0.60}.get(hours, 0.30)
    return int(vendor_sell_price * quantity * rate)


def _percentile(sorted_vals: list[int] | list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * q
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(sorted_vals[lo])
    return float(sorted_vals[lo]) + (float(sorted_vals[hi]) - float(sorted_vals[lo])) * (idx - lo)


def _percentile_rank(sorted_vals: list[int], value: int) -> float:
    if not sorted_vals:
        return 50.0
    n = len(sorted_vals)
    below = sum(1 for x in sorted_vals if x < value)
    equal = sum(1 for x in sorted_vals if x == value)
    return round(((below + 0.5 * equal) / n) * 100.0, 1)


def _compute_robust_stats(prices: list[int]) -> tuple[float, float, float, float]:
    if not prices:
        return 0.0, 0.0, 0.0, 0.0
    s = sorted(prices)
    p25 = _percentile(s, 0.25)
    median = _percentile(s, 0.50)
    p75 = _percentile(s, 0.75)
    if len(s) < 4:
        return median, max(median * 0.10, 1.0), p25, p75

    abs_devs = sorted(abs(x - median) for x in s)
    mad = _percentile(abs_devs, 0.50)
    robust_std = max(1.4826 * mad, median * 0.05, 1.0)
    return median, robust_std, p25, p75


def _compute_qty_churn(quantities: list[int]) -> float:
    if len(quantities) < 2:
        return 0.0
    deltas = sorted(abs(quantities[i] - quantities[i - 1]) for i in range(1, len(quantities)))
    return _percentile(deltas, 0.50)


def _compute_item_clock(
    series: list[tuple[int, int, int, int, int, int]],
    current_hour: int,
) -> dict:
    """
    Персональные биологические часы актива (круговое 3-часовое сглаживание по чистым срезам Blizzard).
    Сам различает сырьё (дно вечером -> пик утром) и расходники (дно днём -> пик в 19-22 UTC).
    """
    by_hour: dict[int, list[int]] = defaultdict(list)
    for ep, _, _, hod, _, _ in series:
        by_hour[hod % 24].append(ep)

    if len(by_hour) < 12:
        return {
            "has_clock": False,
            "trough_hour": -1,
            "peak_hour": -1,
            "trough_med": 0.0,
            "peak_med": 0.0,
            "clock_edge_pct": 0.0,
            "hours_to_peak": 12,
        }

    smooth_med: dict[int, float] = {}
    for h in range(24):
        bucket = by_hour.get((h - 1) % 24, []) + by_hour.get(h, []) + by_hour.get((h + 1) % 24, [])
        if len(bucket) >= 2:
            smooth_med[h] = _percentile(sorted(bucket), 0.50)

    if len(smooth_med) < 10:
        return {
            "has_clock": False,
            "trough_hour": -1,
            "peak_hour": -1,
            "trough_med": 0.0,
            "peak_med": 0.0,
            "clock_edge_pct": 0.0,
            "hours_to_peak": 12,
        }

    trough_hour, trough_med = min(smooth_med.items(), key=lambda kv: kv[1])
    peak_hour, peak_med = max(smooth_med.items(), key=lambda kv: kv[1])
    edge_pct = round(((peak_med / trough_med) - 1.0) * 100.0, 1) if trough_med > 0 else 0.0
    hours_to_peak = (peak_hour - current_hour) % 24

    return {
        "has_clock": True,
        "trough_hour": trough_hour,
        "peak_hour": peak_hour,
        "trough_med": trough_med,
        "peak_med": peak_med,
        "clock_edge_pct": edge_pct,
        "hours_to_peak": hours_to_peak,
    }


def _analyze_price_regime(
    series: list[tuple[int, int, int, int, int, int]],
    clock: dict,
) -> dict:
    """
    Режимный фильтр v4 на последних 72ч чистых тиков house_110:
    убивает умирающие тренды, падающие ножи и разовые всплески; требует подтверждённый цикл.
    """
    recent_series = series[-REGIME_WINDOW_TICKS:]
    prices = [x[0] for x in recent_series]
    n = len(prices)
    if n < MIN_HIST_TICKS_FLIP:
        return {"tradable": False, "reason": "short_history"}

    # 1a. Долгий структурный спад (старое окно vs последние 16ч)
    split = max(4, n - 16)
    old_med = _percentile(sorted(prices[:split]), 0.50)
    recent_16 = prices[split:]
    recent_16_med = _percentile(sorted(recent_16), 0.50)
    if old_med > 0 and recent_16_med < old_med * 0.85 and max(recent_16) < old_med * 0.94:
        return {"tradable": False, "reason": "structural_downtrend"}

    # 1b. Падающий нож (последние 6ч против предыдущих 12ч)
    if n >= 18:
        prev_12 = prices[-18:-6]
        last_6 = prices[-6:]
        prev_12_med = _percentile(sorted(prev_12), 0.50)
        last_6_med = _percentile(sorted(last_6), 0.50)
        if prev_12_med > 0 and last_6_med < prev_12_med * 0.88 and max(last_6) < prev_12_med * 0.95:
            return {"tradable": False, "reason": "falling_knife"}

    # 2. Устойчивая полка (3-часовой rolling min + обязательная проверка за последние 24 тика)
    sustained_3h = [min(prices[i : i + 3]) for i in range(n - 2)]
    plateau_window = _percentile(sorted(sustained_3h), 0.80)

    recent_24 = prices[-24:] if n >= 24 else prices
    sustained_2h_24 = [min(recent_24[i : i + 2]) for i in range(len(recent_24) - 1)]
    fresh_ceiling_24 = float(max(sustained_2h_24)) if sustained_2h_24 else plateau_window

    ref_plateau = min(plateau_window, fresh_ceiling_24)

    # Подтягиваем полку по персональным часам только когда накопилось >= 96 срезов (4 суток)
    if (
        len(series) >= MIN_TICKS_FOR_CLOCK_BOOST
        and clock["has_clock"]
        and clock["clock_edge_pct"] >= 18.0
        and clock["peak_med"] > 0
    ):
        ref_plateau = min(max(ref_plateau, clock["peak_med"] * 0.96), fresh_ceiling_24)

    recent_low = _percentile(sorted(recent_24), 0.20)
    if recent_low <= 0 or ref_plateau <= recent_low * 1.12:
        return {"tradable": False, "reason": "flat_recent_24h"}

    # 3. Счётчик подтверждённых циклов возврата (общий + минимум 1 свежий за последние 30 тиков)
    low_thr = recent_low * 1.08
    high_thr = ref_plateau * 0.93

    def _count_bounces(seq: list[int]) -> int:
        st, cnt = 0, 0
        for p in seq:
            if p <= low_thr:
                st = -1
            elif p >= high_thr:
                if st == -1:
                    cnt += 1
                st = 1
        return cnt

    bounces = _count_bounces(prices)
    recent_bounces = _count_bounces(prices[-30:] if n >= 30 else prices)
    min_bounces = 2 if n >= 28 else 1
    if bounces < min_bounces or recent_bounces < 1:
        return {"tradable": False, "reason": f"no_fresh_pattern({bounces}/{recent_bounces})"}

    return {
        "tradable": True,
        "ref_plateau": ref_plateau,
        "fresh_ceiling_24": fresh_ceiling_24,
        "recent_low": recent_low,
        "recent_med": recent_16_med,
        "bounces": bounces,
        "recent_bounces": recent_bounces,
        "spread_pct": round((ref_plateau / recent_low - 1.0) * 100.0, 1),
    }


async def _load_snapshot_context(snapshot_id: int | None = None, force_reload: bool = False) -> _SnapshotCache | None:
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

        min_snap_id = _min_snapshot_id()

        async with async_session_factory() as session:
            if snapshot_id is None:
                latest_run = (
                    await session.execute(
                        select(SnapshotRun)
                        .where(SnapshotRun.id >= min_snap_id)
                        .order_by(desc(SnapshotRun.id))
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if not latest_run:
                    return None
                target_run = latest_run
            else:
                target_run = await session.get(SnapshotRun, snapshot_id)
                if not target_run:
                    return None

            if min_snap_id > 0 and target_run.id < min_snap_id:
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
                            MarketTick.snapshot_id >= min_snap_id,
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
                int(iid): _MetaLite(
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

            latest_ticks: dict[int, _TickLite] = {}
            for row in cur_rows:
                ep = int(row[2] or 0)
                if ep <= 0:
                    continue
                iid = int(row[0])
                latest_ticks[iid] = _TickLite(
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

            # История строго по монотонному id: >= min_snap_id и строго < target_run.id
            hist_run_ids = list(
                (
                    await session.execute(
                        select(SnapshotRun.id)
                        .where(
                            SnapshotRun.id >= min_snap_id,
                            SnapshotRun.id < target_run.id,
                        )
                        .order_by(desc(SnapshotRun.id))
                        .limit(HIST_WINDOW_SNAPSHOTS)
                    )
                ).scalars().all()
            )

            hist_by_item: dict[int, list[tuple[int, int, int, int, int, int]]] = defaultdict(list)
            if hist_run_ids:
                h_cols = [
                    MarketTick.item_id,
                    MarketTick.entry_price,
                    MarketTick.quantity,
                    MarketTick.num_auctions,
                    MarketTick.hour_of_day,
                    MarketTick.day_of_week,
                ]
                if has_v4_cols:
                    h_cols.append(MarketTick.est_sold_qty)

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

                for row in h_rows:
                    iid_int = int(row[0])
                    if iid_int in latest_ticks or iid_int in _RAID_WATCH_ID_SET:
                        sold_q = int(row[6] or 0) if has_v4_cols else 0
                        hist_by_item[iid_int].append(
                            (int(row[1]), int(row[2] or 0), int(row[3] or 0), int(row[4] or 0), int(row[5] or 0), sold_q)
                        )

            _cache.snapshot_id = target_run.id
            _cache.snap_ts = snap_ts_utc
            _cache.runs_count = runs_count
            _cache.checked_at = now_mono
            _cache.items_map = items_map
            _cache.latest_ticks = latest_ticks
            _cache.hist_by_item = dict(hist_by_item)
            _cache.scored_cache_key = None
            _cache.scored_ops = []
            _cache.raid_radar_cache = None

            logger.info(
                "Кэш анализатора v4 прогрет: snapshot_id=%d (min=%d), blizzard_snaps=%d, позиций=%d",
                target_run.id,
                min_snap_id,
                runs_count,
                len(latest_ticks),
            )
            return _cache


async def refresh_call_outcomes(session) -> None:
    """Батчевая проверка открытых коллов за 24ч строго по чистым срезам house_110."""
    if _is_market_read_only():
        return

    pending_calls = list(
        (await session.execute(select(SignalCallLog).where(SignalCallLog.status == "pending"))).scalars().all()
    )
    if not pending_calls:
        return

    now = datetime.now(timezone.utc)
    updated = False

    raw_min_ts = min(c.called_at for c in pending_calls)
    pending_item_ids = {c.item_id for c in pending_calls}
    min_snap_id = _min_snapshot_id()

    vendor_map = {
        int(iid): int(vp or 0)
        for iid, vp in (
            await session.execute(
                select(ItemMeta.item_id, ItemMeta.vendor_sell_price).where(ItemMeta.item_id.in_(pending_item_ids))
            )
        ).all()
    }

    ticks_rows = (
        await session.execute(
            select(
                MarketTick.item_id,
                MarketTick.snapshot_ts,
                MarketTick.entry_price,
                MarketTick.num_auctions,
            ).where(
                MarketTick.source == "blizzard",
                MarketTick.snapshot_id >= min_snap_id,
                MarketTick.item_id.in_(pending_item_ids),
                MarketTick.snapshot_ts > raw_min_ts,
                MarketTick.entry_price > 0,
            )
        )
    ).all()

    ticks_by_item: dict[int, list[tuple[datetime, int, int]]] = defaultdict(list)
    for iid, ts, ep, na in ticks_rows:
        ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        ticks_by_item[int(iid)].append((ts_utc, int(ep), int(na or 0)))

    for call in pending_calls:
        call_ts = call.called_at if call.called_at.tzinfo else call.called_at.replace(tzinfo=timezone.utc)
        horizon_end = call_ts + timedelta(hours=24)

        future_rows = [
            (ep, na)
            for ts, ep, na in ticks_by_item.get(call.item_id, ())
            if call_ts < ts <= horizon_end
        ]

        if not future_rows:
            if now > horizon_end + timedelta(hours=2):
                call.status = "missed"
                call.resolved_at = now
                updated = True
            continue

        max_seen = max(ep for ep, _ in future_rows)
        if max_seen > call.max_observed_copper:
            call.max_observed_copper = max_seen
            dep = calculate_deposit_copper(vendor_map.get(call.item_id, 0), 1, hours=8)
            net_realized = int(max_seen * 0.95) - call.buy_price_copper - dep
            call.actual_roi = (
                round((net_realized / call.buy_price_copper) * 100.0, 1)
                if call.buy_price_copper > 0
                else 0.0
            )
            updated = True

        ticks_at_target = [na for ep, na in future_rows if ep >= call.predicted_sell_copper]
        confirmed_hit = len(ticks_at_target) >= 2 or (
            len(ticks_at_target) >= 1 and max(ticks_at_target) >= 6
        )

        if confirmed_hit:
            call.status = "hit"
            call.resolved_at = now
            updated = True
        elif now > horizon_end:
            call.status = "partial" if call.actual_roi >= 5.0 else "missed"
            call.resolved_at = now
            updated = True

    if updated:
        await session.commit()


def _score_from_cache(ctx: _SnapshotCache) -> list[dict]:
    """
    Скоринг v4:
    Персональные часы актива, защита от покупки выше медианы, оценка воздушного кармана
    и выбор слота (8ч/24ч) через честное матожидание E[PnL] с учётом риска залога.
    """
    bankroll_gold = float(settings.BANKROLL_GOLD)
    max_share = float(settings.MAX_SHARE_PER_ITEM)
    cache_key = (ctx.snapshot_id, bankroll_gold, max_share)
    if ctx.scored_cache_key == cache_key and ctx.scored_ops is not None:
        return ctx.scored_ops

    max_budget_per_item = int(bankroll_gold * 10000 * max_share)
    snap_ts = ctx.snap_ts or datetime.now(timezone.utc)
    cur_hour = snap_ts.hour
    is_weekend = snap_ts.weekday() >= 5

    opportunities: list[dict] = []

    for iid, tick in ctx.latest_ticks.items():
        if iid in VENDOR_JUNK_ITEM_IDS:
            continue
        meta = ctx.items_map.get(iid)
        if not meta or meta.is_hidden:
            continue

        buy_price = tick.entry_price
        wall_price = tick.wall_price
        total_qty = tick.quantity
        num_auctions = tick.num_auctions

        if buy_price < 20:
            continue
        if num_auctions < MIN_AUCTIONS_FLIP or total_qty < MIN_TOTAL_QTY_FLIP:
            continue

        hist_series = ctx.hist_by_item.get(iid, [])
        if len(hist_series) < MIN_HIST_TICKS_FLIP:
            continue

        recent_slice = hist_series[-REGIME_WINDOW_TICKS:]
        local_prices = [x[0] for x in recent_slice]
        local_qtys = [x[1] for x in recent_slice]
        local_aucs = [x[2] for x in recent_slice]
        local_sold = [x[5] for x in recent_slice if x[5] > 0]

        qty_churn = _compute_qty_churn(local_qtys)
        avg_sold_hr = (sum(local_sold) / len(local_sold)) if local_sold else 0.0
        effective_flow = max(avg_sold_hr, qty_churn)
        median_aucs = _percentile(sorted(local_aucs), 0.50) if local_aucs else float(num_auctions)

        if effective_flow < MIN_QTY_CHURN_FLIP and median_aucs < 12:
            continue

        # 1. Персональные биологические часы + режимный фильтр графика
        clock = _compute_item_clock(hist_series, cur_hour)
        regime = _analyze_price_regime(hist_series, clock)
        if not regime["tradable"]:
            continue

        median, r_std, _, _ = _compute_robust_stats(local_prices)
        ref_plateau = regime["ref_plateau"]
        bounces = regime["bounces"]

        # Страховка: не покупаем "дно", если цена входа выше локальной медианы
        if median <= 0 or buy_price > int(median * 1.02):
            continue
        if buy_price >= ref_plateau * 0.90:
            continue

        # 2. Геометрия стены: жёсткий бан стены на дне, кроме сверхликвидных маятников (200+ лотов)
        wall_note = ""
        is_dump_floor_wall = False
        target_sell = int(ref_plateau * 0.95)

        if wall_price > 0:
            if wall_price <= int(buy_price * 1.04):
                if num_auctions < 200 or bounces < 2:
                    continue
                is_dump_floor_wall = True
                wall_note = f"дамп-стена {format_wow_money(wall_price)}"
            else:
                capped_by_wall = int(wall_price * 0.98)
                if capped_by_wall < target_sell:
                    target_sell = capped_by_wall
                    wall_note = f"под стеной {format_wow_money(wall_price)}"

        if target_sell <= buy_price:
            continue

        z_score = (ref_plateau - buy_price) / r_std
        disc_plateau_pct = (1.0 - buy_price / ref_plateau) * 100.0

        # 3. Оценка геометрии воздушного кармана и вероятности исполнения p_fill
        if tick.gap_qty > 0 and effective_flow > 0:
            hours_to_clear_gap = tick.gap_qty / max(effective_flow, 1.0)
        else:
            hours_to_clear_gap = 2.0

        if hours_to_clear_gap > 18.0:
            continue

        p_fill = 0.45 + min(0.18, 0.05 * bounces)
        if clock["has_clock"] and clock["clock_edge_pct"] >= 15.0 and 2 <= clock["hours_to_peak"] <= 14:
            p_fill += 0.14
        elif clock["has_clock"] and (clock["hours_to_peak"] <= 1 or clock["hours_to_peak"] >= 20):
            p_fill -= 0.10
        if hours_to_clear_gap <= 3.0:
            p_fill += 0.08
        elif hours_to_clear_gap >= 8.0:
            p_fill -= 0.12
        if is_dump_floor_wall:
            p_fill -= 0.08
        if is_weekend:
            p_fill -= 0.04
        p_fill = max(0.25, min(0.85, p_fill))

        # 4. Выбор оптимального слота (8ч vs 24ч) и расчёт матожидания E[PnL]
        vendor_price = meta.vendor_sell_price
        dep_8h = calculate_deposit_copper(vendor_price, 1, hours=8)
        dep_24h = calculate_deposit_copper(vendor_price, 1, hours=24)
        dep_24h_risk_pct = (dep_24h / buy_price) * 100.0 if buy_price > 0 else 0.0

        peak_within_8h = clock["has_clock"] and 1 <= clock["hours_to_peak"] <= 8
        rec_list_hours = 8 if (dep_24h_risk_pct > 5.0 or peak_within_8h) else 24
        deposit_unit = dep_8h if rec_list_hours == 8 else dep_24h

        net_if_filled = int(target_sell * 0.95) - buy_price
        ev_unit = p_fill * net_if_filled - (1.0 - p_fill) * deposit_unit
        if net_if_filled <= 0 or ev_unit <= 0:
            continue

        roi_pct = (net_if_filled / buy_price) * 100.0
        ev_roi_pct = (ev_unit / buy_price) * 100.0

        in_own_trough_window = clock["has_clock"] and clock["clock_edge_pct"] >= 15.0 and 2 <= clock["hours_to_peak"] <= 14
        min_roi_req = 14.0 if in_own_trough_window else 18.0
        min_ev_roi_req = 8.0 if in_own_trough_window else 11.0
        min_z_req = 1.05 if in_own_trough_window else 1.35

        if roi_pct < min_roi_req or ev_roi_pct < min_ev_roi_req or z_score < min_z_req:
            continue

        # 5. Реальный размер пакета (по cheap_qty, когда он уже пишется в БД)
        max_by_budget = max_budget_per_item // buy_price
        est_cheap_depth = tick.cheap_qty if tick.cheap_qty > 0 else max(1, total_qty // 4)
        affordable_qty = min(meta.max_stack, max_by_budget, est_cheap_depth)
        if affordable_qty <= 0:
            continue

        total_profit = net_if_filled * affordable_qty
        total_ev = int(ev_unit * affordable_qty)
        if total_profit < 1500 or total_ev < 800:
            continue

        if in_own_trough_window and bounces >= 2:
            tag = "МАЯТНИК"
        elif tick.gap_qty > 0 and hours_to_clear_gap <= 2.5 and z_score >= 1.5:
            tag = "КАРМАН"
        elif z_score >= 2.0 and disc_plateau_pct >= 22.0:
            tag = "СИЛЬНОЕ ДНО"
        else:
            tag = "ПРОСАДКА"

        reason_parts = [
            f"-{disc_plateau_pct:.0f}% от полки ({format_wow_money(int(ref_plateau))})",
            f"циклов {bounces}",
            f"p_fill {int(p_fill * 100)}%",
            f"слот {rec_list_hours}ч",
        ]
        if clock["has_clock"] and clock["clock_edge_pct"] >= 12.0:
            reason_parts.append(f"пик ~{clock['peak_hour']:02d} UTC (+{clock['clock_edge_pct']:.0f}%)")
        if tick.gap_qty > 0:
            reason_parts.append(f"карман {tick.gap_qty}шт")
        if avg_sold_hr >= 1.0:
            reason_parts.append(f"выкуп ~{avg_sold_hr:.0f}/ч")
        else:
            reason_parts.append(f"churn {qty_churn:.0f}/ч")
        if wall_note:
            reason_parts.append(wall_note)
        reason_str = " • ".join(reason_parts)

        # Мультипликативный скоринг через math.prod (защищён от искажений при копировании)
        depth_mult = math.log1p(num_auctions) / math.log1p(40.0)
        flow_mult = math.log1p(effective_flow) / math.log1p(50.0)
        liquidity_mult = max(0.25, 0.5 * depth_mult + 0.5 * flow_mult)
        turnover_penalty = min(1.0, max(0.35, (effective_flow * 4.0) / max(affordable_qty, 1)))
        cycle_mult = min(1.6, 0.85 + 0.20 * bounces)
        clock_mult = 1.25 if in_own_trough_window else 0.90
        wall_mult = 0.70 if is_dump_floor_wall else 1.0

        score = math.prod([
            float(total_ev),
            liquidity_mult,
            turnover_penalty,
            cycle_mult,
            clock_mult,
            wall_mult,
            min(2.2, max(0.8, z_score)),
        ])

        opportunities.append({
            "item_id": iid,
            "name": meta.name,
            "category": meta.category,
            "tag": tag,
            "reason": reason_str,
            "buy": buy_price,
            "sell": target_sell,
            "market_value": 0,
            "local_median": int(median),
            "day_plateau": int(ref_plateau),
            "wall_price": wall_price,
            "vendor": vendor_price,
            "roi": round(roi_pct, 1),
            "ev_roi": round(ev_roi_pct, 1),
            "p_fill": round(p_fill, 2),
            "z_score": round(z_score, 2),
            "bounces": bounces,
            "peak_hour_utc": clock["peak_hour"],
            "hours_to_peak": clock["hours_to_peak"],
            "list_hours": rec_list_hours,
            "deposit_risk_pct": round(dep_24h_risk_pct, 1),
            "cheap_qty": tick.cheap_qty,
            "gap_qty": tick.gap_qty,
            "avg_sold_hr": round(avg_sold_hr, 1),
            "qty": affordable_qty,
            "total_profit": total_profit,
            "total_ev": total_ev,
            "daily_sales": round(effective_flow * 24.0, 1),
            "num_auctions": num_auctions,
            "qty_churn": round(qty_churn, 1),
            "score": round(score, 1),
        })

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    ctx.scored_cache_key = cache_key
    ctx.scored_ops = opportunities
    return opportunities


def _build_raid_radar_from_cache(ctx: _SnapshotCache) -> dict:
    """Отдельный инвест-радар к 9 декабря с персональным лучшим часом входа и кросс-паритетами."""
    if ctx.raid_radar_cache is not None:
        return ctx.raid_radar_cache

    snap_ts = ctx.snap_ts or datetime.now(timezone.utc)
    cur_hour = snap_ts.hour
    days_left = max(0, (RAID_LAUNCH_UTC - snap_ts).days)

    if days_left > 10:
        phase_hint = "ФАЗА НАБОРА: выкупаем откаты ниже медианы строго в суточное окно дна каждого актива."
    elif days_left >= 2:
        phase_hint = "ПРЕДРЕЙДОВАЯ РАЗГРУЗКА: фиксируем 70-80% позиций траншами до вечера 8 декабря."
    else:
        phase_hint = "РЕЛИЗ РЕЙДОВ: фиксируем остаток на пиках, не передерживаем до второго дня КД."

    items_report: list[dict] = []
    status_order = {"RIPE": 0, "ACCUM": 1, "HOT": 2, "MISSING": 3}

    for iid, note, tier in RAID_WATCHLIST:
        tick = ctx.latest_ticks.get(iid)
        meta = ctx.items_map.get(iid)
        name = meta.name if meta else note

        if not tick:
            items_report.append({
                "item_id": iid,
                "name": name,
                "note": note,
                "tier": tier,
                "status_code": "MISSING",
                "status_label": "НЕТ НА АН",
                "price": 0,
                "median": 0,
                "p25": 0,
                "p75": 0,
                "lo": 0,
                "hi": 0,
                "rank_pct": 50.0,
                "disc_med_pct": 0.0,
                "qty": 0,
                "num_auctions": 0,
                "best_buy_hour_utc": -1,
                "supply_note": "",
            })
            continue

        series = ctx.hist_by_item.get(iid, [])
        clock = _compute_item_clock(series, cur_hour)
        prices = [x[0] for x in series[-REGIME_WINDOW_TICKS:]] + [tick.entry_price]
        qtys = [x[1] for x in series[-REGIME_WINDOW_TICKS:]]
        med, _, p25, p75 = _compute_robust_stats(prices)
        s_prices = sorted(prices)
        lo, hi = s_prices[0], s_prices[-1]
        rank_pct = _percentile_rank(s_prices, tick.entry_price)
        disc_med_pct = round(((1.0 - tick.entry_price / med) * 100.0), 1) if med > 0 else 0.0

        med_qty = _percentile(sorted(qtys), 0.50) if qtys else float(tick.quantity)
        supply_note = ""
        if med_qty > 0:
            ratio = tick.quantity / med_qty
            if ratio <= 0.60:
                supply_note = "стакан сжат!"
            elif ratio >= 1.50:
                supply_note = "наплыв фарма"

        if rank_pct <= 28.0 or disc_med_pct >= 8.0:
            status_code = "RIPE"
            status_label = "СОЗРЕЛО (ДНО)"
        elif rank_pct >= 72.0 or disc_med_pct <= -12.0:
            status_code = "HOT"
            status_label = "ПЕРЕГРЕТО (ЖДАТЬ)"
        else:
            status_code = "ACCUM"
            status_label = "НАБОР ЧАСТЯМИ"

        items_report.append({
            "item_id": iid,
            "name": name,
            "note": note,
            "tier": tier,
            "status_code": status_code,
            "status_label": status_label,
            "price": tick.entry_price,
            "median": int(med),
            "p25": int(p25),
            "p75": int(p75),
            "lo": lo,
            "hi": hi,
            "rank_pct": rank_pct,
            "disc_med_pct": disc_med_pct,
            "qty": tick.quantity,
            "num_auctions": tick.num_auctions,
            "best_buy_hour_utc": clock["trough_hour"] if clock["has_clock"] else -1,
            "supply_note": supply_note,
        })

    tier_rank = {"T1": 0, "T2": 1, "T3": 2}
    items_report.sort(key=lambda x: (status_order[x["status_code"]], tier_rank[x["tier"]], x["rank_pct"]))

    def _p(iid: int) -> int:
        t = ctx.latest_ticks.get(iid)
        return t.entry_price if t else 0

    parities: list[str] = []

    eel_p, oil_p = _p(13422), _p(13423)
    if eel_p > 0 and oil_p > 0:
        mats = eel_p * 2
        diff_pct = (oil_p / mats - 1.0) * 100.0
        verdict = "БРАТЬ МАСЛО (ниже сырья!)" if oil_p < mats else "масло дороже рыбы"
        parities.append(
            f"Масло угря {format_wow_money(oil_p)} vs 2x Угорь {format_wow_money(mats)} ({diff_pct:+.0f}%) -> {verdict}"
        )

    fire_p, dream_p, gfpp_p = _p(7068), _p(13463), _p(13457)
    if fire_p > 0 and dream_p > 0 and gfpp_p > 0:
        mats = fire_p + dream_p + 500
        diff_pct = (gfpp_p / mats - 1.0) * 100.0
        verdict = "выгоднее брать готовый GFPP" if gfpp_p <= mats * 1.05 else "выгоднее брать Огонь+Снолист"
        parities.append(
            f"GFPP {format_wow_money(gfpp_p)} vs Огонь+Снолист {format_wow_money(mats)} ({diff_pct:+.0f}%) -> {verdict}"
        )

    sage_p, plague_p, mong_p = _p(13465), _p(13466), _p(13452)
    if sage_p > 0 and plague_p > 0 and mong_p > 0:
        mats = 2 * sage_p + 2 * plague_p + 500
        diff_pct = (mong_p / mats - 1.0) * 100.0
        verdict = "БРАТЬ ГОТОВЫЙ МАНГУСТ (по цене травы!)" if mong_p <= mats * 1.08 else "трава дешевле готового"
        parities.append(
            f"Мангуст {format_wow_money(mong_p)} vs 2xШалфей+2xЧумоцвет {format_wow_money(mats)} ({diff_pct:+.0f}%) -> {verdict}"
        )

    cryst_p, bar_p = _p(12363), _p(12360)
    if cryst_p > 0 and bar_p > 0:
        cd_prem = bar_p - cryst_p
        parities.append(
            f"Арканит {format_wow_money(bar_p)} vs Кристалл {format_wow_money(cryst_p)} -> премия КД трансмута ~{format_wow_money(cd_prem)}"
        )

    dl_p, bl_p = _p(248822), _p(13468)
    if dl_p > 0 and bl_p > 0:
        ratio = (dl_p / bl_p) * 100.0
        verdict = "Лотос смерти недооценён к Чёрному!" if ratio < 85.0 else "паритет близок"
        parities.append(
            f"Лотос смерти {format_wow_money(dl_p)} ({ratio:.0f}% от Чёрного лотоса {format_wow_money(bl_p)}) -> {verdict}"
        )

    radar = {
        "days_to_raids": days_left,
        "phase_hint": phase_hint,
        "items": items_report,
        "parities": parities,
    }
    ctx.raid_radar_cache = radar
    return radar


async def get_raid_investment_radar(snapshot_id: int | None = None) -> dict:
    ctx = await _load_snapshot_context(snapshot_id=snapshot_id)
    if not ctx:
        return {"days_to_raids": 0, "phase_hint": "Нет данных", "items": [], "parities": []}
    return _build_raid_radar_from_cache(ctx)


async def get_scored_opportunities(snapshot_id: int | None = None, record_calls: bool = False) -> list[dict]:
    can_write_cloud = record_calls and not _is_market_read_only()

    if can_write_cloud:
        async with async_session_factory() as session:
            await refresh_call_outcomes(session)

    ctx = await _load_snapshot_context(snapshot_id=snapshot_id, force_reload=can_write_cloud)
    if not ctx:
        return []

    opportunities = _score_from_cache(ctx)

    if can_write_cloud and opportunities:
        has_ver = hasattr(SignalCallLog, "model_version")
        has_metrics = hasattr(SignalCallLog, "metrics")

        async with async_session_factory() as session:
            active_pending_ids = set(
                (
                    await session.execute(
                        select(SignalCallLog.item_id).where(SignalCallLog.status == "pending")
                    )
                ).scalars().all()
            )
            added = 0
            for op in opportunities[:15]:
                if op["item_id"] in active_pending_ids:
                    continue
                call_kwargs = dict(
                    snapshot_id=ctx.snapshot_id,
                    called_at=ctx.snap_ts or datetime.now(timezone.utc),
                    item_id=op["item_id"],
                    tag=op["tag"],
                    reason=op["reason"],
                    buy_price_copper=op["buy"],
                    predicted_sell_copper=op["sell"],
                    market_value_copper=op["market_value"],
                    local_median_copper=op["local_median"],
                    predicted_roi=op["roi"],
                    z_score=op["z_score"],
                    recommended_qty=op["qty"],
                    predicted_profit_copper=op["total_profit"],
                    status="pending",
                    max_observed_copper=op["buy"],
                )
                if has_ver:
                    call_kwargs["model_version"] = MODEL_VERSION
                if has_metrics:
                    call_kwargs["metrics"] = {
                        "p_fill": op["p_fill"],
                        "ev_roi": op["ev_roi"],
                        "bounces": op["bounces"],
                        "peak_hour_utc": op["peak_hour_utc"],
                        "list_hours": op["list_hours"],
                        "cheap_qty": op["cheap_qty"],
                        "gap_qty": op["gap_qty"],
                        "avg_sold_hr": op["avg_sold_hr"],
                    }
                session.add(SignalCallLog(**call_kwargs))
                active_pending_ids.add(op["item_id"])
                added += 1
            if added > 0:
                await session.commit()

    return opportunities


async def get_call_history_overview(limit: int = 50, refresh: bool = False) -> dict:
    async with async_session_factory() as session:
        if refresh and not _is_market_read_only():
            await refresh_call_outcomes(session)

        rows = list(
            (
                await session.execute(
                    select(SignalCallLog)
                    .order_by(desc(SignalCallLog.called_at), desc(SignalCallLog.id))
                    .limit(limit)
                )
            ).scalars().all()
        )
        status_rows = (
            await session.execute(
                select(SignalCallLog.status, func.count(SignalCallLog.id)).group_by(SignalCallLog.status)
            )
        ).all()

        row_item_ids = {c.item_id for c in rows}
        if _cache.items_map and row_item_ids.issubset(_cache.items_map.keys()):
            meta_lookup = {iid: _cache.items_map[iid] for iid in row_item_ids}
        elif row_item_ids:
            meta_lookup = {
                r.item_id: r
                for r in (
                    await session.execute(select(ItemMeta).where(ItemMeta.item_id.in_(row_item_ids)))
                ).scalars().all()
            }
        else:
            meta_lookup = {}

    counts = {st: int(cnt) for st, cnt in status_rows}
    hits = counts.get("hit", 0)
    partials = counts.get("partial", 0)
    missed = counts.get("missed", 0)
    pending = counts.get("pending", 0)
    total = sum(counts.values())

    resolved = hits + partials + missed
    hit_rate = round((hits / resolved) * 100.0, 1) if resolved > 0 else 0.0
    win_or_partial_rate = round(((hits + partials) / resolved) * 100.0, 1) if resolved > 0 else 0.0

    history_items = []
    for c in rows:
        meta = meta_lookup.get(c.item_id)
        ts = c.called_at if c.called_at.tzinfo else c.called_at.replace(tzinfo=timezone.utc)
        history_items.append({
            "id": c.id,
            "called_at": int(ts.timestamp()),
            "item_id": c.item_id,
            "name": meta.name if meta else f"Item_{c.item_id}",
            "icon": (meta.icon_name if meta else None) or "inv_misc_questionmark",
            "tag": c.tag,
            "reason": c.reason,
            "buy_price": c.buy_price_copper,
            "predicted_sell": c.predicted_sell_copper,
            "market_value": c.market_value_copper,
            "predicted_roi": c.predicted_roi,
            "max_observed": c.max_observed_copper,
            "actual_roi": c.actual_roi,
            "qty": c.recommended_qty,
            "predicted_profit": c.predicted_profit_copper,
            "status": c.status,
        })

    return {
        "summary": {
            "total": total,
            "hits": hits,
            "partials": partials,
            "missed": missed,
            "pending": pending,
            "hit_rate": hit_rate,
            "win_or_partial_rate": win_or_partial_rate,
        },
        "items": history_items,
    }


async def build_ai_market_digest() -> str:
    ctx = await _load_snapshot_context()
    if not ctx:
        return "Нет доступных срезов Blizzard в базе данных."

    ops = _score_from_cache(ctx)
    radar = _build_raid_radar_from_cache(ctx)
    calls_data = await get_call_history_overview(limit=15, refresh=False)

    snap_ts = ctx.snap_ts or datetime.now(timezone.utc)
    day_mode = "ВЫХОДНЫЕ" if snap_ts.weekday() >= 5 else "БУДНИ"

    liquid_candidates = []
    for tick in ctx.latest_ticks.values():
        meta = ctx.items_map.get(tick.item_id)
        if not meta or meta.is_hidden or tick.item_id in VENDOR_JUNK_ITEM_IDS:
            continue
        series = ctx.hist_by_item.get(tick.item_id, [])[-REGIME_WINDOW_TICKS:]
        prices = [x[0] for x in series] + [tick.entry_price]
        qtys = [x[1] for x in series]
        med, _, _, p75 = _compute_robust_stats(prices)
        churn = _compute_qty_churn(qtys)
        lo, hi = min(prices), max(prices)
        if churn < 1.0 or (lo > 0 and p75 <= lo * 1.06):
            continue
        liquid_candidates.append((tick, meta, med, p75, lo, hi, churn))

    liquid_candidates.sort(key=lambda x: (x[0].num_auctions, x[6]), reverse=True)
    market_snapshot_lines = []
    for tick, meta, med, p75, lo, hi, churn in liquid_candidates[:25]:
        extra = f", выкуп: {tick.est_sold_qty}/ч" if tick.est_sold_qty > 0 else ""
        market_snapshot_lines.append(
            f"- {meta.name} (ID:{meta.item_id}) | Вход: {format_wow_money(tick.entry_price)} | "
            f"Стена: {format_wow_money(tick.wall_price)} | "
            f"Мед: {format_wow_money(int(med))} [p75 {format_wow_money(int(p75))} | {format_wow_money(lo)}..{format_wow_money(hi)}] | "
            f"Лотов: {tick.num_auctions} (шт: {tick.quantity}, churn: {churn:.0f}/ч{extra})"
        )

    async with personal_session_factory() as psession:
        open_positions = list(
            (
                await psession.execute(
                    select(TradePosition).where(TradePosition.status.in_(["holding", "listed"]))
                )
            ).scalars().all()
        )

    lines = [
        f"### СРЕЗ РЫНКА WOW FOREVER EU (PvP Alliance, house_{settings.BNET_HOUSE_ID}) | Модель {MODEL_VERSION} | Чистых срезов: {ctx.runs_count}",
        f"Последний скан Blizzard API (UTC): {snap_ts.isoformat()} ({day_mode})",
        "",
        "#### 1. ТЕКУЩИЕ АКТИВНЫЕ КОЛЛЫ СИСТЕМЫ (ПО E[PnL], ЧАСАМ АКТИВА И ГЕОМЕТРИИ СТАКАНА):",
    ]
    if ops:
        for o in ops[:12]:
            lines.append(
                f"- [{o['tag']}] {o['name']}: вход <= {format_wow_money(o['buy'])}, цель {format_wow_money(o['sell'])} "
                f"(ROI +{o['roi']}%, E[ROI] +{o['ev_roi']}%, пакет {o['qty']} шт., E[профит] ~{format_wow_money(o['total_ev'])}) — {o['reason']}"
            )
    elif ctx.runs_count < MIN_HIST_TICKS_FLIP:
        lines.append(
            f"- Идёт прогрев чистой базы house_{settings.BNET_HOUSE_ID}: "
            f"{ctx.runs_count}/{MIN_HIST_TICKS_FLIP} срезов (коллы откроются автоматически)."
        )
    else:
        lines.append("- Подтверждённых цикличных перекосов с положительным E[PnL] на текущем срезе нет.")

    lines.append("")
    lines.append("#### 2. ТОП-25 ЖИВЫХ ЛИКВИДНЫХ ПОЗИЦИЙ (ЧИСТЫЕ ДАННЫЕ BLIZZARD):")
    lines.extend(market_snapshot_lines or ["- Нет данных"])

    lines.append("")
    s = calls_data["summary"]
    lines.append(
        f"#### 3. ИСТОРИЯ КОЛЛОВ {MODEL_VERSION} (Всего: {s['total']}, Цель взята: {s['hits']}, Частичный отскок: {s['partials']}, "
        f"Мимо: {s['missed']}, В ожидании: {s['pending']}, Hit Rate: {s['hit_rate']}%):"
    )
    for c in calls_data["items"][:12]:
        dt_str = datetime.fromtimestamp(c["called_at"], tz=timezone.utc).strftime("%d.%m %H:%M UTC")
        lines.append(
            f"- [{dt_str}] {c['name']} ({c['tag']}): колл по {format_wow_money(c['buy_price'])} -> "
            f"предикт {format_wow_money(c['predicted_sell'])} (+{c['predicted_roi']}%) | "
            f"Макс. факт 24ч: {format_wow_money(c['max_observed'])} ({c['actual_roi']:+.1f}%) | Статус: {c['status']}"
        )

    lines.append("")
    lines.append("#### 4. ТЕКУЩИЙ ПОРТФЕЛЬ (С ЖИВЫМ СПОТОМ):")
    if open_positions:
        for p in open_positions:
            m = ctx.items_map.get(p.item_id)
            nm = m.name if m else f"Item_{p.item_id}"
            live_tick = ctx.latest_ticks.get(p.item_id)
            if live_tick and p.buy_price_copper > 0:
                net_now = int(live_tick.entry_price * 0.95) - p.buy_price_copper
                pnl_pct = (net_now / p.buy_price_copper) * 100.0
                spot_str = f" | Спот: {format_wow_money(live_tick.entry_price)} (net {pnl_pct:+.1f}%)"
            else:
                spot_str = ""
            lines.append(
                f"- {nm} x{p.qty} ({p.status}): куплено по {format_wow_money(p.buy_price_copper)}, "
                f"выставлено по {format_wow_money(p.list_price_copper)}{spot_str}"
            )
    else:
        lines.append("- Открытых позиций в портфеле сейчас нет (банкролл свободен).")

    lines.append("")
    lines.append(
        f"#### 5. ИНВЕСТ-РАДАР К РЕЙДАМ 9 ДЕКАБРЯ (До старта: {radar['days_to_raids']} дн. | {radar['phase_hint']}):"
    )
    if radar["parities"]:
        lines.append("Кросс-паритеты крафта и трансмутов:")
        for line in radar["parities"]:
            lines.append(f"  * {line}")
        lines.append("")

    for it in radar["items"]:
        if it["status_code"] == "MISSING":
            lines.append(f"- [{it['tier']}] [{it['status_label']}] {it['name']} (ID:{it['item_id']}) [{it['note']}]")
            continue
        sup = f", {it['supply_note']}" if it["supply_note"] else ""
        clk = f", дно ~{it['best_buy_hour_utc']:02d} UTC" if it["best_buy_hour_utc"] >= 0 else ""
        lines.append(
            f"- [{it['tier']}] [{it['status_label']}] {it['name']} (ID:{it['item_id']}) | "
            f"Сейчас: {format_wow_money(it['price'])} (ранг {it['rank_pct']:.0f}%, {-it['disc_med_pct']:+.0f}% к мед.) | "
            f"Мед: {format_wow_money(it['median'])} [{format_wow_money(it['lo'])}..{format_wow_money(it['hi'])}] | "
            f"Лотов: {it['num_auctions']} (шт: {it['qty']}{sup}{clk})"
        )

    return "\n".join(lines)


async def evaluate_opportunities(snapshot_id: int) -> None:
    opportunities = await get_scored_opportunities(snapshot_id, record_calls=True)
    if not opportunities:
        logger.info("Анализатор v4: подтверждённых цикличных перекосов с положительным E[PnL] нет.")
        return

    logger.info("=== УМНЫЙ СКОРИНГ СДЕЛОК v4 (БАНКРОЛЛ %.1f GOLD) ===", settings.BANKROLL_GOLD)
    for op in opportunities[:8]:
        logger.info(
            f"  [{op['tag']:<12}] {op['name']:<22} | Купить <= {format_wow_money(op['buy']):<10} | "
            f"Цель: {format_wow_money(op['sell']):<10} | ROI: {op['roi']:>5.1f}% (E:{op['ev_roi']:>4.1f}%) | {op['reason']}"
        )