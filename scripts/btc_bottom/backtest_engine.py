"""Phase 3: Dual-track zero-lookahead backtest engine (Track A & Track B).

Implements:
- run_track_a_backtest: simulates left_market, p_anchor_limit, and dual_key paths with
  strict 1-entry-per-cycle, fee-adjusted forward returns (+1y, +2y, +3y, Next Cycle Peak),
  and fee-unadjusted MAE over [entry_idx .. breakout_idx].
- run_track_b_backtest: simulates baseline 10% risk, standalone single-setup runs (SW-1/2/3),
  closed-trade partitions by WK-Warn (50W & 200W), single-position sequential 1R shadow ledger,
  causal WK-Warn gating (B1-B7b), and non-overlapping 3-loss streak throttling.
"""

from __future__ import annotations

import bisect
import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from scripts.btc_bottom.fetch_snapshots import DailyOHLCVRow, MacroDailyRow, MVRVRow, WeeklyOHLCVRow
from scripts.btc_bottom.indicators import (
    Pivot,
    WKWarnState,
    calc_ema_sma_seeded,
    calc_sma,
    calc_wilder_atr,
    compute_causal_pivots,
    compute_expanding_lgc,
    compute_wk_warn_series,
)
from scripts.btc_bottom.track_a import (
    CompletedCycle,
    DailyCycleState,
    L1Evaluation,
    evaluate_dual_key,
    evaluate_l1,
    evaluate_track_a_action,
    synthesize_track_a_zone,
    track_causal_cycle_peaks,
)
from scripts.btc_bottom.track_b import (
    FEE_ONEWAY,
    SwingSetupCandidate,
    compute_expanding_gap_buffers,
    compute_position_sizing,
    detect_causal_sw_setups,
    evaluate_track_b_action,
)


def track_a_net_return_pct(entry_px: float, exit_px: float, fee_oneway: float = FEE_ONEWAY) -> float:
    """Authoritative Track A net return formula deducting 0.10% one-way fee on both legs."""
    return (((exit_px * (1.0 - fee_oneway)) / (entry_px * (1.0 + fee_oneway))) - 1.0) * 100.0


def _compute_track_a_event_metrics(
    macro_daily: Sequence[MacroDailyRow],
    cycle_states: Sequence[DailyCycleState],
    completed_cycles_end: Sequence[CompletedCycle],
    entry_idx: int,
    entry_price: float,
    early_burn_in: bool,
    path_name: str,
) -> dict[str, Any]:
    n = len(macro_daily)
    entry_date = macro_daily[entry_idx].date_utc
    ath_at_entry = cycle_states[entry_idx].cycle_ath

    # Find breakout_idx: first bar strictly after entry_idx where High[u] > ath_at_entry
    breakout_idx: int | None = None
    for u in range(entry_idx + 1, n):
        if float(macro_daily[u].high) > ath_at_entry:
            breakout_idx = u
            break

    mae_end_idx = breakout_idx if breakout_idx is not None else (n - 1)
    min_low = min(float(macro_daily[u].low) for u in range(entry_idx, mae_end_idx + 1))
    mae_pct = ((min_low - entry_price) / entry_price) * 100.0

    def _fwd_ret(offset_days: int) -> float | None:
        target_idx = entry_idx + offset_days
        if target_idx >= n:
            return None
        return round(track_a_net_return_pct(entry_price, float(macro_daily[target_idx].close)), 4)

    ret_1y = _fwd_ret(365)
    ret_2y = _fwd_ret(730)
    ret_3y = _fwd_ret(1095)

    # Next Cycle Peak: after breakout_idx, if a subsequent completed cycle exists OR current uncompleted
    # downturn satisfies max_dd <= -0.50 and t_to_trough_days >= 90
    ret_next_peak: float | None = None
    if breakout_idx is not None:
        breakout_date = macro_daily[breakout_idx].date_utc
        subsequent_Peak: float | None = None
        for cc in completed_cycles_end:
            if cc.peak_date >= breakout_date:
                subsequent_Peak = cc.ath
                break
        if subsequent_Peak is None and n > 0:
            last_st = cycle_states[-1]
            if last_st.peak_date >= breakout_date:
                cur_dd = (last_st.trough - last_st.cycle_ath) / last_st.cycle_ath if last_st.cycle_ath > 0 else 0.0
                p_dt = dt.date.fromisoformat(last_st.peak_date[:10])
                tr_dt = dt.date.fromisoformat(last_st.trough_date[:10])
                if cur_dd <= -0.50 and (tr_dt - p_dt).days >= 90:
                    subsequent_Peak = last_st.cycle_ath
        if subsequent_Peak is not None:
            ret_next_peak = round(track_a_net_return_pct(entry_price, subsequent_Peak), 4)

    return {
        "path": path_name,
        "entry_idx": entry_idx,
        "entry_date": entry_date,
        "entry_price": round(entry_price, 4),
        "early_burn_in": bool(early_burn_in),
        "mae_pct": round(mae_pct, 4),
        "ret_1y_pct": ret_1y,
        "ret_2y_pct": ret_2y,
        "ret_3y_pct": ret_3y,
        "ret_next_cycle_peak_pct": ret_next_peak,
    }


