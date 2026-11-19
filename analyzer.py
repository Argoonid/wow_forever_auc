import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from sqlalchemy import asc, desc, func, select
from config import settings
from database import async_session_factory
from models import ItemMeta, MarketTick, SignalCallLog, SnapshotRun, TradePosition

logger = logging.getLogger("analyzer")

# Единое окно истории для скоринга и AI-дайджеста (48 часовых срезов ≈ 2 суток)
HIST_WINDOW_SNAPSHOTS = 48
MIN_HIST_TICKS_FLIP = 8
MIN_AUCTIONS_FLIP = 5
MIN_TOTAL_QTY_FLIP = 15
MIN_QTY_CHURN_FLIP = 2.0  # Минимальное среднее движение объёма стакана между срезами

# Пороги для чистого арбитража в NPC (без аукционной спекуляции)
MIN_VENDOR_ARB_ROI_PCT = 5.0
MIN_VENDOR_ARB_PROFIT_COPPER = 2500  # Минимум 25s чистого гарантированного профита

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

# Долгосрочный вотчлист под открытие рейдов WoW: Forever (9 декабря 2026)
# Barrow Deeps (10), Hyjal Summit (20), Onyxia's Lair (40)
RAID_WATCHLIST_IDS: list[tuple[int, str]] = [
    # Новые эндгейм-травы Forever под 4 зональные фласки Хиджала/Барроу Дипс
    (248822, "Death Lotus [Forever фласки]"),
    (248821, "Demonsage [Forever фласки]"),
    (248823, "Marefoil [Forever фласки/крафт]"),
    # Ониксия (40) — Fire Protection + классические фласки
    (7068, "Elemental Fire [GFPP Ониксия]"),
    (13457, "Greater Fire Protection Potion"),
    (13468, "Black Lotus [Титан/СПД фласки]"),
    (13422, "Stonescale Eel [Титан/Защита]"),
    (13423, "Stonescale Oil [Титан/Защита]"),
    # Базовые травы под новые боевые поты (Frenzy/Spellblasting) и эликсиры Forever
    (13463, "Dreamfoil [GFPP/Эликсиры/Фласки]"),
    (8846, "Gromsblood [Титан/Grizzly/Ferocity]"),
    (13466, "Plaguebloom [Frenzy/Mongoose]"),
    (8839, "Blindweed [Frenzy/Grizzly]"),
    (13465, "Mountain Silversage [Mongoose/Mana]"),
    (8838, "Sungrass [Fortitude/Nature Power]"),
    (13464, "Golden Sansam [Sages/Nature Power]"),
    (8845, "Ghost Mushroom [Ferocity/Shadow]"),
    # Готовые топовые расходники (ловить ниже себестоимости сырья)
    (13452, "Elixir of the Mongoose"),
    (13444, "Major Mana Potion"),
    # Трансмуты и пре-рейд BiS (Arcanite / Legionite)
    (12363, "Arcane Crystal [Arcanite CD]"),
    (12360, "Arcanite Bar"),
    (248815, "Heavy Thorium Ore [Legionite CD]"),
    # Спекулятивно под Barrow Deeps (Nature/Shadow) — малая аллокация
    (7067, "Elemental Earth [GNPP спекуляция]"),
    (3824, "Shadow Oil [GSPP спекуляция]"),
]


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


