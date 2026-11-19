import asyncio
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import httpx
from sqlalchemy import desc, select
from config import STARTER_ACTIVE_POOL, settings
from database import async_session_factory
from models import ItemMeta, RawAuctionLot, SnapshotRun
from resolver import resolve_pending_items

logger = logging.getLogger("collector")


class BlizzardAuctionCollector:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._last_modified_header: str | None = None

    async def get_token(self, client: httpx.AsyncClient) -> str:
        now = datetime.now(timezone.utc)
        if self._token and now < self._token_expires_at - timedelta(minutes=5):
            return self._token

        resp = await client.post(
            "https://oauth.battle.net/token",
            data={"grant_type": "client_credentials"},
            auth=(settings.BNET_CLIENT_ID, settings.BNET_CLIENT_SECRET),
            timeout=15.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = now + timedelta(seconds=int(payload["expires_in"]))
        return self._token

    async def verify_houses(self, client: httpx.AsyncClient) -> None:
        """Проверка доступных аукционных домов выбранного ruleset при старте."""
        token = await self.get_token(client)
        url = f"https://{settings.BNET_REGION}.api.blizzard.com/data/wow/ruleset/{settings.RULESET_ID}/auctions/index"
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"namespace": settings.BNET_NAMESPACE_DYNAMIC},
            timeout=15.0,
        )
        resp.raise_for_status()
        houses = resp.json().get("houses", [])
        house_ids = [h.get("id") for h in houses]
        logger.info(f"Ruleset {settings.RULESET_ID}: доступные houseId = {house_ids} (выбран {settings.HOUSE_ID}).")

    async def fetch_dump(
        self, client: httpx.AsyncClient, force_cold_start: bool = False
    ) -> tuple[list[dict] | None, datetime | None]:
        token = await self.get_token(client)
        base_url = (
            f"https://{settings.BNET_REGION}.api.blizzard.com"
            f"/data/wow/ruleset/{settings.RULESET_ID}/auctions/{settings.HOUSE_ID}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "gzip",
        }
        if self._last_modified_header and not force_cold_start:
            headers["If-Modified-Since"] = self._last_modified_header

        params = {"namespace": settings.BNET_NAMESPACE_DYNAMIC}

        # 1. Commodities (трава, руда, ткань, пыль, зелья)
        resp_comm = await client.get(
            f"{base_url}/commodities", headers=headers, params=params, timeout=30.0
        )
        if resp_comm.status_code == 304:
            return None, None
        resp_comm.raise_for_status()

        last_mod = resp_comm.headers.get("Last-Modified")
        if last_mod:
            self._last_modified_header = last_mod
            snapshot_ts = parsedate_to_datetime(last_mod).astimezone(timezone.utc)
        else:
            snapshot_ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        lots: list[dict] = resp_comm.json().get("auctions", [])

        # 2. Основной аукцион (рецепты, сумки, реагенты без случайных суффиксов)
        resp_auc = await client.get(
            base_url,
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
            params=params,
            timeout=30.0,
        )
        if resp_auc.status_code == 200:
            for auc in resp_auc.json().get("auctions", []):
                item_obj = auc.get("item", {})
                # Жёсткий фильтр: отсекаем случайные статы (rand / seed / bonus_lists)
                if item_obj.get("rand") or item_obj.get("seed") or item_obj.get("bonus_lists"):
                    continue
                buyout = int(auc.get("buyout", 0))
                qty = int(auc.get("quantity", 1))
                if buyout > 0 and qty > 0:
                    lots.append(
                        {
                            "id": auc["id"],
                            "item": {"id": item_obj["id"]},
                            "unit_price": buyout // qty,
                            "quantity": qty,
                            "time_left": auc.get("time_left", "LONG"),
                        }
                    )

        return lots, snapshot_ts

    async def persist_dump(
        self, lots: list[dict], snapshot_ts: datetime, is_cold_start: bool
    ) -> bool:
        async with async_session_factory() as session:
            # Защита от дублей при перезапуске скрипта в течение одного часа
            existing = await session.execute(
                select(SnapshotRun).where(SnapshotRun.snapshot_ts == snapshot_ts)
            )
            if existing.scalar_one_or_none():
                logger.info(f"Срез {snapshot_ts.isoformat()} уже есть в базе, пропускаю повторную запись.")
                return False

            # Вычисляем разрыв от предыдущего среза (контроль ночного простоя ПК)
            prev_stmt = select(SnapshotRun).order_by(desc(SnapshotRun.snapshot_ts)).limit(1)
            prev_run = (await session.execute(prev_stmt)).scalar_one_or_none()
            gap_minutes = None
            if prev_run:
                prev_ts = prev_run.snapshot_ts
                if prev_ts.tzinfo is None:
                    prev_ts = prev_ts.replace(tzinfo=timezone.utc)
                gap_minutes = round((snapshot_ts - prev_ts).total_seconds() / 60.0, 1)

            # Загружаем справочник предметов
            known_items = {
                row.item_id: row
                for row in (await session.execute(select(ItemMeta))).scalars().all()
            }

            # Считаем суммарный объём по всем предметам для теневого отбора топ-200
            volume_by_item: dict[int, int] = {}
            for lot in lots:
                iid = int(lot["item"]["id"])
                volume_by_item[iid] = volume_by_item.get(iid, 0) + int(lot.get("quantity", 1))

            top_shadow_ids = {
                iid
                for iid, _ in sorted(
                    volume_by_item.items(), key=lambda x: x[1], reverse=True
                )[: settings.SHADOW_POOL_LIMIT]
            }
            active_ids = set(STARTER_ACTIVE_POOL.keys()) | {
                iid for iid, meta in known_items.items() if meta.is_active_pool
            }
            tracked_ids = active_ids | top_shadow_ids

            new_meta: list[ItemMeta] = []
            for iid in tracked_ids:
                if iid not in known_items:
                    starter = STARTER_ACTIVE_POOL.get(iid)
                    meta = ItemMeta(
                        item_id=iid,
                        name=starter["name"] if starter else f"Item_{iid}",
                        category=starter["category"] if starter else "shadow",
                        max_stack=starter["stack"] if starter else 20,
                        is_active_pool=bool(starter),
                    )
                    known_items[iid] = meta
                    new_meta.append(meta)

            if new_meta:
                session.add_all(new_meta)

            run = SnapshotRun(
                snapshot_ts=snapshot_ts,
                gap_minutes=gap_minutes,
                is_cold_start=is_cold_start,
                total_raw_lots=len(lots),
                stored_lots=0,
            )
            session.add(run)
            await session.flush()

            raw_rows: list[RawAuctionLot] = []
            for lot in lots:
                iid = int(lot["item"]["id"])
                if iid not in tracked_ids:
                    continue
                unit_price = int(lot.get("unit_price", 0))
                qty = int(lot.get("quantity", 1))
                if unit_price <= 0:
                    continue

                meta = known_items[iid]
                raw_rows.append(
                    RawAuctionLot(
                        snapshot_id=run.id,
                        snapshot_ts=snapshot_ts,
                        auction_id=int(lot["id"]),
                        item_id=iid,
                        unit_price=unit_price,
                        quantity=qty,
                        time_left=str(lot.get("time_left", "LONG")),
                        is_full_stack=(qty >= meta.max_stack and meta.max_stack > 1),
                    )
                )

            run.stored_lots = len(raw_rows)
            session.add_all(raw_rows)
            await session.commit()

            gap_note = f"разрыв {gap_minutes} мин" if gap_minutes is not None else "первый срез в базе"
            logger.info(
                f"Срез #{run.id} [{snapshot_ts.isoformat()}] ({gap_note}): "
                f"сохранено {len(raw_rows)} из {len(lots)} лотов по {len(tracked_ids)} позициям."
            )
            return True

    async def run_forever(self) -> None:
        first_boot = True
        async with httpx.AsyncClient() as client:
            await self.verify_houses(client)
            while True:
                try:
                    lots, snapshot_ts = await self.fetch_dump(client, force_cold_start=first_boot)
                    if lots and snapshot_ts:
                        saved = await self.persist_dump(lots, snapshot_ts, is_cold_start=first_boot)
                        first_boot = False

                        # Фоново подтягиваем названия и цены вендора для новых ID
                        token = await self.get_token(client)
                        asyncio.create_task(resolve_pending_items(token))

                        if saved:
                            logger.info("Сон 55 минут до следующего окна обновления API...")
                            await asyncio.sleep(55 * 60)
                        else:
                            await asyncio.sleep(60)
                    else:
                        await asyncio.sleep(60)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(f"Сбой цикла сбора (повтор через 60 сек): {exc}")
                    await asyncio.sleep(60)