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

# Окно анализа: 72 часовых среза (~3 полных суточных цикла)
HIST_WINDOW_SNAPSHOTS = 72
MIN_HIST_TICKS_FLIP = 12
MIN_AUCTIONS_FLIP = 6
MIN_TOTAL_QTY_FLIP = 15
MIN_QTY_CHURN_FLIP = 2.0

# Кэш проверки последнего snapshot_id (секунд), чтобы не спамить Neon на каждый клик UI
LATEST_CHECK_TTL_SEC = 30.0

# Таймфреймы EU PvP Alliance (UTC)
PRIME_DAY_HOURS_UTC = set(range(9, 21))        # 09:00..20:00 UTC (12:00..23:00 MSK) — объективный дневной рынок
EVENING_DUMP_HOURS_UTC = set(range(15, 21))    # 15:00..20:00 UTC (18:00..23:00 MSK) — прайм-тайм слива фарма
THIN_BOOK_HOURS_UTC = {22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8}  # Ночь/утро — тонкий стакан (хорошо продавать, опасно брать)

# Дата открытия рейдов WoW: Forever (Barrow Deeps 10, Hyjal Summit 20, Onyxia 40)
RAID_LAUNCH_UTC = datetime(2026, 12, 9, 18, 0, tzinfo=timezone.utc)

# Чёрный список дешёвых NPC-товаров (колбы, обёртки, нитки, приправы, красители)
VENDOR_JUNK_ITEM_IDS: set[int] = {
    2320,  # Coarse Thread
    2321,  # Fine Thread
    2324,  # Bleach
    2325,  # Black Dye
    2604,  # Red Dye
    2605,  # Green Dye
    2678,  # Mild Spices
    2880,  # Weak Flux
    3371,  # Empty Vial
    3372,  # Leaded Vial
    3466,  # Strong Flux
    3857,  # Coal
    4289,  # Salt
    4291,  # Silken Thread
    4340,  # Gray Dye
    4341,  # Yellow Dye
    4342,  # Purple Dye
    5042,  # Оберточная бумага с голубыми лентами
    5048,  # Оберточная бумага с красными лентами
    6260,  # Blue Dye
    6261,  # Orange Dye
    8343,  # Heavy Silken Thread
    8925,  # Crystal Vial
    14341, # Rune Thread
    18256, # Imbued Vial
}

# Долгосрочный радар инвестиций под открытие рейдов 9 декабря 2026: (item_id, заметка, тир)
RAID_WATCHLIST: list[tuple[int, str, str]] = [
    # T1: Новые эндгейм-травы Forever под 4 зональные фласки Хиджала / Барроу Дипс
    (248822, "Death Lotus [Forever фласки]", "T1"),
    (248821, "Demonsage [Forever фласки]", "T1"),
    (248823, "Marefoil [Forever фласки/крафт]", "T1"),
    # T1: Ониксия (40) — единственный подтверждённый резист-чек
    (7068, "Elemental Fire [GFPP Ониксия]", "T1"),
    (13457, "Greater Fire Protection Potion", "T1"),
    # T1: Классические травы под новые боевые поты (Frenzy/Spellblasting) и эликсиры Forever
    (13463, "Dreamfoil [GFPP/Эликсиры/Фласки]", "T1"),
    (8846, "Gromsblood [Титан/Grizzly/Ferocity]", "T1"),
    (13466, "Plaguebloom [Frenzy/Mongoose]", "T1"),
    (8839, "Blindweed [Frenzy/Grizzly]", "T1"),
    (13465, "Mountain Silversage [Mongoose/Mana]", "T1"),
    (8838, "Sungrass [Fortitude/Nature Power]", "T1"),
    (13464, "Golden Sansam [Sages/Nature Power]", "T1"),
    (13452, "Elixir of the Mongoose [Готовый]", "T1"),
    (13444, "Major Mana Potion [Готовый]", "T1"),
    # T2: Классические фласки (Ониксия) и трансмуты (Arcanite / Legionite общий КД)
    (13468, "Black Lotus [Титан/СПД Ониксия]", "T2"),
    (13422, "Stonescale Eel [Угорь -> Масло]", "T2"),
    (13423, "Stonescale Oil [Титан/Защита]", "T2"),
    (8845, "Ghost Mushroom [Ferocity/Shadow]", "T2"),
    (12363, "Arcane Crystal [Arcanite CD]", "T2"),
    (12360, "Arcanite Bar [Готовый слиток]", "T2"),
    (248815, "Heavy Thorium Ore [Legionite CD]", "T2"),
    (248819, "Fel Crystal [Legionite CD]", "T2"),
    (251290, "Legionite Bar [Пре-рейд BiS]", "T2"),
    # T3: Спекулятивно под Barrow Deeps (Nature/Shadow) — малая аллокация
    (7067, "Elemental Earth [GNPP спекуляция]", "T3"),
    (3824, "Shadow Oil [GSPP спекуляция]", "T3"),
    (3369, "Grave Moss [Сырьё Shadow Oil]", "T3"),
]