def _percentile(sorted_vals: list[int], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * q
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(sorted_vals[lo])
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _compute_robust_stats(prices: list[int]) -> tuple[float, float, float, float]:
    """Возвращает (median, robust_std, p25, p75) без искажения от единичных выбросов."""
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
    # 1.4826 переводит MAD в масштаб нормального std; пол 5% защищает от плоских графиков
    robust_std = max(1.4826 * mad, median * 0.05, 1.0)
    return median, robust_std, p25, p75


def _compute_qty_churn(quantities: list[int]) -> float:
    """Медианное часовое движение объёма стакана (устойчиво к разовым перевыставам китов)."""
    if len(quantities) < 2:
        return 0.0
    deltas = sorted(abs(quantities[i] - quantities[i - 1]) for i in range(1, len(quantities)))
    return _percentile(deltas, 0.50)


async def refresh_call_outcomes(session) -> None:
    """Проверяет открытые спекулятивные коллы за 24ч и фиксирует результат с защитой от шума пустого стакана."""
    pending_calls = list(
        (await session.execute(select(SignalCallLog).where(SignalCallLog.status == "pending"))).scalars().all()
    )
    if not pending_calls:
        return

    now = datetime.now(timezone.utc)
    updated = False

    for call in pending_calls:
        call_ts = call.called_at if call.called_at.tzinfo else call.called_at.replace(tzinfo=timezone.utc)
        horizon_end = call_ts + timedelta(hours=24)

        # Арбитраж ниже вендора закрывается мгновенно в NPC, а не по отскоку AH
        if call.tag == "НИЖЕ ВЕНДОРА":
            call.status = "hit"
            call.resolved_at = now
            updated = True
            continue

        future_rows = (
            await session.execute(
                select(MarketTick.entry_price, MarketTick.num_auctions)
                .where(
                    MarketTick.item_id == call.item_id,
                    MarketTick.snapshot_ts > call_ts,
                    MarketTick.snapshot_ts <= horizon_end,
                    MarketTick.entry_price > 0,
                )
            )
        ).all()

        if not future_rows:
            if now > horizon_end + timedelta(hours=2):
                call.status = "missed"
                call.resolved_at = now
                updated = True
            continue

        max_seen = max(int(ep) for ep, _ in future_rows)
        if max_seen > call.max_observed_copper:
            call.max_observed_copper = max_seen
            meta = await session.get(ItemMeta, call.item_id)
            dep = calculate_deposit_copper(meta.vendor_sell_price if meta else 0, 1, hours=8)
            net_realized = int(max_seen * 0.95) - call.buy_price_copper - dep
            call.actual_roi = (
                round((net_realized / call.buy_price_copper) * 100.0, 1)
                if call.buy_price_copper > 0
                else 0.0
            )
            updated = True

        # Защита от фантомных отскоков: либо >= 2 среза на цели, либо 1 срез на живом стакане (>= 6 лотов)
        ticks_at_target = [int(na or 0) for ep, na in future_rows if int(ep) >= call.predicted_sell_copper]
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


async def get_scored_opportunities(snapshot_id: int | None = None, record_calls: bool = True) -> list[dict]:
    max_budget_per_item = int(settings.BANKROLL_GOLD * 10000 * settings.MAX_SHARE_PER_ITEM)
    now_utc = datetime.now(timezone.utc)
    now_hour = now_utc.hour

    # Временные окна EU (UTC): 14-19 прайм-тайм слива фарма, 04-09 утренний тонкий стакан
    is_dump_window = 14 <= now_hour <= 19
    is_morning_peak = 4 <= now_hour <= 9

    async with async_session_factory() as session:
        await refresh_call_outcomes(session)

        if snapshot_id is None:
            latest_run = (
                await session.execute(select(SnapshotRun).order_by(desc(SnapshotRun.snapshot_ts)).limit(1))
            ).scalar_one_or_none()
            if not latest_run:
                return []
            snapshot_id = latest_run.id
            snap_ts = latest_run.snapshot_ts
        else:
            run_obj = await session.get(SnapshotRun, snapshot_id)
            snap_ts = run_obj.snapshot_ts if run_obj else now_utc

        if snap_ts.tzinfo is None:
            snap_ts = snap_ts.replace(tzinfo=timezone.utc)

        # Строго отсекаем скрытые позиции (даже если is_active_pool=True)
        items_stmt = select(ItemMeta).where(ItemMeta.is_hidden.is_(False))
        candidate_items = {row.item_id: row for row in (await session.execute(items_stmt)).scalars().all()}
        if not candidate_items:
            return []

        ticks_stmt = select(MarketTick).where(
            MarketTick.snapshot_id == snapshot_id,
            MarketTick.item_id.in_(candidate_items.keys()),
        )
        current_ticks = list((await session.execute(ticks_stmt)).scalars().all())
        if not current_ticks:
            return []

        # Батч-загрузка истории строго ДО текущего среза (без утечки текущего тика и будущего)
        hist_run_ids = list(
            (
                await session.execute(
                    select(SnapshotRun.id)
                    .where(SnapshotRun.snapshot_ts < snap_ts)
                    .order_by(desc(SnapshotRun.snapshot_ts))
                    .limit(HIST_WINDOW_SNAPSHOTS)
                )
            ).scalars().all()
        )

        active_item_ids = [t.item_id for t in current_ticks]
        hist_by_item: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        if hist_run_ids and active_item_ids:
            hist_rows = (
                await session.execute(
                    select(
                        MarketTick.item_id,
                        MarketTick.entry_price,
                        MarketTick.quantity,
                        MarketTick.num_auctions,
                    )
                    .where(
                        MarketTick.snapshot_id.in_(hist_run_ids),
                        MarketTick.item_id.in_(active_item_ids),
                        MarketTick.entry_price > 0,
                    )
                    .order_by(asc(MarketTick.snapshot_ts))
                )
            ).all()
            for iid, ep, q, na in hist_rows:
                hist_by_item[iid].append((int(ep), int(q or 0), int(na or 0)))

        opportunities = []
        for tick in current_ticks:
            meta = candidate_items[tick.item_id]
            buy_price = int(tick.entry_price or 0)
            min_buyout = int(tick.min_buyout or buy_price)
            wall_price = int(tick.wall_price or 0)
            total_qty = int(tick.quantity or 0)
            num_auctions = int(tick.num_auctions or 0)

            if buy_price <= 0:
                continue

            # 1. Отдельная ветка: ЧИСТЫЙ безрисковый арбитраж в NPC-вендора
            vendor_price = int(meta.vendor_sell_price or 0)
            arb_buy = min_buyout if 0 < min_buyout < vendor_price else buy_price
            is_below_vendor = vendor_price > 0 and arb_buy < vendor_price
            vendor_unit_profit = (vendor_price - arb_buy) if is_below_vendor else 0
            vendor_roi = (vendor_unit_profit / arb_buy * 100.0) if (is_below_vendor and arb_buy > 0) else 0.0

            max_by_budget_arb = max_budget_per_item // arb_buy if arb_buy > 0 else 0
            # Для вендора консервативно оцениваем доступный объём (до половины стека или до 5 шт. на дне)
            arb_qty = min(meta.max_stack or 20, max_by_budget_arb, max(1, min(total_qty, 5)))
            pure_vendor_arb = (
                is_below_vendor
                and arb_qty > 0
                and vendor_roi >= MIN_VENDOR_ARB_ROI_PCT
                and (vendor_unit_profit * arb_qty) >= MIN_VENDOR_ARB_PROFIT_COPPER
            )

            hist_series = hist_by_item.get(tick.item_id, [])
            local_prices = [x[0] for x in hist_series]
            local_qtys = [x[1] for x in hist_series]
            local_aucs = [x[2] for x in hist_series]
            n_hist = len(local_prices)

            median, r_std, p25, p75 = _compute_robust_stats(local_prices)
            qty_churn = _compute_qty_churn(local_qtys)
            median_aucs = _percentile(sorted(local_aucs), 0.50) if local_aucs else float(num_auctions)

            if pure_vendor_arb:
                total_profit = vendor_unit_profit * arb_qty
                score = total_profit * 3.0
                opportunities.append({
                    "item_id": tick.item_id,
                    "name": meta.name,
                    "category": meta.category,
                    "tag": "НИЖЕ ВЕНДОРА",
                    "reason": f"Арбитраж в NPC ({format_wow_money(vendor_price)}) • лотов {num_auctions}",
                    "buy": arb_buy,
                    "sell": vendor_price,
                    "market_value": int(tick.market_value or 0),
                    "local_median": int(median),
                    "wall_price": wall_price,
                    "vendor": vendor_price,
                    "roi": round(vendor_roi, 1),
                    "z_score": 3.0,
                    "qty": arb_qty,
                    "total_profit": total_profit,
                    "daily_sales": round(qty_churn * 24.0, 1),
                    "num_auctions": num_auctions,
                    "qty_churn": round(qty_churn, 1),
                    "score": round(score, 1),
                })
                continue

            # 2. Ветка аукционной спекуляции — жёсткие фильтры качества и ликвидности
            if tick.item_id in VENDOR_JUNK_ITEM_IDS:
                continue
            if buy_price < 20:
                continue
            if n_hist < MIN_HIST_TICKS_FLIP:
                continue
            if num_auctions < MIN_AUCTIONS_FLIP or total_qty < MIN_TOTAL_QTY_FLIP:
                continue
            if qty_churn < MIN_QTY_CHURN_FLIP and median_aucs < 10:
                continue
            if median <= 0:
                continue

            # Полностью отвязано от мёртвого TSM: якоримся только на локальной истории и стене стакана
            ref_price = median
            z_score = (ref_price - buy_price) / r_std
            disc_median_pct = (1.0 - buy_price / median) * 100.0

            # Базовая цель — андеркат 5% под локальную медиану
            target_sell = int(ref_price * 0.95)

            # Микроструктура стены: если стена выше входа — встаём ПЕРЕД ней; если на дне — штрафуем скор
            wall_penalty = 1.0
            wall_note = ""
            if wall_price > 0:
                if wall_price > int(buy_price * 1.04):
                    capped_by_wall = int(wall_price * 0.98)
                    if capped_by_wall < target_sell:
                        target_sell = capped_by_wall
                        wall_note = f"под стеной {format_wow_money(wall_price)}"
                else:
                    # Стена на дне в тонком стакане = массовый слив или репрайсинг, не ловим нож
                    if num_auctions < 60:
                        continue
                    wall_penalty = 0.35
                    wall_note = f"стена на дне {format_wow_money(wall_price)}"

            # Защита от нереалистичной цели выше p75 локального окна
            if p75 > 0:
                target_sell = min(target_sell, int(p75 * 0.98))

            if target_sell <= buy_price:
                continue

            deposit_unit = calculate_deposit_copper(vendor_price, 1, hours=8)
            net_profit_unit = int(target_sell * 0.95) - buy_price - deposit_unit
            if net_profit_unit <= 0:
                continue

            roi_pct = (net_profit_unit / buy_price) * 100.0

            # Консервативный размер пакета: не предполагаем выкуп всего стакана по цене первых 5 шт.
            max_by_budget = max_budget_per_item // buy_price
            est_cheap_depth = max(1, min(total_qty // 4, meta.max_stack or 20))
            affordable_qty = min(meta.max_stack or 20, max_by_budget, est_cheap_depth)
            if affordable_qty <= 0:
                continue

            # Динамические пороги входа по времени суток и реальной глубине стакана
            is_highly_liquid = num_auctions >= 20 and qty_churn >= 15.0
            if is_morning_peak:
                min_roi_required = 22.0
                min_z_required = 1.50
            elif is_dump_window or is_highly_liquid:
                min_roi_required = 13.0
                min_z_required = 1.05
            else:
                min_roi_required = 16.0
                min_z_required = 1.25

            if roi_pct < min_roi_required or z_score < min_z_required:
                continue

            total_profit = net_profit_unit * affordable_qty
            # Отсекаем копеечный шум (меньше 15s профита на сделку не стоит слотов и перевыставов)
            if total_profit < 1500:
                continue

            # Тегирование и объяснение сигнала
            if z_score >= 2.0 and disc_median_pct >= 22.0:
                tag = "СИЛЬНОЕ ДНО"
            else:
                tag = "ПРОСАДКА"

            reason_parts = [
                f"-{disc_median_pct:.0f}% от мед. ({format_wow_money(int(median))})",
                f"Z={z_score:.1f}",
                f"лотов {num_auctions}",
                f"churn {qty_churn:.0f}/ч",
            ]
            if wall_note:
                reason_parts.append(wall_note)
            reason_str = " • ".join(reason_parts)

            # Логарифмический множитель реальной ликвидности (глубина стакана + оборот)
            depth_mult = math.log1p(num_auctions) / math.log1p(40.0)
            churn_mult = math.log1p(qty_churn) / math.log1p(50.0)
            liquidity_mult = max(0.25, 0.5 * depth_mult + 0.5 * churn_mult)

            # Штраф, если наш пакет превышает ~30% среднего часового движения стакана
            turnover_penalty = min(1.0, max(0.35, (qty_churn * 4.0) / max(affordable_qty, 1)))

            score = (
                total_profit
                * liquidity_mult
                * turnover_penalty
                * wall_penalty
                * min(2.5, max(0.8, z_score))
            )

            opportunities.append({
                "item_id": tick.item_id,
                "name": meta.name,
                "category": meta.category,
                "tag": tag,
                "reason": reason_str,
                "buy": buy_price,
                "sell": target_sell,
                "market_value": int(tick.market_value or 0),
                "local_median": int(median),
                "wall_price": wall_price,
                "vendor": vendor_price,
                "roi": round(roi_pct, 1),
                "z_score": round(z_score, 2),
                "qty": affordable_qty,
                "total_profit": total_profit,
                "daily_sales": round(qty_churn * 24.0, 1),
                "num_auctions": num_auctions,
                "qty_churn": round(qty_churn, 1),
                "score": round(score, 1),
            })

        opportunities.sort(key=lambda x: x["score"], reverse=True)

        # Записываем в лог только спекулятивные коллы с дедупликацией по активным pending-позициям
        if record_calls and opportunities:
            active_pending_ids = set(
                (
                    await session.execute(
                        select(SignalCallLog.item_id).where(SignalCallLog.status == "pending")
                    )
                ).scalars().all()
            )
            added = 0
            for op in opportunities[:15]:
                if op["tag"] == "НИЖЕ ВЕНДОРА":
                    continue
                if op["item_id"] in active_pending_ids:
                    continue
                session.add(
                    SignalCallLog(
                        snapshot_id=snapshot_id,
                        called_at=snap_ts,
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


async def get_call_history_overview(limit: int = 50) -> dict:
    """Возвращает историю всех коллов и сводку точности прогнозов."""
    async with async_session_factory() as session:
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
        all_calls = list((await session.execute(select(SignalCallLog))).scalars().all())
        items_map = {r.item_id: r for r in (await session.execute(select(ItemMeta))).scalars().all()}

    total = len(all_calls)
    hits = sum(1 for c in all_calls if c.status == "hit")
    partials = sum(1 for c in all_calls if c.status == "partial")
    missed = sum(1 for c in all_calls if c.status == "missed")
    pending = sum(1 for c in all_calls if c.status == "pending")

    resolved = hits + partials + missed
    hit_rate = round((hits / resolved) * 100.0, 1) if resolved > 0 else 0.0
    win_or_partial_rate = round(((hits + partials) / resolved) * 100.0, 1) if resolved > 0 else 0.0

    history_items = []
    for c in rows:
        meta = items_map.get(c.item_id)
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
    """Формирует срез рынка, коллов, портфеля и рейд-вотчлиста для анализа и калибровки модели."""
    ops = await get_scored_opportunities(record_calls=False)
    calls_data = await get_call_history_overview(limit=20)

    async with async_session_factory() as session:
        runs_count = (await session.execute(select(func.count(SnapshotRun.id)))).scalar() or 0
        latest_run = (
            await session.execute(select(SnapshotRun).order_by(desc(SnapshotRun.snapshot_ts)).limit(1))
        ).scalar_one_or_none()

        latest_ticks_map: dict[int, MarketTick] = {}
        hist_run_ids: list[int] = []
        if latest_run:
            lt_rows = (
                await session.execute(
                    select(MarketTick).where(MarketTick.snapshot_id == latest_run.id)
                )
            ).scalars().all()
            latest_ticks_map = {t.item_id: t for t in lt_rows}

            hist_run_ids = list(
                (
                    await session.execute(
                        select(SnapshotRun.id)
                        .where(SnapshotRun.snapshot_ts <= latest_run.snapshot_ts)
                        .order_by(desc(SnapshotRun.snapshot_ts))
                        .limit(HIST_WINDOW_SNAPSHOTS)
                    )
                ).scalars().all()
            )

        items_map = {r.item_id: r for r in (await session.execute(select(ItemMeta))).scalars().all()}

        # Топ-25 самых активных позиций по РЕАЛЬНОМУ числу лотов в стакане Blizzard API
        top_liquid_ticks = sorted(
            [
                t for t in latest_ticks_map.values()
                if t.item_id in items_map
                and not items_map[t.item_id].is_hidden
                and t.item_id not in VENDOR_JUNK_ITEM_IDS
            ],
            key=lambda t: (int(t.num_auctions or 0), int(t.quantity or 0)),
            reverse=True,
        )[:25]

        watchlist_ids = [iid for iid, _ in RAID_WATCHLIST_IDS]
        digest_item_ids = list({t.item_id for t in top_liquid_ticks} | set(watchlist_ids))

        hist_by_item: dict[int, list[tuple[int, int]]] = defaultdict(list)
        if hist_run_ids and digest_item_ids:
            h_rows = (
                await session.execute(
                    select(MarketTick.item_id, MarketTick.entry_price, MarketTick.quantity)
                    .where(
                        MarketTick.snapshot_id.in_(hist_run_ids),
                        MarketTick.item_id.in_(digest_item_ids),
                        MarketTick.entry_price > 0,
                    )
                    .order_by(asc(MarketTick.snapshot_ts))
                )
            ).all()
            for iid, ep, q in h_rows:
                hist_by_item[iid].append((int(ep), int(q or 0)))

        market_snapshot_lines = []
        for tick in top_liquid_ticks:
            meta = items_map[tick.item_id]
            series = hist_by_item.get(tick.item_id, [])
            prices = [x[0] for x in series]
            qtys = [x[1] for x in series]
            med, _, _, p75 = _compute_robust_stats(prices)
            churn = _compute_qty_churn(qtys)
            lo = min(prices) if prices else tick.entry_price
            hi = max(prices) if prices else tick.entry_price
            market_snapshot_lines.append(
                f"- {meta.name} (ID:{meta.item_id}) | Вход: {format_wow_money(tick.entry_price)} | "
                f"Стена: {format_wow_money(tick.wall_price)} | "
                f"Мед(48): {format_wow_money(int(med))} [p75 {format_wow_money(int(p75))} | {format_wow_money(lo)}..{format_wow_money(hi)}] | "
                f"Лотов: {tick.num_auctions} (шт: {tick.quantity}, churn: {churn:.0f}/ч)"
            )

        watchlist_lines = []
        for iid, note in RAID_WATCHLIST_IDS:
            tick = latest_ticks_map.get(iid)
            meta = items_map.get(iid)
            nm = meta.name if meta else note
            if not tick:
                watchlist_lines.append(f"- {nm} (ID:{iid}) [{note}] | Нет в текущем срезе AH")
                continue
            series = hist_by_item.get(iid, [])
            prices = [x[0] for x in series]
            med, _, _, _ = _compute_robust_stats(prices)
            lo = min(prices) if prices else tick.entry_price
            hi = max(prices) if prices else tick.entry_price
            watchlist_lines.append(
                f"- {nm} (ID:{iid}) [{note}] | Сейчас: {format_wow_money(tick.entry_price)} | "
                f"Мед(48): {format_wow_money(int(med))} [{format_wow_money(lo)}..{format_wow_money(hi)}] | "
                f"Лотов: {tick.num_auctions} (шт: {tick.quantity})"
            )

        open_positions = list(
            (
                await session.execute(
                    select(TradePosition).where(TradePosition.status.in_(["holding", "listed"]))
                )
            ).scalars().all()
        )

    lines = [
        f"### СРЕЗ РЫНКА WOW FOREVER EU (PvP Alliance) | Срезов в БД: {runs_count}",
        f"Последний скан Blizzard API (UTC): {latest_run.snapshot_ts.isoformat() if latest_run else 'нет'}",
        "",
        "#### 1. ТЕКУЩИЕ АКТИВНЫЕ КОЛЛЫ СИСТЕМЫ:",
    ]
    if ops:
        for o in ops[:12]:
            lines.append(
                f"- [{o['tag']}] {o['name']}: вход <= {format_wow_money(o['buy'])}, цель {format_wow_money(o['sell'])} "
                f"(ROI +{o['roi']}%, пакет {o['qty']} шт., профит ~{format_wow_money(o['total_profit'])}) — {o['reason']}"
            )
    else:
        lines.append("- Активных коллов на текущем срезе нет.")

    lines.append("")
    lines.append("#### 2. ТОП-25 ЛИКВИДНЫХ ПОЗИЦИЙ (ПО ГЛУБИНЕ СТАКАНА И ОБОРОТУ):")
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
    lines.append("#### 4. ТЕКУЩИЙ ПОРТФЕЛЬ (С ЖИВЫМ СПОТОМ):")
    if open_positions:
        for p in open_positions:
            m = items_map.get(p.item_id)
            nm = m.name if m else f"Item_{p.item_id}"
            live_tick = latest_ticks_map.get(p.item_id)
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
    lines.append("#### 5. ИНВЕСТ-ВОТЧЛИСТ К РЕЙДАМ (9 ДЕКАБРЯ):")
    lines.extend(watchlist_lines)

    return "\n".join(lines)


async def evaluate_opportunities(snapshot_id: int) -> None:
    opportunities = await get_scored_opportunities(snapshot_id, record_calls=True)
    if not opportunities:
        logger.info("Анализатор: ярко выраженных перекосов с учётом глубины стакана сейчас нет.")
        return

    logger.info("=== УМНЫЙ СКОРИНГ СДЕЛОК (БАНКРОЛЛ %.1f GOLD) ===", settings.BANKROLL_GOLD)
    for op in opportunities[:8]:
        logger.info(
            f"  [{op['tag']:<12}] {op['name']:<22} | Купить <= {format_wow_money(op['buy']):<10} | "
            f"Цель: {format_wow_money(op['sell']):<10} | ROI: {op['roi']:>5.1f}% | {op['reason']}"
        )