import csv
import gc
import io
import logging
from datetime import datetime, timedelta, timezone
import httpx
from config import settings
from sources.base import AuctionDataSource, NormalizedItemTick

logger = logging.getLogger("source.tsm")

BASE_PUBLIC_URL = "https://public-data.tradeskillmaster.com"


class TSMAuctionSource(AuctionDataSource):
    name = "tsm"

    def __init__(self) -> None:
        # Храним компактные кортежи: item_id -> (sale_rate, sold_per_day, region_market)
        self._region_stats_cache: dict[int, tuple[float, float, int]] = {}
        self._region_cache_expires: datetime = datetime.min.replace(tzinfo=timezone.utc)

    def _realm_items_url(self) -> str:
        return (
            f"{BASE_PUBLIC_URL}/{settings.TSM_GAME_TYPE}/{settings.TSM_REGION_SLUG}"
            f"/realm/{settings.TSM_REALM_SLUG}/items.csv"
        )

    def _region_items_url(self) -> str:
        return (
            f"{BASE_PUBLIC_URL}/{settings.TSM_GAME_TYPE}/{settings.TSM_REGION_SLUG}"
            f"/region/items.csv"
        )

    @staticmethod
    def _extract_item_id(row: dict[str, str]) -> int | None:
        raw = (row.get("itemId") or row.get("itemId / petSpeciesId") or "").strip()
        return int(raw) if raw.isdigit() else None

    @staticmethod
    def _parse_int(val: str | None) -> int:
        if not val:
            return 0
        try:
            return int(float(val.strip()))
        except ValueError:
            return 0

    @staticmethod
    def _parse_float(val: str | None) -> float:
        if not val:
            return 0.0
        try:
            return float(val.strip())
        except ValueError:
            return 0.0

    async def _refresh_region_stats_if_needed(self, client: httpx.AsyncClient) -> None:
        now = datetime.now(timezone.utc)
        if self._region_stats_cache and now < self._region_cache_expires:
            return

        url = self._region_items_url()
        try:
            resp = await client.get(url, headers={"Accept-Encoding": "gzip"}, timeout=30.0)
            if resp.status_code != 200:
                logger.warning(f"Не удалось загрузить region/items.csv ({resp.status_code}): {url}")
                return

            parsed: dict[int, tuple[float, float, int]] = {}
            with io.StringIO(resp.text) as buf:
                for row in csv.DictReader(buf):
                    iid = self._extract_item_id(row)
                    if iid:
                        parsed[iid] = (
                            self._parse_float(row.get("saleRate")),
                            self._parse_float(row.get("soldPerDay")),
                            self._parse_int(row.get("marketValue")),
                        )
            del resp

            if parsed:
                self._region_stats_cache.clear()
                self._region_stats_cache = parsed
                self._region_cache_expires = now + timedelta(hours=6)
                gc.collect()
                logger.info(f"Загружена суточная статистика TSM: {len(parsed)} позиций (компактный кэш).")
        except Exception as exc:
            logger.warning(f"Ошибка чтения region/items.csv: {exc}")

    async def fetch_market_snapshot(self) -> tuple[datetime, list[NormalizedItemTick]]:
        realm_url = self._realm_items_url()
        async with httpx.AsyncClient() as client:
            await self._refresh_region_stats_if_needed(client)

            resp = await client.get(realm_url, headers={"Accept-Encoding": "gzip"}, timeout=30.0)
            if resp.status_code == 404:
                raise RuntimeError(
                    f"404 Not Found по адресу {realm_url}. Проверь TSM_REALM_SLUG в .env."
                )
            resp.raise_for_status()
            raw_csv = resp.text
            del resp

        ticks: list[NormalizedItemTick] = []
        snapshot_ts: datetime | None = None

        with io.StringIO(raw_csv) as buf:
            for row in csv.DictReader(buf):
                iid = self._extract_item_id(row)
                if not iid:
                    continue

                if snapshot_ts is None:
                    raw_ts = (row.get("updatedAt") or "").strip()
                    if raw_ts:
                        snapshot_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).astimezone(timezone.utc)

                min_buyout = self._parse_int(row.get("minBuyout"))
                if min_buyout <= 0:
                    continue

                market_val = self._parse_int(row.get("marketValue"))
                sale_rate, sold_per_day, reg_market = self._region_stats_cache.get(iid, (0.0, 0.0, 0))

                ticks.append(
                    NormalizedItemTick(
                        item_id=iid,
                        item_name=(row.get("name") or "").strip(),
                        min_buyout=min_buyout,
                        entry_price=min_buyout,
                        market_value=market_val or reg_market,
                        recent_value=self._parse_int(row.get("recent")),
                        historical_value=self._parse_int(row.get("historical")),
                        sale_rate=sale_rate,
                        sold_per_day=sold_per_day,
                    )
                )

        del raw_csv
        gc.collect()

        if snapshot_ts is None:
            snapshot_ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        return snapshot_ts, ticks