# Совместимость со старым именем константы, если импортируется из других модулей
RAID_WATCHLIST_IDS: list[tuple[int, str]] = [(iid, note) for iid, note, _ in RAID_WATCHLIST]


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
    market_value: int
    quantity: int
    num_auctions: int
    hour_of_day: int
    day_of_week: int


@dataclass
class _SnapshotCache:
    snapshot_id: int = -1
    snap_ts: datetime | None = None
    runs_count: int = 0
    checked_at: float = 0.0
    items_map: dict[int, _MetaLite] = field(default_factory=dict)
    latest_ticks: dict[int, _TickLite] = field(default_factory=dict)
    # item_id -> [(entry_price, qty, num_auctions, hour_of_day, day_of_week), ...] строго ДО snap_ts
    hist_by_item: dict[int, list[tuple[int, int, int, int, int]]] = field(default_factory=dict)
    scored_cache_key: tuple[int, float, float] | None = None
    scored_ops: list[dict] = field(default_factory=list)
    raid_radar_cache: dict | None = None


_cache = _SnapshotCache()
_cache_lock = asyncio.Lock()


def invalidate_analyzer_cache() -> None:
    """Сбрасывает in-memory кэш (вызывать при ручной правке скрытых позиций или после нового скана)."""
    _cache.snapshot_id = -1
    _cache.checked_at = 0.0
    _cache.scored_cache_key = None
    _cache.raid_radar_cache = None


def _is_market_read_only() -> bool:
    if bool(getattr(settings, "MARKET_READ_ONLY", False)):
        return True
    return os.getenv("MARKET_READ_ONLY", "").strip().lower() in {"1", "true", "yes"}


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
    """Возвращает позицию текущей цены в историческом распределении (0..100%)."""
    if not sorted_vals:
        return 50.0
    n = len(sorted_vals)
    below = sum(1 for x in sorted_vals if x < value)
    equal = sum(1 for x in sorted_vals if x == value)
    return round(((below + 0.5 * equal) / n) * 100.0, 1)


def _compute_robust_stats(prices: list[int]) -> tuple[float, float, float, float]:
    """Возвращает (median, robust_std, p25, p75) устойчиво к единичным выбросам."""
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
    """Медианное часовое движение объёма стакана (не реагирует на разовые перевыставы китов)."""
    if len(quantities) < 2:
        return 0.0
    deltas = sorted(abs(quantities[i] - quantities[i - 1]) for i in range(1, len(quantities)))
    return _percentile(deltas, 0.50)


def _describe_market_window(ts_utc: datetime) -> tuple[str, str]:
    """Возвращает (код_режима, человекочитаемое описание таймфрейма EU)."""
    h = ts_utc.hour
    is_weekend = ts_utc.weekday() >= 5
    day_tag = "ВЫХОДНЫЕ" if is_weekend else "БУДНИ"
    if h in EVENING_DUMP_HOURS_UTC:
        return "dump", f"{day_tag} • ВЕЧЕРНИЙ СЛИВ (15-20 UTC / прайм-закуп)"
    if h in THIN_BOOK_HOURS_UTC:
        return "thin", f"{day_tag} • НОЧЬ/УТРО (тонкий стакан — время продаж, осторожный вход)"
    return "day", f"{day_tag} • ДНЕВНАЯ СЕССИЯ (09-14 UTC / живой спрос)"


