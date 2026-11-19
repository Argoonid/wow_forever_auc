import asyncio
import sqlite3
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, text
from database import (
    PERSONAL_TABLES,
    _get_all_metadata,
    init_market_db,
    init_personal_db,
    market_engine,
    personal_engine,
)

SOURCE_SQLITE = "forever_ah.db"


def _coerce_row(table, row_dict: dict) -> dict:
    out = {}
    for col in table.columns:
        if col.name not in row_dict:
            continue
        val = row_dict[col.name]
        if val is None:
            out[col.name] = None
        elif isinstance(col.type, Boolean):
            out[col.name] = bool(val)
        elif isinstance(col.type, DateTime) and isinstance(val, str):
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            out[col.name] = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        else:
            out[col.name] = val
    return out


async def migrate() -> None:
    await init_market_db()
    await init_personal_db()

    src = sqlite3.connect(SOURCE_SQLITE)
    src.row_factory = sqlite3.Row
    existing_src_tables = {
        r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }

    meta = _get_all_metadata()

    for table in meta.sorted_tables:
        if table.name not in existing_src_tables:
            continue
        rows = [dict(r) for r in src.execute(f"SELECT * FROM {table.name}").fetchall()]
        if not rows:
            print(f"[SKIP] {table.name}: пусто")
            continue

        target_engine = personal_engine if table.name in PERSONAL_TABLES else market_engine
        coerced = [_coerce_row(table, r) for r in rows]

        async with target_engine.begin() as conn:
            # Очищаем целевую таблицу перед разовой заливкой, чтобы не было дублей
            await conn.execute(table.delete())
            batch_size = 500
            for i in range(0, len(coerced), batch_size):
                await conn.execute(table.insert(), coerced[i : i + batch_size])

            # Синхронизируем SERIAL-счётчики PostgreSQL после вставки явных ID
            if target_engine is market_engine and "id" in table.columns:
                await conn.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), true)"
                    )
                )
        dest_label = "personal.db" if table.name in PERSONAL_TABLES else "Neon Cloud"
        print(f"[OK] {table.name} -> {dest_label}: перенесено {len(coerced)} строк")

    # Переносим радар (watchlist_items), если он уже есть в старой базе
    if "watchlist_items" in existing_src_tables:
        wl_rows = [dict(r) for r in src.execute("SELECT * FROM watchlist_items").fetchall()]
        if wl_rows:
            async with personal_engine.begin() as conn:
                await conn.execute(text("DELETE FROM watchlist_items"))
                for r in wl_rows:
                    await conn.execute(
                        text(
                            "INSERT INTO watchlist_items "
                            "(item_id, bucket, added_price_copper, target_buy_copper, target_sell_copper, note, created_at) "
                            "VALUES (:item_id, :bucket, :added_price_copper, :target_buy_copper, :target_sell_copper, :note, :created_at)"
                        ),
                        r,
                    )
            print(f"[OK] watchlist_items -> personal.db: перенесено {len(wl_rows)} строк")

    src.close()
    await market_engine.dispose()
    await personal_engine.dispose()


if __name__ == "__main__":
    asyncio.run(migrate())