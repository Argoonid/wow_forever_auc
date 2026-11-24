import os
from datetime import datetime, timezone
from config import settings

MODEL_VERSION = "v4.1"

# Глубина кэша и окна анализа
HIST_WINDOW_SNAPSHOTS = 120
REGIME_WINDOW_TICKS = 72
MIN_HIST_TICKS_FLIP = 14
MIN_TICKS_FOR_CLOCK_BOOST = 96

# Базовая ликвидность и приоритет синтетической ленты
MIN_AUCTIONS_FLIP = 6
MIN_TOTAL_QTY_FLIP = 15
MIN_QTY_CHURN_FLIP = 2.0
MIN_TAPE_TICKS_FOR_PRIORITY = 6
MIN_SOLD_HR_FLIP = 2.0

# Защита от "выдуваемого за секунду" дна:
# True  -> пускать только дно, продержавшееся 2 соседних часовых среза подряд
# False -> 2 часа подряд ИЛИ глубокий дамп >= 25% стакана (book_p25) с запасом полки >= 35 мин
STRICT_TWO_HOUR_BOTTOM = False
BOTTOM_2H_MAX_DRIFT = 1.08
BOTTOM_MAX_GAP_MINUTES = 90.0
MIN_CHEAP_QTY_FLIP = 8
MIN_SHELF_LIFE_MINUTES = 15.0
DEEP_BOOK_P25_RATIO = 1.10
DEEP_BOOK_MIN_CHEAP_QTY = 14
DEEP_BOOK_MIN_SHELF_MINUTES = 35.0

# Интервал перепроверки нового snapshot_id в Neon (секунды)
LATEST_CHECK_TTL_SEC = 30.0

# Открытие рейдов WoW: Forever (Barrow Deeps 10, Hyjal Summit 20, Onyxia 40)
RAID_LAUNCH_UTC = datetime(2026, 12, 9, 18, 0, tzinfo=timezone.utc)

# Чёрный список дешёвых NPC-товаров
VENDOR_JUNK_ITEM_IDS: set[int] = {
    2320, 2321, 2324, 2325, 2604, 2605, 2678, 2880,
    3371, 3372, 3466, 3857, 4289, 4291, 4340, 4341,
    4342, 5042, 5048, 6260, 6261, 8343, 8925, 14341, 18256,
}

# Долгосрочный радар инвестиций под рейды 9 декабря: (item_id, заметка, тир)
RAID_WATCHLIST: list[tuple[int, str, str]] = [
    (248822, "Death Lotus [Forever фласки]", "T1"),
    (248821, "Demonsage [Forever фласки]", "T1"),
    (248823, "Marefoil [Forever фласки/крафт]", "T1"),
    (7068, "Elemental Fire [GFPP Ониксия]", "T1"),
    (13457, "Greater Fire Protection Potion", "T1"),
    (13463, "Dreamfoil [GFPP/Эликсиры/Фласки]", "T1"),
    (8846, "Gromsblood [Титан/Grizzly/Ferocity]", "T1"),
    (13466, "Plaguebloom [Frenzy/Mongoose]", "T1"),
    (8839, "Blindweed [Frenzy/Grizzly]", "T1"),
    (13465, "Mountain Silversage [Mongoose/Mana]", "T1"),
    (8838, "Sungrass [Fortitude/Nature Power]", "T1"),
    (13464, "Golden Sansam [Sages/Nature Power]", "T1"),
    (13452, "Elixir of the Mongoose [Готовый]", "T1"),
    (13444, "Major Mana Potion [Готовый]", "T1"),
    (13468, "Black Lotus [Титан/СПД Ониксия]", "T2"),
    (13422, "Stonescale Eel [Угорь -> Масло]", "T2"),
    (13423, "Stonescale Oil [Титан/Защита]", "T2"),
    (8845, "Ghost Mushroom [Ferocity/Shadow]", "T2"),
    (12363, "Arcane Crystal [Arcanite CD]", "T2"),
    (12360, "Arcanite Bar [Готовый слиток]", "T2"),
    (248815, "Heavy Thorium Ore [Legionite CD]", "T2"),
    (248819, "Fel Crystal [Legionite CD]", "T2"),
    (251290, "Legionite Bar [Пре-рейд BiS]", "T2"),
    (7067, "Elemental Earth [GNPP спекуляция]", "T3"),
    (3824, "Shadow Oil [GSPP спекуляция]", "T3"),
    (3369, "Grave Moss [Сырьё Shadow Oil]", "T3"),
]
RAID_WATCHLIST_IDS: list[tuple[int, str]] = [(iid, note) for iid, note, _ in RAID_WATCHLIST]
RAID_WATCH_ID_SET: set[int] = {iid for iid, _, _ in RAID_WATCHLIST}


def is_market_read_only() -> bool:
    if bool(getattr(settings, "MARKET_READ_ONLY", False)):
        return True
    return os.getenv("MARKET_READ_ONLY", "").strip().lower() in {"1", "true", "yes"}


def min_snapshot_id() -> int:
    val = getattr(settings, "ANALYZER_MIN_SNAPSHOT_ID", None)
    if val is None:
        val = os.getenv("ANALYZER_MIN_SNAPSHOT_ID", "0")
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 0