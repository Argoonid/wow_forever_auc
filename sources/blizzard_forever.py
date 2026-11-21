import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import httpx
from sqlalchemy import or_, select
from config import settings
from database import async_session_factory
from models import ItemMeta, MarketTick

logger = logging.getLogger("source.blizzard")

MIN_AUTO_ADD_PRICE_COPPER = 80
_DIAG_WATCH_IDS = (7067, 13463, 13464, 13466)


class BlizzardForeverSource:
    """Прямой коннектор к официальному Game Data API Blizzard для WoW: Forever (v4)."""

    def __init__(self) -> None:
        self.region = getattr(settings, "BNET_REGION", "eu").strip().lower()
        self.ruleset_id = int(getattr(settings, "BNET_RULESET_ID", 1))
        self.house_id = int(getattr(settings, "BNET_HOUSE_ID", 0))
        self.namespace = f"dynamic-forever-{self.region}"
        self.static_namespace = f"static-forever-{self.region}"
        self.api_base = f"https://{self.region}.api.blizzard.com"
        self.oauth_url = "https://oauth.battle.net/token"

        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._last_modified_header: str | None = None
        self._last_snapshot_ts: datetime | None = None
        self._commodity_mode: str = "house_commodities"

        # Память прошлого часа для синтетической ленты сделок: auction_id -> (item_id, unit_price, qty, time_left)
        self._prev_lots: dict[int, tuple[int, int, int, str]] = {}
        self._prev_entries: dict[int, int] = {}
        self._prev_lots_ts: datetime | None = None
        self._logged_sample_lot: bool = False

    async def _get_token(self, client: httpx.AsyncClient, force_refresh: bool = False) -> str:
        now_ts = datetime.now(timezone.utc).timestamp()
        if not force_refresh and self._access_token and now_ts < (self._token_expires_at - 120):
            return self._access_token

        client_id = settings.BNET_CLIENT_ID.strip()
        client_secret = settings.BNET_CLIENT_SECRET.strip()
        if not client_id or not client_secret:
            raise RuntimeError("В .env не заполнены BNET_CLIENT_ID и BNET_CLIENT_SECRET")

        resp = await client.post(
            self.oauth_url,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            timeout=20.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._token_expires_at = now_ts + float(payload.get("expires_in", 86400))
        logger.info("Blizzard OAuth2: токен успешно получен.")
        return self._access_token

    @staticmethod
    def _extract_house_ids(index_payload: dict) -> list[int]:
        ids: list[int] = []
        houses = index_payload.get("houses") or index_payload.get("auctions") or []
        for h in houses:
            if isinstance(h, int):
                ids.append(h)
            elif isinstance(h, dict):
                if "id" in h:
                    ids.append(int(h["id"]))
                else:
                    href = (h.get("key") or {}).get("href") or h.get("href") or ""
                    m = re.search(r"/auctions/(\d+)", href)
                    if m:
                        ids.append(int(m.group(1)))
        return sorted(set(ids))

    async def _discover_active_house(self, client: httpx.AsyncClient, token: str) -> int:
        if self.house_id > 0:
            return self.house_id

        headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
        index_urls = [
            f"{self.api_base}/data/wow/ruleset/{self.ruleset_id}/auctions/index",
            f"{self.api_base}/data/wow/ruleset/{self.ruleset_id}/auctions/",
        ]

        candidate_ids: list[int] = []
        for url in index_urls:
            resp = await client.get(url, params={"namespace": self.namespace}, headers=headers, timeout=20.0)
            if resp.status_code == 200:
                data = resp.json()
                candidate_ids = self._extract_house_ids(data)
                if candidate_ids:
                    logger.info(f"Blizzard Forever: найдены houseId в индексе: {candidate_ids}")
                    break

        if not candidate_ids:
            candidate_ids = [110, 111, 113, 114, 116, 117, 119, 120, 121, 122, 123, 124, 125]

        best_house = 0
        best_count = -1
        best_mode = "house_commodities"

        reg_comm_url = f"{self.api_base}/data/wow/auctions/commodities"
        r_reg = await client.get(reg_comm_url, params={"namespace": self.namespace}, headers=headers, timeout=30.0)
        if r_reg.status_code == 200:
            reg_aucs = r_reg.json().get("auctions") or []
            if len(reg_aucs) > 0:
                best_count = len(reg_aucs)
                best_mode = "regional_commodities"

        for hid in candidate_ids:
            if best_mode != "regional_commodities":
                comm_url = f"{self.api_base}/data/wow/ruleset/{self.ruleset_id}/auctions/{hid}/commodities"
                r = await client.get(comm_url, params={"namespace": self.namespace}, headers=headers, timeout=25.0)
                if r.status_code == 200:
                    aucs = r.json().get("auctions") or []
                    if len(aucs) > best_count:
                        best_count = len(aucs)
                        best_house = hid
                        best_mode = "house_commodities"

            if best_count <= 0:
                auc_url = f"{self.api_base}/data/wow/ruleset/{self.ruleset_id}/auctions/{hid}"
                r_all = await client.get(auc_url, params={"namespace": self.namespace}, headers=headers, timeout=25.0)
                if r_all.status_code == 200:
                    aucs = r_all.json().get("auctions") or []
                    if len(aucs) > best_count:
                        best_count = len(aucs)
                        best_house = hid
                        best_mode = "combined_house"

        if best_count <= 0:
            raise RuntimeError(f"Не удалось найти активные лоты в namespace={self.namespace}")

        self.house_id = best_house if best_house > 0 else candidate_ids[0]
        self._commodity_mode = best_mode
        logger.info(f"Blizzard Forever: режим='{best_mode}', houseId={self.house_id} ({best_count} лотов).")
        return self.house_id

    async def _fetch_single_item_meta(
        self, client: httpx.AsyncClient, token: str, item_id: int, sem: asyncio.Semaphore
    ) -> dict | None:
        headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
        async with sem:
            result = {"item_id": item_id, "name": None, "sell_price": None, "max_stack": None, "icon_url": None}
            try:
                r, rm = await asyncio.gather(
                    client.get(
                        f"{self.api_base}/data/wow/item/{item_id}",
                        params={"namespace": self.static_namespace, "locale": "ru_RU"},
                        headers=headers,
                        timeout=15.0,
                    ),
                    client.get(
                        f"{self.api_base}/data/wow/media/item/{item_id}",
                        params={"namespace": self.static_namespace},
                        headers=headers,
                        timeout=15.0,
                    ),
                    return_exceptions=True,
                )

                if not isinstance(r, Exception) and r.status_code == 200:
                    data = r.json()
                    name = data.get("name")
                    if isinstance(name, dict):
                        name = name.get("ru_RU") or name.get("en_US")
                    if name:
                        result["name"] = str(name)
                    if data.get("sell_price") is not None:
                        result["sell_price"] = int(data["sell_price"])
                    if data.get("max_count"):
                        result["max_stack"] = int(data["max_count"])

                if not isinstance(rm, Exception) and rm.status_code == 200:
                    for a in rm.json().get("assets") or []:
                        if a.get("key") == "icon" and a.get("value"):
                            # Сохраняем полный URL Blizzard CDN, чтобы работали и кастомные иконки Forever
                            result["icon_url"] = str(a["value"])
                            break
            except Exception:
                return None
            return result

    async def resolve_unknown_items(
        self, client: httpx.AsyncClient, token: str, limit: int = 80, include_non_http_icons: bool = False
    ) -> int:
        async with async_session_factory() as session:
            conds = [
                ItemMeta.name.like("Item_%"),
                ItemMeta.icon_name.is_(None),
                ItemMeta.icon_name == "",
                ItemMeta.icon_name == "inv_misc_questionmark",
            ]
            if include_non_http_icons:
                conds.append(~ItemMeta.icon_name.like("http%"))

            stmt = select(ItemMeta).where(or_(*conds)).limit(limit)
            rows = list((await session.execute(stmt)).scalars().all())
            if not rows:
                return 0

            sem = asyncio.Semaphore(10)
            tasks = [self._fetch_single_item_meta(client, token, r.item_id, sem) for r in rows]
            fetched = await asyncio.gather(*tasks)

            by_id = {r.item_id: r for r in rows}
            updated = 0
            for res in fetched:
                if not res:
                    continue
                meta = by_id.get(res["item_id"])
                if not meta:
                    continue
                changed = False
                if res["name"] and (meta.name.startswith("Item_") or not meta.meta_resolved):
                    meta.name = res["name"]
                    changed = True
                if res["sell_price"] is not None and res["sell_price"] > 0:
                    meta.vendor_sell_price = res["sell_price"]
                    changed = True
                if res["max_stack"]:
                    meta.max_stack = res["max_stack"]
                if res["icon_url"]:
                    meta.icon_name = res["icon_url"]
                    changed = True
                if changed:
                    meta.meta_resolved = True
                    updated += 1

            if updated > 0:
                await session.commit()
                logger.info(f"Blizzard Static API: обновлено {updated}/{len(rows)} предметов (имена и иконки CDN).")
            return updated

    async def backfill_missing_meta(self, limit: int = 600) -> int:
        """Разовая пакетная починка всех битых иконок и безымянных предметов в базе."""
        async with httpx.AsyncClient() as client:
            token = await self._get_token(client)
            return await self.resolve_unknown_items(client, token, limit=limit, include_non_http_icons=True)

    @staticmethod
    def _analyze_commodity_book(
        lots: list[tuple[int, int]],
    ) -> tuple[int, int, int, int, int, int, int, int]:
        """
        Разбирает геометрию стакана:
        (min_buyout, entry_price, wall_price, total_qty, num_auctions, cheap_qty, gap_qty, book_p25_price)
        """
        lots.sort(key=lambda x: x[0])
        min_buyout = lots[0][0]
        num_auctions = len(lots)

        levels: list[tuple[int, int]] = []
        total_qty = 0
        for price, qty in lots:
            total_qty += qty
            if levels and levels[-1][0] == price:
                levels[-1] = (price, levels[-1][1] + qty)
            else:
                levels.append((price, qty))

        # Цена входа (глубина 5 единиц — защита от одиночного байт-лота)
        entry_price = min_buyout
        cum_qty = 0
        target_depth = min(5, max(1, total_qty))
        for price, lvl_qty in levels:
            cum_qty += lvl_qty
            if cum_qty >= target_depth:
                entry_price = price
                break

        # Цена на 25% накопленного объёма всей книги
        book_p25_price = entry_price
        p25_target = max(1, int(total_qty * 0.25))
        cum_p25 = 0
        for price, lvl_qty in levels:
            cum_p25 += lvl_qty
            if cum_p25 >= p25_target:
                book_p25_price = price
                break

        # Стена предложения (крупнейший уровень в нижних 60% ценовых ступеней)
        lower_levels = levels[: max(1, int(len(levels) * 0.60))]
        wall_price = max(lower_levels, key=lambda x: x[1])[0] if lower_levels else entry_price

        # Реальная глубина дешёвой полки (<= +5% от входа) и "воздушный карман" до стены
        cheap_cap = int(entry_price * 1.05)
        wall_floor = int(wall_price * 0.98)
        cheap_qty = 0
        gap_qty = 0
        for price, lvl_qty in levels:
            if price <= cheap_cap:
                cheap_qty += lvl_qty
            elif wall_price > cheap_cap and price < wall_floor:
                gap_qty += lvl_qty

        return (
            min_buyout,
            entry_price,
            wall_price,
            total_qty,
            num_auctions,
            cheap_qty,
            gap_qty,
            book_p25_price,
        )

    def _compute_synthetic_tape(
        self,
        snapshot_ts: datetime,
        cur_lots: dict[int, tuple[int, int, int, str]],
        cur_entries: dict[int, int],
    ) -> dict[int, tuple[int, int]]:
        """
        Синтетическая лента сделок по диффу auction_id + time_left между соседними часами.
        Возвращает item_id -> (est_sold_qty, est_sold_vwap).
        """
        tape: dict[int, tuple[int, int]] = {}

        if (
            self._prev_lots
            and self._prev_lots_ts is not None
            and 0 < (snapshot_ts - self._prev_lots_ts).total_seconds() <= 5400
        ):
            sold_qty_acc: dict[int, int] = defaultdict(int)
            sold_val_acc: dict[int, int] = defaultdict(int)

            for aid, (iid, prev_p, prev_q, prev_tl) in self._prev_lots.items():
                cur = cur_lots.get(aid)
                if cur is not None:
                    # Лот остался, но количество уменьшилось — 100% реальный частичный выкуп
                    cur_q = cur[2]
                    if cur_q < prev_q:
                        dq = prev_q - cur_q
                        sold_qty_acc[iid] += dq
                        sold_val_acc[iid] += dq * prev_p
                else:
                    # Лот исчез: отбрасываем SHORT/MEDIUM (истечение по таймеру) и завышенные снятия
                    prev_entry = self._prev_entries.get(iid, 0)
                    if (
                        prev_tl in ("LONG", "VERY_LONG")
                        and prev_entry > 0
                        and prev_p <= int(prev_entry * 1.25)
                    ):
                        sold_qty_acc[iid] += prev_q
                        sold_val_acc[iid] += prev_q * prev_p

            for iid, sq in sold_qty_acc.items():
                if sq > 0:
                    tape[iid] = (sq, int(sold_val_acc[iid] // sq))

        self._prev_lots = cur_lots
        self._prev_entries = cur_entries
        self._prev_lots_ts = snapshot_ts
        return tape

    async def fetch_market_snapshot(self) -> tuple[datetime, list[MarketTick]]:
        async with httpx.AsyncClient() as client:
            token = await self._get_token(client)
            house_id = await self._discover_active_house(client, token)
            await self.resolve_unknown_items(client, token, limit=80)

            if self._commodity_mode == "regional_commodities":
                primary_url = f"{self.api_base}/data/wow/auctions/commodities"
            elif self._commodity_mode == "combined_house":
                primary_url = f"{self.api_base}/data/wow/ruleset/{self.ruleset_id}/auctions/{house_id}"
            else:
                primary_url = f"{self.api_base}/data/wow/ruleset/{self.ruleset_id}/auctions/{house_id}/commodities"

            headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}
            if self._last_modified_header:
                headers["If-Modified-Since"] = self._last_modified_header

            resp = await client.get(primary_url, params={"namespace": self.namespace}, headers=headers, timeout=45.0)
            if resp.status_code == 401:
                token = await self._get_token(client, force_refresh=True)
                headers["Authorization"] = f"Bearer {token}"
                resp = await client.get(primary_url, params={"namespace": self.namespace}, headers=headers, timeout=45.0)

            if resp.status_code == 304 and self._last_snapshot_ts is not None:
                return self._last_snapshot_ts, []

            resp.raise_for_status()

            last_mod_raw = resp.headers.get("Last-Modified")
            if last_mod_raw:
                self._last_modified_header = last_mod_raw
                snapshot_ts = parsedate_to_datetime(last_mod_raw).astimezone(timezone.utc)
            else:
                snapshot_ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

            self._last_snapshot_ts = snapshot_ts
            primary_auctions = resp.json().get("auctions") or []

            if primary_auctions and not self._logged_sample_lot:
                logger.info(f"Blizzard sample lot keys: {list(primary_auctions[0].keys())} | {primary_auctions[0]}")
                self._logged_sample_lot = True

            comm_books: dict[int, list[tuple[int, int]]] = defaultdict(list)
            cur_lots: dict[int, tuple[int, int, int, str]] = {}

            for row in primary_auctions:
                item_obj = row.get("item") or {}
                iid = int(item_obj.get("id") or 0)
                qty = max(1, int(row.get("quantity") or 1))
                unit_p = int(row.get("unit_price") or 0)
                if unit_p <= 0:
                    buyout = int(row.get("buyout") or 0)
                    if buyout > 0:
                        unit_p = buyout // qty
                if iid > 0 and unit_p > 0:
                    comm_books[iid].append((unit_p, qty))
                    aid = int(row.get("id") or 0)
                    if aid > 0:
                        cur_lots[aid] = (iid, unit_p, qty, str(row.get("time_left") or ""))

            regular_auctions = []
            if self._commodity_mode != "combined_house" and house_id > 0:
                items_url = f"{self.api_base}/data/wow/ruleset/{self.ruleset_id}/auctions/{house_id}"
                items_resp = await client.get(
                    items_url,
                    params={"namespace": self.namespace},
                    headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
                    timeout=45.0,
                )
                if items_resp.status_code == 200:
                    regular_auctions = items_resp.json().get("auctions") or []
                else:
                    logger.warning(f"Blizzard regular_auctions HTTP {items_resp.status_code}")

            async with async_session_factory() as session:
                known_meta = {
                    r.item_id: r for r in (await session.execute(select(ItemMeta))).scalars().all()
                }

                reg_books: dict[int, list[tuple[int, int]]] = defaultdict(list)
                for row in regular_auctions:
                    item_obj = row.get("item") or {}
                    iid = int(item_obj.get("id") or 0)
                    if iid not in known_meta:
                        continue
                    buyout = int(row.get("buyout") or 0)
                    qty = max(1, int(row.get("quantity") or 1))
                    if buyout > 0:
                        unit_p = buyout // qty
                        reg_books[iid].append((unit_p, qty))
                        aid = int(row.get("id") or 0)
                        if aid > 0:
                            cur_lots[aid] = (iid, unit_p, qty, str(row.get("time_left") or ""))

                # Диагностика пропажи ключевых трав прямо на выходе API
                missing_diag = [
                    iid for iid in _DIAG_WATCH_IDS
                    if iid not in comm_books and iid not in reg_books
                ]
                if missing_diag:
                    logger.warning(
                        f"Blizzard API raw missing watch IDs: {missing_diag} "
                        f"(primary={len(primary_auctions)}, regular={len(regular_auctions)}, unique_comm={len(comm_books)})"
                    )

                all_candidate_ids = set(comm_books.keys()) | set(reg_books.keys())
                parsed_books: dict[int, tuple[int, int, int, int, int, int, int, int]] = {}
                cur_entries: dict[int, int] = {}

                for iid in all_candidate_ids:
                    book = comm_books.get(iid, []) + reg_books.get(iid, [])
                    if not book:
                        continue
                    stats = self._analyze_commodity_book(book)
                    parsed_books[iid] = stats
                    cur_entries[iid] = stats[1]

                tape_map = self._compute_synthetic_tape(snapshot_ts, cur_lots, cur_entries)
                has_v4_cols = hasattr(MarketTick, "cheap_qty")

                ticks: list[MarketTick] = []
                new_items_added = 0
                dow = snapshot_ts.weekday()
                hod = snapshot_ts.hour

                for iid, stats in parsed_books.items():
                    min_b, entry_p, wall_p, total_q, num_auc, cheap_q, gap_q, p25_p = stats
                    meta = known_meta.get(iid)

                    if not meta:
                        # Жёстко отсекаем мусор дешевле 80 меди и неликвид
                        if entry_p < MIN_AUTO_ADD_PRICE_COPPER or (total_q < 40 and num_auc < 5):
                            continue
                        est_velocity = round(max(25.0, min(5000.0, total_q * 0.35)), 1)
                        meta = ItemMeta(
                            item_id=iid,
                            name=f"Item_{iid}",
                            category="shadow",
                            is_active_pool=False,
                            velocity_score=est_velocity,
                        )
                        session.add(meta)
                        known_meta[iid] = meta
                        new_items_added += 1
                    elif meta.velocity_score <= 0:
                        meta.velocity_score = round(max(15.0, min(5000.0, total_q * 0.35)), 1)

                    sold_q, sold_vwap = tape_map.get(iid, (0, 0))
                    ref_market = meta.tsm_market_value or 0

                    tick_kwargs = dict(
                        snapshot_ts=snapshot_ts,
                        day_of_week=dow,
                        hour_of_day=hod,
                        source="blizzard",
                        item_id=iid,
                        min_buyout=min_b,
                        entry_price=entry_p,
                        wall_price=wall_p,
                        market_value=ref_market,
                        quantity=total_q,
                        num_auctions=num_auc,
                    )
                    if has_v4_cols:
                        tick_kwargs.update(
                            cheap_qty=cheap_q,
                            gap_qty=gap_q,
                            book_p25_price=p25_p,
                            est_sold_qty=sold_q,
                            est_sold_vwap=sold_vwap,
                        )
                    ticks.append(MarketTick(**tick_kwargs))

                if new_items_added > 0:
                    await session.commit()

            if new_items_added > 0:
                await self.resolve_unknown_items(client, token, limit=80)

        logger.info(
            f"Blizzard Forever v4 ({self._commodity_mode}, house {self.house_id}): "
            f"разобрано {len(primary_auctions)} лотов -> {len(ticks)} позиций "
            f"(новых: {new_items_added}, лента сделок: {len(tape_map)} поз.)."
        )
        return snapshot_ts, ticks