import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import asc, delete, desc, func, select, text
from analyzer import (
    build_ai_market_digest,
    format_wow_money,
    get_call_history_overview,
    get_scored_opportunities,
)
from config import settings
from database import async_session_factory, init_db
from models import ChartDrawing, ItemMeta, MarketTick, TradePosition
from portfolio import (
    create_buy_position,
    delete_position,
    get_item_trade_events,
    get_portfolio_overview,
    list_position_on_ah,
    mark_position_expired,
    mark_position_sold,
    mark_position_vendored,
    reset_burned_deposit,
)

import mimetypes

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")

app = FastAPI(title="WoW Forever Terminal")
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

VALID_BUCKETS = {"long_invest", "fast_flip", "watch"}


class DrawingPayload(BaseModel):
    item_id: int
    tool_type: str
    points: list[dict]
    note_text: str = ""


class BuyPayload(BaseModel):
    item_id: int
    qty: int
    buy_price_copper: int
    comment: str = ""
    list_price_copper: int = 0
    deposit_hours: int = 8


class ListPayload(BaseModel):
    list_price_copper: int
    deposit_hours: int = 8
    burn_previous: bool = False
    custom_deposit_copper: int | None = None


class SoldPayload(BaseModel):
    sold_qty: int | None = None
    actual_sell_price_copper: int | None = None


class WatchlistPayload(BaseModel):
    item_id: int
    bucket: str = "watch"
    target_buy_copper: int = 0
    target_sell_copper: int = 0
    note: str = ""


async def _ensure_watchlist_table() -> None:
    async with async_session_factory() as session:
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS watchlist_items (
                    item_id INTEGER PRIMARY KEY,
                    bucket TEXT NOT NULL DEFAULT 'watch',
                    added_price_copper INTEGER NOT NULL DEFAULT 0,
                    target_buy_copper INTEGER NOT NULL DEFAULT 0,
                    target_sell_copper INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
        )
        await session.commit()


async def _get_latest_price_map(session) -> dict[int, dict]:
    latest_id_subq = (
        select(MarketTick.item_id, func.max(MarketTick.id).label("max_id"))
        .where(MarketTick.entry_price > 0)
        .group_by(MarketTick.item_id)
        .subquery()
    )
    stmt = select(MarketTick.item_id, MarketTick.entry_price, MarketTick.quantity).join(
        latest_id_subq, MarketTick.id == latest_id_subq.c.max_id
    )
    return {
        iid: {"price": int(ep or 0), "qty": int(q or 0)}
        for iid, ep, q in (await session.execute(stmt)).all()
    }


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()
    await _ensure_watchlist_table()


@app.get("/")
async def index_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/items")
async def api_items(min_price: int = Query(80, ge=0)):
    async with async_session_factory() as session:
        stmt = select(ItemMeta).order_by(
            desc(ItemMeta.is_active_pool), desc(ItemMeta.velocity_score), asc(ItemMeta.name)
        )
        rows = (await session.execute(stmt)).scalars().all()
        latest_map = await _get_latest_price_map(session)

        # Защищаем от скрытия предметы, которые уже в портфеле или на радаре
        pf_ids = {
            r[0]
            for r in (
                await session.execute(
                    select(TradePosition.item_id).where(TradePosition.status.in_(("holding", "listed")))
                )
            ).all()
        }
        wl_ids = {
            int(r[0]) for r in (await session.execute(text("SELECT item_id FROM watchlist_items"))).all()
        }
        protected_ids = pf_ids | wl_ids

        out = []
        for r in rows:
            latest = latest_map.get(r.item_id, {})
            last_price = latest.get("price", 0)
            last_qty = latest.get("qty", 0)
            effective_price = last_price if last_price > 0 else (r.tsm_market_value or 0)

            if min_price > 0 and effective_price > 0 and effective_price < min_price and r.item_id not in protected_ids:
                continue

            out.append({
                "item_id": r.item_id,
                "name": r.name,
                "icon": r.icon_name or "inv_misc_questionmark",
                "category": r.category,
                "is_active_pool": r.is_active_pool,
                "is_hidden": bool(r.is_hidden),
                "velocity_score": round(r.velocity_score, 1),
                "market_value": r.tsm_market_value,
                "last_price": last_price,
                "effective_price": effective_price,
                "last_qty": last_qty,
                "vendor_price": r.vendor_sell_price,
                "max_stack": r.max_stack,
            })
        return out


@app.post("/api/items/{item_id}/toggle-hide")
async def api_toggle_hide(item_id: int):
    async with async_session_factory() as session:
        item = await session.get(ItemMeta, item_id)
        if not item:
            return {"error": "not found"}
        item.is_hidden = not bool(item.is_hidden)
        await session.commit()
        return {"item_id": item_id, "is_hidden": item.is_hidden}


