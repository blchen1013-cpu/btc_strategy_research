"""Phase 2: Track A (Capital_A) causal cycle tracking, expanding L1 / T_max calibration,
grid value-zone synthesis ([Zone_Low, Zone_High] & P_anchor), Dual-Key fallback, and halving diagnostics.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from scripts.btc_bottom.fetch_snapshots import (
    DAY_SEC,
    MacroDailyRow,
    MVRVRow,
    WeeklyOHLCVRow,
    build_macro_daily_series,
    date_str_to_open_ts,
)
from scripts.btc_bottom.indicators import Pivot, calc_ema_sma_seeded, calc_sma, compute_causal_pivots

__all__ = [
    "CompletedCycle",
    "DailyCycleState",
    "L1Evaluation",
    "HALVING_DATES",
    "build_macro_daily_series",
    "track_causal_cycle_peaks",
    "evaluate_l1",
    "synthesize_track_a_zone",
    "evaluate_dual_key",
    "evaluate_track_a_action",
    "compute_halving_diagnostics",
]

HALVING_DATES = ("2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20")


@dataclass(frozen=True)
class CompletedCycle:
    peak_date: str
    ath: float
    trough_date: str
    trough: float
    max_dd: float
    t_to_trough_days: int
    breakout_date: str
    breakout_idx: int


@dataclass(frozen=True)
class DailyCycleState:
    idx: int
    date_utc: str
    close_ts: int
    cycle_ath: float
    peak_date: str
    trough: float
    trough_date: str
    t0_date: str | None
    elapsed_days: int | None
    calibrated_dd_floor: float
    early_burn_in: bool
    t_max_days: int
    completed_cycles_count: int
    prev_completed_trough: float
    new_ath_breakout: bool


@dataclass(frozen=True)
class L1Evaluation:
    score: int
    sma200w_ok: bool
    mvrv_ok: bool
    dd_ok: bool
    sma200w: float | None
    cap_mvrv_cur: float | None
    realized_price: float | None
    ath_dd: float
    calibrated_dd_floor: float


def _calibrate_dd_and_tmax(completed_cycles: Sequence[CompletedCycle]) -> tuple[float, bool, int]:
    m = len(completed_cycles)
    dd = [c.max_dd for c in completed_cycles]
    d_days = [c.t_to_trough_days for c in completed_cycles]

    if m < 2:
        cal_dd = -0.80
        early_burn_in = True
    else:
        cal_dd = min(-0.55, max(-0.85, max(dd[-2:]) + 0.05))
        early_burn_in = False

    if m == 0:
        t_max = 365
    elif m == 1:
        t_max = max(365, int(d_days[0]))
    else:
        t_max = max(365, int(round(statistics.mean(d_days) + statistics.stdev(d_days))))

    return cal_dd, early_burn_in, t_max


def track_causal_cycle_peaks(
    macro_daily: Sequence[MacroDailyRow],
) -> tuple[list[DailyCycleState], list[list[CompletedCycle]]]:
    """Track Rolling_ATH (Cycle_ATH), Peak_Date, Trough, t_0, and CompletedCycle table causally bar-by-bar."""
    if not macro_daily:
        return [], []

    states: list[DailyCycleState] = []
    completed_history: list[list[CompletedCycle]] = []
    completed: list[CompletedCycle] = []

    first = macro_daily[0]
    rolling_ath = float(first.high)
    peak_date = str(first.date_utc)
    trough = float(first.low)
    trough_date = str(first.date_utc)
    t_0: str | None = None
    first_low = float(first.low)

    for idx, row in enumerate(macro_daily):
        h = float(row.high)
        l = float(row.low)
        c = float(row.close)
        cur_date = str(row.date_utc)
        cur_dt = dt.date.fromisoformat(cur_date[:10])
        new_breakout = False

        if idx == 0:
            pass
        elif h > rolling_ath:
            new_breakout = True
            if t_0 is not None:
                max_dd = (trough - rolling_ath) / rolling_ath
                peak_dt = dt.date.fromisoformat(peak_date[:10])
                trough_dt = dt.date.fromisoformat(trough_date[:10])
                t_to_trough = (trough_dt - peak_dt).days
                if max_dd <= -0.50 and t_to_trough >= 90:
                    completed.append(
                        CompletedCycle(
                            peak_date=peak_date,
                            ath=rolling_ath,
                            trough_date=trough_date,
                            trough=trough,
                            max_dd=max_dd,
                            t_to_trough_days=t_to_trough,
                            breakout_date=cur_date,
                            breakout_idx=idx,
                        )
                    )
            rolling_ath = h
            peak_date = cur_date
            trough = l
            trough_date = cur_date
            t_0 = None
        else:
            # High[t] <= Rolling_ATH (tie High[t] == Rolling_ATH keeps existing Peak_Date and t_0)
            if l < trough:
                trough = l
                trough_date = cur_date
            if t_0 is None:
                peak_dt = dt.date.fromisoformat(peak_date[:10])
                days_from_peak = (cur_dt - peak_dt).days
                close_dd = (c - rolling_ath) / rolling_ath
                if days_from_peak >= 60 and close_dd <= -0.25:
                    t_0 = peak_date

        cal_dd, early_burn_in, t_max = _calibrate_dd_and_tmax(completed)
        if t_0 is not None:
            t0_dt = dt.date.fromisoformat(t_0[:10])
            elapsed = (cur_dt - t0_dt).days
        else:
            elapsed = None

        prev_trough = completed[-1].trough if completed else first_low
        snap_completed = list(completed)
        completed_history.append(snap_completed)
        states.append(
            DailyCycleState(
                idx=idx,
                date_utc=cur_date,
                close_ts=int(row.close_ts),
                cycle_ath=rolling_ath,
                peak_date=peak_date,
                trough=trough,
                trough_date=trough_date,
                t0_date=t_0,
                elapsed_days=elapsed,
                calibrated_dd_floor=cal_dd,
                early_burn_in=early_burn_in,
                t_max_days=t_max,
                completed_cycles_count=len(snap_completed),
                prev_completed_trough=prev_trough,
                new_ath_breakout=new_breakout,
            )
        )

    return states, completed_history


def evaluate_l1(
    close_price: float,
    sma200w: float | None,
    cap_mvrv_cur: float | None,
    realized_price: float | None,
    cycle_ath: float,
    calibrated_dd_floor: float,
) -> L1Evaluation:
    sma200w_ok = bool(sma200w is not None and close_price <= 1.10 * sma200w)
    mvrv_ok = bool(cap_mvrv_cur is not None and cap_mvrv_cur <= 1.00)
    ath_dd = (close_price - cycle_ath) / cycle_ath if cycle_ath > 0.0 else 0.0
    dd_ok = bool(ath_dd <= calibrated_dd_floor)
    score = int(sma200w_ok) + int(mvrv_ok) + int(dd_ok)
    return L1Evaluation(
        score=score,
        sma200w_ok=sma200w_ok,
        mvrv_ok=mvrv_ok,
        dd_ok=dd_ok,
        sma200w=sma200w,
        cap_mvrv_cur=cap_mvrv_cur,
        realized_price=realized_price,
        ath_dd=ath_dd,
        calibrated_dd_floor=calibrated_dd_floor,
    )


def synthesize_track_a_zone(
    *,
    sma200w: float | None,
    realized_price: float | None,
    lgc_lower_band: float | None,
    prev_cycle_low: float,
    cycle_ath: float,
    calibrated_dd_floor: float,
    confirmed_weekly_low_prices: Sequence[float],
) -> tuple[float | None, float | None, float | None]:
    """Synthesize [Zone_Low, Zone_High] and P_anchor over a 0.5% geometric grid filtered by projected_L1(P) >= 2."""
    if cycle_ath <= 0.0 or prev_cycle_low <= 0.0:
        return None, None, None

    fib_low = (prev_cycle_low ** 0.786) * (cycle_ath ** 0.214)
    fib_high = (prev_cycle_low ** 0.618) * (cycle_ath ** 0.382)
    if fib_low > fib_high:
        fib_low, fib_high = fib_high, fib_low

    valid_bands: list[tuple[float, float, float]] = []
    if sma200w is not None and sma200w > 0.0:
        valid_bands.append((0.85 * sma200w, 1.10 * sma200w, 1.0))
    if realized_price is not None and realized_price > 0.0:
        valid_bands.append((0.80 * realized_price, 1.00 * realized_price, 1.0))
    if lgc_lower_band is not None and lgc_lower_band > 0.0:
        valid_bands.append((0.90 * lgc_lower_band, 1.10 * lgc_lower_band, 0.8))
    valid_bands.append((fib_low, fib_high, 0.8))

    dd_target_px = cycle_ath * (1.0 + calibrated_dd_floor)
    anchors = [
        x
        for x in (sma200w, realized_price, lgc_lower_band, fib_low, dd_target_px)
        if x is not None and x > 0.0
    ]
    if not anchors:
        return None, None, None

    p_min = max(0.01, 0.25 * min(anchors))
    upper_candidates = [b[1] for b in valid_bands] + [cycle_ath]
    p_max = 1.15 * max(upper_candidates)
    if p_max <= p_min:
        return None, None, None

    # Generate geometric grid with step factor 1.005 and filter to C = {P : projected_L1(P) >= 2}
    grid_c: list[tuple[float, int, float]] = []  # (P, hits, score)
    p = p_min
    step = 1.005
    while p <= p_max:
        c1 = 1 if (sma200w is not None and p <= 1.10 * sma200w) else 0
        c2 = 1 if (realized_price is not None and realized_price > 0.0 and (p / realized_price) <= 1.00) else 0
        c3 = 1 if ((p - cycle_ath) / cycle_ath <= calibrated_dd_floor) else 0
        if (c1 + c2 + c3) >= 2:
            hits = 0
            w_sum = 0.0
            for lo, hi, w in valid_bands:
                if lo <= p <= hi:
                    hits += 1
                    w_sum += w
            # Linear scan over trailing confirmed weekly (3,3) lows within ±3.5%
            near_wk_low = any(abs(p - wl) / wl <= 0.035 for wl in confirmed_weekly_low_prices if wl > 0.0)
            score = w_sum + (0.4 if near_wk_low else 0.0)
            grid_c.append((p, hits, round(score, 8)))
        p *= step

    if not grid_c:
        return None, None, None

    max_hits = max(item[1] for item in grid_c)
    if max_hits == 0:
        max_c = grid_c[-1][0]
        zone_low = 0.85 * max_c
        zone_high = max_c
    else:
        # Extract contiguous segments in grid_c where hits == max_hits
        segments: list[list[tuple[float, int, float]]] = []
        cur_seg: list[tuple[float, int, float]] = []
        for item in grid_c:
            if item[1] == max_hits:
                cur_seg.append(item)
            else:
                if cur_seg:
                    segments.append(cur_seg)
                    cur_seg = []
        if cur_seg:
            segments.append(cur_seg)

        best_seg = max(segments, key=lambda seg: (max(x[2] for x in seg), seg[-1][0]))
        zone_low = best_seg[0][0]
        zone_high = best_seg[-1][0]

    in_zone = [item for item in grid_c if zone_low - 1e-9 <= item[0] <= zone_high + 1e-9]
    if not in_zone:
        in_zone = [grid_c[-1]]

    max_score = max(item[2] for item in in_zone)
    runs: list[list[float]] = []
    cur_run: list[float] = []
    for px_val, _, sc_val in in_zone:
        if abs(sc_val - max_score) <= 1e-9:
            cur_run.append(px_val)
        else:
            if cur_run:
                runs.append(cur_run)
                cur_run = []
    if cur_run:
        runs.append(cur_run)

    best_run = max(runs, key=lambda r: (len(r), r[-1]))
    p_anchor = math.sqrt(best_run[0] * best_run[-1])
    return zone_low, zone_high, p_anchor


def evaluate_dual_key(
    *,
    t0_date: str | None,
    closed_weekly_rows: Sequence[WeeklyOHLCVRow],
    weekly_ema21_prefix: Sequence[float | None],
    confirmed_weekly_lows: Sequence[Pivot],
    daily_states_by_close_ts: dict[int, DailyCycleState],
    daily_l1_by_close_ts: dict[int, L1Evaluation],
) -> tuple[bool, float | None]:
    """Evaluate Dual-Key Fallback on the latest closed weekly bar w (week_close_ts <= daily.close_ts).

    Returns (dual_key_active, latest_post_t0_weekly_hl_price).
    """
    if t0_date is None or not closed_weekly_rows:
        return False, None

    t0_ts = date_str_to_open_ts(t0_date)
    w_idx = len(closed_weekly_rows) - 1
    w_bar = closed_weekly_rows[w_idx]

    # Post-t0 confirmed (3,3) weekly lows
    post_t0_lows = [
        p
        for p in confirmed_weekly_lows
        if p.confirm_idx <= w_idx and closed_weekly_rows[p.idx].week_open_ts > t0_ts
    ]
    latest_hl_px: float | None = None
    if len(post_t0_lows) >= 2 and post_t0_lows[-1].price > post_t0_lows[-2].price:
        p_last = post_t0_lows[-1]
        # Verify no weekly close < p_last.price from p_last.idx .. w_idx
        unbroken = all(float(closed_weekly_rows[u].close) >= p_last.price for u in range(p_last.idx, w_idx + 1))
        if unbroken:
            latest_hl_px = p_last.price

    if w_bar.week_open_ts <= t0_ts or latest_hl_px is None:
        return False, latest_hl_px

    ema21_w = weekly_ema21_prefix[w_idx]
    if ema21_w is None or float(w_bar.close) <= ema21_w:
        return False, latest_hl_px

    # Valuation Key at the daily bar corresponding to the end of week w (week_close_ts)
    w_end_close_ts = int(w_bar.week_close_ts)
    d_state = daily_states_by_close_ts.get(w_end_close_ts)
    d_l1 = daily_l1_by_close_ts.get(w_end_close_ts)
    if d_state is None or d_l1 is None:
        return False, latest_hl_px

    trough_dd_w = (d_state.trough - d_state.cycle_ath) / d_state.cycle_ath if d_state.cycle_ath > 0 else 0.0
    val_key_ok = (d_l1.score >= 1) or (trough_dd_w <= 0.75 * d_state.calibrated_dd_floor)
    return bool(val_key_ok), latest_hl_px


def evaluate_track_a_action(
    *,
    l1_score: int,
    zone_high: float | None,
    effective_spot: float,
    replay_mode: str,
    live_spot: float | None,
    t0_date: str | None,
    elapsed_days: int | None,
    t_max_days: int,
    dual_key_active: bool,
) -> str:
    """Evaluate uninvested Capital_A action enum across mutually exclusive rules A2-A7."""
    # A2: L1 >= 2/3 and (Zone_High is None or effective_spot <= Zone_High)
    if l1_score >= 2 and (zone_high is None or effective_spot <= zone_high):
        return "BUY_NOW_LEFT_ZONE"

    is_expired = bool(t0_date is not None and elapsed_days is not None and elapsed_days >= t_max_days)
    if not is_expired:
        # Pre-expiry (or t_0 not yet locked): A3, A4, A5
        if l1_score >= 2 and zone_high is not None and effective_spot > zone_high:
            return "BUY_LIMIT_PULLBACK"
        if (
            l1_score < 2
            and replay_mode == "LIVE"
            and live_spot is not None
            and zone_high is not None
            and effective_spot <= zone_high
        ):
            return "INTRADAY_ALERT_PENDING_CLOSE"
        return "WAIT_LEFT_TARGET"

    # Expired (Elapsed >= T_max) and not A2: A6 or A7
    if dual_key_active:
        return "BUY_NOW_DUAL_KEY_FALLBACK"
    return "WAIT_WEEKLY_RIGHT_SIDE"


def compute_halving_diagnostics(
    eff_date_str: str,
    completed_cycles: Sequence[CompletedCycle],
) -> dict[str, Any]:
    """Compute causal halving cycle diagnostics and historical_cycle_offsets as of eff_date_str."""
    eff_date = dt.date.fromisoformat(eff_date_str[:10])
    known = [d for d in HALVING_DATES if d <= eff_date_str[:10]]
    known_dates = [dt.date.fromisoformat(d) for d in known]

    if len(known) == 0:
        era_status = "PRE_HALVING_ERA"
        last_halving_date: str | None = None
        days_since: int | None = None
        next_est_date: str | None = HALVING_DATES[0]
        progress_pct: float | None = None
    elif len(known) == 1:
        era_status = "ERA_1"
        last_halving_date = known[-1]
        days_since = (eff_date - known_dates[-1]).days
        next_est_date = None
        progress_pct = None
    else:
        era_status = f"ERA_{len(known)}"
        last_halving_date = known[-1]
        days_since = (eff_date - known_dates[-1]).days
        intervals = [(known_dates[k] - known_dates[k - 1]).days for k in range(1, len(known_dates))]
        est_len = int(round(statistics.mean(intervals)))
        next_dt = known_dates[-1] + dt.timedelta(days=est_len)
        next_est_date = next_dt.isoformat()
        progress_pct = round((days_since / est_len) * 100.0, 2)

    offsets: list[dict[str, Any]] = []
    for c in completed_cycles:
        prior_halvings = [d for d in known if d <= c.peak_date[:10]]
        if not prior_halvings:
            offsets.append(
                {
                    "peak_date": c.peak_date,
                    "trough_date": c.trough_date,
                    "halving_date": None,
                    "days_from_halving_to_peak": None,
                    "days_from_halving_to_trough": None,
                }
            )
        else:
            h_str = prior_halvings[-1]
            h_dt = dt.date.fromisoformat(h_str)
            p_dt = dt.date.fromisoformat(c.peak_date[:10])
            tr_dt = dt.date.fromisoformat(c.trough_date[:10])
            offsets.append(
                {
                    "peak_date": c.peak_date,
                    "trough_date": c.trough_date,
                    "halving_date": h_str,
                    "days_from_halving_to_peak": (p_dt - h_dt).days,
                    "days_from_halving_to_trough": (tr_dt - h_dt).days,
                }
            )

    return {
        "era_status": era_status,
        "last_halving_date": last_halving_date,
        "days_since_halving": days_since,
        "next_est_date": next_est_date,
        "cycle_progress_pct": progress_pct,
        "historical_cycle_offsets": offsets,
    }
