"""Unit tests for Phase 1 snapshot fetching, validation, resampling, and canonical hashing.

Zero external network access: all HTTP responses are mocked via synthetic payloads.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.btc_bottom.fetch_snapshots import (
    DAY_SEC,
    YAHOO_START_TS,
    SnapshotIntegrityError,
    build_macro_daily_series,
    compute_canonical_manifest_sha256,
    create_snapshot,
    date_str_to_open_ts,
    load_and_verify_snapshot,
    parse_as_of_arg,
    refresh_live_spot_in_snapshot,
    resample_iso_monday_weekly,
    ts_to_date_str,
)


def make_mock_fetcher(
    start_cm_ts: int,
    end_close_ts: int,
    *,
    include_unclosed_yahoo_bar: bool = True,
    corrupt_bitstamp: bool = False,
    cm_lag_days: int = 0,
    fng_fail: bool = False,
    cm_multipage: bool = False,
) -> Any:
    """Construct a deterministic offline URL router for snapshot unit tests."""
    cm_end_close_ts = end_close_ts - cm_lag_days * DAY_SEC

    # Build CoinMetrics items
    cm_all: list[dict[str, Any]] = []
    cur_ts = start_cm_ts
    while cur_ts + DAY_SEC <= cm_end_close_ts:
        d_str = ts_to_date_str(cur_ts)
        px = 100.0 + ((cur_ts - start_cm_ts) // DAY_SEC) * 1.5
        cm_all.append(
            {
                "asset": "btc",
                "time": f"{d_str}T00:00:00.000000000Z",
                "CapMVRVCur": "1.25000000",
                "PriceUSD": f"{px:.4f}",
            }
        )
        cur_ts += DAY_SEC

    # Build Yahoo parallel arrays starting from max(start_cm_ts, YAHOO_START_TS)
    yh_start = max(start_cm_ts, YAHOO_START_TS)
    yh_ts: list[int] = []
    yh_o: list[float | None] = []
    yh_h: list[float | None] = []
    yh_l: list[float | None] = []
    yh_c: list[float | None] = []
    yh_v: list[float | None] = []

    cur_ts = yh_start
    while cur_ts + DAY_SEC <= end_close_ts:
        px = 100.0 + ((cur_ts - start_cm_ts) // DAY_SEC) * 1.5
        yh_ts.append(cur_ts)
        yh_o.append(px * 0.99)
        yh_h.append(px * 1.02)
        yh_l.append(px * 0.98)
        yh_c.append(px)
        yh_v.append(1000.0)
        cur_ts += DAY_SEC

    if include_unclosed_yahoo_bar:
        # Unclosed bar at end_close_ts with null volume/open to verify pre-filtering before null check!
        yh_ts.append(end_close_ts + 3600)
        yh_o.append(None)
        yh_h.append(None)
        yh_l.append(None)
        yh_c.append(None)
        yh_v.append(None)

    def _router(url: str) -> Any:
        if "finance.yahoo.com" in url:
            return {
                "chart": {
                    "result": [
                        {
                            "timestamp": yh_ts,
                            "indicators": {
                                "quote": [
                                    {
                                        "open": yh_o,
                                        "high": yh_h,
                                        "low": yh_l,
                                        "close": yh_c,
                                        "volume": yh_v,
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        if "coinmetrics.io" in url:
            if cm_multipage and "page_token=2" not in url:
                mid = len(cm_all) // 2
                return {
                    "data": cm_all[:mid],
                    "next_page_url": "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?page_token=2",
                }
            if cm_multipage and "page_token=2" in url:
                mid = len(cm_all) // 2
                return {"data": cm_all[mid:], "next_page_url": None}
            return {"data": cm_all, "next_page_url": None}
        if "bitstamp.net" in url:
            ohlc = []
            for ts_val, c_val in zip(yh_ts, yh_c):
                if c_val is None:
                    continue
                mult = 1.05 if corrupt_bitstamp else 1.0005
                ohlc.append({"timestamp": str(ts_val), "close": f"{c_val * mult:.4f}"})
            return {"data": {"ohlc": ohlc[-1000:]}}
        if "alternative.me" in url:
            if fng_fail:
                raise RuntimeError("F&G offline")
            # Emit a couple of F&G rows with an intentional missing day gap
            return {
                "data": [
                    {
                        "value": "24",
                        "value_classification": "Extreme Fear",
                        "timestamp": str(end_close_ts - DAY_SEC),
                    },
                    {
                        "value": "30",
                        "value_classification": "Fear",
                        "timestamp": str(end_close_ts - 3 * DAY_SEC),
                    },
                ]
            }
        if "kraken.com" in url:
            return {"result": {"XXBTZUSD": {"c": ["88500.50", "1.0"]}}}
        raise AssertionError(f"Unexpected URL in unit test: {url}")

    return _router


class TestSnapshotsPhase1(unittest.TestCase):
    def test_yahoo_unclosed_bar_filtered_before_null_and_spacing_checks(self) -> None:
        start_cm_ts = date_str_to_open_ts("2014-09-01")
        end_close_ts = date_str_to_open_ts("2014-11-10")
        now_dt = dt.datetime.fromtimestamp(end_close_ts + 7200, tz=dt.timezone.utc)
        fetcher = make_mock_fetcher(
            start_cm_ts,
            end_close_ts,
            include_unclosed_yahoo_bar=True,
            cm_multipage=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "snap1"
            manifest = create_snapshot(
                out_dir,
                as_of_str="2014-11-10",
                fetcher=fetcher,
                now_dt=now_dt,
            )
            self.assertEqual(manifest["cross_check_status"], "VERIFIED")
            self.assertEqual(manifest["ohlcv_effective_as_of_ts"], end_close_ts)
            # Verify load_and_verify_snapshot succeeds and live_spot is LIVE because date == now_dt.date()
            m2, d_rows, m_rows, w_rows, f_rows, live_obj = load_and_verify_snapshot(out_dir)
            self.assertEqual(m2["manifest_sha256"], manifest["manifest_sha256"])
            self.assertEqual(live_obj["mode"], "LIVE")
            self.assertEqual(live_obj["ticker_last"], 88500.50)
            self.assertTrue(len(d_rows) > 40)
            self.assertTrue(len(m_rows) > len(d_rows))
            # Check transition week 2014-09-15 has is_ohlcv == False because Mon/Tue were CoinMetrics-only
            w_by_date = {w.open_date_utc: w for w in w_rows}
            self.assertIn("2014-09-15", w_by_date)
            self.assertFalse(w_by_date["2014-09-15"].is_ohlcv)
            self.assertIn("2014-09-22", w_by_date)
            self.assertTrue(w_by_date["2014-09-22"].is_ohlcv)

    def test_refuse_overwrite_and_tamper_detection(self) -> None:
        start_cm_ts = date_str_to_open_ts("2014-09-15")
        end_close_ts = date_str_to_open_ts("2014-11-03")
        now_dt = dt.datetime.fromtimestamp(end_close_ts + 3600, tz=dt.timezone.utc)
        fetcher = make_mock_fetcher(start_cm_ts, end_close_ts)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "snap_immut"
            create_snapshot(out_dir, as_of_str="2014-11-03", fetcher=fetcher, now_dt=now_dt)

            # Second write to same directory must fail closed
            with self.assertRaises(SnapshotIntegrityError):
                create_snapshot(out_dir, as_of_str="2014-11-03", fetcher=fetcher, now_dt=now_dt)

            # Refreshing live_spot.json does not break manifest_sha256
            refresh_live_spot_in_snapshot(out_dir, fetcher=fetcher, now_dt=now_dt)
            load_and_verify_snapshot(out_dir)

            # Tamper with daily_ohlcv.csv and verify load_and_verify_snapshot raises SnapshotIntegrityError
            daily_csv = out_dir / "daily_ohlcv.csv"
            daily_csv.write_text(daily_csv.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaises(SnapshotIntegrityError):
                load_and_verify_snapshot(out_dir)

    def test_watermark_divergence_and_bitstamp_fail_closed(self) -> None:
        start_cm_ts = date_str_to_open_ts("2014-09-15")
        end_close_ts = date_str_to_open_ts("2014-11-10")
        now_dt = dt.datetime.fromtimestamp(end_close_ts + 3600, tz=dt.timezone.utc)

        # 2-day CoinMetrics lag (> 86400s) must fail closed
        fetcher_lag = make_mock_fetcher(start_cm_ts, end_close_ts, cm_lag_days=2)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SnapshotIntegrityError):
                create_snapshot(Path(tmp) / "s_lag", as_of_str="2014-11-10", fetcher=fetcher_lag, now_dt=now_dt)

        # 1-day CoinMetrics lag (== 86400s) is allowed
        fetcher_1d = make_mock_fetcher(start_cm_ts, end_close_ts, cm_lag_days=1)
        with tempfile.TemporaryDirectory() as tmp:
            m = create_snapshot(Path(tmp) / "s_1d", as_of_str="2014-11-10", fetcher=fetcher_1d, now_dt=now_dt)
            self.assertEqual(m["ohlcv_effective_as_of_ts"] - m["mvrv_effective_as_of_ts"], DAY_SEC)

        # Bitstamp > 0.50% median divergence with >= 30 overlapping bars must fail closed
        fetcher_bs_bad = make_mock_fetcher(start_cm_ts, end_close_ts, corrupt_bitstamp=True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SnapshotIntegrityError):
                create_snapshot(Path(tmp) / "s_bs", as_of_str="2014-11-10", fetcher=fetcher_bs_bad, now_dt=now_dt)

    def test_historical_replay_mode_and_partial_week_discard(self) -> None:
        # Start on a Wednesday (2014-09-17) and end on a Thursday (2014-10-09)
        start_cm_ts = date_str_to_open_ts("2014-09-17")
        end_close_ts = date_str_to_open_ts("2014-10-09")
        now_dt = dt.datetime(2026, 11, 14, 12, 0, 0, tzinfo=dt.timezone.utc)
        fetcher = make_mock_fetcher(start_cm_ts, end_close_ts, fng_fail=True)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "s_hist"
            manifest = create_snapshot(out_dir, as_of_str="2014-10-09", fetcher=fetcher, now_dt=now_dt)
            self.assertEqual(manifest["fng_status"], "UNAVAILABLE")
            _, _, _, w_rows, _, live_obj = load_and_verify_snapshot(out_dir)
            self.assertEqual(live_obj["mode"], "HISTORICAL_REPLAY")
            self.assertIsNone(live_obj["ticker_last"])
            # Only full Monday weeks 2014-09-22 and 2014-09-29 should exist
            self.assertEqual([w.open_date_utc for w in w_rows], ["2014-09-22", "2014-09-29"])


if __name__ == "__main__":
    unittest.main()
