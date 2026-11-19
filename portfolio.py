from datetime import datetime, timezone
from sqlalchemy import desc, func, select
from database import async_session_factory, personal_session_factory
from models import ItemMeta, MarketTick, TradePosition


def calc_wow_deposit(vendor_unit_copper: int, qty: int, hours: int = 8) -> int:
    """Расчёт залога аукциона WoW Classic: 15% (2ч), 30% (8ч), 60% (24ч) от цены вендора."""
    if vendor_unit_copper <= 0 or qty <= 0:
        return 0
    rate = {2: 0.15, 8: 0.30, 24: 0.60}.get(hours, 0.30)
    return max(1, int(vendor_unit_copper * qty * rate))


def _to_unix(dt: datetime | None) -> int:
    if not dt:
        return 0
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return int(aware.timestamp())


async def create_buy_position(
    item_id: int,
    qty: int,
    buy_price_copper: int,
    comment: str = "",
    list_price_copper: int = 0,
    deposit_hours: int = 8,
) -> dict:
    now = datetime.now(timezone.utc)

    async with async_session_factory() as msession:
        meta = await msession.get(ItemMeta, item_id)
        vendor_unit = meta.vendor_sell_price if meta else 0

    status = "holding"
    active_deposit = 0
    listed_at = None

    if list_price_copper > 0:
        status = "listed"
        active_deposit = calc_wow_deposit(vendor_unit, qty, deposit_hours)
        listed_at = now

    async with personal_session_factory() as psession:
        pos = TradePosition(
            item_id=item_id,
            qty=qty,
            buy_price_copper=buy_price_copper,
            list_price_copper=list_price_copper,
            vendor_unit_copper=vendor_unit,
            deposit_hours=deposit_hours,
            active_deposit_copper=active_deposit,
            burned_deposit_copper=0,
            status=status,
            comment=comment.strip(),
            bought_at=now,
            listed_at=listed_at,
        )
        psession.add(pos)
        await psession.commit()
        await psession.refresh(pos)
        return {"id": pos.id, "status": pos.status, "deposit": pos.active_deposit_copper}


