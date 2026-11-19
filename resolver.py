import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
import httpx
from sqlalchemy import or_, select
from database import async_session_factory
from models import ItemMeta

logger = logging.getLogger("resolver")


async def _fetch_wowhead_meta(client: httpx.AsyncClient, item_id: int) -> dict | None:
    urls = [
        f"https://nether.wowhead.com/classic/ru/tooltip/item/{item_id}",
        f"https://nether.wowhead.com/tooltip/item/{item_id}?dataEnv=4&locale=8",
    ]
    for url in urls:
        try:
            resp = await client.get(url, timeout=10.0)
            if resp.status_code != 200:
                continue
            data = resp.json()
            name = (data.get("name") or "").strip()
            if not name:
                continue

            icon = (data.get("icon") or "inv_misc_questionmark").lower()
            tooltip = data.get("tooltip", "")

            sell_block_match = re.search(
                r"(?:whtt-sellprice|Цена продажи:|Sell Price:)(.*?)(?:</div>|<br|$)",
                tooltip,
                re.IGNORECASE | re.DOTALL,
            )
            sell_copper = 0
            if sell_block_match:
                block = sell_block_match.group(1)
                gold = re.search(r'class="moneygold">(\d+)<', block)
                silver = re.search(r'class="moneysilver">(\d+)<', block)
                copper = re.search(r'class="moneycopper">(\d+)<', block)
                sell_copper = (
                    (int(gold.group(1)) * 10000 if gold else 0)
                    + (int(silver.group(1)) * 100 if silver else 0)
                    + (int(copper.group(1)) if copper else 0)
                )

            return {"name": name, "icon": icon, "vendor_sell_price": sell_copper}
        except Exception:
            continue
    return None


async def resolve_missing_metadata(batch_limit: int = 80) -> None:
    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        stmt = (
            select(ItemMeta)
            .where(
                or_(ItemMeta.meta_resolved.is_(False), ItemMeta.icon_name == "inv_misc_questionmark"),
                or_(ItemMeta.meta_retry_after.is_(None), ItemMeta.meta_retry_after <= now),
            )
            .limit(batch_limit)
        )
        pending = list((await session.execute(stmt)).scalars().all())

    if not pending:
        return

    sem = asyncio.Semaphore(6)
    async with httpx.AsyncClient() as client:
        async def _task(iid: int):
            async with sem:
                res = await _fetch_wowhead_meta(client, iid)
                await asyncio.sleep(0.12)
                return iid, res

        resolved = await asyncio.gather(*(_task(item.item_id) for item in pending))

    ok = 0
    async with async_session_factory() as session:
        for iid, info in resolved:
            db_item = await session.get(ItemMeta, iid)
            if not db_item:
                continue
            if info:
                db_item.name = info["name"]
                db_item.icon_name = info["icon"]
                db_item.vendor_sell_price = info["vendor_sell_price"]
                db_item.meta_resolved = True
                db_item.meta_retry_after = None
                ok += 1
            else:
                db_item.meta_retry_after = now + timedelta(hours=24)
        await session.commit()

    logger.info(f"Резолвер Wowhead (ruRU): обновлено {ok}/{len(pending)} предметов.")