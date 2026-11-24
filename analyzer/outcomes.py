from collections import defaultdict
from datetime import datetime, timedelta, timezone
from sqlalchemy import desc, func, select
from database import async_session_factory
from models import ItemMeta, MarketTick, SignalCallLog
from .constants import MODEL_VERSION, is_market_read_only, min_snapshot_id
from .context import get_cached_items_map, load_snapshot_context
from .scoring import score_from_cache


async def refresh_call_outcomes(session) -> None:
    """
    Батчевая проверка открытых коллов строго по реальному слоту list_hours (8ч / 24ч)
    и без ошибочного вычета возвратного залога из actual_roi.
    """
    if is_market_read_only():
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
    cutoff_snap_id = min_snapshot_id()

    ticks_rows = (
        await session.execute(
            select(
                MarketTick.item_id,
                MarketTick.snapshot_ts,
                MarketTick.entry_price,
                MarketTick.num_auctions,
            ).where(
                MarketTick.source == "blizzard",
                MarketTick.snapshot_id >= cutoff_snap_id,
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
        call_metrics = call.metrics if isinstance(call.metrics, dict) else {}
        list_hours = int(call_metrics.get("list_hours") or 24)
        if list_hours not in (2, 8, 24):
            list_hours = 24
        horizon_end = call_ts + timedelta(hours=list_hours)

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
            # При продаже залог возвращается — чистый итог считается только после 5% комиссии АН
            net_realized = int(max_seen * 0.95) - call.buy_price_copper
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


async def get_scored_opportunities(snapshot_id: int | None = None, record_calls: bool = False) -> list[dict]:
    can_write_cloud = record_calls and not is_market_read_only()

    if can_write_cloud:
        async with async_session_factory() as session:
            await refresh_call_outcomes(session)

    ctx = await load_snapshot_context(snapshot_id=snapshot_id, force_reload=can_write_cloud)
    if not ctx:
        return []

    opportunities = score_from_cache(ctx)

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
                        "flow_source": op["flow_source"],
                        "bottom_mode": op["bottom_mode"],
                        "shelf_minutes": op["shelf_minutes"],
                    }
                session.add(SignalCallLog(**call_kwargs))
                active_pending_ids.add(op["item_id"])
                added += 1
            if added > 0:
                await session.commit()

    return opportunities


async def get_call_history_overview(limit: int = 50, refresh: bool = False) -> dict:
    async with async_session_factory() as session:
        if refresh and not is_market_read_only():
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
        cached_meta = get_cached_items_map()
        if cached_meta and row_item_ids.issubset(cached_meta.keys()):
            meta_lookup = {iid: cached_meta[iid] for iid in row_item_ids}
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
        call_metrics = c.metrics if isinstance(c.metrics, dict) else {}
        list_hours = int(call_metrics.get("list_hours") or 24)
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
            "list_hours": list_hours,
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