import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

MAX_SESSION_GAP_SEC = 210 * 60  # 3.5 часа — граница между сессиями


def fmt_money(copper: int) -> str:
    c = abs(int(copper))
    g, s, rem = c // 10000, (c % 10000) // 100, c % 100
    return f"{g}g {s:02d}s {rem:02d}c" if g > 0 else f"{s}s {rem:02d}c"


def parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def split_into_sessions(series: list[dict]) -> list[list[dict]]:
    """Разбивает временной ряд предмета на изолированные дневные сессии без склейки ночных гапов."""
    if not series:
        return []
    sessions = [[series[0]]]
    for prev, curr in zip(series, series[1:]):
        gap = (curr["dt"] - prev["dt"]).total_seconds()
        if gap > MAX_SESSION_GAP_SEC:
            sessions.append([curr])
        else:
            sessions[-1].append(curr)
    return sessions


def run_audit(db_path: str = "forever_ah.db") -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    runs = cur.execute("SELECT * FROM snapshot_runs ORDER BY snapshot_ts ASC").fetchall()
    if len(runs) < 4:
        print(f"Слишком мало срезов для аудита ({len(runs)}). Нужно минимум 4-6 срезов.")
        return

    continuous_count = sum(1 for r in runs if r["is_continuous"])
    print("=== 1. ЗДОРОВЬЕ БАЗЫ И НОЧНЫЕ РАЗРЫВЫ ===")
    print(f"Всего срезов: {len(runs)} | Непрерывных: {continuous_count} | Ночных разрывов: {len(runs) - continuous_count}")
    print(f"Окно наблюдений: {runs[0]['snapshot_ts']}  -->  {runs[-1]['snapshot_ts']}\n")

    items = {
        r["item_id"]: dict(r)
        for r in cur.execute("SELECT * FROM items_meta WHERE is_active_pool = 1").fetchall()
    }

    ticks_rows = cur.execute(
        f"""
        SELECT t.*
        FROM market_ticks t
        WHERE t.item_id IN ({','.join(map(str, items.keys()))})
        ORDER BY t.snapshot_ts ASC
        """
    ).fetchall()

    by_item = defaultdict(list)
    hourly_ratios = defaultdict(list)

    for row in ticks_rows:
        d = dict(row)
        d["dt"] = parse_ts(d["snapshot_ts"])
        by_item[d["item_id"]].append(d)

    # 2. Суточный профиль (считаем только по сессиям, где есть минимум 2 замера)
    for iid, series in by_item.items():
        prices = [x["entry_price"] for x in series if x["entry_price"] > 0]
        if len(prices) < 4:
            continue
        med = sorted(prices)[len(prices) // 2]
        for pt in series:
            if pt["entry_price"] > 0:
                hourly_ratios[pt["hour_of_day"]].append(pt["entry_price"] / med)

    print("=== 2. РЕАЛЬНЫЙ СУТОЧНЫЙ ПРОФИЛЬ ЦЕН (UTC) ===")
    for hour in sorted(hourly_ratios.keys()):
        vals = hourly_ratios[hour]
        avg_ratio = sum(vals) / len(vals)
        bar = "#" * int(avg_ratio * 20)
        tag = " <-- ОКНО ЗАКУПА" if avg_ratio <= 0.92 else (" <-- ПИК ПРОДАЖ" if avg_ratio >= 1.06 else "")
        print(f"  {hour:02d}:00 UTC | коэф. {avg_ratio:.3f} (n={len(vals):>2}) | {bar}{tag}")

    # 3. Бэктест без сращивания ночных гапов
    print("\n=== 3. БЭКТЕСТ СИГНАЛОВ (ИЗОЛИРОВАННЫЕ СЕССИИ, ГОРИЗОНТ <= 24Ч) ===")
    intraday_signals = 0
    intraday_wins = 0
    overnight_signals = 0
    overnight_wins = 0
    total_pnl = 0
    total_invested = 0
    item_stats = []

    for iid, series in by_item.items():
        meta = items[iid]
        sessions = split_into_sessions(series)
        item_trades = 0
        item_wins = 0
        item_pnl = 0

        for s_idx, sess in enumerate(sessions):
            # Волатильность (std) считаем строго внутри непрерывной дневной сессии
            for i in range(2, len(sess)):
                curr = sess[i]
                buy_price = curr["entry_price"]
                if buy_price <= 0:
                    continue

                hist_prices = [x["entry_price"] for x in sess[max(0, i - 12):i] if x["entry_price"] > 0]
                if len(hist_prices) < 2:
                    continue

                s_hist = sorted(hist_prices)
                median = s_hist[len(s_hist) // 2]
                mean = sum(s_hist) / len(s_hist)
                std = max(math.sqrt(sum((x - mean) ** 2 for x in s_hist) / (len(s_hist) - 1)), median * 0.05)

                ref_price = min(median, curr["market_value"]) if curr["market_value"] > 0 else median
                z_score = (ref_price - buy_price) / std
                target_sell = int(ref_price * 0.96)

                deposit = int(meta["vendor_sell_price"] * 0.30)
                net_unit = int(target_sell * 0.95) - buy_price - deposit
                roi = (net_unit / buy_price) * 100

                if net_unit <= 0 or roi < 15.0 or z_score < 1.2:
                    continue

                qty = min(meta["max_stack"], 25000 // buy_price)
                if qty <= 0:
                    continue

                # Будущие точки строго в пределах 24 реальных часов от момента входа
                same_day_future = [
                    f for f in sess[i + 1 :]
                    if 0 < (f["dt"] - curr["dt"]).total_seconds() <= 24 * 3600
                ]

                if same_day_future:
                    # Внутридневной отскок до ночного разрыва
                    intraday_signals += 1
                    item_trades += 1
                    total_invested += buy_price * qty
                    max_p = max(f["entry_price"] for f in same_day_future)
                    if max_p >= target_sell:
                        intraday_wins += 1
                        item_wins += 1
                        pnl = net_unit * qty
                    else:
                        exit_p = same_day_future[-1]["entry_price"]
                        pnl = (int(exit_p * 0.95) - buy_price - deposit) * qty
                    total_pnl += pnl
                    item_pnl += pnl
                elif s_idx + 1 < len(sessions):
                    # Сигнал перед ночным гапом: проверяем открытие следующей сессии в пределах 24ч
                    next_day_future = [
                        f for f in sessions[s_idx + 1]
                        if 0 < (f["dt"] - curr["dt"]).total_seconds() <= 24 * 3600
                    ]
                    if next_day_future:
                        overnight_signals += 1
                        item_trades += 1
                        total_invested += buy_price * qty
                        max_p = max(f["entry_price"] for f in next_day_future)
                        if max_p >= target_sell:
                            overnight_wins += 1
                            item_wins += 1
                            pnl = net_unit * qty
                        else:
                            exit_p = next_day_future[0]["entry_price"]
                            pnl = (int(exit_p * 0.95) - buy_price - deposit) * qty
                        total_pnl += pnl
                        item_pnl += pnl

        if item_trades > 0:
            item_stats.append((meta["name"], item_trades, item_wins, item_pnl))

    intra_wr = (intraday_wins / intraday_signals * 100) if intraday_signals else 0.0
    night_wr = (overnight_wins / overnight_signals * 100) if overnight_signals else 0.0
    total_roi = (total_pnl / total_invested * 100) if total_invested else 0.0

    print(f"Внутридневные сделки (до ночного гапа):  {intraday_wins}/{intraday_signals} (Win Rate: {intra_wr:.1f}%)")
    print(f"Перенос через ночной гап (Overnight):    {overnight_wins}/{overnight_signals} (Win Rate: {night_wr:.1f}%)")
    print(f"Симулированный оборот: {fmt_money(total_invested)} | Чистый PnL: {fmt_money(total_pnl)} ({total_roi:+.1f}%)\n")

    if item_stats:
        print("Разбивка по предметам:")
        item_stats.sort(key=lambda x: x[3], reverse=True)
        for name, trades, wins, pnl in item_stats:
            print(f"  {name:<24} | Сделок: {trades:<2} | Успешных: {wins:<2} | Чистый PnL: {fmt_money(pnl)}")


if __name__ == "__main__":
    run_audit()