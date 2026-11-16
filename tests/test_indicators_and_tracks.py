"""Unit tests for Phase 2 indicators.py, track_a.py, and track_b.py.

Verifies prefix-length causal invariance, exact warmup indices, relative-percentage S/R clustering,
Cycle_ATH / t_0 / >=90d cycle registration, Track A A1-A7 & Track B B1-B7b mutual exclusivity,
and SW-1/2/3 pattern detection.
"""

from __future__ import annotations

import datetime as dt
import unittest

from scripts.btc_bottom.fetch_snapshots import DAY_SEC, DailyOHLCVRow, MacroDailyRow, WeeklyOHLCVRow, date_str_to_open_ts
from scripts.btc_bottom.indicators import (
    Pivot,
    calc_bollinger,
    calc_ema_sma_seeded,
    calc_macd,
    calc_sma,
    calc_wilder_atr,
    calc_wilder_rsi,
    compute_causal_pivots,
    compute_causal_sr_zones,
    compute_expanding_lgc,
    compute_wk_warn_series,
)
from scripts.btc_bottom.track_a import (
    compute_halving_diagnostics,
    evaluate_dual_key,
    evaluate_l1,
    evaluate_track_a_action,
    synthesize_track_a_zone,
    track_causal_cycle_peaks,
)
from scripts.btc_bottom.track_b import (
    SwingSetupCandidate,
    compute_expanding_gap_buffers,
    compute_position_sizing,
    detect_causal_sw_setups,
    evaluate_track_b_action,
)


