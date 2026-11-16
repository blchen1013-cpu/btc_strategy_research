# Goldfish Annotated Review Report (Round 3 — Consolidated & Resolved)

**Target:** [`design_draft.md`](file:///home/antigravity/bitcoin-analyzer/design_draft.md) (`L1–L381`, Full File)  
**Predecessor Baseline:** [`concepts_brief.md`](file:///home/antigravity/bitcoin-analyzer/concepts_brief.md)  
**Review Round:** Round 3 (3rd Adversarial Cycle across 8 Goldfish Personas: `egm-design`, `egm-implementation-readiness`, `egm-comprehension`, `egm-structural`, `egm-codebase-details`, `egm-style`, `egm-inventive-catalyst`, `egm-zoom-out`)  
**Status:** resolved

---

## Resolution Summary

| Category | Count | Disposition |
| :--- | :--- | :--- |
| 🔴 **Blocker** | 5 unique findings (aggregated across auditors) | `PROPOSE FIX` — All 5 applied directly to [`design_draft.md`](file:///home/antigravity/bitcoin-analyzer/design_draft.md) |
| 🟡 **Major** | 16 unique findings (10 accepted fixes, 6 contested) | `10 PROPOSE FIX` applied; `6 CONTEST` backed by locked author/brief constraints |
| 🟢 **Nit** | 6 unique findings | `PROPOSE FIX` — All 6 applied cleanly to [`design_draft.md`](file:///home/antigravity/bitcoin-analyzer/design_draft.md) |

---

## Prioritized Findings & Resolutions

### 🔴 Issue 1 [`egm-comprehension` / `egm-implementation-readiness` / `egm-codebase-details`] - Severity: Blocker
*   **Affected Scope:** [`design_draft.md#L271-L276`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L271-L276) (`Track A 實盤／重放狀態判別與回測路徑`)
*   **Discrepancy Description:** Track A's net return formula was inverted (`1.0 - (exit_px * (1 - 0.001)) / (entry_px * (1 + 0.001))`), producing negative returns (e.g., `-99.6%`) on profitable 2x spot gains. Additionally, final-bar (`t == N - 1`) signals lacked an explicit no-fill guard when `Open[t+1]` does not exist, and `mae_pct` fee treatment was ambiguous.
*   **Rationale:** Incorrect sign inversion invalidates all historical Track A forward return tables (`ret_1y_pct`, `ret_2y_pct`, `ret_3y_pct`, `ret_next_cycle_peak_pct`).
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Valid critical arithmetic defect caught by 3 auditors.
*   **Applied Edit:**
    *   Location: [`design_draft.md#L271-L276`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L271-L276)
    *   Change: Corrected Track A net return formula to `((exit_px * (1.0 - 0.001)) / (entry_px * (1.0 + 0.001)) - 1.0) * 100.0`, excluded `t == N - 1` from backtest order fills due to absent `Open[t+1]`, and locked fee-unadjusted `mae_pct = ((min_low - entry_price) / entry_price) * 100.0` over `[entry_idx .. breakout_idx]`.

### 🔴 Issue 2 [`egm-design` / `egm-codebase-details`] - Severity: Blocker
*   **Affected Scope:** [`design_draft.md#L233-L236`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L233-L236) (`compute_causal_sr_zones`)
*   **Discrepancy Description:** Clustering all historical 2010–2026 weekly pivots without a sliding time bound or relative percentage tolerance caused 2010–2017 pivots (`$0.05–$10,000`) to collapse into one mega-cluster whenever `tol_usd` scaled with a `$90,000` current price.
*   **Rationale:** A mega-cluster corrupts Dimension 7 causal support levels (`S1–S3`) and distorts pivot weights.
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Valid structural scaling flaw across BTC's 6-orders-of-magnitude price history.
*   **Applied Edit:**
    *   Location: [`design_draft.md#L233-L236`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L233-L236)
    *   Change: Bounded candidate pivots to trailing 1 year for daily `(5,5)` pivots and trailing 3 years (`1095 * 86400s`) for closed weekly `(3,3)` pivots; switched clustering to relative percentage tolerance `tol_pct = max(0.015, 0.75 * atr_pct)` with weighted center comparison `abs(pt.price - center) / center <= tol_pct` and `band_half = max(0.5 * (max_p - min_p), 0.005 * center)`.

### 🔴 Issue 3 [`egm-structural` / `egm-implementation-readiness` / `egm-comprehension` / `egm-codebase-details`] - Severity: Blocker
*   **Affected Scope:** [`design_draft.md#L166-L177`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L166-L177), [`design_draft.md#L291-L306`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L291-L306) (`Capital_B` 行動決策矩陣 & Track B 執行契約)
*   **Discrepancy Description:** Rule `B5` (`N_active >= 15 and EV_R_active <= 0.0`) overlapped logically with `B7a` when `WK-Warn == ACTIVE` and `SW-Entry` was valid (`EV_R_active <= 0` also satisfied `EV_R_active < 0.15`). Additionally, `active_stop` and `has_reclaimed_ema50` initial values at fill `Open[i+1]`, net-of-fees `+1.5R` breakeven math, defensive rejection routing, `same_day_exit_cooldown`, and non-overlapping shadow/streak streams had gaps.
*   **Rationale:** Non-mutually-exclusive decision tables and uninitialized position fields make implementation nondeterministic.
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Valid specification defect resolved across both decision matrix and execution steps.
*   **Applied Edit:**
    *   Location: [`design_draft.md#L166-L177`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L166-L177), [`design_draft.md#L291-L306`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L291-L306)
    *   Change: Restricted `B6/B7a/B7b` explicitly with `(N_active < 15 or EV_R_active > 0.0)` so `B1–B7b` form 8 strictly mutually exclusive rules; locked `active_stop = structural_stop` and `has_reclaimed_ema50 = False` at `Open[i+1]`, exact net-of-fees `>= 1.5R` check, `same_day_exit_cooldown = True` on both Step 1 and Step 2 exits, defensive rejection routing to `WAIT_SIGNAL` (unless `B5`), single-position sequential `1R` shadow tracker, and unified non-overlapping `streak_throttled` stream.

### 🔴 Issue 4 [`egm-structural` / `egm-implementation-readiness` / `egm-style`] - Severity: Blocker
*   **Affected Scope:** [`design_draft.md#L148-L158`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L148-L158), [`design_draft.md#L270`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L270) (`Capital_A` 狀態矩陣)
*   **Discrepancy Description:** Rows `A4` and `A5` omitted explicit handling for `HISTORICAL_REPLAY` (`live_spot is None`) and `Zone_High is None`, leaving states unreachable/unmatched. Furthermore, `A1` cited only `A2–A6` (omitting `A7`), and JSON fields needed clean separation between uninvested daily action (`A2–A7`) and cycle deployment tracking (`A1`).
*   **Rationale:** Every reachable state combination in `Capital_A` must map deterministically to one action enum.
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Valid exhaustiveness gap in state machine table.
*   **Applied Edit:**
    *   Location: [`design_draft.md#L148-L158`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L148-L158), [`design_draft.md#L270`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L270)
    *   Change: Scoped `A4` strictly to `L1 < 2/3` AND `LIVE` with non-null `live_spot` & `Zone_High` (`effective_spot <= Zone_High`); broadened `A5` to cover all remaining `L1 < 2/3` pre-expiry cases (`HISTORICAL_REPLAY`, `Zone_High is None`, or `> Zone_High`); aligned `A2` with `(Zone_High is None or effective_spot <= Zone_High)`; updated `A1` reference to `A2–A7` and renamed backtest paths cleanly (`left_market`, `p_anchor_limit`, `dual_key`).

### 🔴 Issue 5 [`egm-implementation-readiness` / `egm-comprehension` / `egm-structural` / `egm-codebase-details` / `egm-style`] - Severity: Blocker
*   **Affected Scope:** [`design_draft.md#L226`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L226), [`design_draft.md#L343-L360`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L343-L360) (`Exact Endpoints, Canonical Hashing & Data Schemas`)
*   **Discrepancy Description:** Nested object schemas for `manifest.json` (`overlap_diffs`, `files`), `live_spot.json`, `dimensions_snapshot` (`valuation.mayer_multiple`, `halving`, `momentum_diag.vol_ratio_20`, `structural_skeleton`), `track_a` (`backtest_stats.summary_all`/`summary_oos`), and `track_b` (`HOLD_POSITION`/`EXIT_NEXT_OPEN` fields, `mtm_stats`, `by_wk_warn_*` keys, and zero-denominator `payoff_ratio`) were incomplete or ambiguous.
*   **Rationale:** Implementers and offline unit tests require exact schema keys and nullability contracts.
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Valid schema completeness requirement for implementation readiness.
*   **Applied Edit:**
    *   Location: [`design_draft.md#L226`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L226), [`design_draft.md#L343-L360`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L343-L360)
    *   Change: Specified exact field names, formulas (`mayer_multiple`, `vol_ratio_20`), nullability (`payoff_ratio: float | None`), empty-array behavior pre-2014, and distinct schemas across `closed_stats`, `mtm_stats`, `by_setup`, and `by_wk_warn_*`.

---

### 🟡 Issue 6 [`egm-design` / `egm-codebase-details`] - Severity: Major
*   **Affected Scope:** [`design_draft.md#L266-L276`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L266-L276) (`Dual-Key Fallback` 與 `Next Cycle Peak`)
*   **Discrepancy Description:** `Dual-Key` Structure Key did not require the prior weekly low `weekly_lows[-2]` to occur strictly after `t_0_ts` (allowing pre-ATH pivots to count as a bear-market Higher Low). Meanwhile, `Next Cycle Peak` stopped at any cycle post-breakout rather than waiting for a genuine cycle downturn (`max_dd <= -0.50` and `t_to_trough_days >= 90`), which would prematurely truncate post-breakout gains on short mid-bull pullbacks (e.g., `2021-04`, `2024-03`). Also `Deployed_In_Cycle` reset needed binding to strict `High[t] > Rolling_ATH`.
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Valid temporal boundary fix preserving causal bear/bull cycle boundaries.
*   **Applied Edit:** Added `weekly_lows[-2].week_open_ts > t_0_ts` check (`t_0_ts = Peak_Date 00:00:00Z`), gated `Next Cycle Peak` on `completed_cycles` or an in-progress `max_dd <= -0.50 and t_to_trough_days >= 90` downturn past `breakout_idx`, and explicitly bound `Deployed_In_Cycle = False` reset to strict `High[t] > Rolling_ATH`.

### 🟡 Issue 7 [`egm-implementation-readiness` / `egm-comprehension` / `egm-codebase-details`] - Severity: Major
*   **Affected Scope:** [`design_draft.md#L219`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L219), [`design_draft.md#L245-L247`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L245-L247), [`design_draft.md#L320`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L320) (`build_macro_daily_series`)
*   **Discrepancy Description:** Phase 1 `fetch_snapshots.py` needed `build_macro_daily_series` to build `weekly_ohlcv.csv` (and synthetic OHLCV fill for CoinMetrics-only rows pre-`2014-09-17`), creating a cross-phase placement conflict if `build_macro_daily_series` resided in Phase 2 `track_a.py`.
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Clean unidirectional DAG dependency (`Phase 1` -> `Phase 2`).
*   **Applied Edit:** Placed `build_macro_daily_series` in Phase 1 `fetch_snapshots.py` (filling pre-OHLCV CoinMetrics rows with `open=high=low=close=price_usd, volume=0.0, is_ohlcv=False`, `weekly_ohlcv.is_ohlcv = true` iff all 7 days have `True`) and documented explicit import into `track_a.py`.

### 🟡 Issue 8 [`egm-style` / `egm-zoom-out` / `egm-codebase-details`] - Severity: Major
*   **Affected Scope:** [`design_draft.md#L133`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L133), [`design_draft.md#L318-L323`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L318-L323), [`design_draft.md#L342`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L342) (`analyze_and_report.py` Network Isolation & `--out-dir` Rules)
*   **Discrepancy Description:** Putting `--refresh-live-spot` inside `analyze_and_report.py` contradicted the strict offline/zero-network invariant; also `--out-dir` overwrite behavior against existing `manifest.json` needed clarity.
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Strict phase separation between online data acquisition and offline computation.
*   **Applied Edit:** Relocated `--refresh-live-spot` exclusively into `fetch_snapshots.py` (checking `ohlcv_effective_as_of_ts == floor_utc_day(now_utc)`); kept `analyze_and_report.py` 100% network-free; specified strict refuse-overwrite for directories containing `manifest.json` or non-`.gitkeep` files.

### 🟡 Issue 9 [`egm-implementation-readiness` / `egm-comprehension`] - Severity: Major
*   **Affected Scope:** [`design_draft.md#L258-L264`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L258-L264) (`synthesize_track_a_zone` `P_min` & `P_anchor`)
*   **Discrepancy Description:** `P_min` lacked explicit guardrails for zero/missing anchors, and tie-breaking across multiple contiguous `max_score` grid runs for `P_anchor` was underspecified.
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Deterministic anchor extraction across edge inputs.
*   **Applied Edit:** Locked `anchors` list composition + `P_min = max(0.01, 0.25 * min(anchors))` and tie-breaking key `(len(run), run[-1])` for selecting the canonical contiguous grid run whose geometric midpoint becomes `P_anchor` (covering both `max_hits > 0` and `max_hits == 0`).

### 🟡 Issue 10 [`egm-design` / `egm-codebase-details` / `egm-comprehension`] - Severity: Major
*   **Affected Scope:** [`design_draft.md#L162`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L162), [`design_draft.md#L284-L288`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L284-L288) (`SW-3` Wyckoff Proxy)
*   **Discrepancy Description:** `SW-3` SOS loop `[max(19, s_rec + 1) .. i - 1]` lacked a guard on `Vol_SMA20[q]` (`None` check), and summary prose conflicted with the code check on `LPS` pullback criteria (`縮量` wording vs `min(Low[q+1..i]) <= 1.02 * AR.price`, `box_mid`, and bullish bar).
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Prevents `TypeError` on early OHLCV bars and removes prose-to-spec mismatch.
*   **Applied Edit:** Added explicit `Vol_SMA20[q] is not None` check at `design_draft.md#L286` and aligned `SW-3` prose at `design_draft.md#L162` with the exact `LPS` confirmation condition.

### 🟡 Issue 11 [`egm-comprehension` / `egm-implementation-readiness` / `egm-design`] - Severity: Major
*   **Affected Scope:** [`design_draft.md#L302-L306`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L302-L306) (Track B Grouped Simulations, Shadow Ledger & Streak Stream)
*   **Discrepancy Description:** Clarify whether `backtest_by_setup` uses standalone equity simulations vs closed-trade splits, how `backtest_by_wk_warn_*` computes group curves, how the shadow 1R tracker executes overlapping signals, and how `streak_throttled` avoids double-counting signals between live and shadow trades.
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Eliminates metric drift across summary tables.
*   **Applied Edit:** Locked `backtest_by_setup` as standalone single-setup equity runs, `by_wk_warn_*` as closed-trade partitions of `backtest_overall`, shadow 1R as single-position sequential execution (`risk_budget = 0.10`, non-compounding unit risk), and `streak_throttled` as one non-overlapping stream (live trade when `effective_budget > 0`, shadow trade strictly when blocked at `0.0`).

### 🟡 Issue 12 [`egm-style`] - Severity: Major (4 Consolidated Documentation & Alignment Repairs)
*   **Affected Scope:** [`design_draft.md#L19-L22`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L19-L22), [`design_draft.md#L100`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L100), [`design_draft.md#L148-L171`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L148-L171), [`design_draft.md#L323`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L323), [`design_draft.md#L376`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L376)
*   **Discrepancy Description:** Flagged unverified empirical figures/assumptions before execution (`0.15% / 2.17%`, `HTTP 451`, `實測`), `handleFetchLive` `160` vs `limit=180` citation nuance, contradictory Bitstamp overlap/fallback wording, mismatched `circuit_price` vs `circuit_breaker_price`, `<effective_date>` vs `<effective_ohlcv_date>` filename token, and Chinese phrasing (`邊際`/`平攤`/`非同步`/`極限`).
*   **AI Suggestion:** PROPOSE FIX (Applied)
*   **Justification:** Valid accuracy, terminology, and naming consistency fixes.
*   **Applied Edit:** Removed all unexecuted figures/external claims, added exact `src/*.js` line citations, standardized `circuit_breaker_price` and `<effective_ohlcv_date>`, clarified Bitstamp historical skip vs timeout `DEGRADED`, and corrected Chinese terminology throughout.

### 🟡 Issue 13 [`egm-inventive-catalyst`] - Severity: Major
*   **Affected Scope:** [`design_draft.md#L58, #L100, #L340`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L340) (`mvrv_lag > 86400s` global `fail-closed`)
*   **Discrepancy Description:** Suggests decoupling `CoinMetrics` lag > 24h from globally aborting `analyze_and_report.py` so Track B can still run in degraded stale-MVRV mode.
*   **AI Suggestion:** CONTEST
*   **Justification:** Checkable specification evidence in [`concepts_brief.md#L22, #L71`](file:///home/antigravity/bitcoin-analyzer/concepts_brief.md#L22) and [`design_draft.md#L58, #L100, #L340`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L100): the system strictly mandates **`fail-closed`** on primary market and on-chain sources so a stale MVRV feed cannot silently corrupt unified reports or Cycle ATH valuation baselines; the already-designed `<= 86,400s` (1-day) dual-watermark tolerance explicitly absorbs normal CoinMetrics post-UTC midnight publishing delays without relaxing primary integrity guarantees.

### 🟡 Issue 14 [`egm-inventive-catalyst`] - Severity: Major
*   **Affected Scope:** [`design_draft.md#L49, #L150-L158`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L150-L158) (Monolithic `Capital_A` entry vs multi-tranche scaling)
*   **Discrepancy Description:** Proposes replacing single-shot `Capital_A` entry with tranche splitting across `[Zone_Low, Zone_High]`.
*   **AI Suggestion:** CONTEST
*   **Justification:** Checkable specification evidence in [`concepts_brief.md#L40-L42`](file:///home/antigravity/bitcoin-analyzer/concepts_brief.md#L40-L42) (`單一核心重押價 P_anchor` for `單筆重押主結論`) and [`design_draft.md#L13, #L28`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L13): the author explicitly locked single-shot all-in anchor/zone deployment for `Capital_A` (preserving optional explanatory notes on anti-miss positioning without altering the monolithic primary strategy or backtest contract).

### 🟡 Issue 15 [`egm-zoom-out`] - Severity: Major (4 YAGNI Scope Challenges)
*   **Affected Scope:** [`design_draft.md#L30-L31`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L30-L31), [`design_draft.md#L259-L264`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L259-L264), [`design_draft.md#L280-L288`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L280-L288), [`design_draft.md#L304-L306`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L304-L306)
*   **Discrepancy Description:** Recommended removing: (1) `backtest_streak_throttled` losing-streak risk reduction; (2) `WK-Warn (200W)` comparison & `EV_R_inactive` causal `1R` shadow gating; (3) `--narrative-overlay`, `LGC`, and weighted grid synthesis for `[Zone_Low, Zone_High]`; and (4) `SW-2` (Double Bottom / Inv H&S) & `SW-3` (Wyckoff `Spring -> SOS -> LPS`) setups.
*   **AI Suggestion:** CONTEST
*   **Justification:** Checkable evidence in [`concepts_brief.md`](file:///home/antigravity/bitcoin-analyzer/concepts_brief.md) and [`design_draft.md`](file:///home/antigravity/bitcoin-analyzer/design_draft.md) proves all four capabilities are explicitly mandated user requirements rather than speculative gold-plating:
    1. Position sizing / consecutive drawdown control was explicitly requested by the user and mapped in [`design_draft.md#L30, #L306`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L306).
    2. Comparative `WK-Warn (50W vs 200W)` empirical win-rate/EV evaluation and causal downgrading (`<= 5% / 0%`) are explicitly mandated in [`concepts_brief.md#L62-L63`](file:///home/antigravity/bitcoin-analyzer/concepts_brief.md#L62-L63), requiring a non-starving `1R` shadow ledger ([`design_draft.md#L106, #L304`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L106)) so causal gates can re-open after recovery.
    3. `[Zone_Low, Zone_High]` synthesis (integrating `LGC`, `200W SMA`, `MVRV`, `Log-Fib`) and separation of deterministic skeleton vs hash-bound `--narrative-overlay` are explicitly specified in [`concepts_brief.md#L40-L42, #L68, #L74, #L87`](file:///home/antigravity/bitcoin-analyzer/concepts_brief.md#L68).
    4. All three daily setups (`SW-1`, `SW-2`, `SW-3`) are directly mandated in [`concepts_brief.md#L53-L56`](file:///home/antigravity/bitcoin-analyzer/concepts_brief.md#L53-L56).

---

### 🟢 Nits (All 6 Accepted & Applied)

1.  **[`egm-design` / `egm-codebase-details`] Yahoo Unclosed Bar Filtering Order** ([`design_draft.md#L333`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L333)): Applied `open_ts + 86400 <= req_as_of_ts` filter *before* running strict `86400s` spacing and non-null checks so partially formed intraday bars with null volume do not trigger spurious aborts.
2.  **[`egm-inventive-catalyst`] `--as-of YYYY-MM-DD` `LIVE` Resolution** ([`design_draft.md#L318, #L337`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L337)): Resolved date-only `--as-of YYYY-MM-DD` to `min(now_utc, YYYY-MM-DDT23:59:59Z)` and validated `LIVE` eligibility via `req_as_of_date == now_utc.date()` AND `ohlcv_effective_as_of_ts == floor_utc_day(now_utc)`.
3.  **[`egm-design`] `narrative_overlay.json` Post-Truncation Binding & Causal `halving` `ERA_1`** ([`design_draft.md#L311, #L350`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L311)): Bound `--narrative-overlay` to both `manifest_sha256` and post-`--as-of` truncated `effective_ohlcv_utc`; returned `next_est_date = None` at `ERA_1` (`len(known) == 1`) so future halving spacing is never leaked.
4.  **[`egm-structural`] Label Collision in `compute_causal_sr_zones` & Bands** ([`design_draft.md#L233-L236, #L260`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L260)): Renamed `(A1)/(A2)/(B)` sub-labels to clean prose bullets and `(a)–(d)` to avoid colliding with `Capital_A` matrix rows `A1–A7` and `Capital_B`.
5.  **[`egm-zoom-out` / `egm-style`] Unnecessary `bisect` Note on Weekly Pivots & Prose Tightening** ([`design_draft.md#L11, #L263`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L263)): Clarified direct linear scan over trailing confirmed weekly pivots during grid scoring (reserving `bisect.insort` for `tail_mults`), and trimmed cross-section fluff.
6.  **[`egm-implementation-readiness` / `egm-comprehension`] Minor Schema & `Zone_High is None` Clarification** ([`design_draft.md#L264, #L270, #L352-L360`](file:///home/antigravity/bitcoin-analyzer/design_draft.md#L270)): Clarified `max_hits == 0` fallback `[0.85 * max(C), max(C)]`, nullability markers across `analysis_summary.json`, and `effective_ohlcv_date` tokens.

---

*Resolved & Synchronized under EGM Round 3*