@app.get("/api/opportunities")
async def api_opportunities(min_price: int = Query(80, ge=0)):
    ops = await get_scored_opportunities()
    async with async_session_factory() as session:
        meta_map = {
            r.item_id: r for r in (await session.execute(select(ItemMeta))).scalars().all()
        }
    filtered = []
    for op in ops:
        if min_price > 0 and op.get("buy", 0) < min_price:
            continue
        m = meta_map.get(op["item_id"])
        op["icon"] = (m.icon_name if m else None) or "inv_misc_questionmark"
        op["is_hidden"] = bool(m.is_hidden) if m else False
        op["buy_fmt"] = format_wow_money(op["buy"])
        op["sell_fmt"] = format_wow_money(op["sell"])
        op["profit_fmt"] = format_wow_money(op["total_profit"])
        filtered.append(op)
    return filtered


@app.get("/api/calls/history")
async def api_calls_history(limit: int = Query(50, ge=5, le=200)):
    return await get_call_history_overview(limit=limit)


@app.get("/api/calls/digest")
async def api_calls_digest():
    digest_text = await build_ai_market_digest()
    return {"digest": digest_text}


def _build_dip_forecast(ticks: list[MarketTick], base_price_silver: float) -> list[dict]:
    # Не рисуем синтетический 24ч прогноз, если у товара меньше 3 реальных срезов
    if base_price_silver <= 0 or len(ticks) < 3:
        return []

    prior_hourly = {
        0: 1.01, 1: 1.02, 2: 1.04, 3: 1.05, 4: 1.07, 5: 1.08,
        6: 1.09, 7: 1.07, 8: 1.05, 9: 1.03, 10: 1.02, 11: 1.01,
        12: 1.00, 13: 0.98, 14: 0.95, 15: 0.92, 16: 0.91, 17: 0.91,
        18: 0.92, 19: 0.94, 20: 0.97, 21: 0.99, 22: 1.00, 23: 1.00,
    }

    by_hour: dict[int, list[float]] = {}
    for t in ticks:
        by_hour.setdefault(t.hour_of_day, []).append(t.entry_price / 100.0)

    sorted_prices = sorted([t.entry_price / 100.0 for t in ticks])
    overall_median = sorted_prices[len(sorted_prices) // 2]
    anchor = overall_median if overall_median > 0 else base_price_silver

    now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    last_tick_ts = ticks[-1].snapshot_ts
    if last_tick_ts.tzinfo is None:
        last_tick_ts = last_tick_ts.replace(tzinfo=timezone.utc)
    start_ts = max(now_utc, last_tick_ts.replace(minute=0, second=0, microsecond=0))

    forecast_points = []
    for step in range(1, 25):
        future_dt = start_ts + timedelta(hours=step)
        h = future_dt.hour
        emp_list = by_hour.get(h, [])
        weight_emp = min(1.0, len(emp_list) / 3.0)
        emp_mult = (sum(emp_list) / len(emp_list)) / anchor if emp_list else prior_hourly[h]
        # Ограничиваем выбросы эмпирического множителя, чтобы график не улетал в аномалии
        emp_mult = max(0.55, min(1.65, emp_mult))
        blended_mult = (1.0 - weight_emp) * prior_hourly[h] + weight_emp * emp_mult

        expected_price = round(anchor * blended_mult, 2)
        forecast_points.append({
            "time": int(future_dt.timestamp()),
            "value": expected_price,
            "is_dip": blended_mult <= 0.94,
        })

    return forecast_points


@app.get("/api/history/{item_id}")
async def api_history(item_id: int, days: int = Query(0, ge=0, le=90)):
    async with async_session_factory() as session:
        stmt = select(MarketTick).where(
            MarketTick.item_id == item_id,
            MarketTick.entry_price > 0,
        )
        if days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            stmt = stmt.where(MarketTick.snapshot_ts >= cutoff)
        stmt = stmt.order_by(asc(MarketTick.snapshot_ts), asc(MarketTick.id))
        ticks = list((await session.execute(stmt)).scalars().all())
        meta = await session.get(ItemMeta, item_id)

    max_gap_sec = settings.MAX_CONTINUOUS_GAP_MINUTES * 60
    sessions: list[dict] = []
    current_session: dict = {"line": [], "market_line": []}
    gaps: list[dict] = []

    prev_unix: int | None = None
    first_unix: int | None = None
    last_unix: int | None = None

    for t in ticks:
        ts = t.snapshot_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        unix_sec = int(ts.timestamp())
        if first_unix is None:
            first_unix = unix_sec
        last_unix = unix_sec

        price_silver = round(t.entry_price / 100.0, 2)
        market_silver = round(t.market_value / 100.0, 2) if t.market_value > 0 else 0.0

        # Защита от дублей timestamp (иначе LightweightCharts ломает рендер линии)
        if prev_unix is not None and unix_sec == prev_unix:
            if current_session["line"]:
                current_session["line"][-1] = {"time": unix_sec, "value": price_silver}
            if market_silver > 0:
                if current_session["market_line"] and current_session["market_line"][-1]["time"] == unix_sec:
                    current_session["market_line"][-1] = {"time": unix_sec, "value": market_silver}
                else:
                    current_session["market_line"].append({"time": unix_sec, "value": market_silver})
            continue

        if prev_unix is not None and (unix_sec - prev_unix) > max_gap_sec:
            if current_session["line"]:
                sessions.append(current_session)
            current_session = {"line": [], "market_line": []}
            gaps.append({
                "start_time": prev_unix,
                "end_time": unix_sec,
                "hours": round((unix_sec - prev_unix) / 3600.0, 1),
            })

        current_session["line"].append({"time": unix_sec, "value": price_silver})
        # Не добавляем нулевые market_value, чтобы не сплющивать ось Y к нулю
        if market_silver > 0:
            current_session["market_line"].append({"time": unix_sec, "value": market_silver})
        prev_unix = unix_sec

    if current_session["line"]:
        sessions.append(current_session)

    base_silver = (meta.tsm_market_value / 100.0) if (meta and meta.tsm_market_value > 0) else (
        ticks[-1].entry_price / 100.0 if ticks else 0.0
    )
    forecast_dips = _build_dip_forecast(ticks, base_silver)
    trade_events = await get_item_trade_events(item_id)

    full_timeline: list[int] = []
    if first_unix is not None:
        start_hour = (first_unix // 3600) * 3600
        end_hour = forecast_dips[-1]["time"] if forecast_dips else ((last_unix // 3600) * 3600)
        cursor_ts = start_hour
        while cursor_ts <= end_hour:
            full_timeline.append(cursor_ts)
            cursor_ts += 3600

    return {
        "full_timeline": full_timeline,
        "sessions": sessions,
        "gaps": gaps,
        "forecast_dips": forecast_dips,
        "trade_events": trade_events,
    }


@app.get("/api/drawings/{item_id}")
async def get_drawings(item_id: int):
    async with async_session_factory() as session:
        stmt = select(ChartDrawing).where(ChartDrawing.item_id == item_id).order_by(asc(ChartDrawing.id))
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "item_id": r.item_id,
                "tool_type": r.tool_type,
                "points": json.loads(r.points_json),
                "note_text": r.note_text or "",
            }
            for r in rows
        ]


@app.post("/api/drawings")
async def save_drawing(payload: DrawingPayload):
    async with async_session_factory() as session:
        rec = ChartDrawing(
            item_id=payload.item_id,
            tool_type=payload.tool_type,
            points_json=json.dumps(payload.points),
            note_text=payload.note_text.strip(),
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return {"id": rec.id, "status": "ok"}


@app.delete("/api/drawings/{item_id}")
async def clear_drawings(item_id: int):
    async with async_session_factory() as session:
        await session.execute(delete(ChartDrawing).where(ChartDrawing.item_id == item_id))
        await session.commit()
        return {"status": "cleared"}




@app.get("/api/watchlist")
async def api_watchlist_list():
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT item_id, bucket, added_price_copper, target_buy_copper, target_sell_copper, note, created_at "
                    "FROM watchlist_items ORDER BY created_at DESC"
                )
            )
        ).all()
        meta_map = {r.item_id: r for r in (await session.execute(select(ItemMeta))).scalars().all()}
        latest_map = await _get_latest_price_map(session)

    items = []
    for iid, bucket, added_p, t_buy, t_sell, note, created_at in rows:
        meta = meta_map.get(iid)
        latest = latest_map.get(iid, {})
        curr_price = latest.get("price", 0) or (meta.tsm_market_value if meta else 0)
        curr_qty = latest.get("qty", 0)
        delta_pct = round(((curr_price - added_p) / added_p) * 100.0, 1) if (added_p > 0 and curr_price > 0) else 0.0
        in_buy_zone = bool(t_buy > 0 and curr_price > 0 and curr_price <= t_buy)
        in_sell_zone = bool(t_sell > 0 and curr_price > 0 and curr_price >= t_sell)

        items.append({
            "item_id": iid,
            "name": meta.name if meta else f"Item_{iid}",
            "icon": (meta.icon_name if meta else None) or "inv_misc_questionmark",
            "bucket": bucket,
            "added_price": added_p,
            "current_price": curr_price,
            "current_qty": curr_qty,
            "delta_pct": delta_pct,
            "target_buy": t_buy,
            "target_sell": t_sell,
            "in_buy_zone": in_buy_zone,
            "in_sell_zone": in_sell_zone,
            "velocity": round(meta.velocity_score, 1) if meta else 0.0,
            "note": note or "",
            "created_at": created_at,
        })
    return items


