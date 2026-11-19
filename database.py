from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from sqlalchemy import DateTime, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from config import settings

PERSONAL_TABLES = {"trade_positions", "chart_drawings", "addon_processed_events"}


class Base(DeclarativeBase):
    pass


def _normalize_db_url(raw_url: str) -> tuple[str, dict, bool]:
    """Приводит URL к формату SQLAlchemy async и вычищает параметры, несовместимые с asyncpg."""
    url = (raw_url or "").strip()
    is_pg = url.startswith("postgres://") or url.startswith("postgresql")
    if not is_pg:
        return url, {}, False

    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]

    parsed = urlparse(url)
    blocked_keys = {"sslmode", "channel_binding", "options"}
    clean_query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in blocked_keys]
    clean_url = urlunparse(parsed._replace(query=urlencode(clean_query)))

    connect_args = {
        "ssl": "require",
        "statement_cache_size": 0,
    }
    return clean_url, connect_args, True


_raw_market_url = settings.MARKET_DATABASE_URL.strip() or settings.DATABASE_URL.strip()
MARKET_URL, _market_connect_args, IS_MARKET_POSTGRES = _normalize_db_url(_raw_market_url)

_market_engine_kwargs = {"echo": False, "connect_args": _market_connect_args}
if IS_MARKET_POSTGRES:
    _market_engine_kwargs.update({"pool_pre_ping": True, "pool_recycle": 240, "pool_size": 5, "max_overflow": 5})

market_engine = create_async_engine(MARKET_URL, **_market_engine_kwargs)
async_session_factory = async_sessionmaker(market_engine, class_=AsyncSession, expire_on_commit=False)

PERSONAL_URL, _personal_connect_args, _ = _normalize_db_url(settings.PERSONAL_DATABASE_URL)
personal_engine = create_async_engine(PERSONAL_URL, echo=False, connect_args=_personal_connect_args)
personal_session_factory = async_sessionmaker(personal_engine, class_=AsyncSession, expire_on_commit=False)

engine = market_engine


def _get_all_metadata():
    import models  # noqa: F401
    meta = getattr(models, "Base", Base).metadata
    if IS_MARKET_POSTGRES:
        for tbl in meta.tables.values():
            if tbl.name in PERSONAL_TABLES:
                continue
            for col in tbl.columns:
                if isinstance(col.type, DateTime) and not col.type.timezone:
                    col.type.timezone = True
    return meta


async def init_market_db() -> None:
    if settings.MARKET_READ_ONLY:
        return
    meta = _get_all_metadata()
    market_tables = [t for t in meta.sorted_tables if t.name not in PERSONAL_TABLES]
    async with market_engine.begin() as conn:
        if IS_MARKET_POSTGRES:
            await conn.execute(text("ALTER TABLE IF EXISTS items_meta ALTER COLUMN icon_name TYPE VARCHAR(255)"))
            await conn.execute(text("ALTER TABLE IF EXISTS items_meta ALTER COLUMN name TYPE VARCHAR(255)"))
        else:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.run_sync(lambda sync_conn: meta.create_all(sync_conn, tables=market_tables))


async def init_personal_db() -> None:
    meta = _get_all_metadata()
    personal_tables = [t for t in meta.sorted_tables if t.name in PERSONAL_TABLES]
    async with personal_engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.run_sync(lambda sync_conn: meta.create_all(sync_conn, tables=personal_tables))
        await conn.execute(
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


async def init_db() -> None:
    await init_market_db()
    await init_personal_db()