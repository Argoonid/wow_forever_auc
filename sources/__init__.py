from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class NormalizedItemTick:
    item_id: int
    min_buyout: int
    entry_price: int
    quantity: int
    num_auctions: int
    market_value: int = 0
    historical_value: int = 0
    sale_rate: float = 0.0
    wall_price: int | None = None


class AuctionDataSource(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch_market_snapshot(self) -> tuple[datetime, list[NormalizedItemTick]]:
        """Возвращает метку времени среза (UTC) и нормализованный список позиций."""
        raise NotImplementedError