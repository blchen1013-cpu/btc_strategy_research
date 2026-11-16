"""BTC Short-Selling Strategy Research, Full Parameter Grid Backtest, and Anti-Overfitting Engine.

Explicitly models and compares three order execution mechanisms for short entries and exits:
1. MARKET_NEXT_OPEN (市價單): Signal confirmed at Daily Close[i] -> Fill at Open[i+1] * (1 - 0.05% slippage), Taker fee 0.10%.
2. STOP_MARKET_BREAKDOWN (觸價後交市價單): Stop-Market sell order placed below signal bar low (Low[i] * 0.998) valid for 3 days ->
   Triggered intraday when Low[d] <= trigger_price, filled at min(Open[d], trigger_price) * (1 - 0.08% slippage), Taker fee 0.10%.
3. LIMIT_PULLBACK (限價單): Limit sell order placed on a pullback above Close[i] (Close[i] + 0.35 * (structural_stop - Close[i])) valid for 5 days ->
   Filled when High[d] >= limit_price (provided High[d] < structural_stop) at max(Open[d], limit_price) with 0% slippage, Maker fee 0.04%.

Also models three stop-loss / exit mechanisms + optional Limit Take-Profit (LIMIT_TP) + Intraday 10% Circuit Breaker (STOP_MARKET):
- CLOSE_STRUCT_AND_EMA: Daily close > active_stop or 2-day close > EMA_N -> T+1 Open Market buy-to-cover (+ Intraday 10% Stop-Market circuit breaker).
- CLOSE_STRUCT_ONLY: Daily close > active_stop only -> T+1 Open Market buy-to-cover (+ Intraday 10% Stop-Market circuit breaker).
- INTRADAY_STOP_MARKET: Active stop placed directly as an intraday Stop-Market buy-to-cover order (filled immediately if High[d] >= active_stop with 0.08% slippage).
"""

from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from scripts.btc_bottom.fetch_snapshots import (
    DailyOHLCVRow,
    WeeklyOHLCVRow,
    load_and_verify_snapshot,
)
from scripts.btc_bottom.indicators import (
    Pivot,
    calc_ema_sma_seeded,
    calc_sma,
    calc_wilder_atr,
    compute_causal_pivots,
    compute_wk_warn_series,
)

# Explicit Fee & Slippage Schedule by Order Type
FEE_TAKER = 0.0010       # 0.10% for Market & Stop-Market orders
FEE_MAKER = 0.0004       # 0.04% for Limit orders
SLIP_MARKET = 0.0005     # 0.05% adverse slippage on Market orders (T+1 Open)
SLIP_STOP_MKT = 0.0008   # 0.08% adverse slippage on Stop-Market orders (breakdown / squeeze)
SLIP_LIMIT = 0.0000      # 0.00% slippage on Limit orders

IS_END_DATE = "2020-12-31"  # In-Sample: 2014-09-17 .. 2020-12-31; Out-of-Sample: 2021-01-01 .. 2026-11-13


@dataclass(frozen=True)
class ShortSetupCandidate:
    setup_id: str          # "SH-1" | "SH-2" | "SH-3"
    subtype: str           # "LH_EMA_BREAK" | "DOUBLE_TOP" | "HEAD_SHOULDERS_TOP" | "WYCKOFF_LPSY"
    signal_idx: int
    signal_close: float
    signal_low: float
    structural_stop: float
    entry_anchor_high: float
    stop_dist_to_close: float
    consumed_pivot_idx: int | None = None


@dataclass
class ClosedShortTrade:
    setup_id: str
    subtype: str
    entry_order_type: str
    stop_order_type: str
    signal_idx: int
    entry_idx: int
    exit_idx: int
    signal_date: str
    entry_date: str
    exit_date: str
    trigger_or_limit_price: float
    entry_price: float
    structural_stop: float
    final_active_stop: float
    circuit_breaker_price: float
    tp_limit_price: float | None
    exit_price: float
    exit_order_type: str
    exit_reason: str
    position_size_pct: float
    equity_at_entry: float
    pnl_usd: float
    trade_r: float
    regime_at_signal: str


def compute_expanding_upside_gap_buffers(
    daily_bars: Sequence[DailyOHLCVRow],
    atr14: Sequence[float | None],
) -> list[float]:
    """Causal expanding P99 of daily positive squeeze returns relative to ATR14."""
    n = len(daily_bars)
    out: list[float] = [0.025] * n
    tail_mults: list[float] = []

    for i in range(n):
        if i >= 15 and atr14[i - 1] is not None:
            prev_c = float(daily_bars[i - 1].close)
            cur_c = float(daily_bars[i].close)
            prev_atr = float(atr14[i - 1])
            if prev_c > 0.0:
                pos_ret = max(0.0, (cur_c - prev_c) / prev_c)
                atr_ratio_prev = max(1e-6, prev_atr / prev_c)
                bisect.insort(tail_mults, pos_ret / atr_ratio_prev)

        if len(tail_mults) < 90:
            mult_i = 1.5
        else:
            pos = 0.98 * (len(tail_mults) - 1)
            lo = int(math.floor(pos))
            hi = int(math.ceil(pos))
            frac = pos - lo
            mult_i = tail_mults[lo] * (1.0 - frac) + tail_mults[hi] * frac

        cur_c = float(daily_bars[i].close)
        cur_atr = atr14[i]
        if cur_atr is not None and cur_c > 0.0:
            out[i] = min(0.15, max(0.025, mult_i * (float(cur_atr) / cur_c)))
        else:
            out[i] = 0.025
    return out


def precompute_short_candidates(
    daily_bars: Sequence[DailyOHLCVRow],
    atr14: Sequence[float | None],
    vol_sma20: Sequence[float | None],
    ema_map: dict[int, Sequence[float | None]],
    all_pivots: Sequence[Pivot],
) -> dict[str, Any]:
    """Precompute raw SH-1 (per EMA period), SH-2, and SH-3 candidates for fast grid evaluation."""
    n = len(daily_bars)
    low_pivs = [p for p in all_pivots if p.kind == "LOW"]
    high_pivs = [p for p in all_pivots if p.kind == "HIGH"]

    sh1_by_ema: dict[int, list[ShortSetupCandidate | None]] = {k: [None] * n for k in ema_map}
    sh2_list: list[ShortSetupCandidate | None] = [None] * n
    sh3_list: list[ShortSetupCandidate | None] = [None] * n

    for i in range(50, n):
        atr_val = atr14[i]
        vsma_val = vol_sma20[i]
        if atr_val is None or vsma_val is None:
            continue
        atr_i = float(atr_val)
        vsma_i = float(vsma_val)
        bi = daily_bars[i]
        bp = daily_bars[i - 1]
        close_i = float(bi.close)
        open_i = float(bi.open)
        high_i = float(bi.high)
        low_i = float(bi.low)
        vol_i = float(bi.volume)
        close_prev = float(bp.close)
        low_prev = float(bp.low)

        conf_highs = [p for p in high_pivs if p.confirm_idx <= i]
        conf_lows = [p for p in low_pivs if p.confirm_idx <= i]

        # 1. SH-1 (Dow Theory Lower High + Lower Low + EMA breakdown / rejection with volume)
        if (
            len(conf_highs) >= 2
            and len(conf_lows) >= 2
            and close_i < open_i
            and close_i < low_prev
            and vol_i >= 1.00 * vsma_i
        ):
            h_cur = conf_highs[-1]
            h_prev = conf_highs[-2]
            l_cur = conf_lows[-1]
            l_prev = conf_lows[-2]
            if h_cur.price < h_prev.price and l_cur.price < l_prev.price and (i - h_cur.confirm_idx) <= 25:
                if all(float(daily_bars[u].close) <= h_cur.price for u in range(h_cur.idx + 1, i + 1)):
                    s_stop = h_cur.price + 0.25 * atr_i
                    dist_c = (s_stop - close_i) / close_i
                    if 0.0 < dist_c <= 0.25:
                        for ema_p, ema_ser in ema_map.items():
                            if ema_ser[i] is not None and ema_ser[i - 1] is not None:
                                ei = float(ema_ser[i])
                                ep = float(ema_ser[i - 1])
                                cross_dn = close_prev >= ep and close_i < ei
                                reject_dn = high_i >= 0.985 * ei and close_i < ei
                                if cross_dn or reject_dn:
                                    sh1_by_ema[ema_p][i] = ShortSetupCandidate(
                                        setup_id="SH-1",
                                        subtype="LH_LL_EMA_BREAK",
                                        signal_idx=i,
                                        signal_close=close_i,
                                        signal_low=low_i,
                                        structural_stop=s_stop,
                                        entry_anchor_high=h_cur.price,
                                        stop_dist_to_close=dist_c,
                                        consumed_pivot_idx=h_cur.idx,
                                    )

        # 2. SH-2 (Double Top / Head & Shoulders Top Neckline Breakdown)
        if vol_i > 1.10 * vsma_i:
            min120 = max(0, i - 120)
            hw120 = [p for p in conf_highs if p.idx >= min120]
            lw120 = [p for p in conf_lows if p.idx >= min120]
            matches2: list[tuple[int, int, int, ShortSetupCandidate]] = []
            nh = len(hw120)
            # Double Top
            for a in range(nh):
                h1 = hw120[a]
                for b in range(a + 1, nh):
                    h2 = hw120[b]
                    span = h2.idx - h1.idx
                    if span < 10:
                        continue
                    max_h = max(h1.price, h2.price)
                    if max_h <= 0 or abs(h1.price - h2.price) / max_h > 0.05:
                        continue
                    mid_lows = [l.price for l in lw120 if h1.idx < l.idx < h2.idx]
                    if not mid_lows:
                        continue
                    neckline = min(mid_lows)
                    if close_i >= neckline:
                        continue
                    if any(float(daily_bars[u].close) > max_h for u in range(h1.idx, i + 1)):
                        continue
                    if any(float(daily_bars[u].close) < neckline for u in range(h2.idx + 1, i)):
                        continue
                    s_stop = h2.price + 0.25 * atr_i
                    dist_c = (s_stop - close_i) / close_i
                    if 0.0 < dist_c <= 0.25:
                        matches2.append(
                            (
                                h2.idx,
                                span,
                                0,
                                ShortSetupCandidate(
                                    setup_id="SH-2",
                                    subtype="DOUBLE_TOP",
                                    signal_idx=i,
                                    signal_close=close_i,
                                    signal_low=low_i,
                                    structural_stop=s_stop,
                                    entry_anchor_high=h2.price,
                                    stop_dist_to_close=dist_c,
                                ),
                            )
                        )
            # Head & Shoulders Top
            if nh >= 3:
                for a in range(nh - 2):
                    ls = hw120[a]
                    for b in range(a + 1, nh - 1):
                        head = hw120[b]
                        if head.idx - ls.idx < 8:
                            continue
                        for c in range(b + 1, nh):
                            rs = hw120[c]
                            if rs.idx - head.idx < 8:
                                continue
                            max_sh = max(ls.price, rs.price)
                            if max_sh <= 0 or head.price <= max_sh * 1.025:
                                continue
                            if abs(ls.price - rs.price) / max_sh > 0.065:
                                continue
                            v1 = [l.price for l in lw120 if ls.idx < l.idx < head.idx]
                            v2 = [l.price for l in lw120 if head.idx < l.idx < rs.idx]
                            if not v1 or not v2:
                                continue
                            neckline = min(min(v1), min(v2))
                            if close_i >= neckline:
                                continue
                            if any(float(daily_bars[u].close) > head.price for u in range(head.idx, i + 1)):
                                continue
                            if any(float(daily_bars[u].close) > rs.price for u in range(rs.idx, i + 1)):
                                continue
                            if any(float(daily_bars[u].close) < neckline for u in range(rs.idx + 1, i)):
                                continue
                            s_stop = rs.price + 0.25 * atr_i
                            dist_c = (s_stop - close_i) / close_i
                            if 0.0 < dist_c <= 0.25:
                                matches2.append(
                                    (
                                        rs.idx,
                                        rs.idx - ls.idx,
                                        1,
                                        ShortSetupCandidate(
                                            setup_id="SH-2",
                                            subtype="HEAD_SHOULDERS_TOP",
                                            signal_idx=i,
                                            signal_close=close_i,
                                            signal_low=low_i,
                                            structural_stop=s_stop,
                                            entry_anchor_high=rs.price,
                                            stop_dist_to_close=dist_c,
                                        ),
                                    )
                                )
            if matches2:
                matches2.sort(key=lambda m: (m[0], m[1], m[2]))
                sh2_list[i] = matches2[-1][3]

        # 3. SH-3 (Wyckoff Distribution Box: UTAD -> SOW -> LPSY)
        if close_i < open_i and close_i < close_prev:
            min90 = max(0, i - 90)
            hw90 = [p for p in conf_highs if p.idx >= min90]
            lw90 = [p for p in conf_lows if p.idx >= min90]
            matches3: list[tuple[int, int, ShortSetupCandidate]] = []
            for bc in hw90:
                if bc.idx > i - 15:
                    continue
                for ar in lw90:
                    if not (bc.idx < ar.idx <= i - 5 and ar.confirm_idx < i - 1):
                        continue
                    if (bc.price - ar.price) / ar.price < 0.12:
                        continue
                    if any(float(daily_bars[u].close) > 1.15 * bc.price for u in range(bc.idx, ar.confirm_idx + 1)):
                        continue
                    u_rec: int | None = None
                    for u in range(ar.confirm_idx, i - 1):
                        bu = daily_bars[u]
                        if 1.005 * bc.price < float(bu.high) <= 1.15 * bc.price:
                            if float(bu.close) < bc.price:
                                u_rec = u
                                break
                            if u + 1 <= i - 2 and float(daily_bars[u + 1].close) < bc.price:
                                u_rec = u + 1
                                break
                    if u_rec is None:
                        continue
                    if any(float(daily_bars[u].close) > 1.15 * bc.price for u in range(ar.confirm_idx, u_rec + 1)):
                        continue
                    q_idx: int | None = None
                    for q in range(max(19, u_rec + 1), i):
                        vq = vol_sma20[q]
                        if vq is not None and float(daily_bars[q].close) < ar.price and float(daily_bars[q].volume) > 1.20 * float(vq):
                            q_idx = q
                            break
                    if q_idx is None:
                        continue
                    if not (1 <= i - q_idx <= 15):
                        continue
                    highs_after = [float(daily_bars[k].high) for k in range(q_idx + 1, i + 1)]
                    max_hi_after = max(highs_after)
                    if max_hi_after < 0.98 * ar.price:
                        continue
                    box_mid = 0.5 * (bc.price + ar.price)
                    if any(float(daily_bars[k].close) > box_mid for k in range(q_idx + 1, i + 1)):
                        continue
                    s_stop = max(max_hi_after, box_mid) + 0.25 * atr_i
                    dist_c = (s_stop - close_i) / close_i
                    if 0.0 < dist_c <= 0.25:
                        matches3.append(
                            (
                                ar.idx,
                                bc.idx,
                                ShortSetupCandidate(
                                    setup_id="SH-3",
                                    subtype="WYCKOFF_LPSY",
                                    signal_idx=i,
                                    signal_close=close_i,
                                    signal_low=low_i,
                                    structural_stop=s_stop,
                                    entry_anchor_high=max_hi_after,
                                    stop_dist_to_close=dist_c,
                                ),
                            )
                        )
            if matches3:
                matches3.sort(key=lambda m: (m[0], m[1]))
                sh3_list[i] = matches3[-1][2]

    high_pivs_by_confirm: dict[int, list[Pivot]] = {}
    for p in high_pivs:
        high_pivs_by_confirm.setdefault(p.confirm_idx, []).append(p)

    return {
        "sh1_by_ema": sh1_by_ema,
        "sh2": sh2_list,
        "sh3": sh3_list,
        "high_pivs": high_pivs,
        "high_pivs_by_confirm": high_pivs_by_confirm,
    }


