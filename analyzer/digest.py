import logging
from datetime import datetime, timezone
from sqlalchemy import select
from config import settings
from database import personal_session_factory
from models import TradePosition
from .constants import MIN_HIST_TICKS_FLIP, MODEL_VERSION, REGIME_WINDOW_TICKS, VENDOR_JUNK_ITEM_IDS
from .context import load_snapshot_context
from .outcomes import get_call_history_overview, get_scored_opportunities
from .raid_radar import build_raid_radar_from_cache
from .scoring import score_from_cache
from .stats import compute_qty_churn, compute_robust_stats, format_wow_money

logger = logging.getLogger("analyzer")


async def build_ai_market_digest() -> str:
    ctx = await load_snapshot_context()
    if not ctx:
        return "Нет доступных срезов Blizzard в базе данных."

    ops = score_from_cache(ctx)
    radar = build_raid_radar_from_cache(ctx)
    calls_data = await get_call_history_overview(limit=15, refresh=False)

    snap_ts = ctx.snap_ts or datetime.now(timezone.utc)
    day_mode = "ВЫХОДНЫЕ" if snap_ts.weekday() >= 5 else "БУДНИ"

    liquid_candidates = []
    for tick in ctx.latest_ticks.values():
        meta = ctx.items_map.get(tick.item_id)
        if not meta or meta.is_hidden or tick.item_id in VENDOR_JUNK_ITEM_IDS:
            continue
        series = ctx.hist_by_item.get(tick.item_id, [])[-REGIME_WINDOW_TICKS:]
        prices = [pt.entry_price for pt in series] + [tick.entry_price]
        qtys = [pt.quantity for pt in series]
        med, _, _, p75 = compute_robust_stats(prices)
        churn = compute_qty_churn(qtys)
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
        lh = c.get("list_hours", 24)
        lines.append(
            f"- [{dt_str}] {c['name']} ({c['tag']}): колл по {format_wow_money(c['buy_price'])} -> "
            f"предикт {format_wow_money(c['predicted_sell'])} (+{c['predicted_roi']}%) | "
            f"Макс. факт {lh}ч: {format_wow_money(c['max_observed'])} ({c['actual_roi']:+.1f}%) | Статус: {c['status']}"
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
        logger.info("Анализатор %s: подтверждённых цикличных перекосов с положительным E[PnL] нет.", MODEL_VERSION)
        return

    logger.info("=== УМНЫЙ СКОРИНГ СДЕЛОК %s (БАНКРОЛЛ %.1f GOLD) ===", MODEL_VERSION, settings.BANKROLL_GOLD)
    for op in opportunities[:8]:
        logger.info(
            f"  [{op['tag']:<12}] {op['name']:<22} | Купить <= {format_wow_money(op['buy']):<10} | "
            f"Цель: {format_wow_money(op['sell']):<10} | ROI: {op['roi']:>5.1f}% (E:{op['ev_roi']:>4.1f}%) | {op['reason']}"
        )