def _analyze_price_regime(
    series: list[tuple[int, int, int, int, int]],
) -> dict:
    """
    Анализирует структуру графика вместо слепой медианы:
    1. Убивает медленно умирающие активы (structural downtrend).
    2. Ищет объективную 3-часовую дневную полку (стирает разовые 1-2ч всплески и ночной шум).
    3. Считает подтверждённые циклы возврата «долина -> полка» (маятник).
    4. Измеряет суточную корреляцию (вечерний слив vs утренний/дневной пик).
    """
    prices = [x[0] for x in series]
    n = len(prices)
    if n < MIN_HIST_TICKS_FLIP:
        return {"tradable": False, "reason": "short_history"}

    # 1. Фильтр медленно умирающих графиков (старая часть окна vs свежие 16 часов)
    split = max(4, n - 16)
    old_med = _percentile(sorted(prices[:split]), 0.50)
    recent_prices = prices[split:]
    recent_med = _percentile(sorted(recent_prices), 0.50)

    is_bleeding = (
        old_med > 0
        and recent_med < old_med * 0.85
        and max(recent_prices) < old_med * 0.94
    )
    if is_bleeding:
        return {"tradable": False, "reason": "structural_downtrend"}

    # 2. Объективный дневной уровень: скользящий минимум по 3 последовательным дневным тикам
    day_prices = [x[0] for x in series if x[3] in PRIME_DAY_HOURS_UTC]
    if len(day_prices) < 6:
        return {"tradable": False, "reason": "low_daytime_samples"}

    sustained_day_levels = [min(day_prices[i : i + 3]) for i in range(len(day_prices) - 2)]
    day_plateau = _percentile(sorted(sustained_day_levels), 0.80)
    day_low = _percentile(sorted(day_prices), 0.25)

    if day_low <= 0 or day_plateau <= day_low * 1.10:
        return {"tradable": False, "reason": "flat_daytime_spread"}

    # 3. Счётчик подтверждённых циклов возврата (долина -> дневная полка)
    all_low_p25 = _percentile(sorted(prices), 0.25)
    low_thr = max(all_low_p25, day_low) * 1.06
    high_thr = day_plateau * 0.94

    state = 0  # -1 в долине, +1 на полке
    bounces = 0
    for p in prices:
        if p <= low_thr:
            state = -1
        elif p >= high_thr:
            if state == -1:
                bounces += 1
            state = 1

    min_bounces = 2 if n >= 28 else 1
    if bounces < min_bounces:
        return {"tradable": False, "reason": f"no_pattern_bounces={bounces}"}

    # 4. Внутридневная корреляция: насколько утро/день дороже вечернего слива
    dump_prices = [x[0] for x in series if x[3] in EVENING_DUMP_HOURS_UTC]
    non_dump_prices = [x[0] for x in series if x[3] not in EVENING_DUMP_HOURS_UTC]
    diurnal_edge_pct = 0.0
    if len(dump_prices) >= 3 and len(non_dump_prices) >= 3:
        dump_med = _percentile(sorted(dump_prices), 0.50)
        peak_med = _percentile(sorted(non_dump_prices), 0.65)
        if dump_med > 0:
            diurnal_edge_pct = round(((peak_med / dump_med) - 1.0) * 100.0, 1)

    return {
        "tradable": True,
        "day_plateau": day_plateau,
        "day_low": day_low,
        "recent_med": recent_med,
        "bounces": bounces,
        "diurnal_edge_pct": diurnal_edge_pct,
        "spread_pct": round((day_plateau / day_low - 1.0) * 100.0, 1),
    }