@dataclass(frozen=True)
class ShortStrategyParams:
    setup_filter: str       # "ALL" | "SH1_SH2" | "SH1_ONLY" | "SH2_ONLY" | "SH3_ONLY"
    regime_filter: str      # "NONE" | "BELOW_SMA200D" | "WK_BEAR_50W"
    entry_order_type: str   # "MARKET_NEXT_OPEN" | "STOP_MARKET_BREAKDOWN" | "LIMIT_PULLBACK"
    stop_order_type: str    # "CLOSE_STRUCT_AND_EMA" | "CLOSE_STRUCT_ONLY" | "INTRADAY_STOP_MARKET"
    ema_period: int         # 30 | 50
    tp_limit_r: float | None  # None | 2.5 | 3.5 | 5.0
    be_trigger_r: float     # 1.0 | 1.5

    def key_tuple(self) -> tuple[Any, ...]:
        return (
            self.setup_filter,
            self.regime_filter,
            self.entry_order_type,
            self.stop_order_type,
            self.ema_period,
            self.tp_limit_r,
            self.be_trigger_r,
        )

    def short_label(self) -> str:
        tp_str = f"TP={self.tp_limit_r}R" if self.tp_limit_r is not None else "TP=TrailOnly"
        return (
            f"Setup={self.setup_filter} | Regime={self.regime_filter} | "
            f"Entry={self.entry_order_type} | Stop={self.stop_order_type} | "
            f"EMA={self.ema_period} | {tp_str} | BE={self.be_trigger_r}R"
        )