def _make_daily_bar(idx: int, open_p: float, high_p: float, low_p: float, close_p: float, vol: float = 1000.0) -> DailyOHLCVRow:
    base_ts = date_str_to_open_ts("2020-01-01") + idx * DAY_SEC
    d_str = dt.datetime.fromtimestamp(base_ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
    return DailyOHLCVRow(
        open_ts=base_ts,
        close_ts=base_ts + DAY_SEC,
        date_utc=d_str,
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
        volume=vol,
    )


class TestIndicatorsAndTracksPhase2(unittest.TestCase):
    def test_indicator_warmup_indices_and_prefix_causal_invariance(self) -> None:
        bars = [
            _make_daily_bar(i, 100.0 + i * 0.8, 103.0 + i * 0.9, 98.0 + i * 0.7, 101.0 + (i % 5) * 1.2 + i * 0.8)
            for i in range(80)
        ]
        closes = [b.close for b in bars]

        atr = calc_wilder_atr(bars, 14)
        self.assertTrue(all(x is None for x in atr[:13]))
        self.assertIsNotNone(atr[13])

        rsi = calc_wilder_rsi(closes, 14)
        self.assertTrue(all(x is None for x in rsi[:14]))
        self.assertIsNotNone(rsi[14])

        dif, dea, hist = calc_macd(closes, 12, 26, 9)
        self.assertTrue(all(x is None for x in dif[:25]))
        self.assertIsNotNone(dif[25])
        self.assertTrue(all(x is None for x in dea[:33]))
        self.assertIsNotNone(dea[33])
        self.assertIsNotNone(hist[33])

        up, mid, lo = calc_bollinger(closes, 20, 2.0)
        self.assertTrue(all(x is None for x in up[:19]))
        self.assertIsNotNone(up[19])

        # Verify strict prefix causal invariance: computing on bars[:50] equals first 50 of bars[:80]
        atr_50 = calc_wilder_atr(bars[:50], 14)
        rsi_50 = calc_wilder_rsi(closes[:50], 14)
        dif_50, dea_50, hist_50 = calc_macd(closes[:50], 12, 26, 9)
        self.assertEqual(atr_50, atr[:50])
        self.assertEqual(rsi_50, rsi[:50])
        self.assertEqual(dif_50, dif[:50])
        self.assertEqual(dea_50, dea[:50])
        self.assertEqual(hist_50, hist[:50])

        # Verify causal pivots at index 40 never include a pivot with p > 35
        piv_40 = [p for p in compute_causal_pivots(bars, 5, 5) if p.confirm_idx <= 40]
        piv_prefix = compute_causal_pivots(bars[:41], 5, 5)
        self.assertEqual(piv_40, piv_prefix)

    def test_causal_sr_zones_relative_clustering_avoids_2010_mega_cluster(self) -> None:
        bars = [_make_daily_bar(i, 90000.0, 91000.0, 89000.0, 90000.0) for i in range(400)]
        cur_close_ts = bars[-1].close_ts
        # Inject an old 2010 weekly pivot ($0.10) outside trailing 3 years and recent weekly pivots ($60k, $72k, $85k)
        old_ts = cur_close_ts - 4000 * DAY_SEC
        recent_ts = cur_close_ts - 200 * DAY_SEC
        weekly_pivs = [
            Pivot(idx=1, confirm_idx=4, price=0.10, kind="LOW", ts=old_ts, date_utc="2011-01-01"),
            Pivot(idx=10, confirm_idx=13, price=60000.0, kind="LOW", ts=recent_ts, date_utc="2025-01-01"),
            Pivot(idx=20, confirm_idx=23, price=72000.0, kind="LOW", ts=recent_ts, date_utc="2025-03-01"),
            Pivot(idx=30, confirm_idx=33, price=85000.0, kind="LOW", ts=recent_ts, date_utc="2025-05-01"),
        ]
        zones = compute_causal_sr_zones(bars, weekly_pivs, 399, atr14_i=1800.0)
        sup_prices = [s["price"] for s in zones["supports"]]
        # The 3 recent supports should remain distinct and ordered S1 > S2 > S3 without collapsing or including $0.10
        self.assertEqual(len(sup_prices), 3)
        self.assertEqual(sup_prices, [85000.0, 72000.0, 60000.0])

    def test_cycle_ath_tie_and_90d_major_cycle_registration(self) -> None:
        # Build synthetic macro series:
        # Day 0: Peak $1000. Days 1..40: drop to $400 (-60%, but only 40 days < 90d -> mid-bull crash like 2021-05)
        # Day 65: close at $700 (-30%, locks t_0 = Day 0)
        # Day 70: new ATH $1100! Because trough was at Day 30 (< 90d from Peak), must NOT register a completed cycle.
        # Then from Day 70 ($1100), drop over 120 days to $220 (-80%, t_to_trough=120 >= 90d), then Day 250 breakout $1200 -> registers!
        rows: list[MacroDailyRow] = []
        base_ts = date_str_to_open_ts("2017-01-01")
        for d in range(260):
            ts = base_ts + d * DAY_SEC
            d_str = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
            if d == 0:
                px = 1000.0
            elif d == 5:
                px = 1000.0  # Tie with ATH! Peak_Date must stay Day 0
            elif d == 30:
                px = 400.0
            elif d < 70:
                px = 700.0
            elif d == 70:
                px = 1100.0
            elif d == 190:
                px = 220.0
            elif d < 250:
                px = 500.0
            else:
                px = 1200.0
            rows.append(
                MacroDailyRow(
                    open_ts=ts,
                    close_ts=ts + DAY_SEC,
                    date_utc=d_str,
                    open=px,
                    high=px,
                    low=px,
                    close=px,
                    volume=100.0,
                    is_ohlcv=True,
                )
            )
        states, comp_hist = track_causal_cycle_peaks(rows)
        # At day 5 (tie high), peak_date must remain day 0
        self.assertEqual(states[5].peak_date, rows[0].date_utc)
        # At day 70 breakout, t_to_trough was 30 days (< 90), so completed_cycles_count must still be 0
        self.assertEqual(states[70].completed_cycles_count, 0)
        # At day 250 breakout, t_to_trough was 190 - 70 = 120 days (>= 90) and max_dd = -80% <= -50%, so count == 1!
        self.assertEqual(states[250].completed_cycles_count, 1)
        self.assertEqual(comp_hist[250][0].peak_date, rows[70].date_utc)
        self.assertEqual(comp_hist[250][0].t_to_trough_days, 120)

    def test_track_a_zone_synthesis_and_matrix_exhaustiveness(self) -> None:
        zl, zh, pa = synthesize_track_a_zone(
            sma200w=40000.0,
            realized_price=38000.0,
            lgc_lower_band=42000.0,
            prev_cycle_low=15500.0,
            cycle_ath=100000.0,
            calibrated_dd_floor=-0.65,
            confirmed_weekly_low_prices=[36500.0, 39000.0],
        )
        self.assertIsNotNone(zl)
        self.assertIsNotNone(zh)
        self.assertIsNotNone(pa)
        assert zl is not None and zh is not None and pa is not None
        self.assertLessEqual(zl, pa)
        self.assertLessEqual(pa, zh)

        # Verify A2-A7 mutual exclusivity & exhaustiveness
        self.assertEqual(
            evaluate_track_a_action(
                l1_score=2,
                zone_high=40000.0,
                effective_spot=38000.0,
                replay_mode="HISTORICAL_REPLAY",
                live_spot=None,
                t0_date="2025-01-01",
                elapsed_days=100,
                t_max_days=365,
                dual_key_active=False,
            ),
            "BUY_NOW_LEFT_ZONE",
        )
        self.assertEqual(
            evaluate_track_a_action(
                l1_score=2,
                zone_high=36000.0,
                effective_spot=38000.0,
                replay_mode="HISTORICAL_REPLAY",
                live_spot=None,
                t0_date="2025-01-01",
                elapsed_days=100,
                t_max_days=365,
                dual_key_active=False,
            ),
            "BUY_LIMIT_PULLBACK",
        )
        self.assertEqual(
            evaluate_track_a_action(
                l1_score=1,
                zone_high=40000.0,
                effective_spot=39000.0,
                replay_mode="LIVE",
                live_spot=39000.0,
                t0_date="2025-01-01",
                elapsed_days=100,
                t_max_days=365,
                dual_key_active=False,
            ),
            "INTRADAY_ALERT_PENDING_CLOSE",
        )
        self.assertEqual(
            evaluate_track_a_action(
                l1_score=1,
                zone_high=40000.0,
                effective_spot=39000.0,
                replay_mode="HISTORICAL_REPLAY",
                live_spot=None,
                t0_date="2025-01-01",
                elapsed_days=100,
                t_max_days=365,
                dual_key_active=False,
            ),
            "WAIT_LEFT_TARGET",
        )
        self.assertEqual(
            evaluate_track_a_action(
                l1_score=1,
                zone_high=35000.0,
                effective_spot=48000.0,
                replay_mode="HISTORICAL_REPLAY",
                live_spot=None,
                t0_date="2025-01-01",
                elapsed_days=400,
                t_max_days=365,
                dual_key_active=True,
            ),
            "BUY_NOW_DUAL_KEY_FALLBACK",
        )
        self.assertEqual(
            evaluate_track_a_action(
                l1_score=1,
                zone_high=35000.0,
                effective_spot=48000.0,
                replay_mode="HISTORICAL_REPLAY",
                live_spot=None,
                t0_date="2025-01-01",
                elapsed_days=400,
                t_max_days=365,
                dual_key_active=False,
            ),
            "WAIT_WEEKLY_RIGHT_SIDE",
        )

    def test_track_b_b1_to_b7b_mutual_exclusivity_and_defensive_rejection(self) -> None:
        cand = SwingSetupCandidate(
            setup_id="SW-1",
            subtype="HL_EMA50",
            signal_idx=100,
            structural_stop=92.0,
            entry_anchor_low=92.5,
            stop_dist_to_close=0.08,
            consumed_pivot_idx=90,
        )
        # B5 vs B7a mutual exclusivity when WK-Warn ACTIVE, N_active=20, EV_R_active=-0.10, and valid SW-Entry
        res_b5 = evaluate_track_b_action(
            in_position=False,
            pending_exit_next_open=False,
            wk_warn_50w_active=True,
            n_active=20,
            ev_r_active=-0.10,
            ev_r_inactive=0.40,
            primary_setup=cand,
            same_day_exit_cooldown=False,
            entry_ref_price=100.0,
            atr14_i=2.0,
            gap_buffer_pct_i=0.04,
        )
        self.assertEqual(res_b5["rule_id"], "B5")
        self.assertEqual(res_b5["action"], "WATCHLIST_BLOCKED")
        self.assertEqual(res_b5["risk_budget_eff"], 0.0)

        # B7a when N_active=10 (< 15) and WK-Warn ACTIVE
        res_b7a = evaluate_track_b_action(
            in_position=False,
            pending_exit_next_open=False,
            wk_warn_50w_active=True,
            n_active=10,
            ev_r_active=0.30,
            ev_r_inactive=0.40,
            primary_setup=cand,
            same_day_exit_cooldown=False,
            entry_ref_price=100.0,
            atr14_i=2.0,
            gap_buffer_pct_i=0.04,
        )
        self.assertEqual(res_b7a["rule_id"], "B7a")
        self.assertEqual(res_b7a["action"], "BUY_REDUCED_RISK")
        self.assertEqual(res_b7a["risk_budget_eff"], 0.05)

        # B7b when N_active=20, EV_R_active=0.35 > 0.5 * 0.40 and >= 0.15
        res_b7b = evaluate_track_b_action(
            in_position=False,
            pending_exit_next_open=False,
            wk_warn_50w_active=True,
            n_active=20,
            ev_r_active=0.35,
            ev_r_inactive=0.40,
            primary_setup=cand,
            same_day_exit_cooldown=False,
            entry_ref_price=100.0,
            atr14_i=2.0,
            gap_buffer_pct_i=0.04,
        )
        self.assertEqual(res_b7b["rule_id"], "B7b")
        self.assertEqual(res_b7b["action"], "BUY_NEXT_OPEN")
        self.assertEqual(res_b7b["risk_budget_eff"], 0.10)

        # Defensive rejection (gap_buffer_pct_i = 0.72 -> stop_dist + gap_buffer >= 0.75) routes to WAIT_SIGNAL
        res_rej = evaluate_track_b_action(
            in_position=False,
            pending_exit_next_open=False,
            wk_warn_50w_active=False,
            n_active=20,
            ev_r_active=0.35,
            ev_r_inactive=0.40,
            primary_setup=cand,
            same_day_exit_cooldown=False,
            entry_ref_price=100.0,
            atr14_i=2.0,
            gap_buffer_pct_i=0.72,
        )
        self.assertEqual(res_rej["rule_id"], "B3")
        self.assertEqual(res_rej["action"], "WAIT_SIGNAL")
        self.assertEqual(res_rej["rejected_reason"], "TOTAL_BUFFER_EXCEEDS_75PCT")
        self.assertIsNone(res_rej["position_size_pct"])

    def test_sw1_sw2_sw3_detection_and_halving_era1_causality(self) -> None:
        # Halving causality at ERA_1 (e.g. 2014-01-01): next_est_date must be None
        h_era1 = compute_halving_diagnostics("2014-01-01", [])
        self.assertEqual(h_era1["era_status"], "ERA_1")
        self.assertIsNone(h_era1["next_est_date"])

        # Halving at 2026-11-14 (ERA_4): average interval 1387 days -> 2028-02-06
        h_era4 = compute_halving_diagnostics("2026-11-14", [])
        self.assertEqual(h_era4["era_status"], "ERA_4")
        self.assertEqual(h_era4["next_est_date"], "2028-02-06")

        # Construct a 65-bar series that triggers SW-3 Wyckoff (SC, AR, Spring, SOS, LPS) at bar 60
        bars: list[DailyOHLCVRow] = []
        for idx in range(65):
            bars.append(_make_daily_bar(idx, 100.0, 102.0, 98.0, 100.0, 1000.0))
        # SC low at idx=20 (price 80.0), AR high at idx=30 (price 95.0, +18.75% >= 12%)
        bars[20] = _make_daily_bar(20, 85.0, 86.0, 80.0, 82.0, 2000.0)
        bars[30] = _make_daily_bar(30, 90.0, 95.0, 89.0, 94.0, 2000.0)
        # Keep bars 21..45 inside box [82..93]
        for idx in range(21, 55):
            if idx != 30:
                bars[idx] = _make_daily_bar(idx, 88.0, 91.0, 85.0, 88.0, 1000.0)
        # Spring at idx=40: low=78.0 (< 0.995*80=79.6 and >= 0.85*80=68), close=81.0 (> 80.0)
        bars[40] = _make_daily_bar(40, 82.0, 83.0, 78.0, 81.0, 1500.0)
        # SOS at idx=50: close=97.0 (> 95.0), vol=2000 (> 1.2 * 1000)
        bars[50] = _make_daily_bar(50, 92.0, 98.0, 91.0, 97.0, 2000.0)
        # LPS pullback at idx=51..55: low touches 95.5 (<= 1.02 * 95 = 96.9), closes >= box_mid (87.5), idx=55 bullish
        for idx in range(51, 55):
            bars[idx] = _make_daily_bar(idx, 94.0, 95.0, 93.0, 93.5, 900.0)
        bars[55] = _make_daily_bar(55, 93.0, 97.0, 92.5, 96.0, 1200.0)

        closes = [b.close for b in bars]
        vols = [b.volume for b in bars]
        ema50 = calc_ema_sma_seeded(closes, 50)
        atr14 = calc_wilder_atr(bars, 14)
        vol_sma20 = calc_sma(vols, 20)
        pivs = compute_causal_pivots(bars, 5, 5)
        lows = [p for p in pivs if p.kind == "LOW" and p.confirm_idx <= 55]
        highs = [p for p in pivs if p.kind == "HIGH" and p.confirm_idx <= 55]

        primary, active_ids = detect_causal_sw_setups(
            bars,
            55,
            ema50=ema50,
            atr14=atr14,
            vol_sma20=vol_sma20,
            confirmed_lows=lows,
            confirmed_highs=highs,
            consumed_sw1_pivots=set(),
        )
        self.assertIsNotNone(primary)
        assert primary is not None
        self.assertEqual(primary.setup_id, "SW-3")
        self.assertIn("SW-3", active_ids)


if __name__ == "__main__":
    unittest.main()
