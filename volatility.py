"""ATR/NATR calculation for event-driven dynamic grid levels."""
from dataclasses import dataclass
from statistics import median
import re
import time


@dataclass(frozen=True)
class VolatilityProfile:
    atr: float
    natr: float
    median_natr: float
    raw_ratio: float
    latest_candle_timestamp: int
    baseline_samples: int


def timeframe_milliseconds(timeframe: str) -> int:
    match = re.fullmatch(r"(\d+)([mhd])", timeframe)
    if not match:
        raise ValueError(f"不支持的K线周期: {timeframe}")
    value = int(match.group(1))
    unit_ms = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return value * unit_ms[match.group(2)]


def calculate_volatility_profile(
    rows: list[list[float]],
    *,
    timeframe: str,
    period: int,
    baseline_bars: int,
    now_ms: int | None = None,
) -> VolatilityProfile | None:
    """Return Wilder ATR and current/median NATR using closed candles only."""
    if period <= 0 or baseline_bars <= 0:
        raise ValueError("ATR period 和 baseline_bars 必须大于零")

    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    duration_ms = timeframe_milliseconds(timeframe)
    candles = [
        row for row in rows
        if len(row) >= 5 and float(row[0]) + duration_ms <= current_ms
    ]
    candles.sort(key=lambda row: float(row[0]))
    if len(candles) < period + baseline_bars + 1:
        return None

    true_ranges: list[tuple[int, float, float]] = []
    previous_close = float(candles[0][4])
    for row in candles[1:]:
        timestamp = int(float(row[0]))
        high = float(row[2])
        low = float(row[3])
        close = float(row[4])
        if high <= 0 or low <= 0 or close <= 0 or high < low:
            return None
        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close),
        )
        true_ranges.append((timestamp, true_range, close))
        previous_close = close

    atr = sum(item[1] for item in true_ranges[:period]) / period
    natr_values: list[tuple[int, float, float]] = []
    first_timestamp, _, first_close = true_ranges[period - 1]
    natr_values.append((first_timestamp, atr, atr / first_close))

    for timestamp, true_range, close in true_ranges[period:]:
        atr = ((atr * (period - 1)) + true_range) / period
        natr_values.append((timestamp, atr, atr / close))

    baseline = natr_values[-baseline_bars:]
    if len(baseline) < baseline_bars:
        return None
    current_timestamp, current_atr, current_natr = baseline[-1]
    median_natr = median(item[2] for item in baseline)
    if current_natr <= 0 or median_natr <= 0:
        return None

    return VolatilityProfile(
        atr=current_atr,
        natr=current_natr,
        median_natr=median_natr,
        raw_ratio=current_natr / median_natr,
        latest_candle_timestamp=current_timestamp,
        baseline_samples=len(baseline),
    )