def simulate_short_strategy(
    daily_bars: Sequence[DailyOHLCVRow],
    *,
    params: ShortStrategyParams,
    precomp: dict[str, Any],
    ema_map: dict[int, Sequence[float | None]],
    sma200: Sequence[float | None],
    atr14: Sequence[float | None],
    upside_gap_buffers: Sequence[float],
    aligned_wk50_active: Sequence[bool],
    start_idx: int = 0,
    end_idx: int | None = None,
    risk_budget: float = 0.10,
) -> tuple[dict[str, Any], list[ClosedShortTrade]]:
    """Simulate a short strategy on daily_bars[start_idx : end_idx+1] with explicit order mechanics."""
    n = len(daily_bars)
    last_bar_idx = (n - 1) if end_idx is None else min(n - 1, end_idx)
    initial_equity = 10000.0
    equity = initial_equity

    ema_ser = ema_map[params.ema_period]
    sh1_list: Sequence[ShortSetupCandidate | None] = precomp["sh1_by_ema"][params.ema_period]
    sh2_list: Sequence[ShortSetupCandidate | None] = precomp["sh2"]
    sh3_list: Sequence[ShortSetupCandidate | None] = precomp["sh3"]
    high_pivs_by_confirm: dict[int, list[Pivot]] = precomp["high_pivs_by_confirm"]

    consumed_sh1_pivots: set[int] = set()
    closed_trades: list[ClosedShortTrade] = []
    daily_equities: list[float] = []

    # Active short position state
    in_pos = False
    pos_setup_id = ""
    pos_subtype = ""
    pos_sig_idx = -1
    pos_ent_idx = -1
    pos_trig_or_lim_px = 0.0
    pos_entry_px = 0.0
    pos_qty = 0.0
    pos_entry_fee = 0.0
    pos_1r_usd = 0.0
    pos_eq_at_entry = initial_equity
    pos_size_pct = 0.0
    pos_s_stop = 0.0
    pos_active_stop = 0.0
    pos_cb_px = 0.0
    pos_tp_px: float | None = None
    pos_anchor_high = 0.0
    pos_below_ema = False
    pos_pending_exit_open = False
    pos_pending_exit_reason = ""
    pos_regime_str = ""

    # Pending entry order state
    pending_cand: ShortSetupCandidate | None = None
    pending_expire_idx = -1
    pending_order_px = 0.0
    pending_regime_str = ""

    def _close_short(exit_idx: int, exit_px: float, exit_order_type: str, reason: str) -> None:
        nonlocal equity, in_pos, pos_pending_exit_open, pos_pending_exit_reason
        fee_out_rate = FEE_MAKER if exit_order_type == "LIMIT" else FEE_TAKER
        exit_fee = pos_qty * exit_px * fee_out_rate
        pnl_usd = pos_qty * (pos_entry_px - exit_px) - pos_entry_fee - exit_fee
        trade_r = pnl_usd / pos_1r_usd if pos_1r_usd > 0.0 else 0.0
        equity = max(1.0, equity + pnl_usd)

        closed_trades.append(
            ClosedShortTrade(
                setup_id=pos_setup_id,
                subtype=pos_subtype,
                entry_order_type=params.entry_order_type,
                stop_order_type=params.stop_order_type,
                signal_idx=pos_sig_idx,
                entry_idx=pos_ent_idx,
                exit_idx=exit_idx,
                signal_date=daily_bars[pos_sig_idx].date_utc,
                entry_date=daily_bars[pos_ent_idx].date_utc,
                exit_date=daily_bars[exit_idx].date_utc,
                trigger_or_limit_price=pos_trig_or_lim_px,
                entry_price=pos_entry_px,
                structural_stop=pos_s_stop,
                final_active_stop=pos_active_stop,
                circuit_breaker_price=pos_cb_px,
                tp_limit_price=pos_tp_px,
                exit_price=exit_px,
                exit_order_type=exit_order_type,
                exit_reason=reason,
                position_size_pct=pos_size_pct,
                equity_at_entry=pos_eq_at_entry,
                pnl_usd=pnl_usd,
                trade_r=trade_r,
                regime_at_signal=pos_regime_str,
            )
        )
        in_pos = False
        pos_pending_exit_open = False
        pos_pending_exit_reason = ""

    for d in range(start_idx, last_bar_idx + 1):
        bar = daily_bars[d]
        open_d = float(bar.open)
        high_d = float(bar.high)
        low_d = float(bar.low)
        close_d = float(bar.close)
        same_day_cooldown = False
        entered_intraday_today = False

        # 0. Check pending short entry order (from signal_idx < d)
        if pending_cand is not None and not in_pos:
            if d > pending_expire_idx:
                pending_cand = None
            else:
                cand = pending_cand
                fill_px: float | None = None
                fee_in_rate = FEE_TAKER

                if params.entry_order_type == "MARKET_NEXT_OPEN":
                    # Market Sell Short at Open[d]
                    fill_px = open_d * (1.0 - SLIP_MARKET)
                    fee_in_rate = FEE_TAKER
                    pending_cand = None
                elif params.entry_order_type == "STOP_MARKET_BREAKDOWN":
                    # If price breaks above structural_stop before hitting breakdown trigger, cancel
                    if open_d >= cand.structural_stop:
                        pending_cand = None
                    elif open_d <= pending_order_px:
                        fill_px = open_d * (1.0 - SLIP_STOP_MKT)
                        fee_in_rate = FEE_TAKER
                        pending_cand = None
                    elif low_d <= pending_order_px:
                        if high_d >= cand.structural_stop:
                            # Ambiguous bar where both high >= stop and low <= trigger: skip/cancel conservatively
                            pending_cand = None
                        else:
                            fill_px = pending_order_px * (1.0 - SLIP_STOP_MKT)
                            fee_in_rate = FEE_TAKER
                            entered_intraday_today = True
                            pending_cand = None
                elif params.entry_order_type == "LIMIT_PULLBACK":
                    # Limit Sell Short at pending_order_px (pullback toward structural_stop)
                    if open_d >= cand.structural_stop:
                        pending_cand = None
                    elif high_d >= pending_order_px:
                        fill_px = max(open_d, pending_order_px)
                        fee_in_rate = FEE_TAKER if open_d > pending_order_px else FEE_MAKER
                        entered_intraday_today = True
                        pending_cand = None

                if fill_px is not None and fill_px > 0.0:
                    s_stop = cand.structural_stop
                    atr_sig = float(atr14[cand.signal_idx])
                    gap_buf = upside_gap_buffers[cand.signal_idx]
                    stop_dist_pct = max(0.03, 1.5 * atr_sig / fill_px, (s_stop - fill_px) / fill_px)
                    if fill_px < s_stop and stop_dist_pct <= 0.25 and (stop_dist_pct + gap_buf) < 0.75:
                        pos_sz = min(
                            1.0 / (1.0 + fee_in_rate),
                            (risk_budget - (fee_in_rate + FEE_TAKER)) / (stop_dist_pct + gap_buf),
                        )
                        if pos_sz > 0.0:
                            cb_px = fill_px * (1.0 + (risk_budget - (fee_in_rate + FEE_TAKER)) / pos_sz)
                            notional = equity * pos_sz
                            pos_setup_id = cand.setup_id
                            pos_subtype = cand.subtype
                            pos_sig_idx = cand.signal_idx
                            pos_ent_idx = d
                            pos_trig_or_lim_px = pending_order_px
                            pos_entry_px = fill_px
                            pos_qty = notional / fill_px
                            pos_entry_fee = notional * fee_in_rate
                            pos_1r_usd = notional * stop_dist_pct
                            pos_eq_at_entry = equity
                            pos_size_pct = pos_sz
                            pos_s_stop = s_stop
                            pos_active_stop = s_stop
                            pos_cb_px = cb_px
                            if params.tp_limit_r is not None:
                                tp_candidate = fill_px * (1.0 - float(params.tp_limit_r) * stop_dist_pct)
                                pos_tp_px = max(0.01, tp_candidate)
                            else:
                                pos_tp_px = None
                            pos_anchor_high = cand.entry_anchor_high
                            pos_below_ema = False
                            pos_pending_exit_open = False
                            pos_pending_exit_reason = ""
                            pos_regime_str = pending_regime_str
                            in_pos = True
                            if cand.setup_id == "SH-1" and cand.consumed_pivot_idx is not None:
                                consumed_sh1_pivots.add(cand.consumed_pivot_idx)

        # 1. Manage active short position on day d
        if in_pos:
            # Step 1: Execute scheduled next-open MARKET exit from yesterday's close
            if pos_pending_exit_open:
                fill_out = open_d * (1.0 + SLIP_MARKET)
                _close_short(d, fill_out, "MARKET", pos_pending_exit_reason)
                same_day_cooldown = True

            # Step 2: Check Intraday Stop-Market (Circuit Breaker / Intraday Structural Stop) & Limit Take-Profit
            if in_pos:
                if params.stop_order_type == "INTRADAY_STOP_MARKET":
                    eff_stop_trig = min(pos_cb_px, pos_active_stop)
                    is_cb_trig = pos_cb_px <= pos_active_stop
                else:
                    eff_stop_trig = pos_cb_px
                    is_cb_trig = True

                if open_d >= eff_stop_trig:
                    fill_out = open_d * (1.0 + SLIP_STOP_MKT)
                    reason = "CIRCUIT_BREAKER_OPEN_GAP" if is_cb_trig else "INTRADAY_STOP_MARKET_OPEN_GAP"
                    _close_short(d, fill_out, "STOP_MARKET", reason)
                    same_day_cooldown = True
                elif high_d >= eff_stop_trig:
                    fill_out = eff_stop_trig * (1.0 + SLIP_STOP_MKT)
                    reason = (
                        "CIRCUIT_BREAKER_INTRADAY"
                        if is_cb_trig
                        else (
                            "INTRADAY_TRAILING_STOP_MARKET"
                            if pos_active_stop < pos_s_stop - 1e-6
                            else "INTRADAY_INITIAL_STOP_MARKET"
                        )
                    )
                    _close_short(d, fill_out, "STOP_MARKET", reason)
                    same_day_cooldown = True
                elif pos_tp_px is not None and not entered_intraday_today and low_d <= pos_tp_px:
                    # Limit Take-Profit hit (with no stop hit on this bar)
                    fill_out = min(open_d, pos_tp_px)
                    _close_short(d, fill_out, "LIMIT", f"LIMIT_TAKE_PROFIT_{params.tp_limit_r}R")
                    same_day_cooldown = True

            # Step 3: Closing structural / EMA reclaim check (for CLOSE_STRUCT_AND_EMA and CLOSE_STRUCT_ONLY)
            if in_pos and params.stop_order_type in ("CLOSE_STRUCT_AND_EMA", "CLOSE_STRUCT_ONLY"):
                stop_break = close_d > pos_active_stop
                ema_break = False
                if params.stop_order_type == "CLOSE_STRUCT_AND_EMA":
                    bars_held = d - pos_sig_idx
                    if (
                        d >= pos_sig_idx + 2
                        and (pos_below_ema or bars_held >= 10)
                        and ema_ser[d] is not None
                        and ema_ser[d - 1] is not None
                    ):
                        if close_d > float(ema_ser[d]) and float(daily_bars[d - 1].close) > float(ema_ser[d - 1]):
                            ema_break = True
                if stop_break or ema_break:
                    pos_pending_exit_open = True
                    if stop_break and ema_break:
                        pos_pending_exit_reason = "CLOSE_STRUCT_AND_EMA_BREAK_NEXT_OPEN_MKT"
                    elif stop_break:
                        pos_pending_exit_reason = (
                            "CLOSE_TRAILING_STOP_NEXT_OPEN_MKT"
                            if pos_active_stop < pos_s_stop - 1e-6
                            else "CLOSE_INITIAL_STOP_NEXT_OPEN_MKT"
                        )
                    else:
                        pos_pending_exit_reason = "CLOSE_EMA_TWO_DAY_RECLAIM_NEXT_OPEN_MKT"

            # Step 4: End-of-day Breakeven & Causal Lower-High Trailing Stop update
            if in_pos and not pos_pending_exit_open:
                if ema_ser[d] is not None and close_d < float(ema_ser[d]):
                    pos_below_ema = True
                net_unreal = (
                    pos_qty * (pos_entry_px - close_d)
                    - pos_entry_fee
                    - (pos_qty * close_d * FEE_TAKER)
                )
                if pos_1r_usd > 0.0 and (net_unreal / pos_1r_usd) >= params.be_trigger_r:
                    be_px = pos_entry_px * (1.0 - 2.0 * FEE_TAKER)
                    pos_active_stop = min(pos_active_stop, be_px)
                if atr14[d] is not None:
                    atr_d = float(atr14[d])
                    for p_hi in high_pivs_by_confirm.get(d, []):
                        if p_hi.idx > pos_sig_idx + 1 and p_hi.price < pos_anchor_high:
                            pos_active_stop = min(pos_active_stop, p_hi.price + 0.25 * atr_d)

        # End-of-day MTM equity
        if in_pos:
            mtm_pnl = pos_qty * (pos_entry_px - close_d) - pos_entry_fee
            daily_equities.append(max(1.0, equity + mtm_pnl))
        else:
            daily_equities.append(equity)

        # 2. Signal detection at Close[d] (if flat and not in same-day exit cooldown)
        if not in_pos and not same_day_cooldown and d < last_bar_idx:
            # Check macro regime filter at bar d
            below_200d = sma200[d] is not None and close_d < float(sma200[d])
            wk50_act = aligned_wk50_active[d]
            if params.regime_filter == "BELOW_SMA200D" and not below_200d:
                continue
            if params.regime_filter == "WK_BEAR_50W" and not wk50_act:
                continue

            c1 = sh1_list[d]
            if c1 is not None and c1.consumed_pivot_idx in consumed_sh1_pivots:
                c1 = None
            c2 = sh2_list[d]
            c3 = sh3_list[d]

            chosen: ShortSetupCandidate | None = None
            if params.setup_filter == "ALL":
                chosen = c3 or c2 or c1
            elif params.setup_filter == "SH1_SH2":
                chosen = c2 or c1
            elif params.setup_filter == "SH1_ONLY":
                chosen = c1
            elif params.setup_filter == "SH2_ONLY":
                chosen = c2
            elif params.setup_filter == "SH3_ONLY":
                chosen = c3

            if chosen is not None:
                reg_labels = []
                if below_200d:
                    reg_labels.append("<200D_SMA")
                if wk50_act:
                    reg_labels.append("WK_BEAR_50W")
                pending_regime_str = "+".join(reg_labels) if reg_labels else "BULL_COUNTERTREND"
                pending_cand = chosen

                if params.entry_order_type == "MARKET_NEXT_OPEN":
                    pending_expire_idx = d + 1
                    pending_order_px = close_d
                elif params.entry_order_type == "STOP_MARKET_BREAKDOWN":
                    pending_expire_idx = min(last_bar_idx, d + 3)
                    pending_order_px = chosen.signal_low * 0.998
                elif params.entry_order_type == "LIMIT_PULLBACK":
                    pending_expire_idx = min(last_bar_idx, d + 5)
                    pending_order_px = close_d + 0.35 * (chosen.structural_stop - close_d)

    # Compute summary statistics
    n_closed = len(closed_trades)
    if n_closed == 0:
        stats = {
            "n_closed": 0,
            "win_rate_pct": 0.0,
            "payoff_ratio": None,
            "ev_r": 0.0,
            "winsorized_ev_r": 0.0,
            "total_return_pct": 0.0,
            "max_dd_pct": 0.0,
            "single_worst_pct": 0.0,
            "profit_factor": 0.0,
        }
    else:
        wins = [t for t in closed_trades if t.pnl_usd > 0.0]
        losses = [t for t in closed_trades if t.pnl_usd <= 0.0]
        win_rate_pct = 100.0 * len(wins) / n_closed
        ev_r = statistics.mean(t.trade_r for t in closed_trades)
        winsorized_ev_r = statistics.mean(max(-2.0, min(4.0, t.trade_r)) for t in closed_trades)
        win_rs = [t.trade_r for t in closed_trades if t.trade_r > 0.0]
        loss_rs = [t.trade_r for t in closed_trades if t.trade_r < 0.0]
        if win_rs and loss_rs:
            mean_loss = abs(statistics.mean(loss_rs))
            payoff_ratio = round(statistics.mean(win_rs) / mean_loss, 4) if mean_loss > 0 else None
        else:
            payoff_ratio = None
        gross_win = sum(t.pnl_usd for t in wins)
        gross_loss = abs(sum(t.pnl_usd for t in losses))
        pf = round(gross_win / gross_loss, 3) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
        single_worst_pct = min((t.pnl_usd / t.equity_at_entry) * 100.0 for t in closed_trades)
        peak_eq = initial_equity
        max_dd_pct = 0.0
        for eq in daily_equities:
            if eq > peak_eq:
                peak_eq = eq
            dd = ((peak_eq - eq) / peak_eq) * 100.0 if peak_eq > 0 else 0.0
            if dd > max_dd_pct:
                max_dd_pct = dd
        tot_ret_pct = ((equity - initial_equity) / initial_equity) * 100.0
        stats = {
            "n_closed": n_closed,
            "win_rate_pct": round(win_rate_pct, 2),
            "payoff_ratio": payoff_ratio,
            "ev_r": round(ev_r, 4),
            "winsorized_ev_r": round(winsorized_ev_r, 4),
            "total_return_pct": round(tot_ret_pct, 2),
            "max_dd_pct": round(max_dd_pct, 2),
            "single_worst_pct": round(single_worst_pct, 2),
            "profit_factor": pf,
        }

    return stats, closed_trades


# ============================================================================
# Parameter Grid Space & Neighborhood Plateau Anti-Overfitting
# ============================================================================

SETUP_FILTERS = ("ALL", "SH1_SH2", "SH1_ONLY", "SH2_ONLY", "SH3_ONLY")
REGIME_FILTERS = ("NONE", "BELOW_SMA200D", "WK_BEAR_50W")
ENTRY_ORDER_TYPES = ("MARKET_NEXT_OPEN", "STOP_MARKET_BREAKDOWN", "LIMIT_PULLBACK")
STOP_ORDER_TYPES = ("CLOSE_STRUCT_AND_EMA", "CLOSE_STRUCT_ONLY", "INTRADAY_STOP_MARKET")
EMA_PERIODS = (30, 50)
TP_LIMIT_RS: tuple[float | None, ...] = (None, 2.5, 3.5, 5.0)
BE_TRIGGER_RS = (1.0, 1.5)


