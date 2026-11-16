"""Phase 2: Track B (Capital_B) daily right-side swing setups (SW-1/2/3),
expanding P99 downside tail gap buffer, position sizing with circuit-breaker floor, and B1-B7b decision rules.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any, Sequence

from scripts.btc_bottom.indicators import Pivot

FEE_ONEWAY = 0.001


@dataclass(frozen=True)
class SwingSetupCandidate:
    setup_id: str  # "SW-1" | "SW-2" | "SW-3"
    subtype: str  # "HL_EMA50" | "DOUBLE_BOTTOM" | "INV_HS" | "WYCKOFF_LPS"
    signal_idx: int
    structural_stop: float
    entry_anchor_low: float
    stop_dist_to_close: float
    consumed_pivot_idx: int | None = None


@dataclass(frozen=True)
class PositionSizingResult:
    valid: bool
    rejected_reason: str | None
    risk_budget_eff: float | None
    position_size_pct: float | None
    entry_ref_price: float | None
    structural_stop: float | None
    stop_dist_pct: float | None
    gap_buffer_pct: float | None
    circuit_breaker_price: float | None


def compute_expanding_gap_buffers(
    daily_bars: Sequence[Any],
    atr14: Sequence[float | None],
) -> list[float]:
    """Maintain sorted list tail_mults via bisect.insort over u in [15..i] of
    max(0.0, (Close[u-1]-Close[u])/Close[u-1]) / max(1e-6, ATR14[u-1]/Close[u-1]).
    For len < 90 use prior 1.5, else 0.99*(len-1) linear percentile.
    gap_buffer_pct[i] = max(0.02, mult_i * (ATR14[i] / Close[i])).
    """
    n = len(daily_bars)
    out: list[float] = [0.02] * n
    tail_mults: list[float] = []

    for i in range(n):
        if i >= 15 and atr14[i - 1] is not None:
            prev_c = float(daily_bars[i - 1].close)
            cur_c = float(daily_bars[i].close)
            prev_atr = float(atr14[i - 1])
            if prev_c > 0.0:
                neg_ret = max(0.0, (prev_c - cur_c) / prev_c)
                atr_ratio_prev = max(1e-6, prev_atr / prev_c)
                bisect.insort(tail_mults, neg_ret / atr_ratio_prev)

        if len(tail_mults) < 90:
            mult_i = 1.5
        else:
            pos = 0.99 * (len(tail_mults) - 1)
            lo = int(math.floor(pos))
            hi = int(math.ceil(pos))
            frac = pos - lo
            mult_i = tail_mults[lo] * (1.0 - frac) + tail_mults[hi] * frac

        cur_c = float(daily_bars[i].close)
        cur_atr = atr14[i]
        if cur_atr is not None and cur_c > 0.0:
            out[i] = max(0.02, mult_i * (float(cur_atr) / cur_c))
        else:
            out[i] = 0.02
    return out


def _detect_sw1(
    daily_bars: Sequence[Any],
    i: int,
    ema50: Sequence[float | None],
    atr14_i: float,
    confirmed_lows: Sequence[Pivot],
    consumed_sw1_pivots: set[int],
) -> SwingSetupCandidate | None:
    if len(confirmed_lows) < 2:
        return None
    p_cur = confirmed_lows[-1]
    p_prev = confirmed_lows[-2]
    if p_cur.price <= p_prev.price:
        return None
    if i - p_cur.confirm_idx > 25:
        return None
    if p_cur.idx in consumed_sw1_pivots:
        return None

    # All daily closes in [p_cur.idx + 1 .. i] must be >= p_cur.price
    for u in range(p_cur.idx + 1, i + 1):
        if float(daily_bars[u].close) < p_cur.price:
            return None

    bar_i = daily_bars[i]
    bar_prev = daily_bars[i - 1]
    close_i = float(bar_i.close)
    open_i = float(bar_i.open)
    low_i = float(bar_i.low)
    close_prev = float(bar_prev.close)
    high_prev = float(bar_prev.high)
    ema_i = float(ema50[i])  # caller verified non-None
    ema_prev = float(ema50[i - 1])

    if not (close_i > open_i and close_i > high_prev):
        return None

    cross_up = close_prev <= ema_prev and close_i > ema_i
    pullback_hold = low_i <= 1.015 * ema_i and close_i > ema_i
    if not (cross_up or pullback_hold):
        return None

    s_stop = p_cur.price - 0.25 * atr14_i
    dist_c = (close_i - s_stop) / close_i
    if not (0.0 < dist_c <= 0.25):
        return None

    return SwingSetupCandidate(
        setup_id="SW-1",
        subtype="HL_EMA50",
        signal_idx=i,
        structural_stop=s_stop,
        entry_anchor_low=p_cur.price,
        stop_dist_to_close=dist_c,
        consumed_pivot_idx=p_cur.idx,
    )


def _detect_sw2(
    daily_bars: Sequence[Any],
    i: int,
    atr14_i: float,
    vol_sma20_i: float,
    confirmed_lows: Sequence[Pivot],
    confirmed_highs: Sequence[Pivot],
) -> SwingSetupCandidate | None:
    bar_i = daily_bars[i]
    close_i = float(bar_i.close)
    vol_i = float(bar_i.volume)
    if vol_i <= 1.10 * vol_sma20_i:
        return None

    min_idx = max(0, i - 120)
    lows_win = [p for p in confirmed_lows if p.idx >= min_idx and p.confirm_idx <= i]
    highs_win = [p for p in confirmed_highs if p.idx >= min_idx and p.confirm_idx <= i]

    # Candidate tuple: (right_idx, span, is_inv_hs_priority, candidate)
    matches: list[tuple[int, int, int, SwingSetupCandidate]] = []

    # 1. Double Bottom (L1, L2)
    n_lows = len(lows_win)
    for a in range(n_lows):
        l1 = lows_win[a]
        for b in range(a + 1, n_lows):
            l2 = lows_win[b]
            span = l2.idx - l1.idx
            if span < 10:
                continue
            min_l = min(l1.price, l2.price)
            if min_l <= 0.0 or abs(l1.price - l2.price) / min_l > 0.05:
                continue
            mid_highs = [h.price for h in highs_win if l1.idx < h.idx < l2.idx]
            if not mid_highs:
                continue
            neckline = max(mid_highs)
            if close_i <= neckline:
                continue
            # L1.idx..i no close < min(L1.price, L2.price)
            if any(float(daily_bars[u].close) < min_l for u in range(l1.idx, i + 1)):
                continue
            # L2.idx+1..i-1 no close > Neckline (first breakout on bar i)
            if any(float(daily_bars[u].close) > neckline for u in range(l2.idx + 1, i)):
                continue
            s_stop = l2.price - 0.25 * atr14_i
            dist_c = (close_i - s_stop) / close_i
            if 0.0 < dist_c <= 0.25:
                matches.append(
                    (
                        l2.idx,
                        span,
                        0,
                        SwingSetupCandidate(
                            setup_id="SW-2",
                            subtype="DOUBLE_BOTTOM",
                            signal_idx=i,
                            structural_stop=s_stop,
                            entry_anchor_low=l2.price,
                            stop_dist_to_close=dist_c,
                        ),
                    )
                )

    # 2. Inverse Head & Shoulders (LS, Head, RS)
    if n_lows >= 3:
        for a in range(n_lows - 2):
            ls = lows_win[a]
            for b in range(a + 1, n_lows - 1):
                head = lows_win[b]
                if head.idx - ls.idx < 8:
                    continue
                for c in range(b + 1, n_lows):
                    rs = lows_win[c]
                    if rs.idx - head.idx < 8:
                        continue
                    min_sh = min(ls.price, rs.price)
                    if min_sh <= 0.0:
                        continue
                    if head.price >= min_sh * 0.975:
                        continue
                    if abs(ls.price - rs.price) / min_sh > 0.065:
                        continue
                    peaks1 = [h.price for h in highs_win if ls.idx < h.idx < head.idx]
                    peaks2 = [h.price for h in highs_win if head.idx < h.idx < rs.idx]
                    if not peaks1 or not peaks2:
                        continue
                    neckline = max(max(peaks1), max(peaks2))
                    if close_i <= neckline:
                        continue
                    # Head.idx..i no close < Head.price and RS.idx..i no close < RS.price
                    if any(float(daily_bars[u].close) < head.price for u in range(head.idx, i + 1)):
                        continue
                    if any(float(daily_bars[u].close) < rs.price for u in range(rs.idx, i + 1)):
                        continue
                    # RS.idx+1..i-1 no close > Neckline
                    if any(float(daily_bars[u].close) > neckline for u in range(rs.idx + 1, i)):
                        continue
                    s_stop = rs.price - 0.25 * atr14_i
                    dist_c = (close_i - s_stop) / close_i
                    if 0.0 < dist_c <= 0.25:
                        matches.append(
                            (
                                rs.idx,
                                rs.idx - ls.idx,
                                1,
                                SwingSetupCandidate(
                                    setup_id="SW-2",
                                    subtype="INV_HS",
                                    signal_idx=i,
                                    structural_stop=s_stop,
                                    entry_anchor_low=rs.price,
                                    stop_dist_to_close=dist_c,
                                ),
                            )
                        )

    if not matches:
        return None
    matches.sort(key=lambda m: (m[0], m[1], m[2]))
    return matches[-1][3]


def _detect_sw3(
    daily_bars: Sequence[Any],
    i: int,
    atr14_i: float,
    vol_sma20: Sequence[float | None],
    confirmed_lows: Sequence[Pivot],
    confirmed_highs: Sequence[Pivot],
) -> SwingSetupCandidate | None:
    bar_i = daily_bars[i]
    close_i = float(bar_i.close)
    open_i = float(bar_i.open)
    close_prev = float(daily_bars[i - 1].close)
    if not (close_i > open_i and close_i > close_prev):
        return None

    min_idx = max(0, i - 90)
    sc_candidates = [p for p in confirmed_lows if min_idx <= p.idx <= i - 15 and p.confirm_idx <= i]
    ar_candidates = [
        p
        for p in confirmed_highs
        if min_idx <= p.idx <= i - 5 and p.confirm_idx < i - 1 and p.confirm_idx <= i
    ]
    if not sc_candidates or not ar_candidates:
        return None

    matches: list[tuple[int, int, SwingSetupCandidate]] = []

    for sc in sc_candidates:
        if sc.price <= 0.0:
            continue
        floor_85 = 0.85 * sc.price
        spring_thresh = 0.995 * sc.price
        for ar in ar_candidates:
            if ar.idx <= sc.idx:
                continue
            if (ar.price - sc.price) / sc.price < 0.12:
                continue
            # [SC.idx .. AR.confirm_idx] no close < 0.85 * SC.price
            if any(float(daily_bars[u].close) < floor_85 for u in range(sc.idx, ar.confirm_idx + 1)):
                continue

            # Find earliest Spring day s in [AR.confirm_idx .. i - 2]
            s_rec: int | None = None
            for s in range(ar.confirm_idx, i - 1):
                low_s = float(daily_bars[s].low)
                if floor_85 <= low_s < spring_thresh:
                    if float(daily_bars[s].close) > sc.price:
                        cand_rec = s
                    elif s + 1 <= i - 1 and float(daily_bars[s + 1].close) > sc.price:
                        cand_rec = s + 1
                    else:
                        continue
                    if all(float(daily_bars[u].close) >= floor_85 for u in range(ar.confirm_idx, cand_rec + 1)):
                        s_rec = cand_rec
                        break
            if s_rec is None:
                continue

            # Find earliest SOS day q in [max(19, s_rec + 1) .. i - 1]
            q_found: int | None = None
            for q in range(max(19, s_rec + 1), i):
                v_sma_q = vol_sma20[q]
                if v_sma_q is None:
                    continue
                if float(daily_bars[q].close) > ar.price and float(daily_bars[q].volume) > 1.20 * float(v_sma_q):
                    if all(float(daily_bars[u].close) >= floor_85 for u in range(s_rec, q + 1)):
                        q_found = q
                        break
            if q_found is None:
                continue

            # Verify LPS at day i
            if not (1 <= i - q_found <= 15):
                continue
            lps_lows = [float(daily_bars[u].low) for u in range(q_found + 1, i + 1)]
            min_lps_low = min(lps_lows)
            if min_lps_low > 1.02 * ar.price:
                continue
            box_mid = 0.5 * (sc.price + ar.price)
            if any(float(daily_bars[u].close) < box_mid for u in range(q_found + 1, i + 1)):
                continue

            s_stop = min(min_lps_low, box_mid) - 0.25 * atr14_i
            dist_c = (close_i - s_stop) / close_i
            if 0.0 < dist_c <= 0.25:
                matches.append(
                    (
                        ar.idx,
                        sc.idx,
                        SwingSetupCandidate(
                            setup_id="SW-3",
                            subtype="WYCKOFF_LPS",
                            signal_idx=i,
                            structural_stop=s_stop,
                            entry_anchor_low=min_lps_low,
                            stop_dist_to_close=dist_c,
                        ),
                    )
                )

    if not matches:
        return None
    matches.sort(key=lambda m: (m[0], m[1]))
    return matches[-1][2]


def detect_causal_sw_setups(
    daily_bars: Sequence[Any],
    i: int,
    *,
    ema50: Sequence[float | None],
    atr14: Sequence[float | None],
    vol_sma20: Sequence[float | None],
    confirmed_lows: Sequence[Pivot],
    confirmed_highs: Sequence[Pivot],
    consumed_sw1_pivots: set[int],
) -> tuple[SwingSetupCandidate | None, list[str]]:
    """Detect SW-1, SW-2, SW-3 on bar i >= 49.

    Returns (primary_candidate_by_priority_SW3_SW2_SW1, active_setups_on_bar).
    """
    if i < 49 or i >= len(daily_bars):
        return None, []
    if (
        ema50[i - 1] is None
        or ema50[i] is None
        or atr14[i] is None
        or vol_sma20[i] is None
    ):
        return None, []

    atr_i = float(atr14[i])
    vsma_i = float(vol_sma20[i])

    c_sw3 = _detect_sw3(daily_bars, i, atr_i, vol_sma20, confirmed_lows, confirmed_highs)
    c_sw2 = _detect_sw2(daily_bars, i, atr_i, vsma_i, confirmed_lows, confirmed_highs)
    c_sw1 = _detect_sw1(daily_bars, i, ema50, atr_i, confirmed_lows, consumed_sw1_pivots)

    active_ids: list[str] = []
    primary: SwingSetupCandidate | None = None
    for cand in (c_sw3, c_sw2, c_sw1):
        if cand is not None:
            active_ids.append(cand.setup_id)
            if primary is None:
                primary = cand
    return primary, active_ids


def compute_position_sizing(
    *,
    entry_price: float,
    structural_stop: float,
    atr14_i: float,
    gap_buffer_pct_i: float,
    risk_budget_eff: float,
    fee_oneway: float = FEE_ONEWAY,
) -> PositionSizingResult:
    """Compute tail-buffered position size and circuit breaker price with defensive rejection guards."""
    if entry_price <= 0.0 or entry_price <= structural_stop:
        return PositionSizingResult(
            valid=False,
            rejected_reason="ENTRY_BELOW_OR_EQUAL_STRUCTURAL_STOP",
            risk_budget_eff=None,
            position_size_pct=None,
            entry_ref_price=None,
            structural_stop=None,
            stop_dist_pct=None,
            gap_buffer_pct=None,
            circuit_breaker_price=None,
        )

    stop_dist_pct = max(
        0.03,
        1.5 * atr14_i / entry_price,
        (entry_price - structural_stop) / entry_price,
    )
    if stop_dist_pct > 0.25:
        return PositionSizingResult(
            valid=False,
            rejected_reason="STOP_DIST_EXCEEDS_25PCT",
            risk_budget_eff=None,
            position_size_pct=None,
            entry_ref_price=None,
            structural_stop=None,
            stop_dist_pct=None,
            gap_buffer_pct=None,
            circuit_breaker_price=None,
        )
    if stop_dist_pct + gap_buffer_pct_i >= 0.75:
        return PositionSizingResult(
            valid=False,
            rejected_reason="TOTAL_BUFFER_EXCEEDS_75PCT",
            risk_budget_eff=None,
            position_size_pct=None,
            entry_ref_price=None,
            structural_stop=None,
            stop_dist_pct=None,
            gap_buffer_pct=None,
            circuit_breaker_price=None,
        )

    if risk_budget_eff <= 2.0 * fee_oneway:
        return PositionSizingResult(
            valid=False,
            rejected_reason="ZERO_RISK_BUDGET",
            risk_budget_eff=0.0,
            position_size_pct=None,
            entry_ref_price=None,
            structural_stop=None,
            stop_dist_pct=None,
            gap_buffer_pct=None,
            circuit_breaker_price=None,
        )

    max_notional_pct = 1.0 / (1.0 + fee_oneway)
    raw_size_pct = (risk_budget_eff - 2.0 * fee_oneway) / (stop_dist_pct + gap_buffer_pct_i)
    pos_size_pct = min(max_notional_pct, raw_size_pct)
    cb_price = entry_price * (1.0 - (risk_budget_eff - 2.0 * fee_oneway) / pos_size_pct)

    return PositionSizingResult(
        valid=True,
        rejected_reason=None,
        risk_budget_eff=risk_budget_eff,
        position_size_pct=pos_size_pct,
        entry_ref_price=entry_price,
        structural_stop=structural_stop,
        stop_dist_pct=stop_dist_pct,
        gap_buffer_pct=gap_buffer_pct_i,
        circuit_breaker_price=cb_price,
    )


def evaluate_track_b_action(
    *,
    in_position: bool,
    pending_exit_next_open: bool,
    wk_warn_50w_active: bool,
    n_active: int,
    ev_r_active: float,
    ev_r_inactive: float,
    primary_setup: SwingSetupCandidate | None,
    same_day_exit_cooldown: bool,
    entry_ref_price: float,
    atr14_i: float | None,
    gap_buffer_pct_i: float,
    open_position_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate Capital_B decision across the 8 mutually exclusive rules B1-B7b."""
    # B1 & B2: Currently holding a position
    if in_position:
        info = open_position_info or {}
        action = "EXIT_NEXT_OPEN" if pending_exit_next_open else "HOLD_POSITION"
        return {
            "rule_id": "B1" if pending_exit_next_open else "B2",
            "action": action,
            "rejected_reason": None,
            "risk_budget_eff": info.get("risk_budget_eff"),
            "position_size_pct": info.get("position_size_pct"),
            "entry_ref_price": info.get("entry_price"),
            "structural_stop": info.get("structural_stop"),
            "active_stop": info.get("active_stop"),
            "circuit_breaker_price": info.get("circuit_breaker_price"),
            "gap_buffer_pct": info.get("gap_buffer_pct"),
        }

    # Flat (not in position): Determine WK-Warn causal risk budget
    if not wk_warn_50w_active:
        standby_budget = 0.10
        is_b5_blocked = False
        is_b7a_reduced = False
    else:
        if n_active >= 15 and ev_r_active <= 0.0:
            standby_budget = 0.0
            is_b5_blocked = True
            is_b7a_reduced = False
        elif n_active < 15 or (
            ev_r_active > 0.0
            and (ev_r_active <= 0.5 * max(0.0, ev_r_inactive) or ev_r_active < 0.15)
        ):
            standby_budget = 0.05
            is_b5_blocked = False
            is_b7a_reduced = True
        else:
            standby_budget = 0.10
            is_b5_blocked = False
            is_b7a_reduced = False

    # B5: Empty & WK-Warn ACTIVE & N_active >= 15 & EV_R_active <= 0.0 -> WATCHLIST_BLOCKED
    if is_b5_blocked:
        return {
            "rule_id": "B5",
            "action": "WATCHLIST_BLOCKED",
            "rejected_reason": "WK_WARN_NEGATIVE_EV_BLOCKED" if primary_setup is not None else None,
            "risk_budget_eff": 0.0,
            "position_size_pct": None,
            "entry_ref_price": None,
            "structural_stop": None,
            "active_stop": None,
            "circuit_breaker_price": None,
            "gap_buffer_pct": None,
        }

    # Check if there is a valid trigger today
    if same_day_exit_cooldown:
        return {
            "rule_id": "B6" if wk_warn_50w_active else "B3",
            "action": "WAIT_SIGNAL",
            "rejected_reason": "SAME_DAY_EXIT_COOLDOWN" if primary_setup is not None else None,
            "risk_budget_eff": standby_budget,
            "position_size_pct": None,
            "entry_ref_price": None,
            "structural_stop": None,
            "active_stop": None,
            "circuit_breaker_price": None,
            "gap_buffer_pct": None,
        }

    if primary_setup is None or atr14_i is None:
        return {
            "rule_id": "B6" if wk_warn_50w_active else "B3",
            "action": "WAIT_SIGNAL",
            "rejected_reason": None,
            "risk_budget_eff": standby_budget,
            "position_size_pct": None,
            "entry_ref_price": None,
            "structural_stop": None,
            "active_stop": None,
            "circuit_breaker_price": None,
            "gap_buffer_pct": None,
        }

    sizing = compute_position_sizing(
        entry_price=entry_ref_price,
        structural_stop=primary_setup.structural_stop,
        atr14_i=atr14_i,
        gap_buffer_pct_i=gap_buffer_pct_i,
        risk_budget_eff=standby_budget,
    )
    if not sizing.valid:
        return {
            "rule_id": "B6" if wk_warn_50w_active else "B3",
            "action": "WAIT_SIGNAL",
            "rejected_reason": sizing.rejected_reason,
            "risk_budget_eff": standby_budget,
            "position_size_pct": None,
            "entry_ref_price": None,
            "structural_stop": None,
            "active_stop": None,
            "circuit_breaker_price": None,
            "gap_buffer_pct": None,
        }

    if not wk_warn_50w_active:
        rule_id = "B4"
        action = "BUY_NEXT_OPEN"
    elif is_b7a_reduced:
        rule_id = "B7a"
        action = "BUY_REDUCED_RISK"
    else:
        rule_id = "B7b"
        action = "BUY_NEXT_OPEN"

    return {
        "rule_id": rule_id,
        "action": action,
        "rejected_reason": None,
        "risk_budget_eff": sizing.risk_budget_eff,
        "position_size_pct": round(float(sizing.position_size_pct) * 100.0, 4),
        "entry_ref_price": round(float(sizing.entry_ref_price), 2),
        "structural_stop": round(float(sizing.structural_stop), 2),
        "active_stop": round(float(sizing.structural_stop), 2),
        "circuit_breaker_price": round(float(sizing.circuit_breaker_price), 2),
        "gap_buffer_pct": round(float(sizing.gap_buffer_pct) * 100.0, 4),
    }
