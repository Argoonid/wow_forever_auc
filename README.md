# WoW: Forever — Auction House Terminal

Терминал мониторинга и анализа аукциона **World of Warcraft: Forever (EU PvP Alliance)** с круглосуточным сбором срезов, скорингом аномалий, трекером портфеля и радаром идей.

## Архитектура (Split-DB)

```text
Blizzard Game Data API (HouseID 113)
        │  каждые 30 мин (GitHub Actions: collect.yml)
        ▼
Neon PostgreSQL (Общий рынок)
  ├── items_meta, market_ticks, snapshot_runs, call_history
        │  SELECT (владелец или read-only роль ah_reader)
        ▼
Локальный веб-терминал (FastAPI + модульный JS)
        │  чтение / запись
        ▼
personal.db (Личная SQLite)
  └── trade_positions, chart_drawings, watchlist_items, hidden_items

  Общий рынок собирается 24/7 в облако без локальных ночных разрывов.Личные данные (портфель, залоги, заметки на графиках, радар, скрытые товары) изолированы в локальной personal.db и не утекают между пользователями.Секреты GitHub ActionsДобавляются в Settings -> Secrets and variables -> Actions -> Repository secrets:СекретНазначениеBNET_CLIENT_IDClient ID приложения Battle.netBNET_CLIENT_SECRETClient Secret приложения Battle.netBNET_HOUSE_IDID аукционного дома (113 для EU Forever)MARKET_DATABASE_URLPooled-строка подключения владельца Neon PostgreSQLЛокальный запускPowerShellpython -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
copy .env.example .env
# Заполнить .env
.\.venv\Scripts\python.exe -m uvicorn web:app --host 127.0.0.1 --port 8000
После запуска интерфейс доступен по адресу http://127.0.0.1:8000.Автоматизация.github/workflows/collect.yml — разовый прогон collect_once.py каждые 30 минут + ручной запуск (workflow_dispatch)..github/workflows/keepalive.yml — пустой коммит 1-го числа каждого месяца, чтобы GitHub не ставил cron на паузу через 60 дней без коммитов.
#### `.github/workflows/keepalive.yml` (триггер против 60-дневной паузы GitHub)
```yaml
name: Keepalive Scheduled Workflows

on:
  schedule:
    # 1-го числа каждого месяца в 04:00 UTC — сбрасывает 60-дневный таймер неактивности
    - cron: '0 4 1 * *'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  keepalive:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Touch repository activity
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git commit --allow-empty -m "chore: keepalive cron"
          git push