@app.post("/api/watchlist")
async def api_watchlist_upsert(payload: WatchlistPayload):
    bucket = payload.bucket if payload.bucket in VALID_BUCKETS else "watch"
    now_iso = datetime.now(timezone.utc).isoformat()

    async with async_session_factory() as session:
        existing = (
            await session.execute(
                text("SELECT added_price_copper FROM watchlist_items WHERE item_id = :iid"),
                {"iid": payload.item_id},
            )
        ).first()

        if existing:
            await session.execute(
                text(
                    "UPDATE watchlist_items "
                    "SET bucket = :b, target_buy_copper = :tb, target_sell_copper = :ts, note = :n "
                    "WHERE item_id = :iid"
                ),
                {
                    "iid": payload.item_id,
                    "b": bucket,
                    "tb": max(0, payload.target_buy_copper),
                    "ts": max(0, payload.target_sell_copper),
                    "n": payload.note.strip(),
                },
            )
        else:
            latest_map = await _get_latest_price_map(session)
            meta = await session.get(ItemMeta, payload.item_id)
            curr_p = latest_map.get(payload.item_id, {}).get("price", 0) or (meta.tsm_market_value if meta else 0)
            await session.execute(
                text(
                    "INSERT INTO watchlist_items "
                    "(item_id, bucket, added_price_copper, target_buy_copper, target_sell_copper, note, created_at) "
                    "VALUES (:iid, :b, :ap, :tb, :ts, :n, :ca)"
                ),
                {
                    "iid": payload.item_id,
                    "b": bucket,
                    "ap": curr_p,
                    "tb": max(0, payload.target_buy_copper),
                    "ts": max(0, payload.target_sell_copper),
                    "n": payload.note.strip(),
                    "ca": now_iso,
                },
            )
        await session.commit()
    return {"status": "ok", "item_id": payload.item_id, "bucket": bucket}


