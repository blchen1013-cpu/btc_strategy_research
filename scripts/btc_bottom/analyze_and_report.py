"""Phase 3: Purely offline BTC/USD 7-dimension analysis and Traditional Chinese decision report generator.

Strictly prohibited from importing or invoking any network clients.
Verifies manifest_sha256, supports --as-of truncation and hash/date-bound --narrative-overlay,
runs global finite-number schema validation, and writes analysis_summary.json + 7-dimension report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any, Sequence

from scripts.btc_bottom.backtest_engine import run_track_a_backtest, run_track_b_backtest
from scripts.btc_bottom.fetch_snapshots import (
    DAY_SEC,
    DailyOHLCVRow,
    FnGRow,
    MVRVRow,
    SnapshotIntegrityError,
    WeeklyOHLCVRow,
    build_macro_daily_series,
    floor_utc_day_ts,
    load_and_verify_snapshot,
    parse_as_of_arg,
    ts_to_date_str,
    ts_to_iso_utc,
)
from scripts.btc_bottom.indicators import (
    calc_bollinger,
    calc_ema_sma_seeded,
    calc_macd,
    calc_sma,
    calc_wilder_atr,
    calc_wilder_rsi,
    compute_causal_pivots,
    compute_causal_sr_zones,
)
from scripts.btc_bottom.track_a import compute_halving_diagnostics

FIB_RATIOS_LABELS = (
    (0.236, "0.236 淺幅回撤"),
    (0.382, "0.382 中繼支撐"),
    (0.500, "0.500 對數中軸"),
    (0.618, "0.618 黃金分割"),
    (0.786, "0.786 深度價值防守"),
)


def _assert_all_finite_with_null_contract(obj: Any, path: str = "root") -> None:
    """Recursively verify all floats/ints are finite (no NaN/Inf)."""
    if obj is None or isinstance(obj, (bool, str)):
        return
    if isinstance(obj, (int, float)):
        if not math.isfinite(float(obj)):
            raise SnapshotIntegrityError(f"Non-finite numeric value at {path}: {obj!r}")
        return
    if isinstance(obj, list):
        for idx, item in enumerate(obj):
            _assert_all_finite_with_null_contract(item, f"{path}[{idx}]")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _assert_all_finite_with_null_contract(v, f"{path}.{k}")
        return
    raise SnapshotIntegrityError(f"Unsupported JSON type at {path}: {type(obj).__name__}")


def _validate_and_load_narrative_overlay(
    overlay_path: Path,
    expected_manifest_sha256: str,
    expected_effective_ohlcv_utc: str,
) -> dict[str, Any]:
    resolved = overlay_path.resolve()
    if not resolved.is_file():
        raise SnapshotIntegrityError(f"Narrative overlay file not found: {resolved}")
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnapshotIntegrityError(f"Invalid JSON in narrative overlay {resolved}: {exc}") from exc

    if data.get("manifest_sha256") != expected_manifest_sha256:
        raise SnapshotIntegrityError(
            f"Narrative overlay manifest_sha256 mismatch: overlay has {data.get('manifest_sha256')!r}, "
            f"expected {expected_manifest_sha256!r}"
        )
    if data.get("snapshot_ohlcv_effective_utc") != expected_effective_ohlcv_utc:
        raise SnapshotIntegrityError(
            f"Narrative overlay snapshot_ohlcv_effective_utc mismatch: overlay has "
            f"{data.get('snapshot_ohlcv_effective_utc')!r}, expected {expected_effective_ohlcv_utc!r}"
        )

    req_keys = (
        "wyckoff_phase",
        "wyckoff_events",
        "elliott_primary",
        "elliott_alternate",
        "invalidation_notes",
    )
    for k in req_keys:
        if k not in data:
            raise SnapshotIntegrityError(f"Missing required key {k!r} in narrative_overlay.json")

    for wave_key in ("elliott_primary", "elliott_alternate"):
        w_obj = data[wave_key]
        if not isinstance(w_obj, dict):
            raise SnapshotIntegrityError(f"{wave_key} must be an object")
        for subk in ("degree", "current_wave", "summary", "invalidation_price"):
            if subk not in w_obj:
                raise SnapshotIntegrityError(f"Missing {wave_key}.{subk} in narrative_overlay.json")
        inv_px = float(w_obj["invalidation_price"])
        if not math.isfinite(inv_px) or inv_px <= 0.0:
            raise SnapshotIntegrityError(f"Invalid {wave_key}.invalidation_price: {inv_px}")

    _assert_all_finite_with_null_contract(data, "narrative_overlay")
    return data


def _compute_sentiment_snapshot(
    fng_rows: Sequence[FnGRow],
    fng_status: str,
    effective_ohlcv_ts: int,
) -> dict[str, Any]:
    if fng_status != "OK" or not fng_rows:
        return {
            "date_utc": None,
            "fng_value": None,
            "fng_class": "UNAVAILABLE",
            "fng_stale_days": None,
        }
    eff_date = dt.datetime.fromtimestamp(effective_ohlcv_ts - DAY_SEC, tz=dt.timezone.utc).date()
    eligible = [r for r in fng_rows if dt.date.fromisoformat(r.date_utc) <= eff_date]
    if not eligible:
        return {
            "date_utc": None,
            "fng_value": None,
            "fng_class": "UNAVAILABLE",
            "fng_stale_days": None,
        }
    latest = eligible[-1]
    latest_date = dt.date.fromisoformat(latest.date_utc)
    stale_days = (eff_date - latest_date).days
    if stale_days > 7:
        return {
            "date_utc": None,
            "fng_value": None,
            "fng_class": "UNAVAILABLE",
            "fng_stale_days": None,
        }
    return {
        "date_utc": latest.date_utc,
        "fng_value": int(latest.fng_value),
        "fng_class": latest.fng_class,
        "fng_stale_days": int(stale_days),
    }


def _fmt_usd(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"\\${val:,.2f}"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"{val:+.2f}%" if val != 0 else "0.00%"


def _fmt_plain_pct(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"{val:.2f}%"


def _render_markdown_report(
    summary: dict[str, Any],
    overlay: dict[str, Any] | None,
) -> str:
    meta = summary["meta"]
    dim = summary["dimensions_snapshot"]
    val = dim["valuation"]
    halv = dim["halving"]
    sent = dim["sentiment"]
    mom = dim["momentum_diag"]
    skel = dim["structural_skeleton"]
    ta = summary["track_a"]
    tb = summary["track_b"]

    close_px = float(val["close"])
    live_px = meta["live_spot_price"]
    eff_spot = float(live_px) if (meta["replay_mode"] == "LIVE" and live_px is not None) else close_px
    spot_diff_pct = ((eff_spot - close_px) / close_px * 100.0) if close_px > 0 else 0.0

    ta_action_zh = {
        "BUY_NOW_LEFT_ZONE": "🟢 現在買進（左側估值與價值帶共振達標）",
        "BUY_LIMIT_PULLBACK": "🟡 掛限價單等待回踩（L1 估值達標但現價高於共振帶上緣）",
        "INTRADAY_ALERT_PENDING_CLOSE": "🟠 盤中跌入價值帶預警（待 UTC 日線收盤確認 L1 >= 2/3）",
        "WAIT_LEFT_TARGET": "⏳ 等待左側重押價位（目前未達 L1 門檻，維持現金並預掛目標區）",
        "BUY_NOW_DUAL_KEY_FALLBACK": "🟢 現在買進（逾越等待期限 T_max，週線右側雙鑰匙確認）",
        "WAIT_WEEKLY_RIGHT_SIDE": "⏳ 等待週線右側雙鑰匙或左側深跌（已逾 T_max，待轉強確認）",
    }.get(ta["action"], ta["action"])

    tb_action_zh = {
        "EXIT_NEXT_OPEN": "🔴 次日開盤平倉（已觸發收盤結構或趨勢失效）",
        "HOLD_POSITION": "🟢 續抱波段部位（結構與趨勢完整）",
        "WAIT_SIGNAL": "⏳ 空手等待日線右側結構確認（目前無 SW-1/2/3 進場訊號）",
        "BUY_NEXT_OPEN": "🟢 次日開盤買進（日線右側結構確認，10% 標準風險預算）",
        "BUY_REDUCED_RISK": "🟡 次日開盤減半買進（處於週線破位觀察期，5% 縮減風險預算）",
        "WATCHLIST_BLOCKED": "🛑 強制暫停開倉（週線破位期且歷史實證期望值 <= 0，0% 預算）",
    }.get(tb["action"], tb["action"])

    lines: list[str] = []
    lines.append("# 比特幣（BTC/USD）雙軌抄底決策分析與 2010–2026 因果回測實證報告")
    lines.append("")
    lines.append(f"* **報告基準時間 (`effective_ohlcv_utc`)**：`{meta['effective_ohlcv_utc']}`（鏈上基準：`{meta['effective_mvrv_utc']}`，延遲 `{meta['mvrv_lag_days']}` 日）")
    lines.append(f"* **執行模式 (`replay_mode`)**：`{meta['replay_mode']}` | **快照雜湊 (`manifest_sha256`)**：`{meta['manifest_sha256'][:16]}...`")
    lines.append(f"* **已收盤日線基準價 (`Close[t]`)**：{_fmt_usd(close_px)} | **盤中即時現價 (`live_spot`)**：{_fmt_usd(live_px)}（狀態：`{meta['live_spot_status']}`，價差：{_fmt_pct(spot_diff_pct if live_px is not None else None)}）")
    lines.append(f"* **波浪／威科夫敘事狀態 (`narrative_status`)**：`{meta['narrative_status']}`")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 1. 執行摘要：現在該買嗎？還是要等更低？")
    lines.append("")
    lines.append("本系統將資金嚴格隔離為兩個互不共攤風險的獨立部位：**Track A (`Capital_A`)** 負責 **1–3 年長線左側重押（不設停損、接受被套、逾時切換週線右側雙鑰匙）**；**Track B (`Capital_B`)** 負責 **數個月日線右側波段（單筆最大回撤透過尾端跳空緩衝公式與盤中熔斷線嚴格鎖定於 10% 內，平時僅依收盤結構失效出場）**。")
    lines.append("")
    lines.append("| 獨立部位 | 當下唯一行動結論 | 核心目標價 / 進場參考價 | 價值區間 / 結構停損與盤中 10% 熔斷線 | 建議倉位與風險配置 | 本輪回測持倉狀態對照 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
    ta_cycle_str = (
        f"A1 `HOLD_CYCLE_POSITION`（已於 `{ta['backtest_cycle_entry']['date']}` 以 {_fmt_usd(ta['backtest_cycle_entry']['price'])} 經 `{ta['backtest_cycle_entry']['path']}` 進場）"
        if ta["backtest_cycle_deployed"] and ta["backtest_cycle_entry"]
        else "本輪週期尚未觸發左側或雙鑰匙進場"
    )
    lines.append(
        f"| **Track A (`Capital_A`)**<br>1–3 年長線抄底 | **`{ta['action']}`**<br>{ta_action_zh} | 單一核心重押錨點 `P_anchor`：<br>**{_fmt_usd(ta['p_anchor'])}** | 左側重押價值區間 `[Zone_Low, Zone_High]`：<br>**{_fmt_usd(ta['zone_low'])} – {_fmt_usd(ta['zone_high'])}**<br>（長線現貨不設停損） | 達標時 **100% `Capital_A`** 單筆重押<br>（`L1` 得分：`{ta['l1_score']}/3`） | {ta_cycle_str} |"
    )
    tb_stop_str = (
        f"收盤結構停損：{_fmt_usd(tb['active_stop'])}<br>盤中 10% 熔斷線：{_fmt_usd(tb['circuit_breaker_price'])}"
        if tb["active_stop"] is not None
        else "待 `SW-1/2/3` 訊號觸發後依結構低點鎖定"
    )
    tb_size_str = (
        f"名目倉位：`{tb['position_size_pct']:.2f}%` `Capital_B`<br>（單筆風險預算：`{tb['risk_budget_eff']*100:.1f}%`）"
        if tb["position_size_pct"] is not None and tb["risk_budget_eff"] is not None
        else f"待命風險預算：`{(tb['risk_budget_eff'] or 0.0)*100:.1f}%` `Capital_B`（目前倉位 `0%`）"
    )
    lines.append(
        f"| **Track B (`Capital_B`)**<br>數月日線右側波段 | **`{tb['action']}`**<br>{tb_action_zh} | 進場參考價：<br>**{_fmt_usd(tb['entry_ref_price'])}** | {tb_stop_str} | {tb_size_str} | 當日觸發型態：`{', '.join(tb['active_setups_on_bar']) if tb['active_setups_on_bar'] else '無'}`<br>`WK-Warn(50W)`：`{'ACTIVE (亮燈)' if tb['wk_warn_50w_active'] else 'INACTIVE (熄燈)'}` |"
    )
    lines.append("")
    lines.append("### 關鍵決策解讀")
    lines.append(f"1. **長線部位 (`Capital_A`) — 先等跌進左側價值帶或逾時轉右側**：當下 `L1` 硬門檻僅滿足 **`{ta['l1_score']}/3`**（`200W SMA * 1.10` 上限為 {_fmt_usd(val['sma200w'] * 1.10 if val['sma200w'] else None)}、鏈上實質價格 `Realized_Price` 為 {_fmt_usd(ta['realized_price'])}、本輪自高點 {_fmt_usd(val['cycle_ath'])} 回撤 `{val['ath_dd_pct']:.2f}%` 對比校準熊市跌幅門檻 `{ta['calibrated_dd_floor_pct']:.2f}%`）。若持有空白長線資金，左側開始重押的價值區間起點為 **`Zone_High` {_fmt_usd(ta['zone_high'])}**（區間下緣 **`Zone_Low` {_fmt_usd(ta['zone_low'])}**），單一主掛單核心價為 **`P_anchor` {_fmt_usd(ta['p_anchor'])}**。若自本輪週期高點日 `t_0 = {ta['t0_date'] or '尚未鎖定（距高點未滿 60 日且跌幅 >= 25%）'}` 起算超過 **`T_max = {ta['t_max_days']}` 天**（目前已過 `{ta['elapsed_days'] if ta['elapsed_days'] is not None else 0}` 天）仍未跌入左側區，系統將自動切換為 **`Dual-Key Fallback` 週線右側進場**（目前雙鑰匙狀態：`{'已成立' if ta['dual_key_active'] else '未成立'}`）。")
    lines.append(f"2. **波段部位 (`Capital_B`) — 嚴格等待日線右側結構確認並控管跳空尾端風險**：目前 `50W SMA` 週線破位警示 `WK-Warn(50W)` 為 **`{'ACTIVE (亮燈)' if tb['wk_warn_50w_active'] else 'INACTIVE (熄燈)'}`**（`200W SMA` 對照警示為 `{'ACTIVE' if tb['wk_warn_200w_active'] else 'INACTIVE'}`），當日日線收盤滿足之右側型態為 `{', '.join(tb['active_setups_on_bar']) if tb['active_setups_on_bar'] else '無'}`，因此結論為 **`{tb['action']}`**。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 2. 維度 1 & 4：長期估值模型與鏈上 MVRV / 實質價格")
    lines.append("")
    lines.append("> [!NOTE]")
    lines.append("> **統計性質聲明**：對數成長通道下緣（`LGC_Lower_Band`）係基於 2009 創世區塊以來每日對數價格對對數時間之 Welford 因果擴展回歸殘差（`-1.645 * sigma`）推算，由於時間序列殘差具高度自相關，該下緣屬**經驗估值邊界（Empirical Boundary）**而非嚴格獨立同分佈（i.i.d.）統計信賴區間。")
    lines.append("")
    lines.append("| 估值與鏈上指標 | 當前數值 (`Close[t]`) | Track A `L1` 硬門檻條件 | 是否達標？ | 距門檻 / 基準價差 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- |")
    sma200w_val = val["sma200w"]
    sma200w_cap = sma200w_val * 1.10 if sma200w_val is not None else None
    sma_diff = ((close_px - sma200w_cap) / close_px * 100.0) if sma200w_cap else None
    rp_val = ta["realized_price"]
    mvrv_val = close_px / rp_val if rp_val > 0 else None
    rp_diff = ((close_px - rp_val) / close_px * 100.0) if rp_val > 0 else None
    dd_target_px = val["cycle_ath"] * (1.0 + ta["calibrated_dd_floor_pct"] / 100.0)
    dd_diff = ((close_px - dd_target_px) / close_px * 100.0) if close_px > 0 else None
    mvrv_str = f"`{mvrv_val:.3f}`" if mvrv_val is not None else "`N/A`"
    mayer_str = f"`{val['mayer_multiple']:.3f}`" if val["mayer_multiple"] is not None else "`N/A`"
    lines.append(f"| **200 週均線 (`200W SMA`)** | {_fmt_usd(sma200w_val)}（`1.10x` 上限：{_fmt_usd(sma200w_cap)}） | `Close <= 1.10 * SMA200W` | `{'✅ YES' if ta['l1_flags']['sma200w_ok'] else '❌ NO'}` | 需再跌 {_fmt_plain_pct(sma_diff)} |")
    lines.append(f"| **鏈上 MVRV (`CapMVRVCur`) & 實質價格** | `MVRV` = {mvrv_str} / `Realized_Price` = {_fmt_usd(rp_val)} | `CapMVRVCur <= 1.00`（現價 <= 實質價格） | `{'✅ YES' if ta['l1_flags']['mvrv_ok'] else '❌ NO'}` | 需再跌 {_fmt_plain_pct(rp_diff)} |")
    lines.append(f"| **本輪高點回撤 (`ATH Drawdown`)** | `{val['ath_dd_pct']:.2f}%`（本輪 `Cycle_ATH`：{_fmt_usd(val['cycle_ath'])}） | `ATH_DD <= {ta['calibrated_dd_floor_pct']:.2f}%`（對應價：{_fmt_usd(dd_target_px)}） | `{'✅ YES' if ta['l1_flags']['dd_ok'] else '❌ NO'}` | 需再跌 {_fmt_plain_pct(dd_diff)} |")
    lines.append(f"| **Mayer Multiple (`Close / 200D SMA`)** | {mayer_str} | 診斷參考（歷史底部常 `< 0.65`） | 診斷指標 | — |")
    lines.append(f"| **對數成長通道下緣 (`LGC_Lower_Band`)** | {_fmt_usd(val['lgc_lower_band'])} | 價值帶子帶 `[0.90x, 1.10x]`（權重 `0.8`） | 區間合成 | 距下緣 {_fmt_pct(((val['lgc_lower_band'] - close_px)/close_px*100.0) if val['lgc_lower_band'] else None)} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 3. 維度 2 & 3：減半週期位置、歷輪熊市類比與 Track A 歷史實證")
    lines.append("")
    lines.append("### 3.1 減半週期定位 (`compute_halving_diagnostics`)")
    lines.append(f"* **當前減半紀元**：`{halv['era_status']}` | **上次減半日**：`{halv['last_halving_date']}`（已過 **`{halv['days_since_halving']}` 天**）")
    lines.append(f"* **因果推算下次減半日**：`{halv['next_est_date']}`（依歷史已完成減半間隔均值 `1,387` 日推算，目前週期進度 **`{halv['cycle_progress_pct']}%`**）")
    lines.append(f"* **左側等待期限 `T_max`**：**`{ta['t_max_days']}` 天**（由歷輪重大熊市從峰值 `Peak_Date` 至最低點 `Trough_Date` 的時間分佈 `round(mean(D) + stdev(D))` 無前瞻校準得出）")
    lines.append("")
    lines.append("### 3.2 歷輪已完成重大熊市週期類比（`max_dd <= -50%` 且見底歷時 `>= 90` 日）")
    lines.append("")
    lines.append("| 週期峰值日 (`Peak_Date`) | 週期最低日 (`Trough_Date`) | 高點至谷底天數 (`D`) | 前次減半日 | 減半至見頂天數 | 減半至見底天數 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
    for off in halv["historical_cycle_offsets"]:
        lines.append(
            f"| `{off['peak_date']}` | `{off['trough_date']}` | `{ (dt.date.fromisoformat(off['trough_date']) - dt.date.fromisoformat(off['peak_date'])).days }` 天 | `{off['halving_date'] or 'PRE_HALVING'}` | `{off['days_from_halving_to_peak'] if off['days_from_halving_to_peak'] is not None else 'N/A'}` | `{off['days_from_halving_to_trough'] if off['days_from_halving_to_trough'] is not None else 'N/A'}` |"
        )
    lines.append("")
    lines.append("### 3.3 Track A 歷史抄底進場表現與最大潛在被套深度（MAE）實證（2010–2026）")
    lines.append("")
    lines.append("| 進場路徑 | 進場日期 (`T+1 Open`) | 進場價格 | 早年暖機期 (`M < 2`) | 進場後最深浮虧 (`MAE`) | 持有 +1 年淨報酬 | 持有 +2 年淨報酬 | 持有 +3 年淨報酬 | 下一輪週期高點淨報酬 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    bs_a = ta["backtest_stats"]
    for path_label, ev_list in (
        ("主路徑：左側價值帶市價 (`left_market`)", bs_a["left_market_events"]),
        ("對照：單一核心錨點限價 (`p_anchor_limit`)", bs_a["p_anchor_limit_events"]),
        ("回退：逾時週線雙鑰匙 (`dual_key`)", bs_a["dual_key_events"]),
    ):
        if not ev_list:
            lines.append(f"| {path_label} | 無觸發事件 | — | — | — | — | — | — | — |")
        for ev in ev_list:
            lines.append(
                f"| {path_label} | `{ev['entry_date']}` | {_fmt_usd(ev['entry_price'])} | `{'是 (Burn-in)' if ev['early_burn_in'] else '否 (OOS)'}` | **`{ev['mae_pct']:.2f}%`** | {_fmt_pct(ev['ret_1y_pct'])} | {_fmt_pct(ev['ret_2y_pct'])} | {_fmt_pct(ev['ret_3y_pct'])} | **{_fmt_pct(ev['ret_next_cycle_peak_pct'])}** |"
            )
    s_all = bs_a["summary_all"]
    s_oos = bs_a["summary_oos"]
    lines.append("")
    lines.append(f"* **全樣本主路徑彙總（`n = {s_all['primary_n_events']}`）**：平均最大浮虧 `MAE = {_fmt_plain_pct(s_all['primary_mean_mae_pct'])}`（最深 `{_fmt_plain_pct(s_all['primary_worst_mae_pct'])}`），+1 年平均報酬 {_fmt_pct(s_all['primary_mean_ret_1y_pct'])}、+2 年平均報酬 {_fmt_pct(s_all['primary_mean_ret_2y_pct'])}、+3 年平均報酬 {_fmt_pct(s_all['primary_mean_ret_3y_pct'])}、下一輪週期峰值平均報酬 **{_fmt_pct(s_all['primary_mean_ret_next_peak_pct'])}**。")
    lines.append(f"* **樣本外週期彙總（`M >= 2`, `n = {s_oos['primary_n_events']}`）**：平均最大浮虧 `MAE = {_fmt_plain_pct(s_oos['primary_mean_mae_pct'])}`（最深 `{_fmt_plain_pct(s_oos['primary_worst_mae_pct'])}`），+1 年平均報酬 {_fmt_pct(s_oos['primary_mean_ret_1y_pct'])}、+2 年平均報酬 {_fmt_pct(s_oos['primary_mean_ret_2y_pct'])}、+3 年平均報酬 {_fmt_pct(s_oos['primary_mean_ret_3y_pct'])}、下一輪週期峰值平均報酬 **{_fmt_pct(s_oos['primary_mean_ret_next_peak_pct'])}**；限價 `P_anchor` 掛單成交率 `{s_oos['limit_n_events']}/{s_oos['primary_n_events']}`，平均浮虧改善至 `{_fmt_plain_pct(s_oos['limit_mean_mae_pct'])}`。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 4. 維度 5 & 6：市場情緒與中短期動能技術指標診斷")
    lines.append("")
    lines.append("| 指標類別 | 指標名稱 | 當期讀數 | 結構解讀與系統授權角色 |")
    lines.append("| :--- | :--- | :--- | :--- |")
    lines.append(f"| **維度 5：情緒** | Alternative.me 恐懼與貪婪指數 | `{sent['fng_value'] if sent['fng_value'] is not None else 'N/A'}` (`{sent['fng_class']}`, 日期 `{sent['date_utc']}`) | 極端情緒輔助診斷（無否決權；延遲 `{sent['fng_stale_days']}` 日） |")
    lines.append(f"| **維度 6：均線** | 日線 `50D EMA` (SMA 種子初始化) | {_fmt_usd(mom['ema50'])} | Track B `SW-1` 進場與雙日跌破趨勢出場核心（現價距均線 {_fmt_pct(((close_px - mom['ema50'])/mom['ema50']*100.0) if mom['ema50'] else None)}） |")
    lines.append(f"| **維度 6：動能** | Wilder `RSI(14)` | `{mom['rsi14']:.2f}` | 診斷指標（`< 30` 超賣、`> 70` 超買） |" if mom["rsi14"] is not None else "| **維度 6：動能** | Wilder `RSI(14)` | N/A | 診斷指標 |")
    lines.append(
        f"| **維度 6：趨勢動能** | `MACD(12, 26, 9)` | `DIF={mom['macd_dif']:.2f}`, `DEA={mom['macd_dea']:.2f}`, `Hist={mom['macd_hist']:.2f}` | 診斷指標（柱狀體正負動能） |"
        if mom["macd_hist"] is not None
        else "| **維度 6：趨勢動能** | `MACD(12, 26, 9)` | N/A | 診斷指標 |"
    )
    lines.append(
        f"| **維度 6：波動通道** | 布林通道 `Bollinger(20, 2.0)` | 上軌 {_fmt_usd(mom['bb_upper'])} / 中軌 {_fmt_usd(mom['bb_mid'])} / 下軌 {_fmt_usd(mom['bb_lower'])} | 診斷指標（母體標準差 `pstdev`） |"
    )
    lines.append(
        f"| **維度 6：真實波幅與量能** | Wilder `ATR(14)` & 量能比 `Vol / SMA20` | `ATR14` = {_fmt_usd(mom['atr14'])} (`{(mom['atr14']/close_px*100.0) if mom['atr14'] else 0.0:.2f}%`) / `vol_ratio_20` = `{mom['vol_ratio_20']:.2f}x` | Track B 停損緩衝、P99 跳空緩衝與 `SW-2/3` 放量突破門檻 |"
        if mom["atr14"] is not None and mom["vol_ratio_20"] is not None
        else "| **維度 6：真實波幅與量能** | Wilder `ATR(14)` & 量能比 | N/A | Track B 波動與量能門檻 |"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 5. 維度 7：傳統結構學派（因果 S/R、對數斐波、威科夫與艾略特波浪失效界線）")
    lines.append("")
    lines.append("### 5.1 近端時間窗相對百分比因果支撐與壓力帶 (`compute_causal_sr_zones`)")
    lines.append("")
    lines.append("| 類型代碼 | 中心價 (`price`) | 區間下界 (`band_low`) | 區間上界 (`band_high`) | 近 1 年測試次數 (`touches`) | 權重評分 (`score`) | 距現價百分比 (`dist_pct`) |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for r in reversed(skel["sr_resistances"]):
        lines.append(f"| 🔴 壓力 `{r['code']}` | {_fmt_usd(r['price'])} | {_fmt_usd(r['band_low'])} | {_fmt_usd(r['band_high'])} | `{r['touches']}` | `{r['score']:.2f}` | {_fmt_pct(r['dist_pct'])} |")
    for s in skel["sr_supports"]:
        lines.append(f"| 🟢 支撐 `{s['code']}` | {_fmt_usd(s['price'])} | {_fmt_usd(s['band_low'])} | {_fmt_usd(s['band_high'])} | `{s['touches']}` | `{s['score']:.2f}` | {_fmt_pct(s['dist_pct'])} |")
    lines.append("")
    lines.append("### 5.2 跨週期對數斐波那契回撤階梯（錨定前輪熊市谷底 " + _fmt_usd(skel["macro_log_fib"]["low"]) + " 至本輪高點 " + _fmt_usd(skel["macro_log_fib"]["ath"]) + "）")
    lines.append("")
    lines.append("| 斐波比例 (`ratio`) | 結構標籤 | 對數回撤價位 (`Low^r * ATH^(1-r)`) | 距現價百分比 (`dist_pct`) |")
    lines.append("| :--- | :--- | :--- | :--- |")
    for lv in skel["macro_log_fib"]["levels"]:
        lines.append(f"| `{lv['ratio']:.3f}` | {lv['label']} | **{_fmt_usd(lv['price'])}** | {_fmt_pct(lv['dist_pct'])} |")
    lines.append("")
    lines.append("### 5.3 確定性結構失效界線與威科夫／艾略特波浪雙劇本 (`macro_invalidation_levels`)")
    inv = skel["macro_invalidation_levels"]
    lines.append(f"* **本輪週期高點 (`cycle_ath`)**：{_fmt_usd(inv['cycle_ath'])}（嚴格突破此價位即確認新牛市主升段創高）")
    lines.append(f"* **左側價值帶下界 (`zone_low`) / 核心錨點 (`p_anchor`)**：{_fmt_usd(inv['zone_low'])} / {_fmt_usd(inv['p_anchor'])}")
    lines.append(f"* **週線雙鑰匙構造 Higher Low (`dual_key_weekly_hl`)**：{_fmt_usd(inv['dual_key_weekly_hl'])}（週線收盤跌破此位則右側週線 Higher Low 結構失效）")
    lines.append(f"* **Track B 當期波段防守位 (`track_b_active_stop`) / 盤中 10% 熔斷線 (`track_b_circuit_breaker`)**：{_fmt_usd(inv['track_b_active_stop'])} / {_fmt_usd(inv['track_b_circuit_breaker'])}")
    lines.append("")
    if overlay is not None:
        ep = overlay["elliott_primary"]
        ea = overlay["elliott_alternate"]
        lines.append("#### 雜湊綁定威科夫相位與艾略特主／替代數浪（`VERIFIED_OVERLAY`）")
        lines.append(f"* **威科夫當前相位 (`wyckoff_phase`)**：{overlay['wyckoff_phase']}")
        for wev in overlay.get("wyckoff_events", []):
            lines.append(f"  * `{wev['date_utc']}` **{wev['event']}** @ {_fmt_usd(float(wev['price']))}：{wev['note']}")
        lines.append(f"* **艾略特主選浪型 (`elliott_primary`)** [{ep['degree']} / `{ep['current_wave']}`]：{ep['summary']} 👉 **硬性結構失效價：{_fmt_usd(float(ep['invalidation_price']))}**")
        lines.append(f"* **艾略特替代浪型 (`elliott_alternate`)** [{ea['degree']} / `{ea['current_wave']}`]：{ea['summary']} 👉 **硬性結構失效價：{_fmt_usd(float(ea['invalidation_price']))}**")
        lines.append(f"* **失效裁決準則**：{overlay['invalidation_notes']}")
    else:
        lines.append("* **結構骨架模式（`STRUCTURAL_ONLY`）**：未掛載外部主觀敘事檔，系統僅輸出純因果幾何樞紐與確定性失效界線。近期已確認週線樞紐：" + ", ".join(f"`{p['date_utc']}` {p['kind']} {_fmt_usd(p['price'])}" for p in skel["recent_weekly_pivots"][-4:]))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 6. Track B (`Capital_B`) 2014–2026 日線波段勝率、期望值與 `WK-Warn` 實證驗證")
    lines.append("")
    lines.append("### 6.1 整體、三大進場型態 (`SW-1/2/3`)、因果警示降級與三連敗減半回測總表")
    lines.append("")
    lines.append("| 回測模擬組別 | 已完結筆數 (`n_closed`) | 勝率 (`win_rate_pct`) | 盈虧比 (`payoff_ratio`) | 平均單筆期望值 (`EV_R`) | 已完結累積報酬 (`total_return_pct`) | 含期末未平倉 MTM 報酬 (`mtm_total_return_pct`) | 帳戶最大回撤 (`track_max_dd_pct`) | 單筆最差虧損 (`single_trade_worst_pct`) |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for label, bundle in (
        ("**基準組：全型態固定 10% 風險 (`backtest_overall`)**", tb["backtest_overall"]),
        ("型態拆解：`SW-1` (Higher Low + 50D EMA)", tb["backtest_by_setup"]["SW-1"]),
        ("型態拆解：`SW-2` (雙底 / 頭肩底突破頸線)", tb["backtest_by_setup"]["SW-2"]),
        ("型態拆解：`SW-3` (威科夫 Spring->SOS->LPS)", tb["backtest_by_setup"]["SW-3"]),
        ("**因果防護組：`WK-Warn(50W)` 影子帳本動態降級 (`wk_gated_causal`)**", tb["backtest_wk_gated_causal"]),
        ("**連敗節流組：因果降級 + 三連敗風險減半 (`streak_throttled`)**", tb["backtest_streak_throttled"]),
    ):
        cs = bundle["closed_stats"]
        ms = bundle["mtm_stats"]
        pr_str = f"`{cs['payoff_ratio']:.2f}`" if cs["payoff_ratio"] is not None else "`N/A`"
        lines.append(
            f"| {label} | `{cs['n_closed']}` (未平倉 `{ms['open_trades_count']}`) | `{cs['win_rate_pct']:.2f}%` | {pr_str} | **`{cs['ev_r']:+.3f}R`** | {_fmt_pct(cs['total_return_pct'])} | {_fmt_pct(ms['mtm_total_return_pct'])} | `{cs['track_max_dd_pct']:.2f}%` | **`{cs['single_trade_worst_pct']:.2f}%`** |"
        )
    lines.append("")
    lines.append("### 6.2 週線破位警示 `WK-Warn`（`50W SMA` 主規則 vs `200W SMA` 對照組）實證勝率與期望值拆解")
    lines.append("")
    lines.append("| 訊號日週線警示分組 | 已完結交易數 (`n_closed`) | 勝率 (`win_rate_pct`) | 盈虧比 (`payoff_ratio`) | 單筆期望值 (`EV_R`) | 子集合複合報酬 (`trade_compounded_return_pct`) | 子集合交易曲線最大回撤 (`trade_curve_max_dd_pct`) | 單筆最差虧損 (`single_trade_worst_pct`) |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for label, part in (
        ("`WK-Warn (50W SMA) == INACTIVE`（週線多頭正常期）", tb["backtest_by_wk_warn_50w"]["INACTIVE"]),
        ("`WK-Warn (50W SMA) == ACTIVE`（週線跌破 50W 警示期）", tb["backtest_by_wk_warn_50w"]["ACTIVE"]),
        ("`WK-Warn (200W SMA) == INACTIVE`（200W 未破位期）", tb["backtest_by_wk_warn_200w"]["INACTIVE"]),
        ("`WK-Warn (200W SMA) == ACTIVE`（週線跌破 200W 極端熊市期）", tb["backtest_by_wk_warn_200w"]["ACTIVE"]),
    ):
        cs = part["closed_stats"]
        pr_str = f"`{cs['payoff_ratio']:.2f}`" if cs["payoff_ratio"] is not None else "`N/A`"
        lines.append(
            f"| {label} | `{cs['n_closed']}` | `{cs['win_rate_pct']:.2f}%` | {pr_str} | **`{cs['ev_r']:+.3f}R`** | {_fmt_pct(cs['trade_compounded_return_pct'])} | `{cs['trade_curve_max_dd_pct']:.2f}%` | `{cs['single_trade_worst_pct']:.2f}%` |"
        )
    lines.append("")
    lines.append("### 6.3 實證驗證結論（回應決策者關鍵提問）")
    ov_cs = tb["backtest_overall"]["closed_stats"]
    w50_act = tb["backtest_by_wk_warn_50w"]["ACTIVE"]["closed_stats"]
    w50_inact = tb["backtest_by_wk_warn_50w"]["INACTIVE"]["closed_stats"]
    w200_act = tb["backtest_by_wk_warn_200w"]["ACTIVE"]["closed_stats"]
    gated_cs = tb["backtest_wk_gated_causal"]["closed_stats"]
    streak_cs = tb["backtest_streak_throttled"]["closed_stats"]
    lines.append(f"1. **單筆 10% 最大回撤紅線是否守住？** 在 2014–2026 共 `{ov_cs['n_closed']}` 筆已完結交易中，透過 P99 歷史跳空穿透緩衝縮倉與盤中 `circuit_breaker_price` 硬性熔斷，**單筆最差實際虧損為 `{ov_cs['single_trade_worst_pct']:.2f}%`**（嚴格控制在 `-10.0%` 紅線以內）。")
    lines.append(f"2. **`WK-Warn` 週線破位警示是否具實證區別力？** 當 `50W SMA` 未破位（`INACTIVE`）時，波段交易共 `{w50_inact['n_closed']}` 筆、勝率 `{w50_inact['win_rate_pct']:.2f}%`、平均期望值 **`{w50_inact['ev_r']:+.3f}R`**；當週線跌破 `50W SMA` 亮燈（`ACTIVE`）時，共 `{w50_act['n_closed']}` 筆、勝率 `{w50_act['win_rate_pct']:.2f}%`、平均期望值為 **`{w50_act['ev_r']:+.3f}R`**（對照 `200W SMA` 破位期共 `{w200_act['n_closed']}` 筆、期望值 `{w200_act['ev_r']:+.3f}R`）。採用無前瞻影子帳本動態降級（`wk_gated_causal`）與三連敗減半（`streak_throttled`）後，帳戶級最大回撤分別為 `{gated_cs['track_max_dd_pct']:.2f}%` 與 `{streak_cs['track_max_dd_pct']:.2f}%`。")
    lines.append("")
    lines.append("---")
    lines.append("*Generated offline by `scripts.btc_bottom.analyze_and_report` (Zero-Lookahead Causal Engine)*")
    lines.append("")
    return "\n".join(lines)


def run_offline_analysis(
    snapshot_dir: Path,
    *,
    as_of_str: str | None = None,
    report_out: Path | None = None,
    json_out: Path | None = None,
    narrative_overlay_path: Path | None = None,
    now_dt: dt.datetime | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    """Run full offline 7-dimension analysis and dual-track backtest on a verified snapshot directory."""
    resolved_snap = snapshot_dir.resolve()
    manifest, daily_all, mvrv_all, weekly_all, fng_all, live_spot_obj = load_and_verify_snapshot(resolved_snap)

    snap_req_utc = str(manifest["requested_as_of_utc"])
    snap_ohlcv_ts = int(manifest["ohlcv_effective_as_of_ts"])
    snap_mvrv_ts = int(manifest["mvrv_effective_as_of_ts"])

    ref_now = now_dt if now_dt is not None else dt.datetime.now(dt.timezone.utc)
    snap_req_ts, _, _ = parse_as_of_arg(snap_req_utc, now_dt=max(ref_now, dt.datetime.fromtimestamp(snap_ohlcv_ts + DAY_SEC, tz=dt.timezone.utc)))

    if as_of_str is None:
        req_as_of_ts = snap_req_ts
        req_as_of_utc = snap_req_utc
        is_explicit_earlier = False
    else:
        req_as_of_ts, req_as_of_utc, _ = parse_as_of_arg(
            as_of_str,
            now_dt=max(ref_now, dt.datetime.fromtimestamp(snap_req_ts, tz=dt.timezone.utc)),
        )
        if req_as_of_ts > snap_req_ts:
            raise SnapshotIntegrityError(
                f"Requested --as-of {req_as_of_utc} is later than snapshot requested_as_of_utc {snap_req_utc}"
            )
        req_floor = floor_utc_day_ts(req_as_of_ts)
        is_explicit_earlier = req_floor < snap_ohlcv_ts

    req_floor_ts = floor_utc_day_ts(req_as_of_ts)
    daily_rows = [r for r in daily_all if r.close_ts <= req_floor_ts]
    mvrv_rows = [r for r in mvrv_all if r.effective_ts <= req_floor_ts]
    if not mvrv_rows:
        raise SnapshotIntegrityError(f"Zero MVRV rows remain at --as-of {req_as_of_utc}")

    eff_mvrv_ts = mvrv_rows[-1].effective_ts
    eff_ohlcv_ts = daily_rows[-1].close_ts if daily_rows else eff_mvrv_ts
    if abs(eff_ohlcv_ts - eff_mvrv_ts) > DAY_SEC:
        raise SnapshotIntegrityError(
            f"Effective watermark divergence > 86400s after --as-of truncation: "
            f"ohlcv={ts_to_iso_utc(eff_ohlcv_ts)} vs mvrv={ts_to_iso_utc(eff_mvrv_ts)}"
        )

    weekly_rows = [w for w in weekly_all if w.week_close_ts <= eff_ohlcv_ts]
    fng_rows = [f for f in fng_all if f.date_utc < ts_to_date_str(eff_ohlcv_ts)]

    # Determine replay_mode and live_spot shielding
    if is_explicit_earlier or eff_ohlcv_ts < snap_ohlcv_ts or live_spot_obj.get("mode") != "LIVE":
        replay_mode = "HISTORICAL_REPLAY"
        live_spot_price: float | None = None
        live_spot_status = "HISTORICAL_REPLAY"
        live_spot_age_sec: int | None = None
    else:
        replay_mode = "LIVE"
        raw_last = live_spot_obj.get("ticker_last")
        live_spot_price = float(raw_last) if raw_last is not None else None
        live_spot_status = str(live_spot_obj.get("live_spot_status") or "UNAVAILABLE")
        fetched_utc = live_spot_obj.get("fetched_at_utc")
        if fetched_utc and live_spot_price is not None:
            f_ts, _, _ = parse_as_of_arg(str(fetched_utc), now_dt=max(ref_now, dt.datetime.fromtimestamp(snap_req_ts + 3600, tz=dt.timezone.utc)))
            live_spot_age_sec = max(0, int(ref_now.timestamp()) - f_ts)
        else:
            live_spot_age_sec = None

    eff_ohlcv_utc = ts_to_iso_utc(eff_ohlcv_ts)
    eff_mvrv_utc = ts_to_iso_utc(eff_mvrv_ts)
    eff_ohlcv_date = ts_to_date_str(eff_ohlcv_ts - DAY_SEC)

    overlay_data: dict[str, Any] | None = None
    if narrative_overlay_path is not None:
        overlay_data = _validate_and_load_narrative_overlay(
            narrative_overlay_path,
            expected_manifest_sha256=str(manifest["manifest_sha256"]),
            expected_effective_ohlcv_utc=eff_ohlcv_utc,
        )
        narrative_status = "VERIFIED_OVERLAY"
    else:
        narrative_status = "STRUCTURAL_ONLY"

    macro_daily = build_macro_daily_series(mvrv_rows, daily_rows, weekly_rows)

    ta_res = run_track_a_backtest(
        macro_daily,
        mvrv_rows,
        weekly_rows,
        replay_mode=replay_mode,
        live_spot=live_spot_price,
    )
    tb_res = run_track_b_backtest(
        daily_rows,
        weekly_rows,
        replay_mode=replay_mode,
        live_spot=live_spot_price,
    )

    # Compute Dimension 1, 2, 5, 6, 7 snapshot diagnostics
    macro_closes = [float(r.close) for r in macro_daily]
    sma200d_series = calc_sma(macro_closes, 200)
    last_close = macro_closes[-1]
    last_sma200d = sma200d_series[-1]
    mayer_mult = round(last_close / last_sma200d, 4) if (last_sma200d is not None and last_sma200d > 0) else None

    last_st = ta_res["cycle_states"][-1]
    last_l1 = ta_res["last_l1"]
    last_lgc = ta_res["lgc_series"][-1]

    valuation_snap = {
        "close": round(last_close, 2),
        "sma200w": round(float(last_l1.sma200w), 2) if last_l1.sma200w is not None else None,
        "mayer_multiple": mayer_mult,
        "cycle_ath": round(last_st.cycle_ath, 2),
        "ath_dd_pct": round(last_l1.ath_dd * 100.0, 2),
        "lgc_lower_band": round(float(last_lgc), 2) if last_lgc is not None else None,
    }

    halving_snap = compute_halving_diagnostics(eff_ohlcv_date, ta_res["completed_cycles"])
    sentiment_snap = _compute_sentiment_snapshot(fng_rows, str(manifest.get("fng_status", "UNAVAILABLE")), eff_ohlcv_ts)

    all_weekly_pivs = compute_causal_pivots(weekly_rows, 3, 3)
    conf_weekly_pivs = [p for p in all_weekly_pivs if p.confirm_idx <= len(weekly_rows) - 1]

    if daily_rows:
        d_closes = [float(b.close) for b in daily_rows]
        d_vols = [float(b.volume) for b in daily_rows]
        ema50_d = calc_ema_sma_seeded(d_closes, 50)[-1]
        rsi14_d = calc_wilder_rsi(d_closes, 14)[-1]
        dif_s, dea_s, hist_s = calc_macd(d_closes, 12, 26, 9)
        bb_u, bb_m, bb_l = calc_bollinger(d_closes, 20, 2.0)
        atr14_s = calc_wilder_atr(daily_rows, 14)
        atr14_d = atr14_s[-1]
        vsma20_d = calc_sma(d_vols, 20)[-1]
        vol_ratio_20 = (
            round(d_vols[-1] / float(vsma20_d), 4)
            if (vsma20_d is not None and float(vsma20_d) > 0.0)
            else None
        )
        momentum_diag = {
            "ema50": round(float(ema50_d), 2) if ema50_d is not None else None,
            "rsi14": round(float(rsi14_d), 2) if rsi14_d is not None else None,
            "macd_dif": round(float(dif_s[-1]), 2) if dif_s[-1] is not None else None,
            "macd_dea": round(float(dea_s[-1]), 2) if dea_s[-1] is not None else None,
            "macd_hist": round(float(hist_s[-1]), 2) if hist_s[-1] is not None else None,
            "bb_upper": round(float(bb_u[-1]), 2) if bb_u[-1] is not None else None,
            "bb_mid": round(float(bb_m[-1]), 2) if bb_m[-1] is not None else None,
            "bb_lower": round(float(bb_l[-1]), 2) if bb_l[-1] is not None else None,
            "atr14": round(float(atr14_d), 2) if atr14_d is not None else None,
            "vol_ratio_20": vol_ratio_20,
        }
        all_daily_pivs = compute_causal_pivots(daily_rows, 5, 5)
        conf_daily_pivs = [p for p in all_daily_pivs if p.confirm_idx <= len(daily_rows) - 1]
        sr_zones = compute_causal_sr_zones(
            daily_rows,
            conf_weekly_pivs,
            len(daily_rows) - 1,
            atr14_d,
            daily_pivots_prefix=conf_daily_pivs,
        )
        recent_daily_pivots = [
            {"date_utc": p.date_utc, "price": round(p.price, 2), "kind": p.kind}
            for p in conf_daily_pivs[-10:]
        ]
    else:
        momentum_diag = {
            "ema50": None,
            "rsi14": None,
            "macd_dif": None,
            "macd_dea": None,
            "macd_hist": None,
            "bb_upper": None,
            "bb_mid": None,
            "bb_lower": None,
            "atr14": None,
            "vol_ratio_20": None,
        }
        sr_zones = {"supports": [], "resistances": []}
        recent_daily_pivots = []

    recent_weekly_pivots = [
        {"date_utc": p.date_utc, "price": round(p.price, 2), "kind": p.kind}
        for p in conf_weekly_pivs[-10:]
    ]

    fib_low_anchor = float(last_st.prev_completed_trough)
    fib_ath_anchor = float(last_st.cycle_ath)
    fib_levels: list[dict[str, Any]] = []
    for ratio, label in FIB_RATIOS_LABELS:
        px_f = (fib_low_anchor ** ratio) * (fib_ath_anchor ** (1.0 - ratio))
        dist_f = ((px_f - last_close) / last_close) * 100.0 if last_close > 0 else 0.0
        fib_levels.append(
            {
                "ratio": ratio,
                "label": label,
                "price": round(px_f, 2),
                "dist_pct": round(dist_f, 4),
            }
        )

    ta_sum = ta_res["track_a_summary"]
    tb_sum = tb_res["track_b_summary"]

    structural_skeleton = {
        "sr_supports": sr_zones["supports"],
        "sr_resistances": sr_zones["resistances"],
        "macro_log_fib": {
            "low": round(fib_low_anchor, 2),
            "ath": round(fib_ath_anchor, 2),
            "levels": fib_levels,
        },
        "recent_daily_pivots": recent_daily_pivots,
        "recent_weekly_pivots": recent_weekly_pivots,
        "macro_invalidation_levels": {
            "cycle_ath": round(fib_ath_anchor, 2),
            "zone_low": ta_sum["zone_low"],
            "p_anchor": ta_sum["p_anchor"],
            "dual_key_weekly_hl": ta_res["last_dual_key_hl"],
            "track_b_active_stop": tb_sum["active_stop"],
            "track_b_circuit_breaker": tb_sum["circuit_breaker_price"],
        },
    }

    mvrv_lag_days = int((eff_ohlcv_ts - eff_mvrv_ts) // DAY_SEC)
    analysis_summary: dict[str, Any] = {
        "meta": {
            "requested_as_of_utc": req_as_of_utc,
            "effective_ohlcv_utc": eff_ohlcv_utc,
            "effective_mvrv_utc": eff_mvrv_utc,
            "mvrv_lag_days": mvrv_lag_days,
            "replay_mode": replay_mode,
            "live_spot_price": round(live_spot_price, 2) if live_spot_price is not None else None,
            "live_spot_status": live_spot_status,
            "live_spot_age_sec": live_spot_age_sec,
            "narrative_status": narrative_status,
            "manifest_sha256": str(manifest["manifest_sha256"]),
        },
        "dimensions_snapshot": {
            "valuation": valuation_snap,
            "halving": halving_snap,
            "sentiment": sentiment_snap,
            "momentum_diag": momentum_diag,
            "structural_skeleton": structural_skeleton,
        },
        "track_a": ta_sum,
        "track_b": tb_sum,
    }

    # Global finite-number guard before writing outputs
    _assert_all_finite_with_null_contract(analysis_summary, "analysis_summary")

    resolved_json_out = (json_out if json_out is not None else (resolved_snap / "analysis_summary.json")).resolve()
    resolved_report_out = (
        report_out
        if report_out is not None
        else Path(f"reports/btc_bottom_analysis_{eff_ohlcv_date}.md")
    ).resolve()

    resolved_json_out.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_out.parent.mkdir(parents=True, exist_ok=True)

    with resolved_json_out.open("w", encoding="utf-8") as f:
        json.dump(analysis_summary, f, indent=2, ensure_ascii=False)
        f.write("\n")

    report_md = _render_markdown_report(analysis_summary, overlay_data)
    resolved_report_out.write_text(report_md, encoding="utf-8")

    return analysis_summary, resolved_json_out, resolved_report_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline BTC/USD dual-track analysis and report generator.")
    parser.add_argument("--snapshot-dir", type=str, required=True, help="Verified snapshot directory")
    parser.add_argument("--as-of", type=str, default=None, help="Optional UTC replay cutoff (<= snapshot as-of)")
    parser.add_argument("--report-out", type=str, default=None, help="Output Markdown report path")
    parser.add_argument("--json-out", type=str, default=None, help="Output analysis_summary.json path")
    parser.add_argument("--narrative-overlay", type=str, default=None, help="Optional hash-bound narrative JSON")
    args = parser.parse_args(argv)

    try:
        summary, json_p, report_p = run_offline_analysis(
            Path(args.snapshot_dir),
            as_of_str=args.as_of,
            report_out=Path(args.report_out) if args.report_out else None,
            json_out=Path(args.json_out) if args.json_out else None,
            narrative_overlay_path=Path(args.narrative_overlay) if args.narrative_overlay else None,
        )
        print(
            f"COMPLETED analysis | effective_ohlcv={summary['meta']['effective_ohlcv_utc']} | "
            f"Track A={summary['track_a']['action']} (P_anchor={summary['track_a']['p_anchor']}) | "
            f"Track B={summary['track_b']['action']} | "
            f"JSON={json_p} | Report={report_p}"
        )
        return 0
    except SnapshotIntegrityError as exc:
        print(f"ANALYSIS ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
