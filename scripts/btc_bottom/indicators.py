"""Phase 2: Pure causal technical indicators, causal swing pivots, relative S/R clustering,
expanding Welford LGC, and WK-Warn series.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class Pivot:
    idx: int
    confirm_idx: int
    price: float
    kind: str  # "HIGH" | "LOW"
    ts: int
    date_utc: str


@dataclass(frozen=True)
class WKWarnState:
    week_idx: int
    week_open_ts: int
    week_close_ts: int
    open_date_utc: str
    sma_value: float | None
    active: bool
    reclaim_cnt: int
    active_weeks: int
    episode_start_date: str | None


def calc_sma(values: Sequence[float | None], n: int) -> list[float | None]:
    """Return simple moving average over n items; None if index < n - 1 or any window item is None."""
    if n <= 0:
        raise ValueError("SMA period n must be positive")
    out: list[float | None] = [None] * len(values)
    running_sum = 0.0
    none_count = 0
    for i, val in enumerate(values):
        if val is None:
            none_count += 1
        else:
            running_sum += float(val)
        if i >= n:
            old = values[i - n]
            if old is None:
                none_count -= 1
            else:
                running_sum -= float(old)
        if i >= n - 1 and none_count == 0:
            out[i] = running_sum / n
    return out


def calc_ema_sma_seeded(values: Sequence[float | None], n: int) -> list[float | None]:
    """Scan left-to-right for the first n non-None values; seed at index k (the n-th non-None element)
    with their arithmetic mean, then recurse with alpha = 2 / (n + 1).
    """
    if n <= 0:
        raise ValueError("EMA period n must be positive")
    out: list[float | None] = [None] * len(values)
    seed_vals: list[float] = []
    seed_idx = -1
    for i, val in enumerate(values):
        if val is not None:
            seed_vals.append(float(val))
            if len(seed_vals) == n:
                seed_idx = i
                break
    if seed_idx == -1:
        return out

    ema = sum(seed_vals) / n
    out[seed_idx] = ema
    alpha = 2.0 / (n + 1.0)
    for i in range(seed_idx + 1, len(values)):
        val = values[i]
        if val is None:
            out[i] = None
        else:
            ema = float(val) * alpha + ema * (1.0 - alpha)
            out[i] = ema
    return out


def calc_wilder_atr(bars: Sequence[Any], n: int = 14) -> list[float | None]:
    """Wilder ATR(n): TR[0] = High[0] - Low[0]; ATR[n-1] = sum(TR[0..n-1])/n; recurse (prev*(n-1)+TR)/n."""
    if n <= 0:
        raise ValueError("ATR period n must be positive")
    length = len(bars)
    out: list[float | None] = [None] * length
    if length < n:
        return out

    trs: list[float] = [0.0] * length
    for i, b in enumerate(bars):
        h = float(b.high)
        l = float(b.low)
        if i == 0:
            trs[i] = h - l
        else:
            prev_c = float(bars[i - 1].close)
            trs[i] = max(h - l, abs(h - prev_c), abs(l - prev_c))

    atr = sum(trs[:n]) / n
    out[n - 1] = atr
    for i in range(n, length):
        atr = (atr * (n - 1) + trs[i]) / n
        out[i] = atr
    return out


def calc_wilder_rsi(closes: Sequence[float], n: int = 14) -> list[float | None]:
    """Wilder RSI(n): diffs d[1..n] seed avg_gain & avg_loss at index n; 0..n-1 are None."""
    if n <= 0:
        raise ValueError("RSI period n must be positive")
    length = len(closes)
    out: list[float | None] = [None] * length
    if length <= n:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        d = float(closes[i]) - float(closes[i - 1])
        if d > 0.0:
            gains += d
        elif d < 0.0:
            losses += -d

    avg_gain = gains / n
    avg_loss = losses / n
    out[n] = 100.0 if avg_loss == 0.0 else (100.0 - 100.0 / (1.0 + avg_gain / avg_loss))

    for i in range(n + 1, length):
        d = float(closes[i]) - float(closes[i - 1])
        g = d if d > 0.0 else 0.0
        l = -d if d < 0.0 else 0.0
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
        out[i] = 100.0 if avg_loss == 0.0 else (100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return out


def calc_macd(
    closes: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    sig: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return (dif, dea, hist). DIF is non-None from index slow-1 (25); DEA & Hist from slow+sig-2 (33)."""
    ema_fast = calc_ema_sma_seeded(closes, fast)
    ema_slow = calc_ema_sma_seeded(closes, slow)
    length = len(closes)
    dif: list[float | None] = [None] * length
    for i in range(length):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = float(ema_fast[i]) - float(ema_slow[i])
    dea = calc_ema_sma_seeded(dif, sig)
    hist: list[float | None] = [None] * length
    for i in range(length):
        if dif[i] is not None and dea[i] is not None:
            hist[i] = float(dif[i]) - float(dea[i])
    return dif, dea, hist


