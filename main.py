import asyncio
import ctypes
import gc
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from sqlalchemy import desc, select
from analyzer import evaluate_opportunities
from config import STARTER_ACTIVE_POOL, settings
from database import async_session_factory, init_db
from models import ItemMeta, MarketTick, SnapshotRun
from resolver import resolve_missing_metadata
from sources.blizzard_forever import BlizzardForeverSource



def trim_process_memory() -> None:
    """Принудительно собирает мусор и возвращает неиспользуемые страницы памяти ОС Windows."""
    gc.collect()
    if sys.platform == "win32":
        try:
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.psapi.EmptyWorkingSet(handle)
        except Exception:
            pass


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        "collector.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


async def store_and_analyze() -> None:
    logger = logging.getLogger("pipeline")

    source = BlizzardForeverSource()
    source_name = "blizzard"
    source_label = "Blizzard API"
    poll_seconds = 4 * 60  # Проверка заголовка Last-Modified каждые 4 минуты
   
    while True:
        try:
            await resolve_missing_metadata(batch_limit=150)

            snapshot_ts, ticks = await source.fetch_market_snapshot()
            if snapshot_ts.tzinfo is None:
                snapshot_ts = snapshot_ts.replace(tzinfo=timezone.utc)

            if not ticks:
                trim_process_memory()
                local_ts = snapshot_ts.astimezone().strftime("%H:%M:%S")
                age_min = max(0, int((datetime.now(timezone.utc) - snapshot_ts).total_seconds() // 60))
                logger.info(
                    f"[{source_label}] Новых изменений нет (последний скан в {local_ts}, возраст {age_min // 60}ч {age_min % 60}м). "
                    f"Следующая проверка через {poll_seconds // 60} мин..."
                )
                await asyncio.sleep(poll_seconds)
                continue

            async with async_session_factory() as session:
                dup_stmt = select(SnapshotRun).where(
                    SnapshotRun.source == source_name,
                    SnapshotRun.snapshot_ts == snapshot_ts,
                )
                if (await session.execute(dup_stmt)).scalar_one_or_none():
                    del ticks
                    session.expunge_all()
                    trim_process_memory()
                    local_ts = snapshot_ts.astimezone().strftime("%H:%M:%S")
                    logger.info(
                        f"[{source_label}] Срез [{local_ts}] уже записан в БД, жду следующий час..."
                    )
                    await asyncio.sleep(poll_seconds)
                    continue

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
                    for t in sorted(
                        ticks, key=lambda x: (getattr(x, "sold_per_day", 0.0), x.quantity), reverse=True
                    )[: settings.SHADOW_POOL_LIMIT]
                }
                known_items = {
                    row.item_id: row for row in (await session.execute(select(ItemMeta))).scalars().all()
                }
                active_ids = set(STARTER_ACTIVE_POOL.keys()) | {
                    iid for iid, m in known_items.items() if m.is_active_pool
                }
                tracked_ids = active_ids | top_shadow_ids

                for t in ticks:
                    if t.item_id not in tracked_ids:
                        continue
                    item_name = getattr(t, "item_name", None)
                    meta = known_items.get(t.item_id)
                    if not meta:
                        starter = STARTER_ACTIVE_POOL.get(t.item_id)
                        resolved_name = (
                            starter["name"]
                            if starter
                            else (item_name if item_name else f"Item_{t.item_id}")
                        )
                        meta = ItemMeta(
                            item_id=t.item_id,
                            name=resolved_name,
                            category=starter["category"] if starter else "shadow",
                            max_stack=starter["stack"] if starter else 20,
                            is_active_pool=bool(starter),
                        )
                        session.add(meta)
                        known_items[t.item_id] = meta
                    elif meta.name.startswith("Item_") and item_name:
                        meta.name = item_name

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
                    meta = known_items[t.item_id]
                    hist_val = getattr(t, "historical_value", 0)
                    sale_rate = getattr(t, "sale_rate", 0.0)
                    sold_per_day = getattr(t, "sold_per_day", 0.0)

                    if t.market_value > 0:
                        meta.tsm_market_value = t.market_value
                    if hist_val > 0:
                        meta.tsm_historical_value = hist_val
                    if sale_rate > 0:
                        meta.tsm_sale_rate = sale_rate
                    if sold_per_day > 0:
                        meta.velocity_score = sold_per_day

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

                saved_count = len(db_ticks)
                run.tracked_items_count = saved_count
                session.add_all(db_ticks)
                await session.commit()
                snapshot_id = run.id
                session.expunge_all()

            del ticks, db_ticks, known_items
            gap_msg = f"разрыв {gap_minutes} мин" if gap_minutes is not None else "первый срез"
            logger.info(f"Срез #{snapshot_id} [{source_label}] ({gap_msg}): записано {saved_count} позиций.")

            await resolve_missing_metadata(batch_limit=150)
            await evaluate_opportunities(snapshot_id)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger("pipeline").exception(f"Ошибка цикла сбора ({source_label}): {exc}")
        finally:
            trim_process_memory()

        await asyncio.sleep(poll_seconds)


async def main() -> None:
    setup_logging()
    logging.getLogger("main").info("Инициализация БД и запуск коллектора [Blizzard Game Data API]...")
    await init_db()
    await store_and_analyze()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nКоллектор остановлен.")