import math
from collections import defaultdict
from .constants import MIN_HIST_TICKS_FLIP, MIN_TICKS_FOR_CLOCK_BOOST, REGIME_WINDOW_TICKS
from .types import HistPoint


def format_wow_money(copper: int) -> str:
    sign = "-" if copper < 0 else ""
    c = abs(int(copper))
    gold = c // 10000
    silver = (c % 10000) // 100
    rem_copper = c % 100
    if gold > 0:
        return f"{sign}{gold}g {silver:02d}s {rem_copper:02d}c"
    return f"{sign}{silver}s {rem_copper:02d}c"


def calculate_deposit_copper(vendor_sell_price: int, quantity: int, hours: int = 8) -> int:
    rate = {2: 0.15, 8: 0.30, 24: 0.60}.get(hours, 0.30)
    return int(vendor_sell_price * quantity * rate)


def percentile(sorted_vals: list[int] | list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * q
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(sorted_vals[lo])
    return float(sorted_vals[lo]) + (float(sorted_vals[hi]) - float(sorted_vals[lo])) * (idx - lo)


def percentile_rank(sorted_vals: list[int], value: int) -> float:
    if not sorted_vals:
        return 50.0
    n = len(sorted_vals)
    below = sum(1 for x in sorted_vals if x < value)
    equal = sum(1 for x in sorted_vals if x == value)
    return round(((below + 0.5 * equal) / n) * 100.0, 1)


def compute_robust_stats(prices: list[int]) -> tuple[float, float, float, float]:
    if not prices:
        return 0.0, 0.0, 0.0, 0.0
    s = sorted(prices)
    p25 = percentile(s, 0.25)
    median = percentile(s, 0.50)
    p75 = percentile(s, 0.75)
    if len(s) < 4:
        return median, max(median * 0.10, 1.0), p25, p75

    abs_devs = sorted(abs(x - median) for x in s)
    mad = percentile(abs_devs, 0.50)
    robust_std = max(1.4826 * mad, median * 0.05, 1.0)
    return median, robust_std, p25, p75


def compute_qty_churn(quantities: list[int]) -> float:
    if len(quantities) < 2:
        return 0.0
    deltas = sorted(abs(quantities[i] - quantities[i - 1]) for i in range(1, len(quantities)))
    return percentile(deltas, 0.50)


def compute_item_clock(series: list[HistPoint], current_hour: int) -> dict:
    """
    Персональные биологические часы актива (круговое 3-часовое сглаживание по чистым срезам Blizzard).
    Сам различает сырьё (дно вечером -> пик утром) и расходники (дно днём -> пик в 19-22 UTC).
    """
    by_hour: dict[int, list[int]] = defaultdict(list)
    for pt in series:
        by_hour[pt.hour_of_day % 24].append(pt.entry_price)

    if len(by_hour) < 12:
        return {
            "has_clock": False,
            "trough_hour": -1,
            "peak_hour": -1,
            "trough_med": 0.0,
            "peak_med": 0.0,
            "clock_edge_pct": 0.0,
            "hours_to_peak": 12,
        }

    smooth_med: dict[int, float] = {}
    for h in range(24):
        bucket = by_hour.get((h - 1) % 24, []) + by_hour.get(h, []) + by_hour.get((h + 1) % 24, [])
        if len(bucket) >= 2:
            smooth_med[h] = percentile(sorted(bucket), 0.50)

    if len(smooth_med) < 10:
        return {
            "has_clock": False,
            "trough_hour": -1,
            "peak_hour": -1,
            "trough_med": 0.0,
            "peak_med": 0.0,
            "clock_edge_pct": 0.0,
            "hours_to_peak": 12,
        }

    trough_hour, trough_med = min(smooth_med.items(), key=lambda kv: kv[1])
    peak_hour, peak_med = max(smooth_med.items(), key=lambda kv: kv[1])
    edge_pct = round(((peak_med / trough_med) - 1.0) * 100.0, 1) if trough_med > 0 else 0.0
    hours_to_peak = (peak_hour - current_hour) % 24

    return {
        "has_clock": True,
        "trough_hour": trough_hour,
        "peak_hour": peak_hour,
        "trough_med": trough_med,
        "peak_med": peak_med,
        "clock_edge_pct": edge_pct,
        "hours_to_peak": hours_to_peak,
    }


def analyze_price_regime(series: list[HistPoint], clock: dict) -> dict:
    """
    Режимный фильтр на последних 72ч чистых тиков house_110:
    убивает умирающие тренды, падающие ножи и разовые всплески; требует минимум 2 цикла.
    """
    recent_series = series[-REGIME_WINDOW_TICKS:]
    prices = [pt.entry_price for pt in recent_series]
    n = len(prices)
    if n < MIN_HIST_TICKS_FLIP:
        return {"tradable": False, "reason": "short_history"}

    # 1a. Долгий структурный спад (старое окно vs последние 16ч)
    split = max(4, n - 16)
    old_med = percentile(sorted(prices[:split]), 0.50)
    recent_16 = prices[split:]
    recent_16_med = percentile(sorted(recent_16), 0.50)
    if old_med > 0 and recent_16_med < old_med * 0.85 and max(recent_16) < old_med * 0.94:
        return {"tradable": False, "reason": "structural_downtrend"}

    # 1b. Падающий нож (последние 6ч против предыдущих 12ч)
    if n >= 18:
        prev_12 = prices[-18:-6]
        last_6 = prices[-6:]
        prev_12_med = percentile(sorted(prev_12), 0.50)
        last_6_med = percentile(sorted(last_6), 0.50)
        if prev_12_med > 0 and last_6_med < prev_12_med * 0.88 and max(last_6) < prev_12_med * 0.95:
            return {"tradable": False, "reason": "falling_knife"}

    # 2. Устойчивая полка (3-часовой rolling min + обязательная проверка за последние 24 тика)
    sustained_3h = [min(prices[i : i + 3]) for i in range(n - 2)]
    plateau_window = percentile(sorted(sustained_3h), 0.80)

    recent_24 = prices[-24:] if n >= 24 else prices
    sustained_2h_24 = [min(recent_24[i : i + 2]) for i in range(len(recent_24) - 1)]
    fresh_ceiling_24 = float(max(sustained_2h_24)) if sustained_2h_24 else plateau_window

    ref_plateau = min(plateau_window, fresh_ceiling_24)

    # Подтягиваем полку по персональным часам только когда накопилось >= 96 срезов (4 суток)
    if (
        len(series) >= MIN_TICKS_FOR_CLOCK_BOOST
        and clock["has_clock"]
        and clock["clock_edge_pct"] >= 18.0
        and clock["peak_med"] > 0
    ):
        ref_plateau = min(max(ref_plateau, clock["peak_med"] * 0.96), fresh_ceiling_24)

    recent_low = percentile(sorted(recent_24), 0.20)
    if recent_low <= 0 or ref_plateau <= recent_low * 1.12:
        return {"tradable": False, "reason": "flat_recent_24h"}

    # 3. Счётчик подтверждённых циклов возврата (всегда >= 2 + минимум 1 свежий за последние 30 тиков)
    low_thr = recent_low * 1.08
    high_thr = ref_plateau * 0.93

    def _count_bounces(seq: list[int]) -> int:
        st, cnt = 0, 0
        for p in seq:
            if p <= low_thr:
                st = -1
            elif p >= high_thr:
                if st == -1:
                    cnt += 1
                st = 1
        return cnt

    bounces = _count_bounces(prices)
    recent_bounces = _count_bounces(prices[-30:] if n >= 30 else prices)
    if bounces < 2 or recent_bounces < 1:
        return {"tradable": False, "reason": f"no_fresh_pattern({bounces}/{recent_bounces})"}

    return {
        "tradable": True,
        "ref_plateau": ref_plateau,
        "fresh_ceiling_24": fresh_ceiling_24,
        "recent_low": recent_low,
        "recent_med": recent_16_med,
        "bounces": bounces,
        "recent_bounces": recent_bounces,
        "spread_pct": round((ref_plateau / recent_low - 1.0) * 100.0, 1),
    }