def calc_bollinger(
    closes: Sequence[float],
    n: int = 20,
    k: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return (upper, mid, lower) using population standard deviation statistics.pstdev over n bars."""
    length = len(closes)
    mid = calc_sma(closes, n)
    upper: list[float | None] = [None] * length
    lower: list[float | None] = [None] * length
    for i in range(n - 1, length):
        m = mid[i]
        if m is None:
            continue
        sd = statistics.pstdev(closes[i - n + 1 : i + 1])
        upper[i] = m + k * sd
        lower[i] = m - k * sd
    return upper, mid, lower


def compute_causal_pivots(
    bars: Sequence[Any],
    left_bars: int = 5,
    right_bars: int = 5,
) -> list[Pivot]:
    """Identify strict local highs and lows confirmed at confirm_idx = p + right_bars.

    For weekly bars, uses week_close_ts as ts and open_date_utc as date_utc; for daily bars,
    uses close_ts and date_utc.
    """
    n = len(bars)
    pivots: list[Pivot] = []
    if n < left_bars + right_bars + 1:
        return pivots

    for p in range(left_bars, n - right_bars):
        bar_p = bars[p]
        hp = float(bar_p.high)
        lp = float(bar_p.low)
        is_high = True
        is_low = True
        for j in range(p - left_bars, p + right_bars + 1):
            if j == p:
                continue
            if float(bars[j].high) >= hp:
                is_high = False
            if float(bars[j].low) <= lp:
                is_low = False
            if not is_high and not is_low:
                break

        close_ts = int(getattr(bar_p, "close_ts", getattr(bar_p, "week_close_ts", 0)))
        d_utc = str(getattr(bar_p, "date_utc", getattr(bar_p, "open_date_utc", "")))
        if is_high:
            pivots.append(
                Pivot(
                    idx=p,
                    confirm_idx=p + right_bars,
                    price=hp,
                    kind="HIGH",
                    ts=close_ts,
                    date_utc=d_utc,
                )
            )
        if is_low:
            pivots.append(
                Pivot(
                    idx=p,
                    confirm_idx=p + right_bars,
                    price=lp,
                    kind="LOW",
                    ts=close_ts,
                    date_utc=d_utc,
                )
            )
    return pivots


def compute_causal_sr_zones(
    daily_bars_prefix: Sequence[Any],
    weekly_pivots_prefix: Sequence[Pivot],
    i: int,
    atr14_i: float | None,
    *,
    daily_pivots_prefix: Sequence[Pivot] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Compute causal relative-percentage S/R zones at index i.

    Candidate pool:
      - Confirmed daily (5,5) pivots with max(0, i - 365) <= p.idx and p.confirm_idx <= i (weight 1.0)
      - Closed confirmed weekly (3,3) pivots with bar_close_ts - 1095*86400 <= p.ts <= bar_close_ts (weight 2.0)
    """
    if not daily_bars_prefix or i < 0 or i >= len(daily_bars_prefix):
        return {"supports": [], "resistances": []}

    cur_bar = daily_bars_prefix[i]
    close_i = float(cur_bar.close)
    bar_close_ts = int(cur_bar.close_ts)

    if daily_pivots_prefix is None:
        all_daily_pivs = compute_causal_pivots(daily_bars_prefix[: i + 1], 5, 5)
    else:
        all_daily_pivs = [p for p in daily_pivots_prefix if p.confirm_idx <= i]

    min_daily_idx = max(0, i - 365)
    min_weekly_ts = bar_close_ts - 1095 * 86400

    candidates: list[tuple[float, float]] = []
    for p in all_daily_pivs:
        if p.idx >= min_daily_idx and p.confirm_idx <= i:
            candidates.append((float(p.price), 1.0))
    for wp in weekly_pivots_prefix:
        if min_weekly_ts <= wp.ts <= bar_close_ts:
            candidates.append((float(wp.price), 2.0))

    if not candidates:
        return {"supports": [], "resistances": []}

    atr_pct = (float(atr14_i) / close_i) if (atr14_i is not None and atr14_i > 0.0 and close_i > 0.0) else 0.02
    tol_pct = max(0.015, 0.75 * atr_pct)

    candidates.sort(key=lambda x: x[0])
    clusters: list[dict[str, float]] = []
    for px, w in candidates:
        if not clusters:
            clusters.append(
                {
                    "center": px,
                    "total_weight": w,
                    "weighted_sum": px * w,
                    "min_p": px,
                    "max_p": px,
                }
            )
            continue
        cur = clusters[-1]
        if abs(px - cur["center"]) / cur["center"] <= tol_pct:
            cur["total_weight"] += w
            cur["weighted_sum"] += px * w
            cur["center"] = cur["weighted_sum"] / cur["total_weight"]
            cur["min_p"] = min(cur["min_p"], px)
            cur["max_p"] = max(cur["max_p"], px)
        else:
            clusters.append(
                {
                    "center": px,
                    "total_weight": w,
                    "weighted_sum": px * w,
                    "min_p": px,
                    "max_p": px,
                }
            )

    recent_bars = daily_bars_prefix[min_daily_idx : i + 1]
    scored_above: list[dict[str, Any]] = []
    scored_below: list[dict[str, Any]] = []

    for cl in clusters:
        center = cl["center"]
        band_half = max(0.5 * (cl["max_p"] - cl["min_p"]), 0.005 * center)
        band_low = center - band_half
        band_high = center + band_half
        touches = sum(1 for b in recent_bars if float(b.high) >= band_low and float(b.low) <= band_high)
        score = cl["total_weight"] + 0.25 * touches
        dist_pct = ((center - close_i) / close_i) * 100.0
        item = {
            "price": round(center, 2),
            "band_low": round(band_low, 2),
            "band_high": round(band_high, 2),
            "touches": int(touches),
            "score": round(score, 4),
            "dist_pct": round(dist_pct, 4),
            "_raw_center": center,
            "_raw_score": score,
        }
        if center > close_i:
            scored_above.append(item)
        else:
            scored_below.append(item)

    scored_above.sort(key=lambda z: (-z["_raw_score"], abs(z["_raw_center"] - close_i), z["_raw_center"]))
    top_res = sorted(scored_above[:3], key=lambda z: z["_raw_center"])
    resistances: list[dict[str, Any]] = []
    for idx, z in enumerate(top_res, start=1):
        resistances.append(
            {
                "code": f"R{idx}",
                "price": z["price"],
                "band_low": z["band_low"],
                "band_high": z["band_high"],
                "touches": z["touches"],
                "score": z["score"],
                "dist_pct": z["dist_pct"],
            }
        )

    scored_below.sort(key=lambda z: (-z["_raw_score"], abs(z["_raw_center"] - close_i), z["_raw_center"]))
    top_sup = sorted(scored_below[:3], key=lambda z: -z["_raw_center"])
    supports: list[dict[str, Any]] = []
    for idx, z in enumerate(top_sup, start=1):
        supports.append(
            {
                "code": f"S{idx}",
                "price": z["price"],
                "band_low": z["band_low"],
                "band_high": z["band_high"],
                "touches": z["touches"],
                "score": z["score"],
                "dist_pct": z["dist_pct"],
            }
        )

    return {"supports": supports, "resistances": resistances}


def compute_expanding_lgc(macro_daily: Sequence[Any]) -> list[float | None]:
    """Welford/Chan online centered log-log regression from Genesis (2009-01-03).

    Returns LGC_Lower_Band[t] = 10 ** (intercept + slope * x_t - 1.645 * sigma) for n >= 365.
    """
    genesis = dt.date(2009, 1, 3)
    out: list[float | None] = [None] * len(macro_daily)
    n = 0
    mean_x = 0.0
    mean_y = 0.0
    c_xx = 0.0
    c_xy = 0.0
    c_yy = 0.0

    for t, row in enumerate(macro_daily):
        d_utc = dt.date.fromisoformat(str(row.date_utc)[:10])
        days_since = max(1, (d_utc - genesis).days)
        x_t = math.log10(float(days_since))
        y_t = math.log10(float(row.close))

        n += 1
        dx = x_t - mean_x
        dy = y_t - mean_y
        mean_x += dx / n
        mean_y += dy / n
        dx2 = x_t - mean_x
        dy2 = y_t - mean_y
        c_xx += dx * dx2
        c_xy += dx * dy2
        c_yy += dy * dy2

        if n >= 365 and c_xx > 0.0:
            slope = c_xy / c_xx
            intercept = mean_y - slope * mean_x
            sse = max(0.0, c_yy - slope * c_xy)
            sigma = math.sqrt(sse / (n - 2))
            out[t] = 10.0 ** (intercept + slope * x_t - 1.645 * sigma)
    return out


def compute_wk_warn_series(weekly_rows: Sequence[Any], ma_period: int = 50) -> list[WKWarnState]:
    """Compute weekly breakdown warning state series for ma_period (50W primary, 200W comparison).

    Turns ACTIVE when Close[w] < SMA[w]; resets to INACTIVE only after 2 consecutive weekly closes >= SMA[w].
    """
    closes = [float(w.close) for w in weekly_rows]
    sma_vals = calc_sma(closes, ma_period)

    active = False
    reclaim_cnt = 0
    active_weeks = 0
    episode_start_date: str | None = None
    states: list[WKWarnState] = []

    for w_idx, w in enumerate(weekly_rows):
        sma_w = sma_vals[w_idx]
        if sma_w is None:
            active = False
            reclaim_cnt = 0
            active_weeks = 0
            episode_start_date = None
        else:
            if float(w.close) < sma_w:
                if not active:
                    episode_start_date = str(w.open_date_utc)
                active = True
                reclaim_cnt = 0
                active_weeks += 1
            else:
                if active:
                    reclaim_cnt += 1
                    active_weeks += 1
                    if reclaim_cnt >= 2:
                        active = False
                        reclaim_cnt = 0
                        active_weeks = 0
                        episode_start_date = None

        states.append(
            WKWarnState(
                week_idx=w_idx,
                week_open_ts=int(w.week_open_ts),
                week_close_ts=int(w.week_close_ts),
                open_date_utc=str(w.open_date_utc),
                sma_value=sma_w,
                active=active,
                reclaim_cnt=reclaim_cnt,
                active_weeks=active_weeks,
                episode_start_date=episode_start_date,
            )
        )
    return states