@app.delete("/api/watchlist/{item_id}")
async def api_watchlist_delete(item_id: int):
    async with async_session_factory() as session:
        await session.execute(text("DELETE FROM watchlist_items WHERE item_id = :iid"), {"iid": item_id})
        await session.commit()
    return {"status": "deleted"}




@app.get("/api/portfolio")
async def api_portfolio():
    return await get_portfolio_overview()


@app.post("/api/portfolio/buy")
async def api_portfolio_buy(payload: BuyPayload):
    return await create_buy_position(
        item_id=payload.item_id,
        qty=payload.qty,
        buy_price_copper=payload.buy_price_copper,
        comment=payload.comment,
        list_price_copper=payload.list_price_copper,
        deposit_hours=payload.deposit_hours,
    )


@app.post("/api/portfolio/{pos_id}/list")
async def api_portfolio_list(pos_id: int, payload: ListPayload):
    return await list_position_on_ah(
        pos_id=pos_id,
        list_price_copper=payload.list_price_copper,
        deposit_hours=payload.deposit_hours,
        burn_previous=payload.burn_previous,
        custom_deposit_copper=payload.custom_deposit_copper,
    )


@app.post("/api/portfolio/{pos_id}/reset-burned")
async def api_portfolio_reset_burned(pos_id: int):
    return await reset_burned_deposit(pos_id=pos_id)


@app.post("/api/portfolio/{pos_id}/sold")
async def api_portfolio_sold(pos_id: int, payload: SoldPayload):
    return await mark_position_sold(
        pos_id=pos_id,
        sold_qty=payload.sold_qty,
        actual_sell_price_copper=payload.actual_sell_price_copper,
    )


@app.post("/api/portfolio/{pos_id}/expire")
async def api_portfolio_expire(pos_id: int):
    return await mark_position_expired(pos_id=pos_id)


@app.post("/api/portfolio/{pos_id}/vendor")
async def api_portfolio_vendor(pos_id: int):
    return await mark_position_vendored(pos_id=pos_id)


@app.delete("/api/portfolio/{pos_id}")
async def api_portfolio_delete(pos_id: int):
    return await delete_position(pos_id=pos_id)