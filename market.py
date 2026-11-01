"""XAU/USD demo feed, Twelve Data adapter, and transparent indicator rules.

The dashboard uses the last *closed* candle. Nothing here places trades or
predicts outcomes. The demo feed is deliberately synthetic.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

INTERVALS = {
    "1m": ("1min", 60),
    "5m": ("5min", 5 * 60),
    "15m": ("15min", 15 * 60),
    "1h": ("1h", 60 * 60),
    "4h": ("4h", 4 * 60 * 60),
}


class MarketDataError(Exception):
    """A provider could not supply a usable series."""


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # We request Twelve Data's response in UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def demo_candles(interval: str, now: float | None = None, count: int = 150) -> list[dict]:
    """Reproducible demo OHLC bars, aggregated from one synthetic 1m path."""
    if interval not in INTERVALS:
        raise ValueError("Unsupported timeframe")
    now = time.time() if now is None else now
    seconds = INTERVALS[interval][1]
    current_slot = int(now // seconds)
    parts_per_bar = seconds // 60

    def close_for(slot: int) -> float:
        # A deterministic mathematical series, never real market data.
        return (3945.0 + 15.0 * math.sin(slot / 450.0)
                + 6.2 * math.sin(slot / 125.0)
                + 2.1 * math.cos(slot / 46.5)
                + 0.46 * math.sin(slot * 1.731)
                + 0.16 * math.cos(slot * 2.137))

    candles = []
    for group in range(current_slot - count, current_slot):
        opens, highs, lows, closes = [], [], [], []
        for slot in range(group * parts_per_bar, (group + 1) * parts_per_bar):
            open_price = close_for(slot - 1) + 0.09 * math.sin(slot * 1.13)
            close = close_for(slot)
            upper_wick = 0.12 + 0.36 * (0.5 + 0.5 * math.sin(slot * 2.43))
            lower_wick = 0.12 + 0.36 * (0.5 + 0.5 * math.cos(slot * 1.81))
            opens.append(round(open_price, 4))
            closes.append(round(close, 4))
            highs.append(round(max(open_price, close) + upper_wick, 4))
            lows.append(round(min(open_price, close) - lower_wick, 4))
        candles.append({
            "time": _iso(group * seconds), "open": opens[0], "high": max(highs),
            "low": min(lows), "close": closes[-1],
        })
    return candles


def twelve_data_candles(interval: str, api_key: str, now: float | None = None, include_open: bool = False) -> list[dict]:
    """Validate XAU/USD OHLC; optionally return the currently forming bar."""
    if interval not in INTERVALS:
        raise ValueError("Unsupported timeframe")
    now = time.time() if now is None else now
    provider_interval, seconds = INTERVALS[interval]
    params = urlencode({
        "symbol": "XAU/USD",
        "interval": provider_interval,
        "outputsize": 160,
        "timezone": "UTC",
        "apikey": api_key,
    })
    req = Request(
        "https://api.twelvedata.com/time_series?" + params,
        headers={"User-Agent": "XAUSignal/1.0", "Accept": "application/json"},
    )
    try:
        with urlopen(req, timeout=12) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 429:
            raise MarketDataError("Twelve Data's rate or daily credit limit was reached. Wait for a reset.") from exc
        raise MarketDataError("Could not retrieve provider data. Check the key and plan.") from exc
    except Exception as exc:
        # Do not pass a URL (which contains the secret API key) to the client.
        raise MarketDataError("Could not reach the market-data provider.") from exc

    if isinstance(payload, dict) and str(payload.get("code", "")) == "429":
        raise MarketDataError("Twelve Data's rate or daily credit limit was reached. Wait for a reset.")
    if not isinstance(payload, dict) or payload.get("status") == "error" or not isinstance(payload.get("values"), list):
        raise MarketDataError("The provider did not return usable XAU/USD data. Check the key, plan, and API allowance.")

    by_timestamp: dict[float, dict] = {}
    for row in payload["values"]:
        try:
            stamp = _parse_timestamp(str(row["datetime"]))
            # Time-series datetimes represent candle starts. A forming bar is
            # returned only on request and never used by the signal engine.
            if stamp > now or (stamp + seconds > now and not include_open):
                continue
            o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
            if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
                continue
            if h < max(o, c, l) or l > min(o, c, h):
                continue
            candle = {"time": _iso(stamp), "open": o, "high": h, "low": l, "close": c}
            if include_open:
                candle["closed"] = stamp + seconds <= now
            by_timestamp[stamp] = candle
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    candles = [by_timestamp[s] for s in sorted(by_timestamp)]
    if sum(1 for candle in candles if candle.get("closed", True)) < 35:
        raise MarketDataError("Not enough valid closed candles were returned for the indicators.")
    return candles


def ema(values: list[float], period: int) -> list[float]:
    """EMA initialized with the first close."""
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(value * alpha + out[-1] * (1.0 - alpha))
    return out


def rsi_wilder(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        raise ValueError("Not enough prices for RSI")
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(x, 0) for x in changes[:period]) / period
    loss = sum(max(-x, 0) for x in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    if loss == 0:
        return 50.0 if gain == 0 else 100.0
    return 100.0 - (100.0 / (1.0 + gain / loss))


def atr_wilder(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period:
        raise ValueError("Not enough prices for ATR")
    ranges = []
    previous_close = None
    for bar in candles:
        high, low = bar["high"], bar["low"]
        true_range = high - low
        if previous_close is not None:
            true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
        ranges.append(true_range)
        previous_close = bar["close"]
    atr = sum(ranges[:period]) / period
    for value in ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def aggregate_five_minute(closed_one_minute: list[dict]) -> list[dict]:
    """Build complete, contiguous 5m bars from closed 1m bars, without lookahead."""
    groups: dict[int, dict[int, dict]] = {}
    for bar in closed_one_minute:
        stamp = int(_parse_timestamp(bar["time"]))
        start = (stamp // 300) * 300
        groups.setdefault(start, {})[stamp] = bar
    out = []
    for start in sorted(groups):
        expected = [start + offset for offset in range(0, 300, 60)]
        if not all(stamp in groups[start] for stamp in expected):
            continue
        bars = [groups[start][stamp] for stamp in expected]
        out.append({"time": _iso(start), "open": bars[0]["open"],
                    "high": max(bar["high"] for bar in bars),
                    "low": min(bar["low"] for bar in bars), "close": bars[-1]["close"]})
    return out


def scalp_setup(candles: list[dict], rsi: float, stale: bool) -> dict:
    """Experimental 5m trend + closed 1m pullback/re-cross, not a trade."""
    five_minute = aggregate_five_minute(candles)
    if stale:
        return {"signal": "WAIT", "reason": "The 1m data is stale; no current entry is issued.", "bias5m": "Unavailable"}
    last_end = int(_parse_timestamp(candles[-1]["time"])) + 60
    previous_start = int(_parse_timestamp(candles[-2]["time"]))
    if previous_start + 60 != last_end - 60:
        return {"signal": "WAIT", "reason": "A recent 1m candle is missing; no entry is issued.", "bias5m": "Unavailable"}
    expected_five_close = (last_end // 300) * 300
    if not five_minute or int(_parse_timestamp(five_minute[-1]["time"])) + 300 != expected_five_close:
        return {"signal": "WAIT", "reason": "The latest complete 5m candle is missing.", "bias5m": "Unavailable"}
    # Only a consecutive 5m suffix is used; never bridge a gap or look ahead.
    suffix = [five_minute[-1]]
    for bar in reversed(five_minute[:-1]):
        if int(_parse_timestamp(bar["time"])) + 300 != int(_parse_timestamp(suffix[0]["time"])):
            break
        suffix.insert(0, bar)
    if len(suffix) < 21:
        return {"signal": "WAIT", "reason": "Not enough consecutive complete 5m candles for the trend filter.", "bias5m": "Unavailable"}
    # Also check the current partial 5m group: the last two 1m bars can be
    # contiguous even when an earlier minute in this group is missing.
    present = {int(_parse_timestamp(bar["time"])) for bar in candles}
    if any(stamp not in present for stamp in range(expected_five_close, last_end, 60)):
        return {"signal": "WAIT", "reason": "A 1m candle is missing after the latest complete 5m bar.", "bias5m": "Unavailable"}
    closes = [float(bar["close"]) for bar in candles]
    m1_fast = ema(closes, 9)
    m1_slow = ema(closes, 21)
    five_closes = [float(bar["close"]) for bar in suffix]
    m5_fast = ema(five_closes, 9)[-1]
    m5_slow = ema(five_closes, 21)[-1]
    bias = "Bullish" if m5_fast > m5_slow else "Bearish" if m5_fast < m5_slow else "Flat"
    long_reclaim = closes[-2] <= m1_fast[-2] and closes[-1] > m1_fast[-1]
    short_reclaim = closes[-2] >= m1_fast[-2] and closes[-1] < m1_fast[-1]
    if m5_fast > m5_slow and m1_fast[-1] > m1_slow[-1] and long_reclaim and 52 <= rsi <= 68:
        signal = "BUY"
        reason = "5m trend is up; the closed 1m candle reclaimed EMA 9 with RSI 52–68."
    elif m5_fast < m5_slow and m1_fast[-1] < m1_slow[-1] and short_reclaim and 32 <= rsi <= 48:
        signal = "SELL"
        reason = "5m trend is down; the closed 1m candle fell below EMA 9 with RSI 32–48."
    else:
        signal = "WAIT"
        reason = "No 1m pullback entry with 5m trend and RSI confirmation on the latest closed candle."
    return {"signal": signal, "reason": reason, "bias5m": bias,
            "ema9_5m": round(m5_fast, 4), "ema21_5m": round(m5_slow, 4)}


def analyze(candles: list[dict], interval: str, source: str, now: float | None = None) -> dict:
    """Create an analysis snapshot, never an order or performance claim."""
    if interval not in INTERVALS or len(candles) < 35:
        raise ValueError("Unsupported interval or not enough candles")
    now = time.time() if now is None else now
    closes = [float(c["close"]) for c in candles]
    fast_series, slow_series = ema(closes, 9), ema(closes, 21)
    fast, slow = fast_series[-1], slow_series[-1]
    rsi = rsi_wilder(closes)
    atr = atr_wilder(candles)
    seconds = INTERVALS[interval][1]
    closed_at = _parse_timestamp(candles[-1]["time"]) + seconds
    # The 1m scalping example needs fresher data; longer intervals allow
    # 3 bars or 30 minutes. This is not a market-hours detector.
    max_age = 60 if interval == "1m" else max(3 * seconds, 30 * 60)
    stale = source != "demo" and now - closed_at >= max_age
    scalp = None
    if interval == "1m":
        scalp = scalp_setup(candles, rsi, stale)
        signal, reason = scalp["signal"], scalp["reason"]
    elif stale:
        signal = "WAIT"
        reason = "The last candle is stale, so no current signal is issued."
    elif fast > slow and 55 <= rsi <= 70:
        signal = "BUY"
        reason = "EMA 9 is above EMA 21 and RSI is in the 55–70 buy range."
    elif fast < slow and 30 <= rsi <= 45:
        signal = "SELL"
        reason = "EMA 9 is below EMA 21 and RSI is in the 30–45 sell range."
    else:
        signal = "WAIT"
        reason = "The EMA trend and RSI are not aligned within the strategy ranges."

    if rsi > 70:
        momentum = "Overbought zone"
    elif rsi < 30:
        momentum = "Oversold zone"
    elif rsi >= 55:
        momentum = "Bullish zone"
    elif rsi <= 45:
        momentum = "Bearish zone"
    else:
        momentum = "Neutral zone"

    trend = "Bullish" if fast > slow else "Bearish" if fast < slow else "Flat"
    enriched = []
    for candle, fast_value, slow_value in zip(candles, fast_series, slow_series):
        enriched.append({**candle, "ema9": round(fast_value, 5), "ema21": round(slow_value, 5)})

    previous = closes[-2]
    return {
        "symbol": "XAU/USD",
        "interval": interval,
        "source": {
            "mode": source,
            "label": "Simulated data" if source == "demo" else "Data provided by Twelve Data",
            "stale": stale,
            "max_age_seconds": max_age,
            "forming": False,
            "last_closed_at": _iso(closed_at),
            "retrieved_at": _iso(now),
        },
        "latest": {
            "price": round(closes[-1], 4),
            "closed_price": round(closes[-1], 4),
            "previous_close": round(previous, 4),
            "change": round(closes[-1] - previous, 4),
            "change_percent": round((closes[-1] - previous) / previous * 100, 4),
        },
        "analysis": {
            "signal": signal,
            "reason": reason,
            "trend": trend,
            "ema9": round(fast, 4),
            "ema21": round(slow, 4),
            "rsi14": round(rsi, 2),
            "rsi_zone": momentum,
            "atr14": round(atr, 4),
            "strategy": "scalp_1m" if interval == "1m" else "ema_rsi_alignment",
            "bias5m": scalp.get("bias5m") if scalp else None,
            "ema9_5m": scalp.get("ema9_5m") if scalp else None,
            "ema21_5m": scalp.get("ema21_5m") if scalp else None,
        },
        "candles": enriched,
        # Historical reconstructions, not real-time notifications or fills.
        "recent_signals": recent_scalp_signals(candles) if interval == "1m" and not stale else [],
    }


def recent_scalp_signals(closed_candles: list[dict]) -> list[dict]:
    """Evaluate the last five closed 1m bars independently without lookahead."""
    matches = []
    start = max(34, len(closed_candles) - 5)
    for index in range(start, len(closed_candles)):
        prefix = closed_candles[:index + 1]
        rsi = rsi_wilder([float(bar["close"]) for bar in prefix], 14)
        result = scalp_setup(prefix, rsi, stale=False)
        if result["signal"] in ("BUY", "SELL"):
            matches.append({"signal": result["signal"],
                            "closed_at": _iso(_parse_timestamp(prefix[-1]["time"]) + 60),
                            "price": round(float(prefix[-1]["close"]), 4)})
    return matches


def append_forming_candle(snapshot: dict, candle: dict) -> dict:
    """Add a polled, incomplete bar to the chart, not to the closed-bar signal."""
    if not snapshot["candles"]:
        return snapshot
    last = snapshot["candles"][-1]
    value = float(candle["close"])
    fast = value * (2 / 10) + last["ema9"] * (1 - 2 / 10)
    slow = value * (2 / 22) + last["ema21"] * (1 - 2 / 22)
    new_bar = {**candle, "closed": False, "ema9": round(fast, 5), "ema21": round(slow, 5)}
    previous = float(snapshot["latest"]["closed_price"])
    snapshot["candles"].append(new_bar)
    snapshot["source"]["forming"] = True
    snapshot["latest"].update(price=round(value, 4), previous_close=round(previous, 4),
                              change=round(value - previous, 4),
                              change_percent=round((value - previous) / previous * 100, 4))
    return snapshot