async def _load_snapshot_context(snapshot_id: int | None = None, force_reload: bool = False) -> _SnapshotCache | None:
    """
    Лениво подгружает и кэширует срез + 72ч историю из Neon.
    Защищено asyncio.Lock от одновременной перегрузки при смене часа.
    """
    now_mono = time.monotonic()

    # Быстрый путь без блокировки и без SQL, если кэш свежий
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

        async with async_session_factory() as session:
            if snapshot_id is None:
                latest_run = (
                    await session.execute(select(SnapshotRun).order_by(desc(SnapshotRun.snapshot_ts)).limit(1))
                ).scalar_one_or_none()
                if not latest_run:
                    return None
                target_run = latest_run
            else:
                target_run = await session.get(SnapshotRun, snapshot_id)
                if not target_run:
                    return None

            # Если ID среза не изменился — тяжёлые таблицы не трогаем
            if not force_reload and target_run.id == _cache.snapshot_id:
                _cache.checked_at = now_mono
                return _cache

            raw_snap_ts = target_run.snapshot_ts
            snap_ts_utc = raw_snap_ts if raw_snap_ts.tzinfo else raw_snap_ts.replace(tzinfo=timezone.utc)

            runs_count = int((await session.execute(select(func.count(SnapshotRun.id)))).scalar() or 0)

            # 1. Метаданные предметов (только нужные поля, без ORM-объектов в кэше)
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

            # 2. Текущий срез (1 запрос без тяжёлого IN по тысячам item_id)
            cur_rows = (
                await session.execute(
                    select(
                        MarketTick.item_id,
                        MarketTick.min_buyout,
                        MarketTick.entry_price,
                        MarketTick.wall_price,
                        MarketTick.market_value,
                        MarketTick.quantity,
                        MarketTick.num_auctions,
                        MarketTick.hour_of_day,
                        MarketTick.day_of_week,
                    ).where(MarketTick.snapshot_id == target_run.id)
                )
            ).all()
            latest_ticks = {
                int(iid): _TickLite(
                    item_id=int(iid),
                    min_buyout=int(mb or ep or 0),
                    entry_price=int(ep or 0),
                    wall_price=int(wp or 0),
                    market_value=int(mv or 0),
                    quantity=int(q or 0),
                    num_auctions=int(na or 0),
                    hour_of_day=int(hod if hod is not None else snap_ts_utc.hour),
                    day_of_week=int(dow if dow is not None else snap_ts_utc.weekday()),
                )
                for iid, mb, ep, wp, mv, q, na, hod, dow in cur_rows
                if int(ep or 0) > 0
            }

            # 3. История строго ДО текущего среза (72 последних SnapshotRun -> 1 батч-запрос без item_id IN)
            hist_run_ids = list(
                (
                    await session.execute(
                        select(SnapshotRun.id)
                        .where(SnapshotRun.snapshot_ts < raw_snap_ts)
                        .order_by(desc(SnapshotRun.snapshot_ts))
                        .limit(HIST_WINDOW_SNAPSHOTS)
                    )
                ).scalars().all()
            )

            hist_by_item: dict[int, list[tuple[int, int, int, int, int]]] = defaultdict(list)
            if hist_run_ids:
                h_rows = (
                    await session.execute(
                        select(
                            MarketTick.item_id,
                            MarketTick.entry_price,
                            MarketTick.quantity,
                            MarketTick.num_auctions,
                            MarketTick.hour_of_day,
                            MarketTick.day_of_week,
                        )
                        .where(
                            MarketTick.snapshot_id.in_(hist_run_ids),
                            MarketTick.entry_price > 0,
                        )
                        .order_by(asc(MarketTick.snapshot_ts))
                    )
                ).all()
                for iid, ep, q, na, hod, dow in h_rows:
                    iid_int = int(iid)
                    if iid_int in latest_ticks or any(iid_int == w[0] for w in RAID_WATCHLIST):
                        hist_by_item[iid_int].append(
                            (int(ep), int(q or 0), int(na or 0), int(hod or 0), int(dow or 0))
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
                "Кэш анализатора обновлён: snapshot_id=%d, позиций=%d, истории=%d",
                target_run.id,
                len(latest_ticks),
                len(hist_by_item),
            )
            return _cache