def generate_parameter_grid() -> list[ShortStrategyParams]:
    """Generate the full 2,160-combination parameter space."""
    grid: list[ShortStrategyParams] = []
    for sf in SETUP_FILTERS:
        for rf in REGIME_FILTERS:
            for eot in ENTRY_ORDER_TYPES:
                for sot in STOP_ORDER_TYPES:
                    for ema_p in EMA_PERIODS:
                        for tp_r in TP_LIMIT_RS:
                            for be_r in BE_TRIGGER_RS:
                                grid.append(
                                    ShortStrategyParams(
                                        setup_filter=sf,
                                        regime_filter=rf,
                                        entry_order_type=eot,
                                        stop_order_type=sot,
                                        ema_period=ema_p,
                                        tp_limit_r=tp_r,
                                        be_trigger_r=be_r,
                                    )
                                )
    return grid


def build_hamming_neighborhood_map(
    grid: Sequence[ShortStrategyParams],
) -> dict[tuple[Any, ...], list[tuple[Any, ...]]]:
    """For each parameter tuple, find all parameter tuples with Hamming distance <= 1."""
    keys = [p.key_tuple() for p in grid]
    key_set = set(keys)
    domains: list[Sequence[Any]] = [
        SETUP_FILTERS,
        REGIME_FILTERS,
        ENTRY_ORDER_TYPES,
        STOP_ORDER_TYPES,
        EMA_PERIODS,
        TP_LIMIT_RS,
        BE_TRIGGER_RS,
    ]
    neighbor_map: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for k in keys:
        nbrs = [k]
        k_list = list(k)
        for dim_idx, dom in enumerate(domains):
            orig_val = k_list[dim_idx]
            for alt_val in dom:
                if alt_val == orig_val:
                    continue
                k_list[dim_idx] = alt_val
                cand_k = tuple(k_list)
                if cand_k in key_set:
                    nbrs.append(cand_k)
            k_list[dim_idx] = orig_val
        neighbor_map[k] = nbrs
    return neighbor_map