def _summarize_track_a_events(
    primary_events: Sequence[dict[str, Any]],
    limit_events: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    def _avg(vals: list[float]) -> float | None:
        return round(statistics.mean(vals), 4) if vals else None

    maes = [float(e["mae_pct"]) for e in primary_events]
    r1 = [float(e["ret_1y_pct"]) for e in primary_events if e["ret_1y_pct"] is not None]
    r2 = [float(e["ret_2y_pct"]) for e in primary_events if e["ret_2y_pct"] is not None]
    r3 = [float(e["ret_3y_pct"]) for e in primary_events if e["ret_3y_pct"] is not None]
    r_pk = [float(e["ret_next_cycle_peak_pct"]) for e in primary_events if e["ret_next_cycle_peak_pct"] is not None]
    lim_maes = [float(e["mae_pct"]) for e in limit_events]

    return {
        "primary_n_events": len(primary_events),
        "primary_mean_mae_pct": _avg(maes),
        "primary_worst_mae_pct": round(min(maes), 4) if maes else None,
        "primary_mean_ret_1y_pct": _avg(r1),
        "primary_mean_ret_2y_pct": _avg(r2),
        "primary_mean_ret_3y_pct": _avg(r3),
        "primary_mean_ret_next_peak_pct": _avg(r_pk),
        "limit_n_events": len(limit_events),
        "limit_mean_mae_pct": _avg(lim_maes),
    }


def run_track_a_backtest(
    macro_daily: Sequence[MacroDailyRow],
    mvrv_rows: Sequence[MVRVRow],
    weekly_rows: Sequence[WeeklyOHLCVRow],
    *,
    replay_mode: str = "HISTORICAL_REPLAY",
    live_spot: float | None = None,
) -> dict[str, Any]:
    """Execute Track A causal backtest across 2010-2026 and evaluate current Capital_A state."""
    n = len(macro_daily)
    if n == 0:
        raise ValueError("macro_daily cannot be empty")

    cycle_states, completed_hist = track_causal_cycle_peaks(macro_daily)
    lgc_series = compute_expanding_lgc(macro_daily)

    weekly_closes = [float(w.close) for w in weekly_rows]
    sma200w_series = calc_sma(weekly_closes, 200)
    ema21w_series = calc_ema_sma_seeded(weekly_closes, 21)
    weekly_close_ts_list = [int(w.week_close_ts) for w in weekly_rows]
    all_weekly_pivots = compute_causal_pivots(weekly_rows, 3, 3)
    weekly_low_pivots = [p for p in all_weekly_pivots if p.kind == "LOW"]

    mvrv_eff_ts_list = [int(m.effective_ts) for m in mvrv_rows]

    daily_states_by_close_ts: dict[int, DailyCycleState] = {}
    daily_l1_by_close_ts: dict[int, L1Evaluation] = {}
    l1_series: list[L1Evaluation] = []
    aligned_w_idx_series: list[int] = []

    for t, row in enumerate(macro_daily):
        c_ts = int(row.close_ts)
        st = cycle_states[t]
        daily_states_by_close_ts[c_ts] = st

        # Latest closed weekly bar with week_close_ts <= bar.close_ts
        w_idx = bisect.bisect_right(weekly_close_ts_list, c_ts) - 1
        aligned_w_idx_series.append(w_idx)
        sma200w_t = sma200w_series[w_idx] if w_idx >= 0 else None

        # Latest effective MVRV row with effective_ts <= bar.close_ts
        m_idx = bisect.bisect_right(mvrv_eff_ts_list, c_ts) - 1
        if m_idx >= 0:
            mvrv_t = float(mvrv_rows[m_idx].cap_mvrv_cur)
            rp_t = float(mvrv_rows[m_idx].realized_price)
        else:
            mvrv_t = None
            rp_t = None

        l1_ev = evaluate_l1(
            close_price=float(row.close),
            sma200w=sma200w_t,
            cap_mvrv_cur=mvrv_t,
            realized_price=rp_t,
            cycle_ath=st.cycle_ath,
            calibrated_dd_floor=st.calibrated_dd_floor,
        )
        l1_series.append(l1_ev)
        daily_l1_by_close_ts[c_ts] = l1_ev

    # Track per-cycle deployment across the 3 paths
    deployed_primary = False
    deployed_limit = False
    current_cycle_entry_info: dict[str, Any] | None = None

    left_market_raw: list[tuple[int, float, bool]] = []
    p_anchor_limit_raw: list[tuple[int, float, bool]] = []
    dual_key_raw: list[tuple[int, float, bool]] = []

    last_zone_low: float | None = None
    last_zone_high: float | None = None
    last_p_anchor: float | None = None
    last_dual_key_active: bool = False
    last_dual_key_hl: float | None = None

    for t in range(n):
        st = cycle_states[t]
        l1_ev = l1_series[t]
        w_idx = aligned_w_idx_series[t]

        if st.new_ath_breakout:
            deployed_primary = False
            deployed_limit = False
            current_cycle_entry_info = None

        need_zone = (l1_ev.score >= 2) or (t == n - 1)
        if need_zone:
            conf_wk_lows_px = [p.price for p in weekly_low_pivots if p.confirm_idx <= w_idx] if w_idx >= 0 else []
            z_lo, z_hi, p_anc = synthesize_track_a_zone(
                sma200w=l1_ev.sma200w,
                realized_price=l1_ev.realized_price,
                lgc_lower_band=lgc_series[t],
                prev_cycle_low=st.prev_completed_trough,
                cycle_ath=st.cycle_ath,
                calibrated_dd_floor=st.calibrated_dd_floor,
                confirmed_weekly_low_prices=conf_wk_lows_px,
            )
        else:
            z_lo, z_hi, p_anc = None, None, None

        is_expired = bool(st.t0_date is not None and st.elapsed_days is not None and st.elapsed_days >= st.t_max_days)
        if (is_expired or t == n - 1) and w_idx >= 0:
            dk_active, dk_hl = evaluate_dual_key(
                t0_date=st.t0_date,
                closed_weekly_rows=weekly_rows[: w_idx + 1],
                weekly_ema21_prefix=ema21w_series[: w_idx + 1],
                confirmed_weekly_lows=weekly_low_pivots,
                daily_states_by_close_ts=daily_states_by_close_ts,
                daily_l1_by_close_ts=daily_l1_by_close_ts,
            )
        else:
            dk_active, dk_hl = False, None

        if t == n - 1:
            last_zone_low, last_zone_high, last_p_anchor = z_lo, z_hi, p_anc
            last_dual_key_active = dk_active
            last_dual_key_hl = dk_hl
            # Final bar t == n - 1 cannot fill at Open[t+1]
            break

        close_t = float(macro_daily[t].close)
        open_next = float(macro_daily[t + 1].open)
        low_next = float(macro_daily[t + 1].low)

        # 1. Primary path: left_market OR dual_key fallback
        if not deployed_primary:
            if l1_ev.score >= 2 and (z_hi is None or close_t <= z_hi):
                deployed_primary = True
                left_market_raw.append((t + 1, open_next, st.early_burn_in))
                current_cycle_entry_info = {
                    "path": "left_market",
                    "date": macro_daily[t + 1].date_utc,
                    "price": round(open_next, 2),
                }
            elif is_expired and dk_active:
                deployed_primary = True
                dual_key_raw.append((t + 1, open_next, st.early_burn_in))
                current_cycle_entry_info = {
                    "path": "dual_key",
                    "date": macro_daily[t + 1].date_utc,
                    "price": round(open_next, 2),
                }

        # 2. Limit path: p_anchor_limit when L1 >= 2/3 and p_anc is not None
        if not deployed_limit and l1_ev.score >= 2 and p_anc is not None:
            if low_next <= p_anc:
                fill_px = min(open_next, p_anc)
                deployed_limit = True
                p_anchor_limit_raw.append((t + 1, fill_px, st.early_burn_in))

    completed_end = completed_hist[-1] if completed_hist else []

    def _clean_event(ev: dict[str, Any]) -> dict[str, Any]:
        return {
            "entry_date": ev["entry_date"],
            "entry_price": ev["entry_price"],
            "early_burn_in": ev["early_burn_in"],
            "mae_pct": ev["mae_pct"],
            "ret_1y_pct": ev["ret_1y_pct"],
            "ret_2y_pct": ev["ret_2y_pct"],
            "ret_3y_pct": ev["ret_3y_pct"],
            "ret_next_cycle_peak_pct": ev["ret_next_cycle_peak_pct"],
        }

    left_events_full = [
        _compute_track_a_event_metrics(macro_daily, cycle_states, completed_end, idx, px, ebi, "left_market")
        for idx, px, ebi in left_market_raw
    ]
    limit_events_full = [
        _compute_track_a_event_metrics(macro_daily, cycle_states, completed_end, idx, px, ebi, "p_anchor_limit")
        for idx, px, ebi in p_anchor_limit_raw
    ]
    dk_events_full = [
        _compute_track_a_event_metrics(macro_daily, cycle_states, completed_end, idx, px, ebi, "dual_key")
        for idx, px, ebi in dual_key_raw
    ]

    primary_all = sorted(left_events_full + dk_events_full, key=lambda x: x["entry_idx"])
    primary_oos = [e for e in primary_all if not e["early_burn_in"]]
    limit_oos = [e for e in limit_events_full if not e["early_burn_in"]]

    summary_all = _summarize_track_a_events(primary_all, limit_events_full)
    summary_oos = _summarize_track_a_events(primary_oos, limit_oos)

    last_st = cycle_states[-1]
    last_l1 = l1_series[-1]
    last_close = float(macro_daily[-1].close)
    eff_spot = live_spot if (replay_mode == "LIVE" and live_spot is not None) else last_close

    action_enum = evaluate_track_a_action(
        l1_score=last_l1.score,
        zone_high=last_zone_high,
        effective_spot=eff_spot,
        replay_mode=replay_mode,
        live_spot=live_spot,
        t0_date=last_st.t0_date,
        elapsed_days=last_st.elapsed_days,
        t_max_days=last_st.t_max_days,
        dual_key_active=last_dual_key_active,
    )

    return {
        "track_a_summary": {
            "action": action_enum,
            "backtest_cycle_deployed": bool(deployed_primary),
            "backtest_cycle_entry": current_cycle_entry_info,
            "l1_score": last_l1.score,
            "l1_flags": {
                "sma200w_ok": last_l1.sma200w_ok,
                "mvrv_ok": last_l1.mvrv_ok,
                "dd_ok": last_l1.dd_ok,
            },
            "realized_price": round(float(last_l1.realized_price), 2) if last_l1.realized_price is not None else 0.0,
            "zone_low": round(last_zone_low, 2) if last_zone_low is not None else None,
            "zone_high": round(last_zone_high, 2) if last_zone_high is not None else None,
            "p_anchor": round(last_p_anchor, 2) if last_p_anchor is not None else None,
            "t0_date": last_st.t0_date,
            "t_max_days": last_st.t_max_days,
            "elapsed_days": last_st.elapsed_days,
            "calibrated_dd_floor_pct": round(last_st.calibrated_dd_floor * 100.0, 2),
            "dual_key_active": bool(last_dual_key_active),
            "backtest_stats": {
                "left_market_events": [_clean_event(e) for e in left_events_full],
                "p_anchor_limit_events": [_clean_event(e) for e in limit_events_full],
                "dual_key_events": [_clean_event(e) for e in dk_events_full],
                "summary_all": summary_all,
                "summary_oos": summary_oos,
            },
        },
        "cycle_states": cycle_states,
        "completed_cycles": completed_end,
        "lgc_series": lgc_series,
        "sma200w_series": sma200w_series,
        "last_l1": last_l1,
        "last_dual_key_hl": round(last_dual_key_hl, 2) if last_dual_key_hl is not None else None,
    }


@dataclass
class _ClosedTradeRecord:
    setup_id: str
    subtype: str
    signal_idx: int
    entry_idx: int
    exit_idx: int
    signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    structural_stop: float
    final_active_stop: float
    circuit_breaker_price: float
    exit_price: float
    exit_reason: str
    exit_trigger_detail: str
    risk_budget_eff: float
    position_size_pct: float
    equity_at_entry: float
    pnl_usd: float
    trade_r: float
    wk_warn_50w_at_signal: bool
    wk_warn_200w_at_signal: bool


def _compute_continuous_stats(
    closed_trades: Sequence[_ClosedTradeRecord],
    daily_equities: Sequence[float],
    initial_equity: float,
    open_trades_count: int,
    closed_only_final_equity: float,
) -> dict[str, Any]:
    n_closed = len(closed_trades)
    if n_closed == 0:
        win_rate_pct = 0.0
        payoff_ratio: float | None = None
        ev_r = 0.0
        single_worst_pct = 0.0
    else:
        wins = [t for t in closed_trades if t.pnl_usd > 0.0]
        losses = [t for t in closed_trades if t.pnl_usd <= 0.0]
        win_rate_pct = 100.0 * len(wins) / n_closed
        ev_r = statistics.mean(t.trade_r for t in closed_trades)
        win_rs = [t.trade_r for t in closed_trades if t.trade_r > 0.0]
        loss_rs = [t.trade_r for t in closed_trades if t.trade_r < 0.0]
        if win_rs and loss_rs:
            mean_loss = abs(statistics.mean(loss_rs))
            payoff_ratio = round(statistics.mean(win_rs) / mean_loss, 4) if mean_loss > 0.0 else None
        else:
            payoff_ratio = None
        single_worst_pct = min((t.pnl_usd / t.equity_at_entry) * 100.0 for t in closed_trades)

    # MTM equity curve stats (daily cash + qty * Close[d])
    peak_eq = initial_equity
    max_dd_pct = 0.0
    for eq in daily_equities:
        if eq > peak_eq:
            peak_eq = eq
        dd = ((peak_eq - eq) / peak_eq) * 100.0 if peak_eq > 0.0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    mtm_final = daily_equities[-1] if daily_equities else initial_equity
    mtm_ret_pct = ((mtm_final - initial_equity) / initial_equity) * 100.0
    closed_ret_pct = ((closed_only_final_equity - initial_equity) / initial_equity) * 100.0

    return {
        "closed_stats": {
            "n_closed": n_closed,
            "win_rate_pct": round(win_rate_pct, 4),
            "payoff_ratio": payoff_ratio,
            "ev_r": round(ev_r, 4),
            "total_return_pct": round(closed_ret_pct, 4),
            "track_max_dd_pct": round(max_dd_pct, 4),
            "single_trade_worst_pct": round(single_worst_pct, 4),
        },
        "mtm_stats": {
            "mtm_total_return_pct": round(mtm_ret_pct, 4),
            "mtm_max_dd_pct": round(max_dd_pct, 4),
            "open_trades_count": int(open_trades_count),
        },
    }


def _compute_partition_stats(trades: Sequence[_ClosedTradeRecord]) -> dict[str, Any]:
    n_closed = len(trades)
    if n_closed == 0:
        return {
            "closed_stats": {
                "n_closed": 0,
                "win_rate_pct": 0.0,
                "payoff_ratio": None,
                "ev_r": 0.0,
                "trade_compounded_return_pct": 0.0,
                "trade_curve_max_dd_pct": 0.0,
                "single_trade_worst_pct": 0.0,
            }
        }

    wins = [t for t in trades if t.pnl_usd > 0.0]
    win_rate_pct = 100.0 * len(wins) / n_closed
    ev_r = statistics.mean(t.trade_r for t in trades)
    win_rs = [t.trade_r for t in trades if t.trade_r > 0.0]
    loss_rs = [t.trade_r for t in trades if t.trade_r < 0.0]
    if win_rs and loss_rs:
        mean_loss = abs(statistics.mean(loss_rs))
        payoff_ratio: float | None = round(statistics.mean(win_rs) / mean_loss, 4) if mean_loss > 0.0 else None
    else:
        payoff_ratio = None

    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    worst_pct = 0.0
    for t in trades:
        ret_frac = t.pnl_usd / t.equity_at_entry
        pct = ret_frac * 100.0
        if pct < worst_pct:
            worst_pct = pct
        eq *= 1.0 + ret_frac
        if eq > peak:
            peak = eq
        dd = ((peak - eq) / peak) * 100.0 if peak > 0.0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return {
        "closed_stats": {
            "n_closed": n_closed,
            "win_rate_pct": round(win_rate_pct, 4),
            "payoff_ratio": payoff_ratio,
            "ev_r": round(ev_r, 4),
            "trade_compounded_return_pct": round((eq - 1.0) * 100.0, 4),
            "trade_curve_max_dd_pct": round(max_dd, 4),
            "single_trade_worst_pct": round(worst_pct, 4),
        }
    }


def _simulate_track_b_variant(
    daily_bars: Sequence[DailyOHLCVRow],
    *,
    ema50: Sequence[float | None],
    atr14: Sequence[float | None],
    vol_sma20: Sequence[float | None],
    gap_buffers: Sequence[float],
    all_daily_pivots: Sequence[Pivot],
    aligned_wk50_active: Sequence[bool],
    aligned_wk200_active: Sequence[bool],
    mode: str,  # "OVERALL" | "SETUP_ONLY" | "SHADOW_1R" | "WK_GATED" | "STREAK_THROTTLED"
    only_setup_id: str | None = None,
    shadow_trades: Sequence[_ClosedTradeRecord] | None = None,
) -> tuple[dict[str, Any], list[_ClosedTradeRecord], dict[str, Any]]:
    """Simulate a single Track B portfolio variant bar-by-bar from 2014-09-17 onward."""
    n = len(daily_bars)
    initial_equity = 10000.0
    cash = initial_equity
    closed_only_equity = initial_equity

    low_pivots = [p for p in all_daily_pivots if p.kind == "LOW"]
    high_pivots = [p for p in all_daily_pivots if p.kind == "HIGH"]
    low_pivots_by_confirm: dict[int, list[Pivot]] = {}
    for p in low_pivots:
        low_pivots_by_confirm.setdefault(p.confirm_idx, []).append(p)

    consumed_sw1_pivots: set[int] = set()
    closed_trades: list[_ClosedTradeRecord] = []
    daily_equities: list[float] = []

    # Active position state
    in_pos = False
    pos_setup_id = ""
    pos_subtype = ""
    pos_signal_idx = -1
    pos_entry_idx = -1
    pos_entry_price = 0.0
    pos_qty = 0.0
    pos_notional_in = 0.0
    pos_buy_fee = 0.0
    pos_1r_usd = 0.0
    pos_equity_at_entry = initial_equity
    pos_risk_budget_eff = 0.10
    pos_size_pct = 0.0
    pos_structural_stop = 0.0
    pos_active_stop = 0.0
    pos_cb_price = 0.0
    pos_gap_buf = 0.0
    pos_anchor_low = 0.0
    pos_has_reclaimed_ema50 = False
    pos_pending_exit_next_open = False
    pos_pending_exit_detail = ""
    pos_wk50_at_signal = False
    pos_wk200_at_signal = False

    # Pending entry scheduled at Close[i] to fill at Open[i+1]
    pending_entry_cand: SwingSetupCandidate | None = None
    pending_entry_budget: float = 0.10
    pending_wk50_sig: bool = False
    pending_wk200_sig: bool = False

    # Streak throttling state (non-overlapping stream of closed trade_r)
    streak_r_stream: list[float] = []
    streak_budget = 0.10
    shadow_by_exit_idx: dict[int, list[_ClosedTradeRecord]] = {}
    if shadow_trades:
        for st in shadow_trades:
            shadow_by_exit_idx.setdefault(st.exit_idx, []).append(st)

    last_bar_eval: dict[str, Any] = {}

    def _close_position(exit_idx: int, exit_px: float, reason: str, detail: str = "") -> _ClosedTradeRecord:
        nonlocal cash, closed_only_equity, in_pos, pos_pending_exit_next_open, pos_pending_exit_detail, streak_budget
        sell_val = pos_qty * exit_px
        sell_fee = sell_val * FEE_ONEWAY
        pnl_usd = (sell_val - sell_fee) - (pos_notional_in + pos_buy_fee)
        trade_r = pnl_usd / pos_1r_usd if pos_1r_usd > 0.0 else 0.0

        if mode == "SHADOW_1R":
            cash = initial_equity
            closed_only_equity = initial_equity
        else:
            cash += sell_val - sell_fee
            closed_only_equity = cash

        rec = _ClosedTradeRecord(
            setup_id=pos_setup_id,
            subtype=pos_subtype,
            signal_idx=pos_signal_idx,
            entry_idx=pos_entry_idx,
            exit_idx=exit_idx,
            signal_date=daily_bars[pos_signal_idx].date_utc,
            entry_date=daily_bars[pos_entry_idx].date_utc,
            exit_date=daily_bars[exit_idx].date_utc,
            entry_price=pos_entry_price,
            structural_stop=pos_structural_stop,
            final_active_stop=pos_active_stop,
            circuit_breaker_price=pos_cb_price,
            exit_price=exit_px,
            exit_reason=reason,
            exit_trigger_detail=detail or reason,
            risk_budget_eff=pos_risk_budget_eff,
            position_size_pct=pos_size_pct,
            equity_at_entry=pos_equity_at_entry,
            pnl_usd=pnl_usd,
            trade_r=trade_r,
            wk_warn_50w_at_signal=pos_wk50_at_signal,
            wk_warn_200w_at_signal=pos_wk200_at_signal,
        )
        closed_trades.append(rec)
        in_pos = False
        pos_pending_exit_next_open = False
        pos_pending_exit_detail = ""

        if mode == "STREAK_THROTTLED":
            streak_r_stream.append(trade_r)
            if len(streak_r_stream) >= 3 and all(r < 0.0 for r in streak_r_stream[-3:]):
                streak_budget = 0.05
            elif trade_r >= 1.5:
                streak_budget = 0.10

        return rec

    for d in range(n):
        bar_d = daily_bars[d]
        open_d = float(bar_d.open)
        low_d = float(bar_d.low)
        close_d = float(bar_d.close)
        same_day_exit_cooldown = False

        # If we had a pending entry from Close[d-1], fill at Open[d]
        if pending_entry_cand is not None and not in_pos:
            cand = pending_entry_cand
            bud = pending_entry_budget
            pending_entry_cand = None
            atr_prev = float(atr14[cand.signal_idx])  # guaranteed non-None
            gap_prev = gap_buffers[cand.signal_idx]
            sizing = compute_position_sizing(
                entry_price=open_d,
                structural_stop=cand.structural_stop,
                atr14_i=atr_prev,
                gap_buffer_pct_i=gap_prev,
                risk_budget_eff=bud,
            )
            if sizing.valid and sizing.position_size_pct is not None and sizing.circuit_breaker_price is not None:
                eq_entry = initial_equity if mode == "SHADOW_1R" else cash
                notional_in = eq_entry * float(sizing.position_size_pct)
                buy_fee = notional_in * FEE_ONEWAY
                if mode != "SHADOW_1R":
                    cash -= notional_in + buy_fee
                pos_setup_id = cand.setup_id
                pos_subtype = cand.subtype
                pos_signal_idx = cand.signal_idx
                pos_entry_idx = d
                pos_entry_price = open_d
                pos_qty = notional_in / open_d
                pos_notional_in = notional_in
                pos_buy_fee = buy_fee
                pos_1r_usd = notional_in * float(sizing.stop_dist_pct)
                pos_equity_at_entry = eq_entry
                pos_risk_budget_eff = bud
                pos_size_pct = float(sizing.position_size_pct)
                pos_structural_stop = cand.structural_stop
                pos_active_stop = cand.structural_stop
                pos_cb_price = float(sizing.circuit_breaker_price)
                pos_gap_buf = gap_prev
                pos_anchor_low = cand.entry_anchor_low
                pos_has_reclaimed_ema50 = False
                pos_pending_exit_next_open = False
                pos_pending_exit_detail = ""
                pos_wk50_at_signal = pending_wk50_sig
                pos_wk200_at_signal = pending_wk200_sig
                in_pos = True
                if cand.setup_id == "SW-1" and cand.consumed_pivot_idx is not None:
                    consumed_sw1_pivots.add(cand.consumed_pivot_idx)

        # Daily execution sequence for open position (since d >= pos_entry_idx)
        if in_pos:
            # Step 1: Pending exit from yesterday's close fills at Open[d]
            if pos_pending_exit_next_open:
                _close_position(d, open_d, "SCHEDULED_NEXT_OPEN_EXIT", pos_pending_exit_detail)
                same_day_exit_cooldown = True

            # Step 2: Intraday circuit breaker check
            if in_pos:
                if open_d <= pos_cb_price:
                    _close_position(d, open_d, "CIRCUIT_BREAKER_OPEN_GAP", "CIRCUIT_BREAKER_OPEN_GAP")
                    same_day_exit_cooldown = True
                elif low_d <= pos_cb_price:
                    _close_position(d, pos_cb_price, "CIRCUIT_BREAKER_INTRADAY", "CIRCUIT_BREAKER_INTRADAY")
                    same_day_exit_cooldown = True

            # Step 3: Closing structural / EMA50 trend breakdown check (reads pre-update active_stop)
            if in_pos:
                bars_held = d - pos_signal_idx
                ema_break = False
                if (
                    d >= pos_signal_idx + 2
                    and (pos_has_reclaimed_ema50 or bars_held >= 10)
                    and ema50[d] is not None
                    and ema50[d - 1] is not None
                ):
                    if close_d < float(ema50[d]) and float(daily_bars[d - 1].close) < float(ema50[d - 1]):
                        ema_break = True
                stop_break = close_d < pos_active_stop
                if stop_break or ema_break:
                    pos_pending_exit_next_open = True
                    if stop_break and ema_break:
                        pos_pending_exit_detail = "STRUCTURAL_AND_EMA50_CLOSE_BREAK"
                    elif stop_break:
                        pos_pending_exit_detail = (
                            "TRAILING_OR_BE_STOP_CLOSE_BREAK"
                            if pos_active_stop > pos_structural_stop + 1e-6
                            else "INITIAL_STRUCTURAL_STOP_CLOSE_BREAK"
                        )
                    else:
                        pos_pending_exit_detail = "EMA50_TWO_DAY_CLOSE_BREAK"

            # Step 4: If still holding and no pending exit scheduled, update reclaim flag, +1.5R BE, and HL trail
            if in_pos and not pos_pending_exit_next_open:
                if ema50[d] is not None and close_d > float(ema50[d]):
                    pos_has_reclaimed_ema50 = True
                net_unreal_usd = (pos_qty * close_d * (1.0 - FEE_ONEWAY)) - (pos_notional_in + pos_buy_fee)
                if pos_1r_usd > 0.0 and (net_unreal_usd / pos_1r_usd) >= 1.5:
                    pos_active_stop = max(pos_active_stop, pos_entry_price * (1.0 + 2.0 * FEE_ONEWAY))
                if atr14[d] is not None:
                    atr_d = float(atr14[d])
                    for p_low in low_pivots_by_confirm.get(d, []):
                        if p_low.idx > pos_signal_idx + 1 and p_low.price > pos_anchor_low:
                            pos_active_stop = max(pos_active_stop, p_low.price - 0.25 * atr_d)

        # In STREAK_THROTTLED mode, if flat all day (no actual trade open or closed today),
        # check if a shadow trade closed on day d while we were blocked at 0.0 budget
        if mode == "STREAK_THROTTLED" and not in_pos and not same_day_exit_cooldown:
            for sh_tr in shadow_by_exit_idx.get(d, []):
                # Only ingest shadow trade if no actual trade overlapped [sh_tr.entry_idx .. sh_tr.exit_idx]
                overlapped = any(
                    not (ct.exit_idx < sh_tr.entry_idx or ct.entry_idx > sh_tr.exit_idx) for ct in closed_trades
                )
                if not overlapped:
                    streak_r_stream.append(sh_tr.trade_r)
                    if len(streak_r_stream) >= 3 and all(r < 0.0 for r in streak_r_stream[-3:]):
                        streak_budget = 0.05
                    elif sh_tr.trade_r >= 1.5:
                        streak_budget = 0.10

        # End of day d: compute MTM equity
        if mode == "SHADOW_1R":
            daily_equities.append(initial_equity)
        else:
            cur_eq = cash + (pos_qty * close_d if in_pos else 0.0)
            daily_equities.append(cur_eq)

        # Signal detection at Close[d]
        conf_lows_d = [p for p in low_pivots if p.confirm_idx <= d]
        conf_highs_d = [p for p in high_pivots if p.confirm_idx <= d]
        primary_cand, active_ids = detect_causal_sw_setups(
            daily_bars,
            d,
            ema50=ema50,
            atr14=atr14,
            vol_sma20=vol_sma20,
            confirmed_lows=conf_lows_d,
            confirmed_highs=conf_highs_d,
            consumed_sw1_pivots=consumed_sw1_pivots,
        )
        if only_setup_id is not None:
            # Filter specifically for single-setup simulation
            atr_d_val = float(atr14[d]) if atr14[d] is not None else None
            vsma_d_val = float(vol_sma20[d]) if vol_sma20[d] is not None else None
            if d < 49 or ema50[d - 1] is None or ema50[d] is None or atr_d_val is None or vsma_d_val is None:
                primary_cand = None
                active_ids = []
            else:
                from scripts.btc_bottom.track_b import _detect_sw1, _detect_sw2, _detect_sw3

                if only_setup_id == "SW-1":
                    primary_cand = _detect_sw1(daily_bars, d, ema50, atr_d_val, conf_lows_d, consumed_sw1_pivots)
                elif only_setup_id == "SW-2":
                    primary_cand = _detect_sw2(daily_bars, d, atr_d_val, vsma_d_val, conf_lows_d, conf_highs_d)
                elif only_setup_id == "SW-3":
                    primary_cand = _detect_sw3(daily_bars, d, atr_d_val, vol_sma20, conf_lows_d, conf_highs_d)
                active_ids = [only_setup_id] if primary_cand is not None else []

        # Determine causal shadow stats (exit_idx < d) for WK_GATED / STREAK_THROTTLED
        if shadow_trades is not None and mode in ("WK_GATED", "STREAK_THROTTLED"):
            past_sh = [st for st in shadow_trades if st.exit_idx < d]
            act_sh = [st.trade_r for st in past_sh if st.wk_warn_50w_at_signal]
            inact_sh = [st.trade_r for st in past_sh if not st.wk_warn_50w_at_signal]
            n_active = len(act_sh)
            ev_r_active = statistics.mean(act_sh) if act_sh else 0.0
            ev_r_inactive = statistics.mean(inact_sh) if inact_sh else 0.0
            wk50_flag = aligned_wk50_active[d]
        else:
            # Baseline fixed 10% risk modes (OVERALL, SETUP_ONLY, SHADOW_1R)
            n_active = 0
            ev_r_active = 1.0
            ev_r_inactive = 1.0
            wk50_flag = False

        open_info = (
            {
                "risk_budget_eff": pos_risk_budget_eff,
                "position_size_pct": round(pos_size_pct * 100.0, 4),
                "entry_price": round(pos_entry_price, 2),
                "structural_stop": round(pos_structural_stop, 2),
                "active_stop": round(pos_active_stop, 2),
                "circuit_breaker_price": round(pos_cb_price, 2),
                "gap_buffer_pct": round(pos_gap_buf * 100.0, 4),
            }
            if in_pos
            else None
        )

        eval_res = evaluate_track_b_action(
            in_position=in_pos,
            pending_exit_next_open=pos_pending_exit_next_open,
            wk_warn_50w_active=wk50_flag,
            n_active=n_active,
            ev_r_active=ev_r_active,
            ev_r_inactive=ev_r_inactive,
            primary_setup=primary_cand,
            same_day_exit_cooldown=same_day_exit_cooldown,
            entry_ref_price=close_d,
            atr14_i=float(atr14[d]) if atr14[d] is not None else None,
            gap_buffer_pct_i=gap_buffers[d],
            open_position_info=open_info,
        )

        if d == n - 1:
            last_bar_eval = {
                "eval": eval_res,
                "active_setups_on_bar": active_ids,
                "primary_setup": primary_cand,
                "same_day_exit_cooldown": same_day_exit_cooldown,
                "n_active": n_active,
                "ev_r_active": ev_r_active,
                "ev_r_inactive": ev_r_inactive,
                "open_info": open_info,
                "in_pos": in_pos,
                "pending_exit_next_open": pos_pending_exit_next_open,
            }

        # Schedule next open entry if flat, signal valid, and d < n - 1
        if not in_pos and d < n - 1 and eval_res["action"] in ("BUY_NEXT_OPEN", "BUY_REDUCED_RISK"):
            wk_bud = float(eval_res["risk_budget_eff"] or 0.10)
            eff_bud = min(wk_bud, streak_budget) if mode == "STREAK_THROTTLED" else wk_bud
            if eff_bud > 0.0 and primary_cand is not None:
                pending_entry_cand = primary_cand
                pending_entry_budget = eff_bud
                pending_wk50_sig = aligned_wk50_active[d]
                pending_wk200_sig = aligned_wk200_active[d]

    open_cnt = 1 if in_pos else 0
    stats_bundle = _compute_continuous_stats(
        closed_trades=closed_trades,
        daily_equities=daily_equities,
        initial_equity=initial_equity,
        open_trades_count=open_cnt,
        closed_only_final_equity=closed_only_equity,
    )
    return stats_bundle, closed_trades, last_bar_eval


def run_track_b_backtest(
    daily_bars: Sequence[DailyOHLCVRow],
    weekly_rows: Sequence[WeeklyOHLCVRow],
    *,
    replay_mode: str = "HISTORICAL_REPLAY",
    live_spot: float | None = None,
) -> dict[str, Any]:
    """Execute full Track B suite on 2014-09-17+ daily_ohlcv and evaluate current Capital_B action."""
    if not daily_bars:
        empty_cont = _compute_continuous_stats([], [10000.0], 10000.0, 0, 10000.0)
        empty_part = _compute_partition_stats([])
        return {
            "track_b_summary": {
                "action": "WAIT_SIGNAL",
                "wk_warn_50w_active": False,
                "wk_warn_200w_active": False,
                "active_setups_on_bar": [],
                "rejected_reason": None,
                "risk_budget_eff": 0.10,
                "position_size_pct": None,
                "entry_ref_price": None,
                "structural_stop": None,
                "active_stop": None,
                "circuit_breaker_price": None,
                "gap_buffer_pct": None,
                "backtest_overall": empty_cont,
                "backtest_by_setup": {"SW-1": empty_cont, "SW-2": empty_cont, "SW-3": empty_cont},
                "backtest_by_wk_warn_50w": {"ACTIVE": empty_part, "INACTIVE": empty_part},
                "backtest_by_wk_warn_200w": {"ACTIVE": empty_part, "INACTIVE": empty_part},
                "backtest_wk_gated_causal": empty_cont,
                "backtest_streak_throttled": empty_cont,
            }
        }

    closes = [float(b.close) for b in daily_bars]
    vols = [float(b.volume) for b in daily_bars]
    ema50 = calc_ema_sma_seeded(closes, 50)
    atr14 = calc_wilder_atr(daily_bars, 14)
    vol_sma20 = calc_sma(vols, 20)
    gap_buffers = compute_expanding_gap_buffers(daily_bars, atr14)
    all_daily_pivots = compute_causal_pivots(daily_bars, 5, 5)

    wk50_series = compute_wk_warn_series(weekly_rows, 50)
    wk200_series = compute_wk_warn_series(weekly_rows, 200)
    weekly_close_ts_list = [int(w.week_close_ts) for w in weekly_rows]

    aligned_wk50_active: list[bool] = []
    aligned_wk200_active: list[bool] = []
    for b in daily_bars:
        w_idx = bisect.bisect_right(weekly_close_ts_list, int(b.close_ts)) - 1
        aligned_wk50_active.append(bool(wk50_series[w_idx].active) if w_idx >= 0 else False)
        aligned_wk200_active.append(bool(wk200_series[w_idx].active) if w_idx >= 0 else False)

    # 1. Baseline 10% overall simulation
    bt_overall, overall_trades, _ = _simulate_track_b_variant(
        daily_bars,
        ema50=ema50,
        atr14=atr14,
        vol_sma20=vol_sma20,
        gap_buffers=gap_buffers,
        all_daily_pivots=all_daily_pivots,
        aligned_wk50_active=aligned_wk50_active,
        aligned_wk200_active=aligned_wk200_active,
        mode="OVERALL",
    )

    # 2. Standalone single-setup simulations (SW-1, SW-2, SW-3)
    bt_by_setup: dict[str, Any] = {}
    setup_trades_map: dict[str, list[_ClosedTradeRecord]] = {}
    for sid in ("SW-1", "SW-2", "SW-3"):
        st_bundle, st_trades, _ = _simulate_track_b_variant(
            daily_bars,
            ema50=ema50,
            atr14=atr14,
            vol_sma20=vol_sma20,
            gap_buffers=gap_buffers,
            all_daily_pivots=all_daily_pivots,
            aligned_wk50_active=aligned_wk50_active,
            aligned_wk200_active=aligned_wk200_active,
            mode="SETUP_ONLY",
            only_setup_id=sid,
        )
        bt_by_setup[sid] = st_bundle
        setup_trades_map[sid] = st_trades

    # 3. Closed-trade partitions of backtest_overall by wk_warn_50w and wk_warn_200w at signal
    bt_by_wk50 = {
        "ACTIVE": _compute_partition_stats([t for t in overall_trades if t.wk_warn_50w_at_signal]),
        "INACTIVE": _compute_partition_stats([t for t in overall_trades if not t.wk_warn_50w_at_signal]),
    }
    bt_by_wk200 = {
        "ACTIVE": _compute_partition_stats([t for t in overall_trades if t.wk_warn_200w_at_signal]),
        "INACTIVE": _compute_partition_stats([t for t in overall_trades if not t.wk_warn_200w_at_signal]),
    }

    # 4. Single-position sequential 1R shadow ledger across 2014-09-17+
    _, shadow_trades, _ = _simulate_track_b_variant(
        daily_bars,
        ema50=ema50,
        atr14=atr14,
        vol_sma20=vol_sma20,
        gap_buffers=gap_buffers,
        all_daily_pivots=all_daily_pivots,
        aligned_wk50_active=aligned_wk50_active,
        aligned_wk200_active=aligned_wk200_active,
        mode="SHADOW_1R",
    )

    # 5. Causal WK-Warn gated portfolio (B1-B7b)
    bt_wk_gated, gated_trades, last_gated_ctx = _simulate_track_b_variant(
        daily_bars,
        ema50=ema50,
        atr14=atr14,
        vol_sma20=vol_sma20,
        gap_buffers=gap_buffers,
        all_daily_pivots=all_daily_pivots,
        aligned_wk50_active=aligned_wk50_active,
        aligned_wk200_active=aligned_wk200_active,
        mode="WK_GATED",
        shadow_trades=shadow_trades,
    )

    # 6. Streak-throttled causal portfolio (3 consecutive losses < 0R -> 5% until >= +1.5R)
    bt_streak, streak_trades, _ = _simulate_track_b_variant(
        daily_bars,
        ema50=ema50,
        atr14=atr14,
        vol_sma20=vol_sma20,
        gap_buffers=gap_buffers,
        all_daily_pivots=all_daily_pivots,
        aligned_wk50_active=aligned_wk50_active,
        aligned_wk200_active=aligned_wk200_active,
        mode="STREAK_THROTTLED",
        shadow_trades=shadow_trades,
    )

    # Current bar evaluation using effective_spot (live_spot in LIVE mode if present else Close[-1])
    last_idx = len(daily_bars) - 1
    eff_spot = live_spot if (replay_mode == "LIVE" and live_spot is not None) else float(daily_bars[last_idx].close)
    cur_eval = evaluate_track_b_action(
        in_position=bool(last_gated_ctx.get("in_pos", False)),
        pending_exit_next_open=bool(last_gated_ctx.get("pending_exit_next_open", False)),
        wk_warn_50w_active=aligned_wk50_active[last_idx],
        n_active=int(last_gated_ctx.get("n_active", 0)),
        ev_r_active=float(last_gated_ctx.get("ev_r_active", 0.0)),
        ev_r_inactive=float(last_gated_ctx.get("ev_r_inactive", 0.0)),
        primary_setup=last_gated_ctx.get("primary_setup"),
        same_day_exit_cooldown=bool(last_gated_ctx.get("same_day_exit_cooldown", False)),
        entry_ref_price=eff_spot,
        atr14_i=float(atr14[last_idx]) if atr14[last_idx] is not None else None,
        gap_buffer_pct_i=gap_buffers[last_idx],
        open_position_info=last_gated_ctx.get("open_info"),
    )

    return {
        "track_b_summary": {
            "action": cur_eval["action"],
            "wk_warn_50w_active": aligned_wk50_active[last_idx],
            "wk_warn_200w_active": aligned_wk200_active[last_idx],
            "active_setups_on_bar": last_gated_ctx.get("active_setups_on_bar", []),
            "rejected_reason": cur_eval["rejected_reason"],
            "risk_budget_eff": cur_eval["risk_budget_eff"],
            "position_size_pct": cur_eval["position_size_pct"],
            "entry_ref_price": cur_eval["entry_ref_price"],
            "structural_stop": cur_eval["structural_stop"],
            "active_stop": cur_eval["active_stop"],
            "circuit_breaker_price": cur_eval["circuit_breaker_price"],
            "gap_buffer_pct": cur_eval["gap_buffer_pct"],
            "backtest_overall": bt_overall,
            "backtest_by_setup": bt_by_setup,
            "backtest_by_wk_warn_50w": bt_by_wk50,
            "backtest_by_wk_warn_200w": bt_by_wk200,
            "backtest_wk_gated_causal": bt_wk_gated,
            "backtest_streak_throttled": bt_streak,
        },
        "shadow_trades_count": len(shadow_trades),
        "overall_trades": overall_trades,
        "gated_trades": gated_trades,
        "streak_trades": streak_trades,
        "setup_trades": setup_trades_map,
    }