async def refresh_call_outcomes(session) -> None:
    """
    Батчевая проверка открытых коллов за 24ч (1 SQL-запрос вместо N+1).
    Вызывается только коллектором в режиме записи.
    """
    if _is_market_read_only():
        return

    pending_calls = list(
        (await session.execute(select(SignalCallLog).where(SignalCallLog.status == "pending"))).scalars().all()
    )
    if not pending_calls:
        return

    now = datetime.now(timezone.utc)
    updated = False

    # Сохраняем нативную tz-схему из БД для безопасного сравнения в asyncpg
    raw_min_ts = min(c.called_at for c in pending_calls)
    pending_item_ids = {c.item_id for c in pending_calls}

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

        if call.tag == "НИЖЕ ВЕНДОРА":
            call.status = "missed"  # Ветка вендора признана недостоверной
            call.resolved_at = now
            updated = True
            continue

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
    """Чистая CPU-математика скоринга по закэшированному срезу и 72ч истории (без единого SQL)."""
    bankroll_gold = float(settings.BANKROLL_GOLD)
    max_share = float(settings.MAX_SHARE_PER_ITEM)
    cache_key = (ctx.snapshot_id, bankroll_gold, max_share)
    if ctx.scored_cache_key == cache_key and ctx.scored_ops is not None:
        return ctx.scored_ops

    max_budget_per_item = int(bankroll_gold * 10000 * max_share)
    snap_ts = ctx.snap_ts or datetime.now(timezone.utc)
    window_code, _ = _describe_market_window(snap_ts)
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

        local_prices = [x[0] for x in hist_series]
        local_qtys = [x[1] for x in hist_series]
        local_aucs = [x[2] for x in hist_series]

        qty_churn = _compute_qty_churn(local_qtys)
        median_aucs = _percentile(sorted(local_aucs), 0.50) if local_aucs else float(num_auctions)
        if qty_churn < MIN_QTY_CHURN_FLIP and median_aucs < 12:
            continue

        # Режимный анализ графика: убираем умирающие тренды, одиночные всплески и пустые графики без циклов
        regime = _analyze_price_regime(hist_series)
        if not regime["tradable"]:
            continue

        median, r_std, _, p75 = _compute_robust_stats(local_prices)
        day_plateau = regime["day_plateau"]
        bounces = regime["bounces"]
        diurnal_edge_pct = regime["diurnal_edge_pct"]

        # Опорная цель — подтверждённая 3-часовая дневная полка (с андеркатом 5%)
        ref_price = day_plateau
        if buy_price >= ref_price * 0.90:
            continue

        z_score = (ref_price - buy_price) / r_std
        disc_plateau_pct = (1.0 - buy_price / ref_price) * 100.0
        target_sell = int(ref_price * 0.95)

        # Микроструктура стены стакана
        wall_penalty = 1.0
        wall_note = ""
        if wall_price > 0:
            if wall_price > int(buy_price * 1.04):
                capped_by_wall = int(wall_price * 0.98)
                if capped_by_wall < target_sell:
                    target_sell = capped_by_wall
                    wall_note = f"под стеной {format_wow_money(wall_price)}"
            else:
                # Стена прямо на дне в тонком стакане = массовый слив, не ловим нож
                if num_auctions < 65:
                    continue
                wall_penalty = 0.35
                wall_note = f"стена на дне {format_wow_money(wall_price)}"

        # Не задираем цель выше p75 всего 72ч окна, если только дневная полка не выше за счёт ночных провалов
        if p75 > 0 and day_plateau <= p75 * 1.15:
            target_sell = min(target_sell, int(max(p75, day_plateau) * 0.97))

        if target_sell <= buy_price:
            continue

        # Контроль риска залога (урок из реальной истории сделок)
        vendor_price = meta.vendor_sell_price
        dep_24h = calculate_deposit_copper(vendor_price, 1, hours=24)
        dep_8h = calculate_deposit_copper(vendor_price, 1, hours=8)
        dep_24h_risk_pct = (dep_24h / buy_price) * 100.0 if buy_price > 0 else 0.0

        # Дешёвый залог (<=5% от входа) — безопасно вешать на 24ч; тяжёлый вендор — строго на 8ч
        rec_list_hours = 24 if dep_24h_risk_pct <= 5.0 else 8
        deposit_unit = dep_24h if rec_list_hours == 24 else dep_8h

        net_profit_unit = int(target_sell * 0.95) - buy_price - deposit_unit
        if net_profit_unit <= 0:
            continue

        roi_pct = (net_profit_unit / buy_price) * 100.0

        # Консервативный размер пакета
        max_by_budget = max_budget_per_item // buy_price
        est_cheap_depth = max(1, min(total_qty // 4, meta.max_stack))
        affordable_qty = min(meta.max_stack, max_by_budget, est_cheap_depth)
        if affordable_qty <= 0:
            continue

        # Динамические пороги по таймфрейму прайма и выходным
        is_highly_liquid = num_auctions >= 25 and qty_churn >= 15.0
        if window_code == "thin":
            # Ночь/утро: стакан тонкий, требуем экстремальный дисконт
            min_roi_required = 24.0
            min_z_required = 1.60
        elif window_code == "dump" or is_highly_liquid:
            # Вечерний слив: основное окно закупа под ночной/утренний отскок
            min_roi_required = 14.0 if not is_weekend else 16.0
            min_z_required = 1.05
        else:
            min_roi_required = 17.0
            min_z_required = 1.25

        if roi_pct < min_roi_required or z_score < min_z_required:
            continue

        total_profit = net_profit_unit * affordable_qty
        if total_profit < 1500:  # Отсекаем шум дешевле 15s
            continue

        # Тег сигнала
        if bounces >= 3 and diurnal_edge_pct >= 12.0:
            tag = "МАЯТНИК"
        elif window_code == "dump" and z_score >= 1.8 and disc_plateau_pct >= 20.0:
            tag = "ПРАЙМ-ДНО"
        elif z_score >= 2.0 and disc_plateau_pct >= 22.0:
            tag = "СИЛЬНОЕ ДНО"
        else:
            tag = "ПРОСАДКА"

        reason_parts = [
            f"-{disc_plateau_pct:.0f}% от дн.полки ({format_wow_money(int(day_plateau))})",
            f"циклов {bounces}",
            f"Z={z_score:.1f}",
            f"лотов {num_auctions}",
            f"churn {qty_churn:.0f}/ч",
            f"слот {rec_list_hours}ч",
        ]
        if diurnal_edge_pct >= 10.0:
            reason_parts.append(f"суточн. +{diurnal_edge_pct:.0f}%")
        if wall_note:
            reason_parts.append(wall_note)
        reason_str = " • ".join(reason_parts)

        # ИСПРАВЛЕНО: мультипликативный скоринг (глубина * оборот * циклы * стена * риск залога)
        depth_mult = math.log1p(num_auctions) / math.log1p(40.0)
        churn_mult = math.log1p(qty_churn) / math.log1p(50.0)
        liquidity_mult = max(0.25, 0.5 * depth_mult + 0.5 * churn_mult)
        turnover_penalty = min(1.0, max(0.35, (qty_churn * 4.0) / max(affordable_qty, 1)))
        cycle_mult = min(1.6, 0.85 + 0.25 * bounces)
        diurnal_mult = 1.25 if (window_code == "dump" and diurnal_edge_pct >= 10.0) else 1.0
        deposit_penalty = 0.70 if dep_24h_risk_pct > 12.0 else 1.0

        score = (
            total_profit
            * liquidity_mult
            * turnover_penalty
            * wall_penalty
            * cycle_mult
            * diurnal_mult
            * deposit_penalty
            * min(2.5, max(0.8, z_score))
        )

        opportunities.append({
            "item_id": iid,
            "name": meta.name,
            "category": meta.category,
            "tag": tag,
            "reason": reason_str,
            "buy": buy_price,
            "sell": target_sell,
            "market_value": tick.market_value,
            "local_median": int(median),
            "day_plateau": int(day_plateau),
            "wall_price": wall_price,
            "vendor": vendor_price,
            "roi": round(roi_pct, 1),
            "z_score": round(z_score, 2),
            "bounces": bounces,
            "diurnal_edge_pct": diurnal_edge_pct,
            "list_hours": rec_list_hours,
            "deposit_risk_pct": round(dep_24h_risk_pct, 1),
            "qty": affordable_qty,
            "total_profit": total_profit,
            "daily_sales": round(qty_churn * 24.0, 1),
            "num_auctions": num_auctions,
            "qty_churn": round(qty_churn, 1),
            "score": round(score, 1),
        })

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    ctx.scored_cache_key = cache_key
    ctx.scored_ops = opportunities
    return opportunities


def _build_raid_radar_from_cache(ctx: _SnapshotCache) -> dict:
    """
    Отдельный аналитический контур долгосрочного инвестирования под рейды 9 декабря:
    ранжирует позиции по степени "созревания" к набору + считает паритет крафта и трансмутов.
    """
    if ctx.raid_radar_cache is not None:
        return ctx.raid_radar_cache

    snap_ts = ctx.snap_ts or datetime.now(timezone.utc)
    days_left = max(0, (RAID_LAUNCH_UTC - snap_ts).days)
    if days_left > 10:
        phase_hint = "ФАЗА НАБОРА: спокойно выкупаем откаты ниже медианы и на вечерних сливах."
    elif days_left >= 2:
        phase_hint = "ПРЕДРЕЙДОВАЯ РАЗГРУЗКА: фиксируем 70-80% позиций траншами до вечера 8 декабря."
    else:
        phase_hint = "РЕЛИЗ РЕЙДОВ: пик волатильности, не держим неликвид до второго дня КД."

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
                "supply_note": "",
            })
            continue

        series = ctx.hist_by_item.get(iid, [])
        prices = [x[0] for x in series] + [tick.entry_price]
        qtys = [x[1] for x in series]
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

        # Классификация точки входа под инвест
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
            "supply_note": supply_note,
        })

    # Сортируем: сначала созревшие T1/T2, потом набор, потом перегретые
    tier_rank = {"T1": 0, "T2": 1, "T3": 2}
    items_report.sort(key=lambda x: (status_order[x["status_code"]], tier_rank[x["tier"]], x["rank_pct"]))

    # Кросс-активные арбитражи (сырьё vs готовый расходник / трансмут)
    def _p(iid: int) -> int:
        t = ctx.latest_ticks.get(iid)
        return t.entry_price if t else 0

    parities: list[str] = []

    # 1. Масло каменного угря vs 2 Угря
    eel_p, oil_p = _p(13422), _p(13423)
    if eel_p > 0 and oil_p > 0:
        mats = eel_p * 2
        diff_pct = (oil_p / mats - 1.0) * 100.0
        verdict = "БРАТЬ МАСЛО (ниже сырья!)" if oil_p < mats else "масло дороже рыбы"
        parities.append(
            f"Масло угря {format_wow_money(oil_p)} vs 2x Угорь {format_wow_money(mats)} ({diff_pct:+.0f}%) -> {verdict}"
        )

    # 2. GFPP vs Первородный огонь + Снолист
    fire_p, dream_p, gfpp_p = _p(7068), _p(13463), _p(13457)
    if fire_p > 0 and dream_p > 0 and gfpp_p > 0:
        mats = fire_p + dream_p + 500  # +5s колба
        diff_pct = (gfpp_p / mats - 1.0) * 100.0
        verdict = "выгоднее брать готовый GFPP" if gfpp_p <= mats * 1.05 else "выгоднее брать Огонь+Снолист"
        parities.append(
            f"GFPP {format_wow_money(gfpp_p)} vs Огонь+Снолист {format_wow_money(mats)} ({diff_pct:+.0f}%) -> {verdict}"
        )

    # 3. Мангуст vs 2 Шалфея + 2 Чумоцвета
    sage_p, plague_p, mong_p = _p(13465), _p(13466), _p(13452)
    if sage_p > 0 and plague_p > 0 and mong_p > 0:
        mats = 2 * sage_p + 2 * plague_p + 500
        diff_pct = (mong_p / mats - 1.0) * 100.0
        verdict = "БРАТЬ ГОТОВЫЙ МАНГУСТ (по цене травы!)" if mong_p <= mats * 1.08 else "трава дешевле готового"
        parities.append(
            f"Мангуст {format_wow_money(mong_p)} vs 2xШалфей+2xЧумоцвет {format_wow_money(mats)} ({diff_pct:+.0f}%) -> {verdict}"
        )

    # 4. Арканит vs Чародейский кристалл (премия трансмута)
    cryst_p, bar_p = _p(12363), _p(12360)
    if cryst_p > 0 and bar_p > 0:
        cd_prem = bar_p - cryst_p
        parities.append(
            f"Арканит {format_wow_money(bar_p)} vs Кристалл {format_wow_money(cryst_p)} -> премия КД трансмута ~{format_wow_money(cd_prem)}"
        )

    # 5. Лотос смерти vs Чёрный лотос
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
    """Публичная точка входа для вкладки/виджета долгосрочного инвеста к рейдам."""
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
                session.add(
                    SignalCallLog(
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
                )
                active_pending_ids.add(op["item_id"])
                added += 1
            if added > 0:
                await session.commit()

    return opportunities


async def get_call_history_overview(limit: int = 50, refresh: bool = False) -> dict:
    """Возвращает историю коллов и сводку точности прогнозов без выгрузки всей таблицы в память."""
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

        # Берём имена/иконки из кэша, если он уже прогрет — экономим ещё один запрос в Neon
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
    """Формирует срез рынка (из RAM-кэша Neon) + портфель (personal.db) + рейд-радар."""
    ctx = await _load_snapshot_context()
    if not ctx:
        return "Нет доступных срезов рынка в базе данных."

    ops = _score_from_cache(ctx)
    radar = _build_raid_radar_from_cache(ctx)
    calls_data = await get_call_history_overview(limit=15, refresh=False)

    snap_ts = ctx.snap_ts or datetime.now(timezone.utc)
    _, window_label = _describe_market_window(snap_ts)

    # Топ-25 живых ликвидных позиций (выкидываем прибитые к полу вендорные товары с нулевым движением)
    liquid_candidates = []
    for tick in ctx.latest_ticks.values():
        meta = ctx.items_map.get(tick.item_id)
        if not meta or meta.is_hidden or tick.item_id in VENDOR_JUNK_ITEM_IDS:
            continue
        series = ctx.hist_by_item.get(tick.item_id, [])
        prices = [x[0] for x in series] + [tick.entry_price]
        qtys = [x[1] for x in series]
        med, _, _, p75 = _compute_robust_stats(prices)
        churn = _compute_qty_churn(qtys)
        lo, hi = min(prices), max(prices)
        # Пропускаем мёртвые плоские товары (спред < 6% и churn == 0)
        if churn < 1.0 or (lo > 0 and p75 <= lo * 1.06):
            continue
        liquid_candidates.append((tick, meta, med, p75, lo, hi, churn))

    liquid_candidates.sort(key=lambda x: (x[0].num_auctions, x[6]), reverse=True)
    market_snapshot_lines = [
        f"- {meta.name} (ID:{meta.item_id}) | Вход: {format_wow_money(tick.entry_price)} | "
        f"Стена: {format_wow_money(tick.wall_price)} | "
        f"Мед(72): {format_wow_money(int(med))} [p75 {format_wow_money(int(p75))} | {format_wow_money(lo)}..{format_wow_money(hi)}] | "
        f"Лотов: {tick.num_auctions} (шт: {tick.quantity}, churn: {churn:.0f}/ч)"
        for tick, meta, med, p75, lo, hi, churn in liquid_candidates[:25]
    ]

    # Локальный портфель читаем строго из personal.db
    async with personal_session_factory() as psession:
        open_positions = list(
            (
                await psession.execute(
                    select(TradePosition).where(TradePosition.status.in_(["holding", "listed"]))
                )
            ).scalars().all()
        )

    lines = [
        f"### СРЕЗ РЫНКА WOW FOREVER EU (PvP Alliance) | Срезов в БД: {ctx.runs_count}",
        f"Последний скан Blizzard API (UTC): {snap_ts.isoformat()} | Режим: {window_label}",
        "",
        "#### 1. ТЕКУЩИЕ АКТИВНЫЕ КОЛЛЫ СИСТЕМЫ (ТОЛЬКО ЖИВЫЕ МАЯТНИКИ И ДНЕВНЫЕ ПОЛКИ):",
    ]
    if ops:
        for o in ops[:12]:
            lines.append(
                f"- [{o['tag']}] {o['name']}: вход <= {format_wow_money(o['buy'])}, цель {format_wow_money(o['sell'])} "
                f"(ROI +{o['roi']}%, пакет {o['qty']} шт., профит ~{format_wow_money(o['total_profit'])}) — {o['reason']}"
            )
    else:
        lines.append("- Подтверждённых цикличных перекосов на текущем срезе нет (фильтр умирающих графиков активен).")

    lines.append("")
    lines.append("#### 2. ТОП-25 ЖИВЫХ ЛИКВИДНЫХ ПОЗИЦИЙ (БЕЗ МЁРТВОГО ФЛЭТА):")
    lines.extend(market_snapshot_lines or ["- Нет данных"])

    lines.append("")
    s = calls_data["summary"]
    lines.append(
        f"#### 3. ИСТОРИЯ КОЛЛОВ (Всего: {s['total']}, Цель взята: {s['hits']}, Частичный отскок: {s['partials']}, "
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
    lines.append("#### 4. ТЕКУЩИЙ ПОРТФЕЛЬ (С ЖИВЫМ СПОТОМ И РИСКОМ ЗАЛОГА):")
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
        lines.append(
            f"- [{it['tier']}] [{it['status_label']}] {it['name']} (ID:{it['item_id']}) | "
            f"Сейчас: {format_wow_money(it['price'])} (ранг {it['rank_pct']:.0f}%, {-it['disc_med_pct']:+.0f}% к мед.) | "
            f"Мед(72): {format_wow_money(it['median'])} [{format_wow_money(it['lo'])}..{format_wow_money(it['hi'])}] | "
            f"Лотов: {it['num_auctions']} (шт: {it['qty']}{sup})"
        )

    return "\n".join(lines)


async def evaluate_opportunities(snapshot_id: int) -> None:
    opportunities = await get_scored_opportunities(snapshot_id, record_calls=True)
    if not opportunities:
        logger.info("Анализатор: подтверждённых цикличных перекосов с учётом прайм-тайма сейчас нет.")
        return

    logger.info("=== УМНЫЙ СКОРИНГ СДЕЛОК v3 (БАНКРОЛЛ %.1f GOLD) ===", settings.BANKROLL_GOLD)
    for op in opportunities[:8]:
        logger.info(
            f"  [{op['tag']:<12}] {op['name']:<22} | Купить <= {format_wow_money(op['buy']):<10} | "
            f"Цель: {format_wow_money(op['sell']):<10} | ROI: {op['roi']:>5.1f}% | {op['reason']}"
        )