def score_parameter_plateaus(
    stats_by_key: dict[tuple[Any, ...], dict[str, Any]],
    neighbor_map: dict[tuple[Any, ...], list[tuple[Any, ...]]],
    *,
    min_trades: int = 4,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """Compute Neighborhood Plateau Robustness Score to eliminate isolated overfit spikes.

    Combines:
    1. Winsorized Hamming-1 neighborhood expectancy (capping single-trade outliers at [-2R, +4R]).
    2. Global marginal factor stability across the training grid.
    3. Multi-setup structural diversity bonus (preferring ALL / SH1_SH2 over single-pattern cherry-picking).
    """
    # Compute global marginal Winsorized EV for each dimension value on this training set
    dim_marginals: list[dict[Any, float]] = [{} for _ in range(7)]
    for d_idx in range(7):
        by_val: dict[Any, list[float]] = {}
        for k, st in stats_by_key.items():
            if int(st["n_closed"]) >= max(2, min_trades - 1):
                by_val.setdefault(k[d_idx], []).append(float(st.get("winsorized_ev_r", st["ev_r"])))
        for v, arr in by_val.items():
            dim_marginals[d_idx][v] = statistics.mean(arr) if arr else -1.0

    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for k, st in stats_by_key.items():
        nbr_keys = neighbor_map[k]
        nbr_wevs = [
            float(stats_by_key[nk].get("winsorized_ev_r", stats_by_key[nk]["ev_r"]))
            for nk in nbr_keys
            if int(stats_by_key[nk]["n_closed"]) >= max(2, min_trades - 1)
        ]
        nbr_calmars = [
            float(stats_by_key[nk]["total_return_pct"]) / max(15.0, float(stats_by_key[nk]["max_dd_pct"]))
            for nk in nbr_keys
            if int(stats_by_key[nk]["n_closed"]) >= max(2, min_trades - 1)
        ]
        n_closed = int(st["n_closed"])
        if n_closed < min_trades or len(nbr_wevs) < 4:
            out[k] = {
                "plateau_score": -999.0,
                "nbr_mean_ev_r": 0.0,
                "nbr_std_ev_r": 0.0,
                "nbr_pos_ev_pct": 0.0,
                "nbr_mean_calmar": 0.0,
                "is_plateau_eligible": False,
            }
            continue

        mean_wev = statistics.mean(nbr_wevs)
        std_wev = statistics.pstdev(nbr_wevs) if len(nbr_wevs) > 1 else 0.0
        pos_ev_pct = 100.0 * sum(1 for x in nbr_wevs if x > 0.0) / len(nbr_wevs)
        mean_calmar = statistics.mean(nbr_calmars) if nbr_calmars else 0.0
        self_wev = float(st.get("winsorized_ev_r", st["ev_r"]))
        self_calmar = float(st["total_return_pct"]) / max(15.0, float(st["max_dd_pct"]))
        marg_sum = sum(dim_marginals[d_i].get(k[d_i], 0.0) for d_i in range(7))
        diversity_bonus = 0.25 if k[0] in ("ALL", "SH1_SH2") else 0.0

        # Penalize if self_wev is a huge spike above neighbors or if neighbor variance is high
        spike_penalty = max(0.0, self_wev - mean_wev) * 0.5
        eligible = (
            n_closed >= min_trades
            and self_wev > 0.05
            and mean_wev > 0.05
            and pos_ev_pct >= 70.0
            and float(st["max_dd_pct"]) <= 35.0
        )
        score = (
            0.40 * mean_wev
            + 0.20 * self_wev
            - 0.35 * std_wev
            - spike_penalty
            + 0.35 * marg_sum
            + 0.10 * mean_calmar
            + 0.05 * self_calmar
            + diversity_bonus
        )
        if not eligible:
            score -= 100.0

        out[k] = {
            "plateau_score": round(score, 4),
            "nbr_mean_ev_r": round(mean_wev, 4),
            "nbr_std_ev_r": round(std_wev, 4),
            "nbr_pos_ev_pct": round(pos_ev_pct, 1),
            "nbr_mean_calmar": round(mean_calmar, 4),
            "is_plateau_eligible": eligible,
        }
    return out


def align_weekly_bear_50w(
    daily_bars: Sequence[DailyOHLCVRow],
    weekly_bars: Sequence[Any],
) -> list[bool]:
    """Causal alignment of Weekly Close < 50W SMA (active after 2 consecutive weekly closes below 50W SMA)."""
    wk_closes = [float(w.close) for w in weekly_bars]
    wk_sma50 = calc_sma(wk_closes, 50)
    wk_close_ts = [int(w.week_close_ts) for w in weekly_bars]
    n_wk = len(weekly_bars)

    wk_bear_state: list[bool] = [False] * n_wk
    active = False
    for w_idx in range(n_wk):
        s50 = wk_sma50[w_idx]
        if s50 is None:
            wk_bear_state[w_idx] = False
            continue
        if not active:
            if w_idx >= 1 and wk_sma50[w_idx - 1] is not None:
                if wk_closes[w_idx] < float(s50) and wk_closes[w_idx - 1] < float(wk_sma50[w_idx - 1]):
                    active = True
        else:
            if wk_closes[w_idx] > float(s50):
                active = False
        wk_bear_state[w_idx] = active

    aligned: list[bool] = []
    for d in daily_bars:
        d_ts = int(d.close_ts)
        pos = bisect.bisect_right(wk_close_ts, d_ts) - 1
        aligned.append(wk_bear_state[pos] if pos >= 0 else False)
    return aligned


def find_date_index(daily_bars: Sequence[DailyOHLCVRow], target_date: str) -> int:
    for idx, b in enumerate(daily_bars):
        if b.date_utc >= target_date:
            return idx
    return len(daily_bars) - 1


def summarize_trade_list(trades: Sequence[ClosedShortTrade]) -> dict[str, Any]:
    """Compute summary metrics across an arbitrary stitched sequence of trades."""
    n_closed = len(trades)
    if n_closed == 0:
        return {
            "n_closed": 0,
            "win_rate_pct": 0.0,
            "payoff_ratio": None,
            "ev_r": 0.0,
            "compounded_return_pct": 0.0,
            "max_dd_trade_pct": 0.0,
            "single_worst_pct": 0.0,
            "profit_factor": 0.0,
        }
    wins = [t for t in trades if t.pnl_usd > 0.0]
    losses = [t for t in trades if t.pnl_usd <= 0.0]
    win_rate_pct = 100.0 * len(wins) / n_closed
    ev_r = statistics.mean(t.trade_r for t in trades)
    win_rs = [t.trade_r for t in trades if t.trade_r > 0.0]
    loss_rs = [t.trade_r for t in trades if t.trade_r < 0.0]
    if win_rs and loss_rs:
        mean_loss = abs(statistics.mean(loss_rs))
        payoff_ratio = round(statistics.mean(win_rs) / mean_loss, 4) if mean_loss > 0 else None
    else:
        payoff_ratio = None

    eq = 10000.0
    peak_eq = eq
    max_dd_pct = 0.0
    gross_win = 0.0
    gross_loss = 0.0
    worst_pct = 0.0
    for t in trades:
        ret_pct = (t.pnl_usd / t.equity_at_entry) if t.equity_at_entry > 0 else 0.0
        worst_pct = min(worst_pct, ret_pct * 100.0)
        pnl_rebased = eq * ret_pct
        if pnl_rebased > 0:
            gross_win += pnl_rebased
        else:
            gross_loss += abs(pnl_rebased)
        eq = max(1.0, eq + pnl_rebased)
        if eq > peak_eq:
            peak_eq = eq
        dd = ((peak_eq - eq) / peak_eq) * 100.0 if peak_eq > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

    pf = round(gross_win / gross_loss, 3) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    comp_ret_pct = ((eq - 10000.0) / 10000.0) * 100.0
    return {
        "n_closed": n_closed,
        "win_rate_pct": round(win_rate_pct, 2),
        "payoff_ratio": payoff_ratio,
        "ev_r": round(ev_r, 4),
        "compounded_return_pct": round(comp_ret_pct, 2),
        "max_dd_trade_pct": round(max_dd_pct, 2),
        "single_worst_pct": round(worst_pct, 2),
        "profit_factor": pf,
    }


def run_short_research_suite(snapshot_dir: Path) -> dict[str, Any]:
    """Run the full 2,160-combo grid, IS/OOS split, Plateau filter, and Walk-Forward Optimization."""
    _, daily_bars, _, weekly_bars, _, _ = load_and_verify_snapshot(snapshot_dir.resolve())
    n = len(daily_bars)

    closes = [float(b.close) for b in daily_bars]
    vols = [float(b.volume) for b in daily_bars]
    sma200 = calc_sma(closes, 200)
    vol_sma20 = calc_sma(vols, 20)
    ema_map: dict[int, Sequence[float | None]] = {
        30: calc_ema_sma_seeded(closes, 30),
        50: calc_ema_sma_seeded(closes, 50),
    }
    atr14 = calc_wilder_atr(daily_bars, 14)
    all_pivots = compute_causal_pivots(daily_bars)
    upside_gap_buffers = compute_expanding_upside_gap_buffers(daily_bars, atr14)
    aligned_wk50_active = align_weekly_bear_50w(daily_bars, weekly_bars)
    precomp = precompute_short_candidates(daily_bars, atr14, vol_sma20, ema_map, all_pivots)

    grid = generate_parameter_grid()
    params_by_key = {p.key_tuple(): p for p in grid}
    neighbor_map = build_hamming_neighborhood_map(grid)

    # Split index: In-Sample = 2014-09-17 .. 2020-12-31, Out-of-Sample = 2021-01-01 .. end
    oos_start_idx = find_date_index(daily_bars, "2021-01-01")
    is_end_idx = oos_start_idx - 1

    is_stats_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    oos_stats_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    full_stats_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}

    for p in grid:
        k = p.key_tuple()
        st_is, _ = simulate_short_strategy(
            daily_bars,
            params=p,
            precomp=precomp,
            ema_map=ema_map,
            sma200=sma200,
            atr14=atr14,
            upside_gap_buffers=upside_gap_buffers,
            aligned_wk50_active=aligned_wk50_active,
            start_idx=0,
            end_idx=is_end_idx,
        )
        st_oos, _ = simulate_short_strategy(
            daily_bars,
            params=p,
            precomp=precomp,
            ema_map=ema_map,
            sma200=sma200,
            atr14=atr14,
            upside_gap_buffers=upside_gap_buffers,
            aligned_wk50_active=aligned_wk50_active,
            start_idx=oos_start_idx,
            end_idx=n - 1,
        )
        st_full, _ = simulate_short_strategy(
            daily_bars,
            params=p,
            precomp=precomp,
            ema_map=ema_map,
            sma200=sma200,
            atr14=atr14,
            upside_gap_buffers=upside_gap_buffers,
            aligned_wk50_active=aligned_wk50_active,
            start_idx=0,
            end_idx=n - 1,
        )
        is_stats_by_key[k] = st_is
        oos_stats_by_key[k] = st_oos
        full_stats_by_key[k] = st_full

    is_plateau_scores = score_parameter_plateaus(is_stats_by_key, neighbor_map, min_trades=3)
    full_plateau_scores = score_parameter_plateaus(full_stats_by_key, neighbor_map, min_trades=6)

    # 1. Naive In-Sample Max Return Overfit Strategy (picking raw highest IS return with no plateau filter)
    naive_is_key = max(
        grid,
        key=lambda p: (
            float(is_stats_by_key[p.key_tuple()]["total_return_pct"]),
            float(is_stats_by_key[p.key_tuple()]["ev_r"]),
        ),
    ).key_tuple()

    # 2. Naive In-Sample Max EV_R Spike (low sample size allowed, e.g., >= 1 trade)
    naive_spike_key = max(
        grid,
        key=lambda p: (
            float(is_stats_by_key[p.key_tuple()]["ev_r"])
            if int(is_stats_by_key[p.key_tuple()]["n_closed"]) >= 1
            else -999.0
        ),
    ).key_tuple()

    # 3. Plateau-Selected Robust Strategy (selected strictly on In-Sample 2014-2020 plateau score)
    plateau_is_key = max(
        grid,
        key=lambda p: float(is_plateau_scores[p.key_tuple()]["plateau_score"]),
    ).key_tuple()

    # 4. Zero-Tuning Symmetric Mirror Baseline (exact mirror of Long Right-Side rules: ALL setups, 50D EMA, 1.5R BE)
    mirror_baseline_params = ShortStrategyParams(
        setup_filter="ALL",
        regime_filter="BELOW_SMA200D",
        entry_order_type="MARKET_NEXT_OPEN",
        stop_order_type="CLOSE_STRUCT_AND_EMA",
        ema_period=50,
        tp_limit_r=None,
        be_trigger_r=1.5,
    )
    mirror_key = mirror_baseline_params.key_tuple()

    # 5. Anchored Expanding Walk-Forward Optimization (2018-2026 in 4 2-year OOS folds)
    wfo_folds_def = [
        ("Fold 1 (2018-2019)", "2018-01-01", "2019-12-31", 2),
        ("Fold 2 (2020-2021)", "2020-01-01", "2021-12-31", 3),
        ("Fold 3 (2022-2023)", "2022-01-01", "2023-12-31", 4),
        ("Fold 4 (2024-2026)", "2024-01-01", "2026-12-31", 5),
    ]
    wfo_fold_results: list[dict[str, Any]] = []
    wfo_all_oos_trades: list[ClosedShortTrade] = []

    for fold_name, test_start_str, test_end_str, min_tr in wfo_folds_def:
        t_start = find_date_index(daily_bars, test_start_str)
        t_end = find_date_index(daily_bars, test_end_str)
        if daily_bars[t_end].date_utc > test_end_str and t_end > t_start:
            t_end -= 1
        train_end = t_start - 1

        # Evaluate grid on training window [0 .. train_end]
        train_stats_map: dict[tuple[Any, ...], dict[str, Any]] = {}
        for p in grid:
            st_tr, _ = simulate_short_strategy(
                daily_bars,
                params=p,
                precomp=precomp,
                ema_map=ema_map,
                sma200=sma200,
                atr14=atr14,
                upside_gap_buffers=upside_gap_buffers,
                aligned_wk50_active=aligned_wk50_active,
                start_idx=0,
                end_idx=train_end,
            )
            train_stats_map[p.key_tuple()] = st_tr

        fold_plateaus = score_parameter_plateaus(train_stats_map, neighbor_map, min_trades=min_tr)
        best_fold_p = max(grid, key=lambda p: float(fold_plateaus[p.key_tuple()]["plateau_score"]))
        best_fold_k = best_fold_p.key_tuple()

        # Now run OOS on [t_start .. t_end]
        st_test, trades_test = simulate_short_strategy(
            daily_bars,
            params=best_fold_p,
            precomp=precomp,
            ema_map=ema_map,
            sma200=sma200,
            atr14=atr14,
            upside_gap_buffers=upside_gap_buffers,
            aligned_wk50_active=aligned_wk50_active,
            start_idx=t_start,
            end_idx=t_end,
        )
        wfo_all_oos_trades.extend(trades_test)
        wfo_fold_results.append(
            {
                "fold_name": fold_name,
                "train_period": f"{daily_bars[0].date_utc} ~ {daily_bars[train_end].date_utc}",
                "test_period": f"{daily_bars[t_start].date_utc} ~ {daily_bars[t_end].date_utc}",
                "selected_params": best_fold_p,
                "train_stats": train_stats_map[best_fold_k],
                "train_plateau": fold_plateaus[best_fold_k],
                "oos_stats": st_test,
                "oos_trades": trades_test,
            }
        )

    wfo_stitched_summary = summarize_trade_list(wfo_all_oos_trades)

    # Full-period trade lists for Plateau-Selected Strategy and Mirror Baseline
    _, plateau_full_trades = simulate_short_strategy(
        daily_bars,
        params=params_by_key[plateau_is_key],
        precomp=precomp,
        ema_map=ema_map,
        sma200=sma200,
        atr14=atr14,
        upside_gap_buffers=upside_gap_buffers,
        aligned_wk50_active=aligned_wk50_active,
        start_idx=0,
        end_idx=n - 1,
    )
    _, mirror_full_trades = simulate_short_strategy(
        daily_bars,
        params=mirror_baseline_params,
        precomp=precomp,
        ema_map=ema_map,
        sma200=sma200,
        atr14=atr14,
        upside_gap_buffers=upside_gap_buffers,
        aligned_wk50_active=aligned_wk50_active,
        start_idx=0,
        end_idx=n - 1,
    )

    # Controlled Marginal Order-Type & Dimension Ablation (holding other params at Plateau winner or averaging across grid)
    base_p = params_by_key[plateau_is_key]
    order_type_comparison: list[dict[str, Any]] = []
    for eot in ENTRY_ORDER_TYPES:
        for sot in STOP_ORDER_TYPES:
            p_var = ShortStrategyParams(
                setup_filter=base_p.setup_filter,
                regime_filter=base_p.regime_filter,
                entry_order_type=eot,
                stop_order_type=sot,
                ema_period=base_p.ema_period,
                tp_limit_r=base_p.tp_limit_r,
                be_trigger_r=base_p.be_trigger_r,
            )
            k_var = p_var.key_tuple()
            order_type_comparison.append(
                {
                    "params": p_var,
                    "is_stats": is_stats_by_key[k_var],
                    "oos_stats": oos_stats_by_key[k_var],
                    "full_stats": full_stats_by_key[k_var],
                    "is_plateau": is_plateau_scores[k_var],
                }
            )

    # Dimension marginal averages across all combinations with n_closed >= 5
    dimension_marginals: dict[str, list[dict[str, Any]]] = {}
    dim_specs = [
        ("entry_order_type", ENTRY_ORDER_TYPES, lambda p: p.entry_order_type),
        ("stop_order_type", STOP_ORDER_TYPES, lambda p: p.stop_order_type),
        ("regime_filter", REGIME_FILTERS, lambda p: p.regime_filter),
        ("setup_filter", SETUP_FILTERS, lambda p: p.setup_filter),
        ("tp_limit_r", TP_LIMIT_RS, lambda p: p.tp_limit_r),
        ("ema_period", EMA_PERIODS, lambda p: p.ema_period),
        ("be_trigger_r", BE_TRIGGER_RS, lambda p: p.be_trigger_r),
    ]
    for dim_name, dim_vals, getter in dim_specs:
        rows_dim = []
        for v in dim_vals:
            matching_keys = [
                p.key_tuple()
                for p in grid
                if getter(p) == v and int(full_stats_by_key[p.key_tuple()]["n_closed"]) >= 5
            ]
            if not matching_keys:
                continue
            mean_full_ev = statistics.mean(float(full_stats_by_key[k]["ev_r"]) for k in matching_keys)
            mean_oos_ev = statistics.mean(
                float(oos_stats_by_key[k]["ev_r"])
                for k in matching_keys
                if int(oos_stats_by_key[k]["n_closed"]) >= 2
            )
            mean_full_ret = statistics.mean(float(full_stats_by_key[k]["total_return_pct"]) for k in matching_keys)
            mean_full_dd = statistics.mean(float(full_stats_by_key[k]["max_dd_pct"]) for k in matching_keys)
            mean_full_wr = statistics.mean(float(full_stats_by_key[k]["win_rate_pct"]) for k in matching_keys)
            mean_full_n = statistics.mean(int(full_stats_by_key[k]["n_closed"]) for k in matching_keys)
            pos_ev_share = 100.0 * sum(1 for k in matching_keys if float(full_stats_by_key[k]["ev_r"]) > 0.0) / len(matching_keys)
            rows_dim.append(
                {
                    "value": str(v) if v is not None else "None (Trail Only)",
                    "n_combos": len(matching_keys),
                    "mean_trades": round(mean_full_n, 1),
                    "mean_win_rate_pct": round(mean_full_wr, 2),
                    "mean_full_ev_r": round(mean_full_ev, 4),
                    "mean_oos_ev_r": round(mean_oos_ev, 4),
                    "mean_full_return_pct": round(mean_full_ret, 2),
                    "mean_full_max_dd_pct": round(mean_full_dd, 2),
                    "positive_ev_Ratio_pct": round(pos_ev_share, 1),
                }
            )
        dimension_marginals[dim_name] = rows_dim

    return {
        "snapshot_dir": str(snapshot_dir),
        "n_daily_bars": n,
        "date_range": (daily_bars[0].date_utc, daily_bars[-1].date_utc),
        "is_date_range": (daily_bars[0].date_utc, daily_bars[is_end_idx].date_utc),
        "oos_date_range": (daily_bars[oos_start_idx].date_utc, daily_bars[-1].date_utc),
        "grid": grid,
        "params_by_key": params_by_key,
        "is_stats_by_key": is_stats_by_key,
        "oos_stats_by_key": oos_stats_by_key,
        "full_stats_by_key": full_stats_by_key,
        "is_plateau_scores": is_plateau_scores,
        "full_plateau_scores": full_plateau_scores,
        "naive_is_key": naive_is_key,
        "naive_spike_key": naive_spike_key,
        "plateau_is_key": plateau_is_key,
        "mirror_key": mirror_key,
        "wfo_fold_results": wfo_fold_results,
        "wfo_all_oos_trades": wfo_all_oos_trades,
        "wfo_stitched_summary": wfo_stitched_summary,
        "plateau_full_trades": plateau_full_trades,
        "mirror_full_trades": mirror_full_trades,
        "order_type_comparison": order_type_comparison,
        "dimension_marginals": dimension_marginals,
    }


# ============================================================================
# Excel Workbook & Markdown Report Exporters
# ============================================================================