async def list_position_on_ah(
    pos_id: int,
    list_price_copper: int,
    deposit_hours: int = 8,
    burn_previous: bool = False,
    custom_deposit_copper: int | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    async with personal_session_factory() as psession:
        pos = await psession.get(TradePosition, pos_id)
        if not pos or pos.status not in ("holding", "listed"):
            return {"error": "Позиция недоступна для выставления"}

        # Сжигаем прошлый залог только при явном подтверждении отмены лота в игре
        if burn_previous and pos.status == "listed" and pos.active_deposit_copper > 0:
            pos.burned_deposit_copper += pos.active_deposit_copper

        dep = (
            custom_deposit_copper
            if custom_deposit_copper is not None
            else calc_wow_deposit(pos.vendor_unit_copper, pos.qty, deposit_hours)
        )
        pos.list_price_copper = list_price_copper
        pos.deposit_hours = deposit_hours
        pos.active_deposit_copper = max(0, dep)
        pos.status = "listed"
        pos.listed_at = now
        await psession.commit()
        return {"id": pos.id, "status": "listed", "deposit": pos.active_deposit_copper}


async def reset_burned_deposit(pos_id: int) -> dict:
    """Обнуляет ошибочно начисленный сгоревший залог на активной позиции."""
    async with personal_session_factory() as psession:
        pos = await psession.get(TradePosition, pos_id)
        if not pos:
            return {"error": "Позиция не найдена"}
        pos.burned_deposit_copper = 0
        await psession.commit()
        return {"status": "ok"}


async def mark_position_sold(
    pos_id: int,
    sold_qty: int | None = None,
    actual_sell_price_copper: int | None = None,
) -> dict:
    """Фиксация продажи: возврат активного залога, вычет 5% комиссии аукциона и сгоревших залогов."""
    now = datetime.now(timezone.utc)
    async with personal_session_factory() as psession:
        pos = await psession.get(TradePosition, pos_id)
        if not pos or pos.status not in ("holding", "listed"):
            return {"error": "Позиция уже закрыта"}

        unit_sell = actual_sell_price_copper if actual_sell_price_copper else pos.list_price_copper
        if unit_sell <= 0:
            return {"error": "Не указана цена продажи"}

        qty_to_sell = min(pos.qty, sold_qty) if (sold_qty and sold_qty > 0) else pos.qty
        ratio = qty_to_sell / pos.qty

        burned_share = int(round(pos.burned_deposit_copper * ratio))
        active_dep_share = int(round(pos.active_deposit_copper * ratio))

        gross_revenue = unit_sell * qty_to_sell
        ah_fee = int(gross_revenue * 0.05)
        cost_basis = pos.buy_price_copper * qty_to_sell
        net_pnl = gross_revenue - ah_fee - cost_basis - burned_share

        if qty_to_sell == pos.qty:
            pos.sell_price_copper = unit_sell
            pos.ah_fee_copper = ah_fee
            pos.realized_pnl_copper = net_pnl
            pos.active_deposit_copper = 0
            pos.status = "sold"
            pos.closed_at = now
        else:
            pos.qty -= qty_to_sell
            pos.burned_deposit_copper -= burned_share
            pos.active_deposit_copper -= active_dep_share

            sold_part = TradePosition(
                item_id=pos.item_id,
                qty=qty_to_sell,
                buy_price_copper=pos.buy_price_copper,
                list_price_copper=unit_sell,
                sell_price_copper=unit_sell,
                vendor_unit_copper=pos.vendor_unit_copper,
                deposit_hours=pos.deposit_hours,
                active_deposit_copper=0,
                burned_deposit_copper=burned_share,
                ah_fee_copper=ah_fee,
                realized_pnl_copper=net_pnl,
                status="sold",
                comment=pos.comment,
                bought_at=pos.bought_at,
                listed_at=pos.listed_at,
                closed_at=now,
            )
            psession.add(sold_part)

        await psession.commit()
        return {"status": "sold", "net_pnl": net_pnl, "ah_fee": ah_fee, "returned_deposit": active_dep_share}


async def mark_position_expired(pos_id: int) -> dict:
    """Лот не купили: залог сгорает, предмет возвращается в сумку (holding)."""
    async with personal_session_factory() as psession:
        pos = await psession.get(TradePosition, pos_id)
        if not pos or pos.status != "listed":
            return {"error": "Лот не находится на аукционе"}

        burned = pos.active_deposit_copper
        pos.burned_deposit_copper += burned
        pos.active_deposit_copper = 0
        pos.list_price_copper = 0
        pos.status = "holding"
        await psession.commit()
        return {"status": "holding", "burned_now": burned, "total_burned": pos.burned_deposit_copper}


async def mark_position_vendored(pos_id: int) -> dict:
    """Закрытие позиции сдачей торговцу (без 5% комиссии АН, с учётом сгоревших залогов)."""
    now = datetime.now(timezone.utc)
    async with personal_session_factory() as psession:
        pos = await psession.get(TradePosition, pos_id)
        if not pos or pos.status not in ("holding", "listed"):
            return {"error": "Позиция уже закрыта"}

        if pos.active_deposit_copper > 0:
            pos.burned_deposit_copper += pos.active_deposit_copper
            pos.active_deposit_copper = 0

        vendor_payout = pos.vendor_unit_copper * pos.qty
        cost_basis = pos.buy_price_copper * pos.qty
        net_pnl = vendor_payout - cost_basis - pos.burned_deposit_copper

        pos.sell_price_copper = pos.vendor_unit_copper
        pos.ah_fee_copper = 0
        pos.realized_pnl_copper = net_pnl
        pos.status = "vendored"
        pos.closed_at = now
        await psession.commit()
        return {"status": "vendored", "net_pnl": net_pnl}


async def delete_position(pos_id: int) -> dict:
    async with personal_session_factory() as psession:
        pos = await psession.get(TradePosition, pos_id)
        if pos:
            await psession.delete(pos)
            await psession.commit()
        return {"status": "deleted"}


async def get_portfolio_overview() -> dict:
    async with personal_session_factory() as psession:
        positions = list(
            (await psession.execute(select(TradePosition).order_by(desc(TradePosition.id)))).scalars().all()
        )

    items_map: dict[int, ItemMeta] = {}
    latest_prices: dict[int, int] = {}

    item_ids = list({p.item_id for p in positions})
    if item_ids:
        async with async_session_factory() as msession:
            meta_rows = (
                await msession.execute(select(ItemMeta).where(ItemMeta.item_id.in_(item_ids)))
            ).scalars().all()
            items_map = {r.item_id: r for r in meta_rows}

            latest_id_subq = (
                select(MarketTick.item_id, func.max(MarketTick.id).label("max_id"))
                .where(MarketTick.item_id.in_(item_ids), MarketTick.entry_price > 0)
                .group_by(MarketTick.item_id)
                .subquery()
            )
            tick_rows = (
                await msession.execute(
                    select(MarketTick.item_id, MarketTick.entry_price).join(
                        latest_id_subq, MarketTick.id == latest_id_subq.c.max_id
                    )
                )
            ).all()
            latest_prices = {int(iid): int(ep or 0) for iid, ep in tick_rows}

    active_list = []
    closed_list = []

    invested_holding = 0
    invested_listed = 0
    active_deposits = 0
    burned_deposits_total = 0
    realized_pnl_total = 0
    total_ah_fees = 0

    for p in positions:
        meta = items_map.get(p.item_id)
        name = meta.name if meta else f"Item_{p.item_id}"
        icon = (meta.icon_name if meta else None) or "inv_misc_questionmark"
        curr_market = latest_prices.get(p.item_id, 0)

        cost_total = p.buy_price_copper * p.qty
        burned_deposits_total += p.burned_deposit_copper

        if p.status in ("holding", "listed"):
            if p.status == "holding":
                invested_holding += cost_total
            else:
                invested_listed += cost_total
                active_deposits += p.active_deposit_copper

            expected_fee = int(p.list_price_copper * p.qty * 0.05) if p.list_price_copper > 0 else 0
            expected_net = (
                (p.list_price_copper * p.qty) - expected_fee - cost_total - p.burned_deposit_copper
                if p.list_price_copper > 0
                else 0
            )

            active_list.append({
                "id": p.id,
                "item_id": p.item_id,
                "name": name,
                "icon": icon,
                "qty": p.qty,
                "buy_unit": p.buy_price_copper,
                "cost_total": cost_total,
                "list_unit": p.list_price_copper,
                "vendor_unit": p.vendor_unit_copper,
                "current_ah_unit": curr_market,
                "deposit_hours": p.deposit_hours,
                "active_deposit": p.active_deposit_copper,
                "burned_deposit": p.burned_deposit_copper,
                "expected_fee": expected_fee,
                "expected_net": expected_net,
                "status": p.status,
                "comment": p.comment,
                "bought_at": _to_unix(p.bought_at),
            })
        else:
            realized_pnl_total += p.realized_pnl_copper
            total_ah_fees += p.ah_fee_copper
            closed_list.append({
                "id": p.id,
                "item_id": p.item_id,
                "name": name,
                "icon": icon,
                "qty": p.qty,
                "buy_unit": p.buy_price_copper,
                "sell_unit": p.sell_price_copper,
                "ah_fee": p.ah_fee_copper,
                "burned_deposit": p.burned_deposit_copper,
                "realized_pnl": p.realized_pnl_copper,
                "status": p.status,
                "comment": p.comment,
                "closed_at": _to_unix(p.closed_at),
            })

    return {
        "summary": {
            "invested_holding": invested_holding,
            "invested_listed": invested_listed,
            "invested_total": invested_holding + invested_listed,
            "active_deposits": active_deposits,
            "burned_deposits": burned_deposits_total,
            "realized_pnl": realized_pnl_total,
            "total_ah_fees": total_ah_fees,
            "active_count": len(active_list),
            "closed_count": len(closed_list),
        },
        "active": active_list,
        "closed": closed_list,
    }


async def get_item_trade_events(item_id: int) -> list[dict]:
    """Возвращает точки покупок и продаж предмета для отрисовки прямо на графике."""
    async with personal_session_factory() as psession:
        rows = (
            await psession.execute(
                select(TradePosition).where(TradePosition.item_id == item_id).order_by(TradePosition.id)
            )
        ).scalars().all()

    events = []
    for r in rows:
        if r.bought_at:
            events.append({
                "type": "buy",
                "time": _to_unix(r.bought_at),
                "price_silver": round(r.buy_price_copper / 100.0, 2),
                "price_copper": r.buy_price_copper,
                "qty": r.qty,
                "comment": r.comment,
            })
        if r.status == "sold" and r.closed_at:
            events.append({
                "type": "sold",
                "time": _to_unix(r.closed_at),
                "price_silver": round(r.sell_price_copper / 100.0, 2),
                "price_copper": r.sell_price_copper,
                "qty": r.qty,
                "pnl_copper": r.realized_pnl_copper,
            })
        elif r.status == "listed" and r.listed_at:
            events.append({
                "type": "listed",
                "time": _to_unix(r.listed_at),
                "price_silver": round(r.list_price_copper / 100.0, 2),
                "price_copper": r.list_price_copper,
                "qty": r.qty,
            })
    return events