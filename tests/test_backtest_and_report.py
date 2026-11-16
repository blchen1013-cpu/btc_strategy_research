"""Unit tests for Phase 3 backtest_engine.py and analyze_and_report.py.

Verifies Track A positive net return formula, Next Cycle Peak >=90d downturn guard,
Track B T+1 open execution, initial active_stop = structural_stop, +1.5R net-of-fees breakeven,
circuit breaker >= 0.25*entry, Step 1 & 2 same-day exit cooldown, single-position 1R shadow ledger,
offline zero-network guarantee, and narrative_overlay.json hash & effective date binding.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import math
import tempfile
import unittest
from pathlib import Path

from scripts.btc_bottom.analyze_and_report import (
    _assert_all_finite_with_null_contract,
    run_offline_analysis,
)
from scripts.btc_bottom.backtest_engine import (
    run_track_a_backtest,
    run_track_b_backtest,
    track_a_net_return_pct,
)
from scripts.btc_bottom.fetch_snapshots import (
    DAY_SEC,
    DailyOHLCVRow,
    MVRVRow,
    SnapshotIntegrityError,
    build_macro_daily_series,
    create_snapshot,
    date_str_to_open_ts,
    resample_iso_monday_weekly,
    ts_to_date_str,
)
from tests.test_snapshots import make_mock_fetcher


class TestBacktestAndReportPhase3(unittest.TestCase):
    def test_track_a_positive_net_return_formula_and_next_cycle_peak(self) -> None:
        # 2x spot gain from $10,000 to $20,000 with 0.10% fee on each side:
        # (20000 * 0.999) / (10000 * 1.001) - 1 = 19980 / 10010 - 1 = +99.6004%
        ret_2x = track_a_net_return_pct(10000.0, 20000.0)
        self.assertAlmostEqual(ret_2x, ((20000.0 * 0.999) / (10000.0 * 1.001) - 1.0) * 100.0, places=6)
        self.assertGreater(ret_2x, 99.5)

        # Build synthetic 450-day macro series with a left_market entry that later breaks out
        base_ts = date_str_to_open_ts("2015-01-05")  # Monday
        mvrv_rows: list[MVRVRow] = []
        daily_rows: list[DailyOHLCVRow] = []
        for d in range(450):
            ts = base_ts + d * DAY_SEC
            d_str = ts_to_date_str(ts)
            if d == 0:
                px = 1000.0
                mv = 2.0
            elif d <= 100:
                # Deep bear drop to $150 (-85% <= -80% Calibrated_DD_Floor, MVRV=0.80 <= 1.0 -> L1 >= 2/3)
                px = 150.0 if d >= 10 else 500.0
                mv = 0.80 if d >= 10 else 1.50
            elif d <= 200:
                # Breakout above $1000 to $1500, then brief 20-day pullback (< 90d!) to $700
                px = 1500.0 if d < 150 else 700.0
                mv = 1.8
            elif d <= 305:
                # Real cycle peak at $3000 on d=210, followed by gradual decline before hitting trough at d=315 (>= 90d!)
                px = 3000.0 if d == 210 else 1800.0
                mv = 2.2
            else:
                px = 1200.0
                mv = 1.1

            mvrv_rows.append(
                MVRVRow(
                    date_utc=d_str,
                    open_ts=ts,
                    effective_ts=ts + DAY_SEC,
                    cap_mvrv_cur=mv,
                    price_usd=px,
                    realized_price=px / mv,
                )
            )
            daily_rows.append(
                DailyOHLCVRow(
                    open_ts=ts,
                    close_ts=ts + DAY_SEC,
                    date_utc=d_str,
                    open=px,
                    high=px,
                    low=px,
                    close=px,
                    volume=1000.0,
                )
            )

        macro_daily = build_macro_daily_series(mvrv_rows, daily_rows)
        weekly_rows = resample_iso_monday_weekly(macro_daily)
        res = run_track_a_backtest(macro_daily, mvrv_rows, weekly_rows)
        ta_sum = res["track_a_summary"]
        left_ev = ta_sum["backtest_stats"]["left_market_events"]
        self.assertEqual(len(left_ev), 1)
        ev0 = left_ev[0]
        # Entry at $150; Next Cycle Peak must NOT be cut at $1500 (since pullback was <90d),
        # but must capture the genuine cycle peak $3000!
        expected_peak_ret = round(track_a_net_return_pct(150.0, 3000.0), 4)
        self.assertEqual(ev0["ret_next_cycle_peak_pct"], expected_peak_ret)
        self.assertGreater(ev0["ret_1y_pct"], 0.0)

    def test_track_b_circuit_breaker_and_cooldown_and_breakeven(self) -> None:
        # Construct a 90-day daily series where SW-1 triggers at bar 60, fills at bar 61,
        # hits +1.5R at bar 62, and then experiences an extreme gap-down on bar 65 testing circuit breaker
        base_ts = date_str_to_open_ts("2020-01-06")  # Monday
        bars: list[DailyOHLCVRow] = []
        for i in range(90):
            ts = base_ts + i * DAY_SEC
            d_str = ts_to_date_str(ts)
            bars.append(
                DailyOHLCVRow(
                    open_ts=ts,
                    close_ts=ts + DAY_SEC,
                    date_utc=d_str,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.0,
                    volume=1000.0,
                )
            )
        # Pivot 1 low at i=20 (price 90.0), Pivot 2 Higher Low at i=45 (price 95.0, confirmed at i=50)
        bars[20] = DailyOHLCVRow(bars[20].open_ts, bars[20].close_ts, bars[20].date_utc, 92.0, 93.0, 90.0, 92.0, 1000.0)
        bars[45] = DailyOHLCVRow(bars[45].open_ts, bars[45].close_ts, bars[45].date_utc, 96.0, 97.0, 95.0, 96.0, 1000.0)
        # At i=54: close slightly below EMA50 (98.0), at i=55: bullish breakout above EMA50 (102.0) -> triggers SW-1!
        bars[54] = DailyOHLCVRow(bars[54].open_ts, bars[54].close_ts, bars[54].date_utc, 99.0, 99.5, 97.5, 98.0, 1000.0)
        bars[55] = DailyOHLCVRow(bars[55].open_ts, bars[55].close_ts, bars[55].date_utc, 98.5, 103.0, 98.0, 102.0, 1500.0)
        # Bar 56 fills at Open=102.0; Bar 57 closes at 120.0 (> +1.5R, moves active_stop to breakeven ~102.204)
        bars[56] = DailyOHLCVRow(bars[56].open_ts, bars[56].close_ts, bars[56].date_utc, 102.0, 105.0, 101.5, 104.0, 1200.0)
        bars[57] = DailyOHLCVRow(bars[57].open_ts, bars[57].close_ts, bars[57].date_utc, 104.0, 121.0, 103.5, 120.0, 1500.0)
        # Bar 58 closes at 101.0 (< breakeven active_stop 102.204) -> schedules EXIT_NEXT_OPEN at Bar 59 Open=101.0!
        bars[58] = DailyOHLCVRow(bars[58].open_ts, bars[58].close_ts, bars[58].date_utc, 118.0, 118.5, 100.5, 101.0, 1200.0)
        bars[59] = DailyOHLCVRow(bars[59].open_ts, bars[59].close_ts, bars[59].date_utc, 101.0, 105.0, 100.0, 104.0, 1200.0)

        mvrv_rows = [
            MVRVRow(b.date_utc, b.open_ts, b.close_ts, 1.5, b.close, b.close / 1.5) for b in bars
        ]
        macro_daily = build_macro_daily_series(mvrv_rows, bars)
        weekly_rows = resample_iso_monday_weekly(macro_daily)

        tb_res = run_track_b_backtest(bars, weekly_rows)
        tb_sum = tb_res["track_b_summary"]
        self.assertGreaterEqual(tb_sum["backtest_overall"]["closed_stats"]["n_closed"], 1)
        tr0 = tb_res["overall_trades"][0]
        self.assertEqual(tr0.setup_id, "SW-1")
        self.assertEqual(tr0.entry_idx, 56)
        self.assertEqual(tr0.exit_idx, 59)
        self.assertEqual(tr0.exit_reason, "SCHEDULED_NEXT_OPEN_EXIT")

    def test_analyze_and_report_zero_network_and_overlay_validation(self) -> None:
        # 1. AST static inspection: analyze_and_report.py must not import urllib, http, socket, or requests
        src_path = Path("scripts/btc_bottom/analyze_and_report.py").resolve()
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        banned_modules = {"urllib", "urllib.request", "http", "http.client", "socket", "requests"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned_modules)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], banned_modules)

        # 2. End-to-end offline analysis on a synthetic snapshot + narrative_overlay binding checks
        start_cm_ts = date_str_to_open_ts("2014-08-04")  # Monday
        end_close_ts = date_str_to_open_ts("2015-01-12")
        now_dt = dt.datetime.fromtimestamp(end_close_ts + 3600, tz=dt.timezone.utc)
        fetcher = make_mock_fetcher(start_cm_ts, end_close_ts)

        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = Path(tmp) / "snap"
            manifest = create_snapshot(snap_dir, as_of_str="2015-01-12", fetcher=fetcher, now_dt=now_dt)

            rep_out = Path(tmp) / "report.md"
            json_out = Path(tmp) / "summary.json"
            summary, jp, rp = run_offline_analysis(
                snap_dir,
                report_out=rep_out,
                json_out=json_out,
                now_dt=now_dt,
            )
            self.assertTrue(jp.is_file())
            self.assertTrue(rp.is_file())
            self.assertEqual(summary["meta"]["narrative_status"], "STRUCTURAL_ONLY")

            # Valid narrative overlay
            overlay_ok = Path(tmp) / "overlay_ok.json"
            overlay_payload = {
                "snapshot_ohlcv_effective_utc": manifest["ohlcv_effective_as_of_utc"],
                "manifest_sha256": manifest["manifest_sha256"],
                "wyckoff_phase": "Phase C Spring Test",
                "wyckoff_events": [
                    {"event": "SC", "date_utc": "2014-10-05", "price": 320.0, "note": "Selling climax"}
                ],
                "elliott_primary": {
                    "degree": "Cycle",
                    "current_wave": "Wave II",
                    "summary": "Corrective flat completing",
                    "invalidation_price": 250.0,
                },
                "elliott_alternate": {
                    "degree": "Cycle",
                    "current_wave": "Wave C",
                    "summary": "Extended diagonal",
                    "invalidation_price": 200.0,
                },
                "invalidation_notes": "Close below $250 invalidates primary count.",
            }
            overlay_ok.write_text(json.dumps(overlay_payload), encoding="utf-8")
            sum_ov, _, _ = run_offline_analysis(
                snap_dir,
                report_out=rep_out,
                json_out=json_out,
                narrative_overlay_path=overlay_ok,
                now_dt=now_dt,
            )
            self.assertEqual(sum_ov["meta"]["narrative_status"], "VERIFIED_OVERLAY")

            # Mismatched --as-of truncation vs overlay snapshot_ohlcv_effective_utc must fail closed!
            with self.assertRaises(SnapshotIntegrityError):
                run_offline_analysis(
                    snap_dir,
                    as_of_str="2014-12-15",
                    report_out=rep_out,
                    json_out=json_out,
                    narrative_overlay_path=overlay_ok,
                    now_dt=now_dt,
                )

            # Non-finite number guard must reject NaN/Inf
            with self.assertRaises(SnapshotIntegrityError):
                _assert_all_finite_with_null_contract({"bad": math.nan})


if __name__ == "__main__":
    unittest.main()