ORDER_TYPE_LABELS_ZH = {
    "MARKET_NEXT_OPEN": "市價單 (T+1 Open Market, 滑價0.05%, Taker 0.10%)",
    "STOP_MARKET_BREAKDOWN": "觸價後交市價單 (跌破訊號低點*0.998觸發市價, 滑價0.08%, Taker 0.10%)",
    "LIMIT_PULLBACK": "限價單 (反彈35%結構停損距離掛單, 滑價0.00%, Maker 0.04%)",
}

STOP_TYPE_LABELS_ZH = {
    "CLOSE_STRUCT_AND_EMA": "收盤結構+EMA雙日收復 -> 隔日開盤市價平倉 (+盤中10%觸價後交市價斷路器)",
    "CLOSE_STRUCT_ONLY": "純收盤結構停損 -> 隔日開盤市價平倉 (+盤中10%觸價後交市價斷路器)",
    "INTRADAY_STOP_MARKET": "盤中觸價後交市價單 (High >= Stop 立即觸發市價平倉, 滑價0.08%)",
}


def _populate_short_trades_sheet(
    ws: Any,
    title_banner: str,
    subtitle_banner: str,
    trades: Sequence[ClosedShortTrade],
) -> None:
    header_fill = PatternFill(start_color="1B263B", end_color="1B263B", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=13, bold=True, color="1B263B")
    sub_font = Font(name="Calibri", size=10, italic=True, color="415A77")
    win_fill = PatternFill(start_color="E8F5E9", end_color="E8F5E9", fill_type="solid")
    loss_fill = PatternFill(start_color="FFEBEE", end_color="FFEBEE", fill_type="solid")
    win_font = Font(name="Calibri", size=10, bold=True, color="1B5E20")
    loss_font = Font(name="Calibri", size=10, bold=True, color="B71C1C")
    data_font = Font(name="Calibri", size=10)
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    ws["A1"] = title_banner
    ws["A1"].font = title_font
    ws["A2"] = subtitle_banner
    ws["A2"].font = sub_font

    headers = [
        "交易編號 (#)",
        "策略代號 (Setup)",
        "型態細類 (Subtype)",
        "總體過濾狀態 (Regime)",
        "進場委託別 (Entry Order Type)",
        "停損委託別 (Stop Order Type)",
        "訊號確認日 (Signal Date)",
        "入場時間 (Entry Date)",
        "出場時間 (Exit Date)",
        "持倉天數 (Days)",
        "觸發/限價委託價 (Trigger/Limit $)",
        "實際做空入場價 (Entry Price $)",
        "初始結構停損價 (Initial Stop $)",
        "出場前移動停損價 (Final Active Stop $)",
        "盤中10%斷路器觸發價 (Circuit Breaker $)",
        "限價停利目標價 (Limit TP $)",
        "實際回補出場價 (Exit Price $)",
        "出場執行單別 (Exit Order)",
        "倉位佔淨值比 (Size %)",
        "標的空頭價差 ((Entry-Exit)/Entry %)",
        "風險倍數 (Trade R)",
        "單筆損益金額 (PnL USD)",
        "單筆對帳戶淨值貢獻 (Account Return %)",
        "交易結果 (Result)",
        "出場原因 (Exit Reason)",
    ]

    header_row = 4
    for col_idx, h_text in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=h_text)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border
    ws.row_dimensions[header_row].height = 28

    for idx, t in enumerate(trades, start=1):
        r = header_row + idx
        is_win = t.pnl_usd > 0.0
        res_str = "獲利 (WIN)" if is_win else "虧損 (LOSS)"
        hold_days = max(1, t.exit_idx - t.entry_idx)
        raw_short_move_pct = (t.entry_price - t.exit_price) / t.entry_price if t.entry_price > 0 else 0.0
        acct_ret_pct = (t.pnl_usd / t.equity_at_entry) if t.equity_at_entry > 0 else 0.0

        row_vals = [
            idx,
            t.setup_id,
            t.subtype,
            t.regime_at_signal,
            t.entry_order_type,
            t.stop_order_type,
            t.signal_date,
            t.entry_date,
            t.exit_date,
            hold_days,
            round(t.trigger_or_limit_price, 2),
            round(t.entry_price, 2),
            round(t.structural_stop, 2),
            round(t.final_active_stop, 2),
            round(t.circuit_breaker_price, 2),
            round(t.tp_limit_price, 2) if t.tp_limit_price is not None else "無 (移動停利)",
            round(t.exit_price, 2),
            t.exit_order_type,
            round(t.position_size_pct, 4),
            round(raw_short_move_pct, 4),
            round(t.trade_r, 4),
            round(t.pnl_usd, 2),
            round(acct_ret_pct, 4),
            res_str,
            t.exit_reason,
        ]
        for c_idx, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=r, column=c_idx, value=val)
            cell.font = data_font
            cell.border = thin_border
            if c_idx in (1, 2, 5, 6, 7, 8, 9, 10, 18, 24):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif c_idx in (3, 4, 25):
                cell.alignment = Alignment(horizontal="left", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="right", vertical="center")

            if c_idx in (11, 12, 13, 14, 15, 17, 22):
                cell.number_format = "$#,##0.00"
            elif c_idx == 16 and isinstance(val, (int, float)):
                cell.number_format = "$#,##0.00"
            elif c_idx in (19, 20, 23):
                cell.number_format = "0.00%"
            elif c_idx == 21:
                cell.number_format = "+0.00\"R\";-0.00\"R\";0.00\"R\""

            if c_idx in (20, 21, 22, 23, 24):
                cell.fill = win_fill if is_win else loss_fill
                cell.font = win_font if is_win else loss_font

    ws.freeze_panes = "A5"
    for col_idx in range(1, len(headers) + 1):
        col_letter = get_column_letter(col_idx)
        max_len = max(
            len(str(ws.cell(row=r_i, column=col_idx).value or ""))
            for r_i in range(4, header_row + len(trades) + 1)
        )
        ws.column_dimensions[col_letter].width = max(13, min(36, max_len + 5))


