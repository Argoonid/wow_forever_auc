from datetime import datetime, timezone
from .constants import RAID_LAUNCH_UTC, RAID_WATCHLIST, REGIME_WINDOW_TICKS
from .context import load_snapshot_context
from .stats import compute_item_clock, compute_robust_stats, format_wow_money, percentile, percentile_rank
from .types import SnapshotCache


def build_raid_radar_from_cache(ctx: SnapshotCache) -> dict:
    """Отдельный инвест-радар к 9 декабря с персональным лучшим часом входа и защитой паритетов от перегрева."""
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
        clock = compute_item_clock(series, cur_hour)
        prices = [pt.entry_price for pt in series[-REGIME_WINDOW_TICKS:]] + [tick.entry_price]
        qtys = [pt.quantity for pt in series[-REGIME_WINDOW_TICKS:]]
        med, _, p25, p75 = compute_robust_stats(prices)
        s_prices = sorted(prices)
        lo, hi = s_prices[0], s_prices[-1]
        rank_pct = percentile_rank(s_prices, tick.entry_price)
        disc_med_pct = round(((1.0 - tick.entry_price / med) * 100.0), 1) if med > 0 else 0.0

        med_qty = percentile(sorted(qtys), 0.50) if qtys else float(tick.quantity)
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

    status_by_id = {it["item_id"]: it["status_code"] for it in items_report}
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
        if oil_p < mats:
            verdict = "масло ниже сырья (но само ПЕРЕГРЕТО — ждать)" if status_by_id.get(13423) == "HOT" else "БРАТЬ МАСЛО (ниже сырья!)"
        else:
            verdict = "масло дороже рыбы"
        parities.append(
            f"Масло угря {format_wow_money(oil_p)} vs 2x Угорь {format_wow_money(mats)} ({diff_pct:+.0f}%) -> {verdict}"
        )

    fire_p, dream_p, gfpp_p = _p(7068), _p(13463), _p(13457)
    if fire_p > 0 and dream_p > 0 and gfpp_p > 0:
        mats = fire_p + dream_p + 500
        diff_pct = (gfpp_p / mats - 1.0) * 100.0
        if gfpp_p <= mats * 1.05:
            verdict = "GFPP по цене сырья (но ПЕРЕГРЕТ — ждать)" if status_by_id.get(13457) == "HOT" else "выгоднее брать готовый GFPP"
        else:
            verdict = "выгоднее брать Огонь+Снолист"
        parities.append(
            f"GFPP {format_wow_money(gfpp_p)} vs Огонь+Снолист {format_wow_money(mats)} ({diff_pct:+.0f}%) -> {verdict}"
        )

    sage_p, plague_p, mong_p = _p(13465), _p(13466), _p(13452)
    if sage_p > 0 and plague_p > 0 and mong_p > 0:
        mats = 2 * sage_p + 2 * plague_p + 500
        diff_pct = (mong_p / mats - 1.0) * 100.0
        if mong_p <= mats * 1.08:
            verdict = "Мангуст по цене травы (но ПЕРЕГРЕТ — ждать отката)" if status_by_id.get(13452) == "HOT" else "БРАТЬ ГОТОВЫЙ МАНГУСТ (по цене травы!)"
        else:
            verdict = "трава дешевле готового"
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
        if ratio < 85.0:
            verdict = "Лотос смерти ниже Чёрного (но локально ПЕРЕГРЕТ — ждать)" if status_by_id.get(248822) == "HOT" else "Лотос смерти недооценён к Чёрному!"
        else:
            verdict = "паритет близок"
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
    ctx = await load_snapshot_context(snapshot_id=snapshot_id)
    if not ctx:
        return {"days_to_raids": 0, "phase_hint": "Нет данных", "items": [], "parities": []}
    return build_raid_radar_from_cache(ctx)