from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATA_SOURCE: str = "blizzard"
    TSM_GAME_TYPE: str = "forever"
    TSM_REGION_SLUG: str = "eu"
    TSM_REALM_SLUG: str = "pvp-alliance"

    BNET_CLIENT_ID: str = ""
    BNET_CLIENT_SECRET: str = ""
    BNET_REGION: str = "eu"
    BNET_RULESET_ID: int = 1
    BNET_HOUSE_ID: int = 0

    BANKROLL_GOLD: float = 10.0
    MAX_SHARE_PER_ITEM: float = 0.25
    SHADOW_POOL_LIMIT: int = 250
    MAX_CONTINUOUS_GAP_MINUTES: int = 210
    POLL_INTERVAL_MINUTES: int = 30
    RETENTION_DAYS: int = 60

    DATABASE_URL: str = "sqlite+aiosqlite:///./forever_ah.db"
    MARKET_DATABASE_URL: str = ""
    PERSONAL_DATABASE_URL: str = "sqlite+aiosqlite:///./personal.db"
    MARKET_READ_ONLY: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

STARTER_ACTIVE_POOL: dict[int, dict] = {
    # Пыль и субстанции наложения чар
    10940: {"name": "Странная пыль", "stack": 20, "category": "enchanting"},
    11083: {"name": "Пыль душ", "stack": 20, "category": "enchanting"},
    11137: {"name": "Пыль видений", "stack": 20, "category": "enchanting"},
    10938: {"name": "Простая магическая субстанция", "stack": 10, "category": "enchanting"},
    10939: {"name": "Великая магическая субстанция", "stack": 10, "category": "enchanting"},
    10998: {"name": "Простая астральная субстанция", "stack": 10, "category": "enchanting"},
    11082: {"name": "Великая астральная субстанция", "stack": 10, "category": "enchanting"},
    # Ткань и рулоны
    2589:  {"name": "Льняной материал", "stack": 20, "category": "cloth"},
    2996:  {"name": "Рулон льняной ткани", "stack": 20, "category": "cloth"},
    2592:  {"name": "Шерсть", "stack": 20, "category": "cloth"},
    2997:  {"name": "Рулон шерсти", "stack": 20, "category": "cloth"},
    4306:  {"name": "Шелковый материал", "stack": 20, "category": "cloth"},
    4305:  {"name": "Рулон шелка", "stack": 20, "category": "cloth"},
    4338:  {"name": "Магическая ткань", "stack": 20, "category": "cloth"},
    4339:  {"name": "Рулон магической ткани", "stack": 20, "category": "cloth"},
    # Травы и алхимия
    785:   {"name": "Магороза", "stack": 20, "category": "herb"},
    2450:  {"name": "Остротерн", "stack": 20, "category": "herb"},
    2453:  {"name": "Синячник", "stack": 20, "category": "herb"},
    2452:  {"name": "Скорополох", "stack": 20, "category": "herb"},
    3820:  {"name": "Удавник", "stack": 20, "category": "herb"},
    3355:  {"name": "Дикий сталецвет", "stack": 20, "category": "herb"},
    3356:  {"name": "Королевская кровь", "stack": 20, "category": "herb"},
    3818:  {"name": "Бледнолист", "stack": 20, "category": "herb"},
    3821:  {"name": "Златошип", "stack": 20, "category": "herb"},
    5634:  {"name": "Зелье свободы действий", "stack": 5, "category": "consumable"},
    2459:  {"name": "Зелье скорости", "stack": 5, "category": "consumable"},
    1710:  {"name": "Сильное лечебное зелье", "stack": 5, "category": "consumable"},
    3928:  {"name": "Наилучшее лечебное зелье", "stack": 5, "category": "consumable"},
    6149:  {"name": "Сильное зелье маны", "stack": 5, "category": "consumable"},
    # Слитки, камни и кожа
    2840:  {"name": "Медный слиток", "stack": 20, "category": "metal"},
    2841:  {"name": "Бронзовый слиток", "stack": 20, "category": "metal"},
    3576:  {"name": "Оловянный слиток", "stack": 20, "category": "metal"},
    3575:  {"name": "Железный слиток", "stack": 20, "category": "metal"},
    2838:  {"name": "Тяжелый камень", "stack": 20, "category": "stone"},
    7912:  {"name": "Твердый камень", "stack": 20, "category": "stone"},
    2318:  {"name": "Тонкая кожа", "stack": 20, "category": "leather"},
    2319:  {"name": "Жесткая кожа", "stack": 20, "category": "leather"},
    4234:  {"name": "Толстая кожа", "stack": 20, "category": "leather"},
}