def export_short_research_excel(suite: dict[str, Any], out_xlsx: Path) -> None:
    """Export multi-sheet Excel workbook with short trades, order-type comparisons, and WFO results."""
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()

    # Sheet 1: Plateau-Selected Robust Short Strategy Trades (2014-2026)
    ws_plateau = wb.active
    ws_plateau.title = "1_穩健高原做空歷史紀錄"
    plateau_p: ShortStrategyParams = suite["params_by_key"][suite["plateau_is_key"]]
    _populate_short_trades_sheet(
        ws_plateau,
        f"BTC 右側做空歷史交易紀錄 (鄰域高原防過擬合首選策略 | {suite['date_range'][0]} ~ {suite['date_range'][1]})",
        f"參數設定: {plateau_p.short_label()} | 初始資金 $10,000 | 單筆最大淨值風險預算 10%",
        suite["plateau_full_trades"],
    )

    # Sheet 2: Walk-Forward Out-of-Sample Stitched Trades (2018-2026)
    ws_wfo = wb.create_sheet(title="2_WalkForward樣本外交易")
    _populate_short_trades_sheet(
        ws_wfo,
        "BTC 做空策略 Walk-Forward 錨定滾動前推純樣本外 (Out-of-Sample) 交易紀錄 (2018-01-01 ~ 2026-11-13)",
        "每 2 年僅用歷史訓練集 (< t) 透過鄰域高原評分選出參數，在未來 2 年未知樣本外實盤執行，零未來函數、零事後調參",
        suite["wfo_all_oos_trades"],
    )

    # Sheet 3: Symmetric Mirror Baseline Trades (2014-2026)
    ws_mirror = wb.create_sheet(title="3_對稱鏡像基準做空紀錄")
    mirror_p: ShortStrategyParams = suite["params_by_key"][suite["mirror_key"]]
    _populate_short_trades_sheet(
        ws_mirror,
        f"BTC 對稱鏡像基準做空歷史紀錄 (零調參多頭規則直接鏡像反轉 | {suite['date_range'][0]} ~ {suite['date_range'][1]})",
        f"參數設定: {mirror_p.short_label()}",
        suite["mirror_full_trades"],
    )

    # Sheet 4: Order Type & Anti-Overfitting Summary
    ws_sum = wb.create_sheet(title="4_委託單別與防過擬合總表")
    header_fill = PatternFill(start_color="1B263B", end_color="1B263B", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=13, bold=True, color="1B263B")
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    ws_sum["A1"] = "BTC 做空策略：委託單別比較 (市價 / 觸價後交市價 / 限價) 與 消除過擬合實證總表"
    ws_sum["A1"].font = title_font

    # Section A: Anti-Overfitting Strategy Comparison (IS vs OOS vs Full)
    ws_sum["A3"] = "【A】消除過擬合前後對比：盲目極大化 (Overfit) vs. 鄰域高原穩健選參 (Plateau) vs. 對稱鏡像基準 vs. Walk-Forward"
    ws_sum["A3"].font = Font(name="Calibri", size=11, bold=True)

    sec_a_headers = [
        "選參方法 (Selection Method)",
        "參數組合 (Parameter Combination)",
        "IS 樣本內筆數 (2014-2020)",
        "IS 勝率 (%)",
        "IS 期望值 E[R]",
        "IS 總報酬 (%)",
        "IS 最大回撤 (%)",
        "IS 鄰域高原分",
        "OOS 樣本外筆數 (2021-2026)",
        "OOS 勝率 (%)",
        "OOS 期望值 E[R]",
        "OOS 總報酬 (%)",
        "OOS 最大回撤 (%)",
        "全樣本總報酬 (2014-2026 %)",
        "全樣本期望值 E[R]",
        "全樣本最大回撤 (%)",
    ]
    for c_i, h in enumerate(sec_a_headers, start=1):
        cell = ws_sum.cell(row=4, column=c_i, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin_border

    compare_keys = [
        ("1. 盲目最高報酬過擬合 (Naive IS Max-Return Overfit)", suite["naive_is_key"]),
        ("2. 盲目最高單筆E[R]尖峰 (Naive IS Max-EV Spike)", suite["naive_spike_key"]),
        ("3. 鄰域高原穩健選參 (Plateau-Selected Robust Winner)", suite["plateau_is_key"]),
        ("4. 零調參多頭規則直接鏡像 (Symmetric Mirror Baseline)", suite["mirror_key"]),
    ]
    for idx_r, (label_m, k_m) in enumerate(compare_keys, start=5):
        p_m = suite["params_by_key"][k_m]
        st_is = suite["is_stats_by_key"][k_m]
        st_oos = suite["oos_stats_by_key"][k_m]
        st_full = suite["full_stats_by_key"][k_m]
        pl_is = suite["is_plateau_scores"][k_m]
        vals = [
            label_m,
            p_m.short_label(),
            st_is["n_closed"],
            st_is["win_rate_pct"],
            st_is["ev_r"],
            st_is["total_return_pct"],
            st_is["max_dd_pct"],
            pl_is["plateau_score"],
            st_oos["n_closed"],
            st_oos["win_rate_pct"],
            st_oos["ev_r"],
            st_oos["total_return_pct"],
            st_oos["max_dd_pct"],
            st_full["total_return_pct"],
            st_full["ev_r"],
            st_full["max_dd_pct"],
        ]
        for c_i, v in enumerate(vals, start=1):
            c = ws_sum.cell(row=idx_r, column=c_i, value=v)
            c.border = thin_border

    # Section B: Controlled 3x3 Entry Order Type x Stop Order Type Matrix
    ws_sum["A11"] = "【B】3種進場委託單 (市價/觸價市價/限價) × 3種停損委託單 控制變數對照表"
    ws_sum["A11"].font = Font(name="Calibri", size=11, bold=True)
    sec_b_headers = [
        "進場委託單別 (Entry Order Type)",
        "停損委託單別 (Stop Order Type)",
        "全樣本筆數 (N)",
        "全樣本勝率 (%)",
        "全樣本賺賠比 (Payoff)",
        "全樣本期望值 E[R]",
        "全樣本總報酬 (%)",
        "全樣本最大回撤 (%)",
        "單筆最差虧損 (%)",
        "IS 期望值 E[R]",
        "OOS 期望值 E[R]",
        "OOS 總報酬 (%)",
    ]
    for c_i, h in enumerate(sec_b_headers, start=1):
        cell = ws_sum.cell(row=12, column=c_i, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin_border

    for idx_b, item in enumerate(suite["order_type_comparison"], start=13):
        p_v: ShortStrategyParams = item["params"]
        st_f = item["full_stats"]
        st_i = item["is_stats"]
        st_o = item["oos_stats"]
        vals = [
            ORDER_TYPE_LABELS_ZH.get(p_v.entry_order_type, p_v.entry_order_type),
            STOP_TYPE_LABELS_ZH.get(p_v.stop_order_type, p_v.stop_order_type),
            st_f["n_closed"],
            st_f["win_rate_pct"],
            st_f["payoff_ratio"] if st_f["payoff_ratio"] is not None else "N/A",
            st_f["ev_r"],
            st_f["total_return_pct"],
            st_f["max_dd_pct"],
            st_f["single_worst_pct"],
            st_i["ev_r"],
            st_o["ev_r"],
            st_o["total_return_pct"],
        ]
        for c_i, v in enumerate(vals, start=1):
            c = ws_sum.cell(row=idx_b, column=c_i, value=v)
            c.border = thin_border

    # Section C: Walk-Forward Fold Breakdown
    ws_sum["A24"] = "【C】Anchored Walk-Forward 錨定滾動前推驗證 (2018-2026 四個 2 年期樣本外視窗)"
    ws_sum["A24"].font = Font(name="Calibri", size=11, bold=True)
    sec_c_headers = [
        "滾動視窗 (WFO Fold)",
        "訓練期 (In-Sample Train)",
        "測試期 (Out-of-Sample Test)",
        "訓練期鄰域高原選出參數",
        "訓練期 E[R]",
        "訓練期高原分",
        "OOS 測試筆數",
        "OOS 勝率 (%)",
        "OOS 期望值 E[R]",
        "OOS 區間報酬 (%)",
        "OOS 最大回撤 (%)",
    ]
    for c_i, h in enumerate(sec_c_headers, start=1):
        cell = ws_sum.cell(row=25, column=c_i, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.border = thin_border

    for idx_f, fold in enumerate(suite["wfo_fold_results"], start=26):
        vals = [
            fold["fold_name"],
            fold["train_period"],
            fold["test_period"],
            fold["selected_params"].short_label(),
            fold["train_stats"]["ev_r"],
            fold["train_plateau"]["plateau_score"],
            fold["oos_stats"]["n_closed"],
            fold["oos_stats"]["win_rate_pct"],
            fold["oos_stats"]["ev_r"],
            fold["oos_stats"]["total_return_pct"],
            fold["oos_stats"]["max_dd_pct"],
        ]
        for c_i, v in enumerate(vals, start=1):
            c = ws_sum.cell(row=idx_f, column=c_i, value=v)
            c.border = thin_border

    for col_idx in range(1, 17):
        ws_sum.column_dimensions[get_column_letter(col_idx)].width = 22

    wb.save(out_xlsx)


def write_short_research_markdown(suite: dict[str, Any], out_md: Path) -> None:
    """Write comprehensive Markdown report on BTC Short-Selling Strategy, Order Mechanics, and Anti-Overfitting."""
    out_md.parent.mkdir(parents=True, exist_ok=True)
    plateau_p: ShortStrategyParams = suite["params_by_key"][suite["plateau_is_key"]]
    naive_p: ShortStrategyParams = suite["params_by_key"][suite["naive_is_key"]]
    spike_p: ShortStrategyParams = suite["params_by_key"][suite["naive_spike_key"]]
    mirror_p: ShortStrategyParams = suite["params_by_key"][suite["mirror_key"]]

    lines: list[str] = [
        "# BTC 右側做空策略研究報告：委託單別精確建模、全參數網格回測與防過擬合驗證",
        "",
        f"- **資料快照期間**：`{suite['date_range'][0]}` 至 `{suite['date_range'][1]}`（共 `{suite['n_daily_bars']}` 根日 K）",
        f"- **樣本內訓練期 (In-Sample, IS)**：`{suite['is_date_range'][0]}` 至 `{suite['is_date_range'][1]}`",
        f"- **樣本外測試期 (Out-of-Sample, OOS)**：`{suite['oos_date_range'][0]}` 至 `{suite['oos_date_range'][1]}`",
        f"- **全參數網格規模**：`{len(suite['grid']):,}` 組完整參數組合（7 個維度全排列）",
        "",
        "---",
        "",
        "## 1. 做空委託單別精確定義與撮合規則（限價單 vs. 市價單 vs. 觸價後交市價單）",
        "",
        "做空（Short Selling）與做多最大的結構差異在於**不對稱軋空風險（Upside Short Squeeze）**——下跌最深只能跌 `100%`，但單日暴漲軋空可以超過 `+25% ~ +40%`。因此回測引擎必須嚴格區分每一種委託單在「進場」、「正常停損」、「盤中極端軋空斷路器」與「停利」時的觸發條件、成交價公式、滑價（Slippage）與手續費（Maker vs. Taker）：",
        "",
        "### 1.1 空單進場（Short Entry）的三種委託方式",
        "",
        "| 進場委託類型 | 代號 | 掛單與觸發時機 | 成交價計算公式（含滑價） | 手續費率 | 優缺點與實盤特性 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
        "| **1. 隔日開盤市價單**<br>(Market Order at Next Open) | `MARKET_NEXT_OPEN` | 日線收盤 `Close[i]` 確認空頭型態後，於隔日開盤 `Open[i+1]` 直接以**市價賣出做空** | `Fill = Open[i+1] * (1 - 0.05%)` | **Taker `0.10%`** | **成交率 100%**，不會漏掉崩盤主跌段；但承擔 `0.05%` 賣出滑價與 Taker 費率。 |",
        "| **2. 跌破觸價後交市價單**<br>(Stop-Market Breakdown Order) | `STOP_MARKET_BREAKDOWN` | 日線收盤確認訊號後，在訊號日低點下方預掛 **Stop-Market 觸價賣單**：<br>`Trigger = Low[i] * 0.998`（有效 3 日） | 當盤中 `Low[d] <= Trigger` 時觸發市價單：<br>`Fill = min(Open[d], Trigger) * (1 - 0.08%)` | **Taker `0.10%`** | **二次確認跌破**才進場，可過濾隔日立刻反彈的假訊號；但突破瞬間流動性消耗大（滑價 `0.08%`）且入場價較低、離結構停損較遠。 |",
        "| **3. 反彈限價單**<br>(Limit Order on Pullback) | `LIMIT_PULLBACK` | 日線收盤確認訊號後，在收盤價與結構停損點之間 `35%` 反彈位預掛 **Limit 限價賣單**：<br>`Limit = Close[i] + 0.35 * (Stop - Close[i])`（有效 5 日） | 當盤中 `High[d] >= Limit`（且未先突破 `Stop`）時成交：<br>`Fill = max(Open[d], Limit)`（**0% 滑價**） | **Maker `0.04%`**<br>(若跳空高開則 Taker) | **入場均價最佳、離停損最近（賺賠比極高）**且享 Maker 低費率；但若行情直接一去不回頭崩盤，會**踏空（Miss Trade）**。 |",
        "",
        "### 1.2 空單停損、斷路器與停利回補（Buy-to-Cover Exit）的委託方式",
        "",
        "| 出場機制類型 | 委託單性質 | 觸發條件 | 實際回補成交價公式 | 手續費率 | 說明 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
        "| **A. 收盤結構/EMA 停損**<br>(`CLOSE_STRUCT_AND_EMA` / `CLOSE_STRUCT_ONLY`) | **隔日開盤市價單**<br>(Market Order) | 日 K 收盤價突破結構停損 `Close[d] > ActiveStop`，或連續 2 日收盤站回 `EMA(30/50)` 之上 | 隔日開盤買進回補：<br>`Fill = Open[d+1] * (1 + 0.05%)` | **Taker `0.10%`** | 避免被盤中上下插針（Wick/Scam Pump）洗出場，但必須搭配盤中斷路器防止單日暴漲。 |",
        "| **B. 盤中 10% 強制斷路器**<br>(Intraday 10% Circuit Breaker) | **盤中觸價後交市價單**<br>(Stop-Market Order) | 不論採用何種收盤停損，進場同時在交易所預掛硬性 **Stop-Market 買單** `CB_Price`（對應帳戶最大淨值風險 `10%`） | 一旦盤中 `High[d] >= CB_Price` 立即觸發市價強平：<br>`Fill = max(Open[d], CB_Price) * (1 + 0.08%)` | **Taker `0.10%`** | 鎖死單筆黑天鵝軋空極端虧損上限。 |",
        "| **C. 盤中結構直接觸價停損**<br>(`INTRADAY_STOP_MARKET`) | **盤中觸價後交市價單**<br>(Stop-Market Order) | 將結構停損價 `ActiveStop` 直接掛為盤中 **Stop-Market 觸價市價買單** | 一旦盤中 `High[d] >= ActiveStop` 立即觸發市價買回：<br>`Fill = max(Open[d], ActiveStop) * (1 + 0.08%)` | **Taker `0.10%`** | 不等收盤，盤中一摸到停損價立刻市價砍倉；不會有隔夜等待風險，但極易被比特幣常見的空頭插針洗出。 |",
        "| **D. 固定 R 倍數限價停利**<br>(`tp_limit_r = 2.5R / 3.5R / 5.0R`) | **盤中限價買單**<br>(Limit Order) | 進場後在下方預掛 **Limit 限價買回單**：<br>`TP_Price = Entry * (1 - k * StopDist%)` | 一旦盤中 `Low[d] <= TP_Price` 立即限價成交：<br>`Fill = min(Open[d], TP_Price)`（**0% 滑價**） | **Maker `0.04%`** | 逢低鎖定固定倍數獲利（Maker 低費率、零滑價），但會截斷熊市主跌段的長尾暴利。 |",
        "",
        "---",
        "",
        "## 2. 做空三大訊號模型 (`SH-1` / `SH-2` / `SH-3`) 與 7 維度全參數空間設計",
        "",
        "### 2.1 三大右側做空結構（嚴格因果確認，零未來函數）",
        "1. **`SH-1`（因果 Lower High + EMA 跌破/受阻轉弱）**：",
        "   - 過去 60 日內出現已因果確認（右5根K棒走完）的波段次高點 `Pivot High (H2 < H1)`，且反彈衰竭後收盤跌破 `EMA(30/50)`，當日為實體陰線（`Close < Open`）。",
        "   - **結構停損點**：`H2 + 0.25 * ATR14`。每個 `H2` 僅消耗一次，禁止重複進場。",
        "2. **`SH-2`（M 頭雙頂 Double Top / 頭肩頂 Head & Shoulders Top 頸線放量跌破）**：",
        "   - **M 頭**：兩個因果確認高點 `H1, H2` 價差在 `1.5 * ATR14` 內（或 `H2` 假突破後失敗），中間夾頸線低點 `Neckline = P_Low`，且 `H1` 與頸線振幅 `>= 2.0 * ATR14`。",
        "   - **頭肩頂**：三個因果確認高點滿足 `Head > Left Shoulder` 且 `Head > Right Shoulder`，右肩與左肩高度對稱（差距 `<= 2.0 * ATR14`），頸線取兩側低點較低者。",
        "   - **觸發條件**：收盤正式跌破頸線（`Close[i] < Neckline` 且前一日 `Close[i-1] >= Neckline`），伴隨空頭出量（`Volume[i] >= 1.10 * VolSMA20`）。",
        "3. **`SH-3`（Wyckoff 威科夫派發模型 `UTAD -> SOW -> LPSY`）**：",
        "   - 先創波段高點或假突破（`UTAD`）後跌破前波低點形成弱勢訊號（`SOW`），隨後無量弱勢反彈形成最後供應點（`LPSY < UTAD`），最後出量長黑跌破前日低點與 `EMA`。",
        "",
        "### 2.2 7 維度全參數網格範圍（共 `2,160` 組）",
        "- **`setup_filter` (5)**：`ALL`、`SH1_SH2`、`SH1_ONLY`、`SH2_ONLY`、`SH3_ONLY`",
        "- **`regime_filter` (3)**：",
        "  - `NONE`（無宏觀過濾，牛熊市皆可做空）",
        "  - `BELOW_SMA200D`（日線收盤 `< 200D SMA` 才允許做空）",
        "  - `WK_BEAR_50W`（週線連續 2 週收盤 `< 50W SMA` 進入週線級別熊市才允許做空）",
        "- **`entry_order_type` (3)**：`MARKET_NEXT_OPEN`（市價）、`STOP_MARKET_BREAKDOWN`（觸價後交市價）、`LIMIT_PULLBACK`（限價）",
        "- **`stop_order_type` (3)**：`CLOSE_STRUCT_AND_EMA`、`CLOSE_STRUCT_ONLY`、`INTRADAY_STOP_MARKET`",
        "- **`ema_period` (2)**：`30`、`50`",
        "- **`tp_limit_r` (4)**：`None`（不設固定停利，純靠因果 Lower High 與 EMA 移動停利）、`2.5R`、`3.5R`、`5.0R`",
        "- **`be_trigger_r` (2)**：`1.0R`、`1.5R`（浮盈達標後將停損下移至保本價 `Entry * (1 - 2*Fee)`）",
        "",
        "---",
        "",
        "## 3. 如何消除過擬合（Three-Layer Anti-Overfitting Protocol）",
        "",
        "如果直接在 `2014–2026` 全樣本跑完 2,160 組參數後挑「總報酬最高」或「E[R] 最高」的第 1 名，一定會挑到**孤立參數尖峰（Parameter Spike）**或**極少交易筆數的幸運偏差**。本研究實作三層防過擬合機制：",
        "",
        "1. **第一層：嚴格樣本內外切分（IS `2014–2020` vs. OOS `2021–2026`）**：",
        "   - 所有參數篩選僅能使用 `2014–2020` 訓練集（In-Sample），完全盲測 `2021–2026`（Out-of-Sample）。",
        "2. **第二層：漢明距離鄰域高原穩定度評分（Hamming-1 Neighborhood Plateau Filter）**：",
        "   - 對任一參數組合 $p$，找出在 7 個維度中「任意改變 1 個參數」的所有相鄰參數組合 $\\mathcal{N}_1(p)$（每個點有 14 個直接鄰居）。",
        "   - 要求訓練集交易筆數 `N >= 8`、自身與鄰居平均期望值 `Mean(EV_R) > 0.05R`、鄰居中至少 `70%` 為正期望值，並對「自身遠高於鄰居的孤立尖峰（Spike Penalty）」與「鄰居標準差」扣分。",
        "3. **第三層：Anchored Walk-Forward 錨定擴展滾動前推驗證（`2018–2026` 四個 2 年期視窗）**：",
        "   - 每 2 年重新用過去歷史（`< t`）選出高原第 1 名參數，直接拿去交易未來 2 年（`2018–2019`、`2020–2021`、`2022–2023`、`2024–2026`），將 4 段完全未知的樣本外交易拼接檢驗。",
        "",
        "### 3.1 過擬合選參 vs. 鄰域高原防過擬合選參 實證對比表",
        "",
        "| 選參策略類型 | 選出之參數組合 | IS 筆數<br>(14-20) | IS 期望值<br>`E[R]` | IS 總報酬 | **OOS 筆數<br>(21-26)** | **OOS 勝率** | **OOS 期望值<br>`E[R]`** | **OOS 總報酬** | **OOS 最大回撤** | 全樣本總報酬<br>(14-26) |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]

    for label_m, k_m in [
        ("**1. 盲目最高報酬過擬合**<br>(Naive IS Max-Return)", suite["naive_is_key"]),
        ("**2. 盲目最高單筆 E[R] 尖峰**<br>(Naive IS Spike)", suite["naive_spike_key"]),
        ("**3. 鄰域高原穩健首選**<br>(Plateau-Selected Robust)", suite["plateau_is_key"]),
        ("**4. 零調參多頭直接鏡像**<br>(Symmetric Mirror Baseline)", suite["mirror_key"]),
    ]:
        p_m = suite["params_by_key"][k_m]
        st_is = suite["is_stats_by_key"][k_m]
        st_oos = suite["oos_stats_by_key"][k_m]
        st_full = suite["full_stats_by_key"][k_m]
        lines.append(
            f"| {label_m} | `{p_m.short_label()}` | {st_is['n_closed']} | `{st_is['ev_r']:+.3f}R` | "
            f"`{st_is['total_return_pct']:+.2f}%` | **{st_oos['n_closed']}** | **{st_oos['win_rate_pct']:.1f}%** | "
            f"**`{st_oos['ev_r']:+.3f}R`** | **`{st_oos['total_return_pct']:+.2f}%`** | "
            f"**`{st_oos['max_dd_pct']:.2f}%`** | `{st_full['total_return_pct']:+.2f}%` |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 4. 三種進場委託單 × 三種停損委託單 控制變數實證比較",
        "",
        f"固定穩健高原策略的核心過濾條件（`Setup={plateau_p.setup_filter}`, `Regime={plateau_p.regime_filter}`, `EMA={plateau_p.ema_period}`, `TP={plateau_p.tp_limit_r}`, `BE={plateau_p.be_trigger_r}R`），單獨替換 **3 種進場單別** 與 **3 種停損單別**（3×3 矩陣）：",
        "",
        "| 進場委託單別 (Entry Order) | 停損委託單別 (Stop Order) | 全樣本筆數 | 全樣本勝率 | 賺賠比 | 全樣本 `E[R]` | 全樣本總報酬 | 最大回撤 | OOS `E[R]` (21-26) | OOS 報酬 (21-26) |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ])

    for item in suite["order_type_comparison"]:
        p_v: ShortStrategyParams = item["params"]
        st_f = item["full_stats"]
        st_o = item["oos_stats"]
        pr_str = f"{st_f['payoff_ratio']:.2f}" if st_f["payoff_ratio"] is not None else "N/A"
        lines.append(
            f"| `{p_v.entry_order_type}` | `{p_v.stop_order_type}` | {st_f['n_closed']} | {st_f['win_rate_pct']:.1f}% | "
            f"{pr_str} | `{st_f['ev_r']:+.3f}R` | `{st_f['total_return_pct']:+.2f}%` | `{st_f['max_dd_pct']:.2f}%` | "
            f"`{st_o['ev_r']:+.3f}R` | `{st_o['total_return_pct']:+.2f}%` |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 5. 全網格 2,160 組參數各維度邊際效應分析（Marginal Effect across Full Grid）",
        "",
    ])

    for dim_name, rows_dim in suite["dimension_marginals"].items():
        lines.append(f"### 維度：`{dim_name}`")
        lines.append("")
        lines.append("| 參數值 | 有效組合數 | 平均交易筆數 | 平均勝率 | 全樣本平均 `E[R]` | OOS 平均 `E[R]` | 全樣本平均報酬 | 全樣本平均最大回撤 | 正期望值組合佔比 |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for r in rows_dim:
            lines.append(
                f"| `{r['value']}` | {r['n_combos']} | {r['mean_trades']:.1f} | {r['mean_win_rate_pct']:.1f}% | "
                f"`{r['mean_full_ev_r']:+.3f}R` | `{r['mean_oos_ev_r']:+.3f}R` | `{r['mean_full_return_pct']:+.2f}%` | "
                f"`{r['mean_full_max_dd_pct']:.2f}%` | `{r['positive_ev_Ratio_pct']:.1f}%` |"
            )
        lines.append("")

    wfo_sum = suite["wfo_stitched_summary"]
    lines.extend([
        "---",
        "",
        "## 6. Anchored Walk-Forward 錨定滾動前推驗證結果（`2018–2026` 純樣本外表現）",
        "",
        "| 滾動視窗 (Fold) | 訓練期 (In-Sample) | 測試期 (Out-of-Sample) | 訓練期高原選出參數 | 訓練期 `E[R]` | OOS 筆數 | OOS 勝率 | OOS `E[R]` | OOS 報酬 | OOS 最大回撤 |",
        "| :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
    ])
    for fold in suite["wfo_fold_results"]:
        lines.append(
            f"| **{fold['fold_name']}** | `{fold['train_period']}` | `{fold['test_period']}` | "
            f"`{fold['selected_params'].short_label()}` | `{fold['train_stats']['ev_r']:+.3f}R` | "
            f"{fold['oos_stats']['n_closed']} | {fold['oos_stats']['win_rate_pct']:.1f}% | "
            f"`{fold['oos_stats']['ev_r']:+.3f}R` | `{fold['oos_stats']['total_return_pct']:+.2f}%` | "
            f"`{fold['oos_stats']['max_dd_pct']:.2f}%` |"
        )
    lines.extend([
        "",
        f"- **2018–2026 四個視窗純樣本外拼接總結（Stitched Walk-Forward OOS）**：",
        f"  - 總樣本外交易筆數：`{wfo_sum['n_closed']}` 筆",
        f"  - 樣本外勝率：`{wfo_sum['win_rate_pct']:.2f}%`",
        f"  - 樣本外賺賠比（Payoff Ratio）：`{wfo_sum['payoff_ratio']}`",
        f"  - 樣本外單筆期望值（`E[R]`）：`{wfo_sum['ev_r']:+.4f}R`",
        f"  - 樣本外複利累積總報酬：`{wfo_sum['compounded_return_pct']:+.2f}%`",
        f"  - 樣本外最大回撤（Trade-to-Trade Max DD）：`{wfo_sum['max_dd_trade_pct']:.2f}%`",
        f"  - 獲利因子（Profit Factor）：`{wfo_sum['profit_factor']}`",
        "",
    ])

    out_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run BTC short-selling strategy research and anti-overfitting suite.")
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=Path("data/snapshots/latest"),
        help="Path to verified snapshot directory",
    )
    parser.add_argument(
        "--out-xlsx",
        type=Path,
        default=Path("reports/btc_short_research_and_trades_2014_2026.xlsx"),
        help="Output Excel workbook path",
    )
    parser.add_argument(
        "--out-md",
        type=Path,
        default=Path("reports/btc_short_strategy_research_2026-11-14.md"),
        help="Output Markdown report path",
    )
    args = parser.parse_args()

    suite = run_short_research_suite(args.snapshot_dir)
    export_short_research_excel(suite, args.out_xlsx)
    write_short_research_markdown(suite, args.out_md)

    plateau_k = suite["plateau_is_key"]
    naive_k = suite["naive_is_key"]
    spike_k = suite["naive_spike_key"]
    mirror_k = suite["mirror_key"]

    print(f"Completed grid search across {len(suite['grid'])} parameter combinations.")
    print("1. Naive IS Max-Return Overfit:", suite["params_by_key"][naive_k].short_label())
    print("   IS:", suite["is_stats_by_key"][naive_k])
    print("   OOS:", suite["oos_stats_by_key"][naive_k])
    print("2. Naive IS Spike Overfit:", suite["params_by_key"][spike_k].short_label())
    print("   IS:", suite["is_stats_by_key"][spike_k])
    print("   OOS:", suite["oos_stats_by_key"][spike_k])
    print("3. Plateau-Selected Robust Winner:", suite["params_by_key"][plateau_k].short_label())
    print("   IS:", suite["is_stats_by_key"][plateau_k])
    print("   OOS:", suite["oos_stats_by_key"][plateau_k])
    print("   FULL:", suite["full_stats_by_key"][plateau_k])
    print("4. Symmetric Mirror Baseline:", suite["params_by_key"][mirror_k].short_label())
    print("   IS:", suite["is_stats_by_key"][mirror_k])
    print("   OOS:", suite["oos_stats_by_key"][mirror_k])
    print("   FULL:", suite["full_stats_by_key"][mirror_k])
    print("5. Walk-Forward Stitched OOS (2018-2026):", suite["wfo_stitched_summary"])
    print(f"Saved Excel to {args.out_xlsx} and Markdown report to {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
