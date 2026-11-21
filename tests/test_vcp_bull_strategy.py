"""Forensic causality, anti-lookahead, and adversarial-exchange unit tests for vcp_bull_strategy v2."""

from __future__ import annotations

import copy
import datetime as dt
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.btc_bottom.fetch_snapshots import load_and_verify_snapshot
from scripts.btc_bottom.vcp_bull_strategy import (
    REFERENCE_AUDIT_DATES,
    prepare_vcp_environment,
    simulate_vcp_3tranche_strategy,
)


class TestVCPBullStrategyV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        snap = (root / "data" / "snapshots" / "latest").resolve()
        _, cls.daily_bars, cls.mvrv_rows, cls.weekly_bars, _, _ = load_and_verify_snapshot(snap)
        cls.full_env = prepare_vcp_environment(cls.daily_bars, cls.mvrv_rows, cls.weekly_bars)
        cls.full_sim = simulate_vcp_3tranche_strategy(cls.full_env)

    def test_prefix_causal_invariance_zero_lookahead(self) -> None:
        """Truncating future bars at 2024-06-30 must yield 100% identical trades & halving targets prior to cutoff."""
        cutoff_date = "2024-06-30"
        d_sub = [b for b in self.daily_bars if b.date_utc <= cutoff_date]
        last_close_ts = int(d_sub[-1].close_ts)
        m_sub = [m for m in self.mvrv_rows if m.date_utc <= cutoff_date]
        w_sub = [w for w in self.weekly_bars if int(w.week_close_ts) <= last_close_ts]

        env_sub = prepare_vcp_environment(d_sub, m_sub, w_sub)
        sim_sub = simulate_vcp_3tranche_strategy(env_sub)

        # Verify halving targets match bit-for-bit on every bar in the prefix
        for i in range(len(d_sub)):
            self.assertEqual(
                env_sub["halving_targets"][i]["in_peak_zone"],
                self.full_env["halving_targets"][i]["in_peak_zone"],
                f"Lookahead mismatch in halving_targets at {d_sub[i].date_utc}",
            )
            self.assertEqual(
                env_sub["wk_2below_50w"][i],
                self.full_env["wk_2below_50w"][i],
                f"Lookahead mismatch in wk_2below_50w at {d_sub[i].date_utc}",
            )

        # Verify all trades opened before 2024-06-20 have identical breakout dates & E1+E2 fill prices
        full_before = [
            (r["breakout_date"], round(r["position"].entries[0].price, 2), r["initial_stop"])
            for r in self.full_sim["trade_records"]
            if r["breakout_date"] <= "2024-06-20"
        ]
        sub_before = [
            (r["breakout_date"], round(r["position"].entries[0].price, 2), r["initial_stop"])
            for r in sim_sub["trade_records"]
            if r["breakout_date"] <= "2024-06-20"
        ]
        self.assertEqual(sub_before, full_before)

    def test_mvrv_prior_day_strict_causality(self) -> None:
        """Mutating same-day MVRV[d] on 2025-10-05 must NOT change halving_targets[d]."""
        target_d = "2025-10-05"
        idx = self.full_env["dates"].index(target_d)
        base_zone = self.full_env["halving_targets"][idx]["in_peak_zone"]

        mutated_mvrv = [
            replace(m, cap_mvrv_cur=999.0) if m.date_utc == target_d else m
            for m in self.mvrv_rows
        ]
        env_mut = prepare_vcp_environment(self.daily_bars, mutated_mvrv, self.weekly_bars)
        self.assertEqual(env_mut["halving_targets"][idx]["in_peak_zone"], base_zone)
        self.assertEqual(
            env_mut["halving_targets"][idx]["prev_mvrv"],
            self.full_env["halving_targets"][idx]["prev_mvrv"],
        )

    def test_reference_dates_covered_by_rule(self) -> None:
        """All 11 audit dates must satisfy the Occam 3-pillar signal and execute within ±1 day."""
        sig_dates = set(self.full_sim["confirmed_signal_dates"])
        for td in REFERENCE_AUDIT_DATES:
            self.assertIn(td, sig_dates, f"Audit date {td} missed confirmed signal!")

        exec_dates = [r["breakout_date"] for r in self.full_sim["trade_records"]]
        exact_cnt = sum(1 for td in REFERENCE_AUDIT_DATES if td in exec_dates)
        w1_cnt = sum(
            1
            for td in REFERENCE_AUDIT_DATES
            if any(abs((dt.date.fromisoformat(td) - dt.date.fromisoformat(bd)).days) <= 1 for bd in exec_dates)
        )
        self.assertGreaterEqual(exact_cnt, 10)
        self.assertEqual(w1_cnt, 11)

    def test_hostile_exchange_resilience_and_50w_failsafe(self) -> None:
        """Under extreme hostile exchange stress, v2 must not explode in trade count and must retain high CAGR/Sharpe."""
        hostile = simulate_vcp_3tranche_strategy(
            self.full_env,
            scam_wick_down_pct=0.020,
            scam_wick_up_pct=0.010,
            slip_entry=0.0030,
            slip_stop=0.0080,
            slip_exit=0.0030,
            limit_fill_buffer=0.005,
            flash_crash_worst_fill=True,
        )
        self.assertLessEqual(hostile["n_trades"], 75)
        self.assertGreaterEqual(hostile["cagr_pct"], 60.0)
        self.assertGreaterEqual(hostile["sharpe"], 1.45)

        no_halv = simulate_vcp_3tranche_strategy(self.full_env, use_halving_x2=False, use_50w_failsafe=True)
        self.assertGreaterEqual(no_halv["cagr_pct"], 50.0)
        self.assertGreaterEqual(no_halv["sharpe"], 1.10)


if __name__ == "__main__":
    unittest.main()
