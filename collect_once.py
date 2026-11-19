import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, desc, select
from analyzer import evaluate_opportunities
from config import STARTER_ACTIVE_POOL, settings
from database import async_session_factory, init_market_db, market_engine
from models import ItemMeta, MarketTick, SnapshotRun
from resolver import resolve_missing_metadata
from sources.blizzard_forever import BlizzardForeverSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("collect_once")


async def prune_old_ticks() -> None:
    if settings.RETENTION_DAYS <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.RETENTION_DAYS)
    async with async_session_factory() as session:
        res = await session.execute(delete(MarketTick).where(MarketTick.snapshot_ts < cutoff))
        await session.commit()
        if res.rowcount and res.rowcount > 0:
            logger.info(f"Автоочистка: удалено {res.rowcount} устаревших тиков старше {settings.RETENTION_DAYS} дн.")


async def run_once() -> None:
    await init_market_db()
    source = BlizzardForeverSource()
    source_name = "blizzard"

    snapshot_ts, ticks = await source.fetch_market_snapshot()
    if snapshot_ts.tzinfo is None:
        snapshot_ts = snapshot_ts.replace(tzinfo=timezone.utc)

    if not ticks:
        logger.info("Новых тиков от Blizzard не получено — завершаю прогон.")
        return

    async with async_session_factory() as session:
        dup_stmt = select(SnapshotRun).where(
            SnapshotRun.source == source_name,
            SnapshotRun.snapshot_ts == snapshot_ts,
        )
        if (await session.execute(dup_stmt)).scalar_one_or_none():
            logger.info(f"Срез [{snapshot_ts.isoformat()}] уже есть в облачной БД — пропуск.")
            return

        prev_run = (
            await session.execute(select(SnapshotRun).order_by(desc(SnapshotRun.snapshot_ts)).limit(1))
        ).scalar_one_or_none()

        gap_minutes = None
        is_continuous = True
        if prev_run:
            prev_ts = prev_run.snapshot_ts
            if prev_ts.tzinfo is None:
                prev_ts = prev_ts.replace(tzinfo=timezone.utc)
            gap_minutes = round((snapshot_ts - prev_ts).total_seconds() / 60.0, 1)
            is_continuous = gap_minutes <= settings.MAX_CONTINUOUS_GAP_MINUTES

        top_shadow_ids = {
            t.item_id
            for t in sorted(ticks, key=lambda x: (getattr(x, "sold_per_day", 0.0), x.quantity), reverse=True)[
                : settings.SHADOW_POOL_LIMIT
            ]
        }
        known_items = {row.item_id: row for row in (await session.execute(select(ItemMeta))).scalars().all()}
        active_ids = set(STARTER_ACTIVE_POOL.keys()) | {
            iid for iid, m in known_items.items() if m.is_active_pool
        }
        tracked_ids = active_ids | top_shadow_ids

        run = SnapshotRun(
            source=source_name,
            snapshot_ts=snapshot_ts,
            gap_minutes=gap_minutes,
            is_continuous=is_continuous,
            tracked_items_count=0,
        )
        session.add(run)
        await session.flush()

        db_ticks = []
        for t in ticks:
            if t.item_id not in tracked_ids:
                continue
            meta = known_items.get(t.item_id)
            if not meta:
                starter = STARTER_ACTIVE_POOL.get(t.item_id)
                meta = ItemMeta(
                    item_id=t.item_id,
                    name=starter["name"] if starter else f"Item_{t.item_id}",
                    category=starter["category"] if starter else "shadow",
                    max_stack=starter["stack"] if starter else 20,
                    is_active_pool=bool(starter),
                )
                session.add(meta)
                known_items[t.item_id] = meta

            db_ticks.append(
                MarketTick(
                    snapshot_id=run.id,
                    snapshot_ts=snapshot_ts,
                    day_of_week=snapshot_ts.weekday(),
                    hour_of_day=snapshot_ts.hour,
                    source=source_name,
                    item_id=t.item_id,
                    min_buyout=t.min_buyout,
                    entry_price=t.entry_price,
                    wall_price=t.wall_price,
                    market_value=t.market_value,
                    quantity=t.quantity,
                    num_auctions=t.num_auctions,
                )
            )

        run.tracked_items_count = len(db_ticks)
        session.add_all(db_ticks)
        await session.commit()
        snapshot_id = run.id

    logger.info(f"Срез #{snapshot_id} записан в облако: {len(db_ticks)} позиций (разрыв: {gap_minutes} мин).")
    await resolve_missing_metadata(batch_limit=80)
    await evaluate_opportunities(snapshot_id)
    await prune_old_ticks()


async def main() -> None:
    try:
        await run_once()
    finally:
        await market_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())