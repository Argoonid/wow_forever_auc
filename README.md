# WoW: Forever — Auction House Terminal

Терминал мониторинга и анализа аукциона **World of Warcraft: Forever (EU PvP Alliance)** с круглосуточным сбором срезов, скорингом аномалий, трекером портфеля и радаром идей.

## Архитектура (Split-DB)

```text
Blizzard Game Data API (HouseID 113)
        │  каждые 30 мин (GitHub Actions: collect.yml)
        ▼
Neon PostgreSQL (Общий рынок)
  └── items_meta, market_ticks, snapshot_runs, call_history
        │  SELECT (read-only роль ah_reader)
        ▼
Локальный терминал (ForeverTerminal.exe / FastAPI + модульный JS)
        │  чтение / запись
        ▼
personal.db (Личная SQLite)
  └── trade_positions, chart_drawings, watchlist_items, hidden_items
```

* **Общий рынок** собирается 24/7 в облако без локальных ночных разрывов.
* **Личные данные** (портфель, залоги, заметки на графиках, радар, скрытые товары) изолированы в локальной `personal.db` и не пересекаются между пользователями.

## Установка для пользователей (Windows)

1. Открой раздел **Releases** справа и скачай последний `ForeverTerminal-win64.zip`.
2. Распакуй архив в любую папку (например, на рабочий стол) и запусти `ForeverTerminal.exe`.
3. Браузер откроет терминал автоматически; в системном трее появится иконка. При закрытии последней вкладки браузера фоновый процесс завершается сам через несколько секунд.

При обновлении на новую версию просто распакуй свежий архив поверх старой папки с заменой файлов — твой локальный `personal.db` останется нетронутым.

## Локальный запуск (разработка)

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
copy .env.example .env
# Заполнить .env

# Веб-сервер в терминале:
.\.venv\Scripts\python.exe -m uvicorn web:app --host 127.0.0.1 --port 8000

# Или запуск десктопного лаунчера с треем:
.\.venv\Scripts\python.exe desktop_app.py
```

После запуска интерфейс доступен по адресу `http://127.0.0.1:8000`.

## Секреты GitHub Actions

Добавляются в `Settings -> Secrets and variables -> Actions -> Repository secrets`:

| Секрет | Назначение |
| :--- | :--- |
| `BNET_CLIENT_ID` | Client ID приложения Battle.net |
| `BNET_CLIENT_SECRET` | Client Secret приложения Battle.net |
| `BNET_HOUSE_ID` | ID аукционного дома (`113` для EU Forever) |
| `MARKET_DATABASE_URL` | Pooled-строка подключения владельца Neon PostgreSQL (для сборщика) |


## Автоматизация

* `.github/workflows/collect.yml` — разовый прогон `collect_once.py` каждые 30 минут + ручной запуск.
* `.github/workflows/release.yml` — сборка `ForeverTerminal-win64.zip` с иконкой и публикация в GitHub Releases по кнопке.
* `.github/workflows/keepalive.yml` — пустой коммит 1-го числа каждого месяца, чтобы GitHub не ставил cron на паузу через 60 дней без коммитов.
