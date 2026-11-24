import math
from datetime import datetime, timezone
from config import settings
from .constants import (
    BOTTOM_2H_MAX_DRIFT,
    BOTTOM_MAX_GAP_MINUTES,
    DEEP_BOOK_MIN_CHEAP_QTY,
    DEEP_BOOK_MIN_SHELF_MINUTES,
    DEEP_BOOK_P25_RATIO,
    MIN_AUCTIONS_FLIP,
    MIN_CHEAP_QTY_FLIP,
    MIN_HIST_TICKS_FLIP,
    MIN_QTY_CHURN_FLIP,
    MIN_SHELF_LIFE_MINUTES,
    MIN_SOLD_HR_FLIP,
    MIN_TAPE_TICKS_FOR_PRIORITY,
    MIN_TOTAL_QTY_FLIP,
    REGIME_WINDOW_TICKS,
    STRICT_TWO_HOUR_BOTTOM,
    VENDOR_JUNK_ITEM_IDS,
)
from .stats import (
    analyze_price_regime,
    calculate_deposit_copper,
    compute_item_clock,
    compute_qty_churn,
    compute_robust_stats,
    format_wow_money,
    percentile,
)
from .types import SnapshotCache


def score_from_cache(ctx: SnapshotCache) -> list[dict]:
    """
    Скоринг v4.1:
    - подтверждение устойчивости дна (2 часа подряд или плотная полка >= 25% книги) + запас времени жизни полки;
    - приоритет реальной ленты сделок (est_sold_qty) над шумным churn;
    - персональные часы актива, честная оценка воздушного кармана и E[PnL].
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
    prev_is_contiguous = (
        ctx.prev_snapshot_id is not None
        and ctx.prev_snap_gap_min is not None
        and ctx.prev_snap_gap_min <= BOTTOM_MAX_GAP_MINUTES
    )

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
        local_prices = [pt.entry_price for pt in recent_slice]
        local_qtys = [pt.quantity for pt in recent_slice]
        local_aucs = [pt.num_auctions for pt in recent_slice]

        tape_samples = [pt.est_sold_qty for pt in recent_slice if pt.est_sold_qty >= 0]
        if ctx.tape_active:
            tape_samples.append(tick.est_sold_qty)

        qty_churn = compute_qty_churn(local_qtys)
        avg_sold_hr = (sum(tape_samples) / len(tape_samples)) if tape_samples else 0.0
        median_aucs = percentile(sorted(local_aucs), 0.50) if local_aucs else float(num_auctions)

        # 1. Реальный поток выкупа: когда лента набрала >= 6ч истории, не даём churn раздувать мёртвые лоты
        if len(tape_samples) >= MIN_TAPE_TICKS_FOR_PRIORITY:
            effective_flow = avg_sold_hr
            flow_source = "tape"
            if effective_flow < MIN_SOLD_HR_FLIP:
                continue
        else:
            effective_flow = max(avg_sold_hr, qty_churn)
            flow_source = "churn"
            if effective_flow < MIN_QTY_CHURN_FLIP and median_aucs < 12:
                continue

        # 2. Персональные биологические часы + режимный фильтр графика
        clock = compute_item_clock(hist_series, cur_hour)
        regime = analyze_price_regime(hist_series, clock)
        if not regime["tradable"]:
            continue

        median, r_std, _, _ = compute_robust_stats(local_prices)
        ref_plateau = regime["ref_plateau"]
        bounces = regime["bounces"]

        if median <= 0 or buy_price > int(median * 1.02):
            continue
        if buy_price >= ref_plateau * 0.90:
            continue

        # 3. Защита от "выдуваемого за секунду" дна
        has_book_geo = tick.cheap_qty > 0
        min_cheap_units = 3 if (meta.max_stack <= 1 or buy_price >= 50000) else MIN_CHEAP_QTY_FLIP

        if has_book_geo:
            shelf_minutes = (tick.cheap_qty / max(effective_flow, 1.0)) * 60.0
            if tick.cheap_qty < min_cheap_units or shelf_minutes < MIN_SHELF_LIFE_MINUTES:
                continue
        else:
            shelf_minutes = 60.0

        held_two_hours = False
        if prev_is_contiguous and hist_series[-1].snapshot_id == ctx.prev_snapshot_id:
            prev_ep = hist_series[-1].entry_price
            held_two_hours = (
                prev_ep <= int(buy_price * BOTTOM_2H_MAX_DRIFT)
                and prev_ep < ref_plateau * 0.90
            )

        deep_min_qty = DEEP_BOOK_MIN_CHEAP_QTY if min_cheap_units >= MIN_CHEAP_QTY_FLIP else 5
        deep_book_bottom = (
            not STRICT_TWO_HOUR_BOTTOM
            and has_book_geo
            and tick.book_p25_price > 0
            and tick.book_p25_price <= int(buy_price * DEEP_BOOK_P25_RATIO)
            and tick.cheap_qty >= deep_min_qty
            and shelf_minutes >= DEEP_BOOK_MIN_SHELF_MINUTES
        )

        if not (held_two_hours or deep_book_bottom):
            continue

        bottom_mode = "2h" if held_two_hours else "deep_p25"

        # 4. Геометрия стены: жёсткий бан стены на дне, кроме сверхликвидных маятников (200+ лотов)
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

        # 5. Оценка воздушного кармана и вероятности исполнения p_fill
        if is_dump_floor_wall:
            hours_to_clear_gap = 5.0
        elif tick.gap_qty > 0 and effective_flow > 0:
            hours_to_clear_gap = tick.gap_qty / max(effective_flow, 1.0)
        elif has_book_geo and wall_price > int(buy_price * 1.05):
            hours_to_clear_gap = 0.5
        else:
            hours_to_clear_gap = 5.0

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

        # 6. Выбор оптимального слота (8ч vs 24ч) и расчёт матожидания E[PnL]
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

        # 7. Реальный размер пакета (по cheap_qty)
        max_by_budget = max_budget_per_item // buy_price
        est_cheap_depth = tick.cheap_qty if has_book_geo else max(1, total_qty // 4)
        affordable_qty = min(meta.max_stack, max_by_budget, est_cheap_depth)
        if affordable_qty <= 0:
            continue

        total_profit = net_if_filled * affordable_qty
        total_ev = int(ev_unit * affordable_qty)
        if total_profit < 1500 or total_ev < 800:
            continue

        if in_own_trough_window and bounces >= 2:
            tag = "МАЯТНИК"
        elif has_book_geo and not is_dump_floor_wall and hours_to_clear_gap <= 2.5 and z_score >= 1.5:
            tag = "КАРМАН"
        elif z_score >= 2.0 and disc_plateau_pct >= 22.0:
            tag = "СИЛЬНОЕ ДНО"
        else:
            tag = "ПРОСАДКА"

        bottom_label = "дно 2ч" if bottom_mode == "2h" else f"полка p25 (~{int(min(shelf_minutes, 999))}м)"
        reason_parts = [
            f"-{disc_plateau_pct:.0f}% от полки ({format_wow_money(int(ref_plateau))})",
            bottom_label,
            f"циклов {bounces}",
            f"p_fill {int(p_fill * 100)}%",
            f"слот {rec_list_hours}ч",
        ]
        if clock["has_clock"] and clock["clock_edge_pct"] >= 12.0:
            reason_parts.append(f"пик ~{clock['peak_hour']:02d} UTC (+{clock['clock_edge_pct']:.0f}%)")
        if tick.gap_qty > 0:
            reason_parts.append(f"карман {tick.gap_qty}шт")
        elif has_book_geo and not is_dump_floor_wall and wall_price > int(buy_price * 1.05):
            reason_parts.append("чистый карман")
        if flow_source == "tape" or avg_sold_hr >= 1.0:
            reason_parts.append(f"выкуп ~{avg_sold_hr:.0f}/ч")
        else:
            reason_parts.append(f"churn {qty_churn:.0f}/ч")
        if wall_note:
            reason_parts.append(wall_note)
        reason_str = " • ".join(reason_parts)

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
            "flow_source": flow_source,
            "bottom_mode": bottom_mode,
            "shelf_minutes": round(shelf_minutes, 1),
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