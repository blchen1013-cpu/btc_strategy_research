"""Export historical right-side entry trade records (2014-2026) to Excel (.xlsx) and CSV.

Generates a multi-sheet formatted Excel workbook containing:
1. Sheet 1: 右側入場歷史明細 (標準10%風險) - All 60 sequential Track B trades (`backtest_overall`)
2. Sheet 2: 右側入場明細 (50W警示動態降級) - All 60 sequential Track B trades with causal WK-Warn gating (`wk_gated_causal`)
3. Sheet 3: 各型態獨立回測明細 (SW-1 / SW-2 / SW-3) - All standalone trades for SW-1 (43), SW-2 (28), SW-3 (16)
4. Sheet 4: 統計摘要與長線右側備援總表 - Portfolio & setup summary metrics + Track A right-side fallback verification
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any, Sequence

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from scripts.btc_bottom.backtest_engine import (
    _ClosedTradeRecord,
    run_track_a_backtest,
    run_track_b_backtest,
)
from scripts.btc_bottom.fetch_snapshots import (
    build_macro_daily_series,
    load_and_verify_snapshot,
)


SETUP_NAME_ZH: dict[str, str] = {
    "SW-1": "SW-1 (日線更高低點 HL + 站上50D EMA)",
    "SW-2": "SW-2 (雙底/頭肩底突破頸線)",
    "SW-3": "SW-3 (威科夫 Spring -> SOS -> LPS)",
}

SUBTYPE_NAME_ZH: dict[str, str] = {
    "HL_EMA50": "日線更高低點 HL + 收盤重返 50D EMA",
    "HL_EMA50_RECLAIM": "日線更高低點 HL + 收盤重返 50D EMA",
    "DOUBLE_BOTTOM": "W底 / 雙底頸線突破",
    "INV_HS": "頭肩底頸線突破",
    "INV_HEAD_SHOULDERS": "頭肩底頸線突破",
    "WYCKOFF_LPS": "威科夫破底翻 Spring -> SOS -> LPS 守住",
}

EXIT_DETAIL_ZH: dict[str, str] = {
    "INITIAL_STRUCTURAL_STOP_CLOSE_BREAK": "日線收盤跌破初始結構停損 (T+1開盤出場)",
    "TRAILING_OR_BE_STOP_CLOSE_BREAK": "日線收盤跌破移動/保本停損 (T+1開盤出場)",
    "EMA50_TWO_DAY_CLOSE_BREAK": "連續2日收盤跌破50日EMA (T+1開盤出場)",
    "STRUCTURAL_AND_EMA50_CLOSE_BREAK": "同時跌破結構停損與連續2日50D EMA (T+1開盤出場)",
    "CIRCUIT_BREAKER_INTRADAY": "盤中觸及 10% 最大回撤熔斷價 (當日熔斷出場)",
    "CIRCUIT_BREAKER_OPEN_GAP": "隔日開盤跳空跌破 10% 熔斷價 (開盤熔斷出場)",
    "SCHEDULED_NEXT_OPEN_EXIT": "日線收盤結構/均線失效 (T+1開盤出場)",
}


def _style_header_row(ws: Any, row_idx: int, col_count: int, fill_hex: str = "1F4E78") -> None:
    header_fill = PatternFill(start_color=fill_hex, end_color=fill_hex, fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="medium", color="1F4E78"),
    )
    for col in range(1, col_count + 1):
        cell = ws.cell(row=row_idx, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border
    ws.row_dimensions[row_idx].height = 28


def _write_trade_sheet(
    ws: Any,
    title_banner: str,
    trades: Sequence[_ClosedTradeRecord],
    *,
    include_setup_group_col: bool = False,
) -> None:
    # Title banner row
    ws.cell(row=1, column=1, value=title_banner)
    ws.cell(row=1, column=1).font = Font(name="Calibri", size=13, bold=True, color="1F4E78")
    ws.row_dimensions[1].height = 24

    headers = [
        "交易序號 (#)",
        "訊號日期 (Signal Date)",
        "入場時間 (Entry Date)",
        "出場時間 (Exit Date)",
        "持倉天數 (Days)",
        "進場型態 (Setup)",
        "型態細項說明 (Subtype)",
        "入場價 (Entry Price USD)",
        "停損價-初始結構 (Initial Stop USD)",
        "初始停損距離 (Stop Dist %)",
        "停損價-出場前最終移動/保本 (Final Active Stop USD)",
        "停損價-盤中10%硬性熔斷 (Circuit Breaker USD)",
        "出場價 (Exit Price USD)",
        "交易結果 (Result)",
        "現貨漲跌幅 (Spot Return %)",
        "單筆R倍數 (Trade R)",
        "投入倉位比例 (Position Size %)",
        "單筆損益 (PnL USD)",
        "對總資金影響 (Account Impact %)",
        "累積總資金 (Cumulative Equity USD)",
        "出場觸發原因 (Exit Reason)",
        "50W週線警示 (WK-Warn 50W)",
        "200W週線警示 (WK-Warn 200W)",
    ]
    if include_setup_group_col:
        headers.insert(1, "獨立回測組別 (Group)")

    header_row = 3
    for col_idx, h_text in enumerate(headers, start=1):
        ws.cell(row=header_row, column=col_idx, value=h_text)
    _style_header_row(ws, header_row, len(headers))

    win_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
    loss_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
    win_font = Font(name="Calibri", size=10, bold=True, color="276A3C")
    loss_font = Font(name="Calibri", size=10, bold=True, color="C00000")
    regular_font = Font(name="Calibri", size=10)
    cell_border = Border(
        left=Side(style="thin", color="E0E0E0"),
        right=Side(style="thin", color="E0E0E0"),
        top=Side(style="thin", color="E0E0E0"),
        bottom=Side(style="thin", color="E0E0E0"),
    )

    for idx, tr in enumerate(trades, start=1):
        r_idx = header_row + idx
        days_held = tr.exit_idx - tr.entry_idx
        stop_dist_frac = (tr.entry_price - tr.structural_stop) / tr.entry_price if tr.entry_price > 0 else 0.0
        spot_ret_frac = (tr.exit_price - tr.entry_price) / tr.entry_price if tr.entry_price > 0 else 0.0
        acct_impact_frac = tr.pnl_usd / tr.equity_at_entry if tr.equity_at_entry > 0 else 0.0
        cum_equity = tr.equity_at_entry + tr.pnl_usd
        is_win = tr.pnl_usd > 0.0
        result_label = "獲利 (WIN)" if is_win else "虧損 (LOSS)"

        row_vals: list[Any] = [
            idx,
            tr.signal_date,
            tr.entry_date,
            tr.exit_date,
            days_held,
            SETUP_NAME_ZH.get(tr.setup_id, tr.setup_id),
            SUBTYPE_NAME_ZH.get(tr.subtype, tr.subtype),
            round(tr.entry_price, 2),
            round(tr.structural_stop, 2),
            stop_dist_frac,
            round(tr.final_active_stop, 2),
            round(tr.circuit_breaker_price, 2),
            round(tr.exit_price, 2),
            result_label,
            spot_ret_frac,
            round(tr.trade_r, 3),
            tr.position_size_pct,
            round(tr.pnl_usd, 2),
            acct_impact_frac,
            round(cum_equity, 2),
            EXIT_DETAIL_ZH.get(tr.exit_trigger_detail, tr.exit_trigger_detail),
            "ACTIVE (警示期內)" if tr.wk_warn_50w_at_signal else "INACTIVE (正常)",
            "ACTIVE (警示期內)" if tr.wk_warn_200w_at_signal else "INACTIVE (正常)",
        ]
        if include_setup_group_col:
            row_vals.insert(1, f"{tr.setup_id} 獨立回測")

        for c_idx, val in enumerate(row_vals, start=1):
            c = ws.cell(row=r_idx, column=c_idx, value=val)
            c.font = regular_font
            c.border = cell_border
            col_header = headers[c_idx - 1]

            if "USD" in col_header or "入場價" in col_header or "出場價" in col_header or "停損價" in col_header:
                c.number_format = "$#,##0.00"
                c.alignment = Alignment(horizontal="right", vertical="center")
            elif "%" in col_header:
                c.number_format = "0.00%"
                c.alignment = Alignment(horizontal="right", vertical="center")
            elif "Trade R" in col_header:
                c.number_format = '+0.00"R";-0.00"R";0.00"R"'
                c.alignment = Alignment(horizontal="right", vertical="center")
                c.font = win_font if is_win else loss_font
            elif col_header in ("交易序號 (#)", "持倉天數 (Days)"):
                c.alignment = Alignment(horizontal="center", vertical="center")
            elif "Date" in col_header:
                c.alignment = Alignment(horizontal="center", vertical="center")
            elif col_header == "交易結果 (Result)":
                c.fill = win_fill if is_win else loss_fill
                c.font = win_font if is_win else loss_font
                c.alignment = Alignment(horizontal="center", vertical="center")
            else:
                c.alignment = Alignment(horizontal="left", vertical="center")

    ws.freeze_panes = f"A{header_row + 1}"
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(headers))}{header_row + len(trades)}"

    # Adjust column widths
    for col_idx, h_text in enumerate(headers, start=1):
        col_letter = get_column_letter(col_idx)
        max_len = max(
            len(str(ws.cell(row=r, column=col_idx).value or ""))
            for r in range(header_row, header_row + len(trades) + 1)
        )
        ws.column_dimensions[col_letter].width = max(min(max_len + 5, 42), 13)


def _write_summary_sheet(
    ws: Any,
    tb_summary: dict[str, Any],
    ta_summary: dict[str, Any],
) -> None:
    ws.cell(row=1, column=1, value="BTC 右側入場歷史回測統計總表 (2014-09-17 至 2026-11-13)")
    ws.cell(row=1, column=1).font = Font(name="Calibri", size=14, bold=True, color="1F4E78")
    ws.row_dimensions[1].height = 26

    headers = [
        "回測模式 / 策略分類",
        "已平倉筆數 (Closed Trades)",
        "勝率 (Win Rate %)",
        "盈虧比 (Payoff Ratio)",
        "單筆期望值 (Expectancy R)",
        "累積總報酬率 (Total Return %)",
        "帳戶最大回撤 (Account Max DD %)",
        "單筆最差虧損 (Worst Trade Impact %)",
        "策略規則說明",
    ]
    header_row = 3
    for col_idx, h_text in enumerate(headers, start=1):
        ws.cell(row=header_row, column=col_idx, value=h_text)
    _style_header_row(ws, header_row, len(headers))

    rows_data = [
        (
            "Track B 整體標準組合 (固定 10% 風險預算)",
            tb_summary["backtest_overall"]["closed_stats"],
            "SW-1 / SW-2 / SW-3 優先序進場，單筆結構失效風險上限 10%，含盤中熔斷與 +1.5R 移至保本",
        ),
        (
            "Track B 週線 50W 警示動態降級組合 (Causal WK-Gated)",
            tb_summary["backtest_wk_gated_causal"]["closed_stats"],
            "當 WK-Warn (50W) 觸發且歷史樣本達標時，自動依期望值維持 10% 或降級至 5% 風險預算",
        ),
        (
            "Track B 連敗降載組合 (Streak-Throttled 3連敗降5%)",
            tb_summary["backtest_streak_throttled"]["closed_stats"],
            "連續 3 筆已平倉交易 < 0R 時自動降級至 5% 風險，直到出現單筆 >= +1.5R 獲利恢復 10%",
        ),
        (
            "SW-1 獨立回測 (日線 HL + 重返 50D EMA)",
            tb_summary["backtest_by_setup"]["SW-1"]["closed_stats"],
            "僅交易 SW-1 型態，停損設於確認更高低點 L2 - 0.5*ATR14",
        ),
        (
            "SW-2 獨立回測 (雙底 / 頭肩底突破頸線)",
            tb_summary["backtest_by_setup"]["SW-2"]["closed_stats"],
            "僅交易 SW-2 型態，停損設於第二底或右肩低點 - 0.5*ATR14",
        ),
        (
            "SW-3 獨立回測 (威科夫 Spring -> SOS -> LPS)",
            tb_summary["backtest_by_setup"]["SW-3"]["closed_stats"],
            "僅交易 SW-3 型態，停損設於 min(Spring 低點, LPS 低點 - 0.25*ATR14)",
        ),
        (
            "WK-Warn (50W) ACTIVE 期間進場子集",
            tb_summary["backtest_by_wk_warn_50w"]["ACTIVE"]["closed_stats"],
            "週線剛跌破 50W SMA 後 4 週警示視窗內觸發的右側交易",
        ),
        (
            "WK-Warn (50W) INACTIVE 期間進場子集",
            tb_summary["backtest_by_wk_warn_50w"]["INACTIVE"]["closed_stats"],
            "非 50W 週線跌破警示期內觸發的右側交易",
        ),
        (
            "WK-Warn (200W) ACTIVE 期間進場子集",
            tb_summary["backtest_by_wk_warn_200w"]["ACTIVE"]["closed_stats"],
            "週線剛跌破 200W SMA 後 4 週警示視窗內觸發的右側交易",
        ),
        (
            "WK-Warn (200W) INACTIVE 期間進場子集",
            tb_summary["backtest_by_wk_warn_200w"]["INACTIVE"]["closed_stats"],
            "非 200W 週線跌破警示期內觸發的右側交易",
        ),
    ]

    regular_font = Font(name="Calibri", size=10)
    bold_font = Font(name="Calibri", size=10, bold=True)
    cell_border = Border(
        left=Side(style="thin", color="E0E0E0"),
        right=Side(style="thin", color="E0E0E0"),
        top=Side(style="thin", color="E0E0E0"),
        bottom=Side(style="thin", color="E0E0E0"),
    )

    for idx, (label, st, desc) in enumerate(rows_data, start=1):
        r_idx = header_row + idx
        tot_ret = st.get("total_return_pct", st.get("trade_compounded_return_pct", 0.0))
        max_dd = st.get("track_max_dd_pct", st.get("trade_curve_max_dd_pct", 0.0))
        vals = [
            label,
            st["n_closed"],
            st["win_rate_pct"] / 100.0,
            st["payoff_ratio"],
            st["ev_r"],
            tot_ret / 100.0,
            max_dd / 100.0,
            st["single_trade_worst_pct"] / 100.0,
            desc,
        ]
        for c_idx, val in enumerate(vals, start=1):
            c = ws.cell(row=r_idx, column=c_idx, value=val)
            c.font = bold_font if idx <= 3 else regular_font
            c.border = cell_border
            if c_idx == 2:
                c.alignment = Alignment(horizontal="center", vertical="center")
            elif c_idx in (3, 6, 7, 8):
                c.number_format = "0.00%"
                c.alignment = Alignment(horizontal="right", vertical="center")
            elif c_idx == 4:
                c.number_format = "0.00"
                c.alignment = Alignment(horizontal="right", vertical="center")
            elif c_idx == 5:
                c.number_format = '+0.000"R";-0.000"R";0.000"R"'
                c.alignment = Alignment(horizontal="right", vertical="center")
            else:
                c.alignment = Alignment(horizontal="left", vertical="center")

    # Add Track A Right-Fallback note below table
    note_row = header_row + len(rows_data) + 3
    ws.cell(row=note_row, column=1, value="補充：Track A (長線部位) 時間轉換右側備援 (RIGHT_FALLBACK) 歷史觸發紀錄")
    ws.cell(row=note_row, column=1).font = Font(name="Calibri", size=12, bold=True, color="1F4E78")

    rf_events = ta_summary["backtest_stats"]["dual_key_events"]
    ws.cell(
        row=note_row + 1,
        column=1,
        value=(
            f"歷史回測結果 (2010–2026)：共觸發 {len(rf_events)} 次 RIGHT_FALLBACK。"
            "原因：歷次熊市 (2011、2015、2018、2022) 均在 L1 估值門檻 (>=2/3) 成立期間直接完成左側建倉 (LEFT_MARKET / LEFT_TRANCHE)，"
            "未曾發生錯過左側底部而被迫啟動長線右側備援之情況。"
        ),
    )
    ws.cell(row=note_row + 1, column=1).font = regular_font

    for col_idx in range(1, len(headers) + 1):
        col_letter = get_column_letter(col_idx)
        widths = [38, 18, 16, 16, 18, 20, 20, 22, 58]
        ws.column_dimensions[col_letter].width = widths[col_idx - 1]


def export_right_side_trades(
    snapshot_dir: Path,
    excel_out: Path,
    csv_out: Path | None = None,
) -> tuple[Path, Path | None, dict[str, Any]]:
    """Run causal Track B (and Track A) backtest on snapshot_dir and write Excel (.xlsx) & CSV."""
    _, daily_bars, mvrv_rows, weekly_rows, _, _ = load_and_verify_snapshot(snapshot_dir.resolve())
    macro_daily = build_macro_daily_series(mvrv_rows, daily_bars, weekly_rows)

    tb_res = run_track_b_backtest(daily_bars, weekly_rows)
    ta_res = run_track_a_backtest(macro_daily, mvrv_rows, weekly_rows)

    overall_trades: list[_ClosedTradeRecord] = tb_res["overall_trades"]
    gated_trades: list[_ClosedTradeRecord] = tb_res["gated_trades"]
    setup_trades_map: dict[str, list[_ClosedTradeRecord]] = tb_res["setup_trades"]

    wb = openpyxl.Workbook()
    # Sheet 1: Overall baseline 10% risk right-side trades
    ws1 = wb.active
    ws1.title = "1_右側入場歷史明細(標準10%風險)"
    _write_trade_sheet(
        ws1,
        "BTC 波段右側入場歷史交易明細 — 標準 10% 結構風險預算 (2014–2026, 共 60 筆)",
        overall_trades,
    )

    # Sheet 2: WK-Warn 50W dynamically gated right-side trades
    ws2 = wb.create_sheet(title="2_右側入場明細(50W警示動態降級)")
    _write_trade_sheet(
        ws2,
        "BTC 波段右側入場歷史交易明細 — 週線 50W 警示動態降級組合 (單筆最大虧損壓低至 -5.60%)",
        gated_trades,
    )

    # Sheet 3: Combined standalone setup trades (SW-1, SW-2, SW-3)
    ws3 = wb.create_sheet(title="3_各型態獨立回測明細(SW1_2_3)")
    combined_setup_trades: list[_ClosedTradeRecord] = (
        list(setup_trades_map["SW-1"])
        + list(setup_trades_map["SW-2"])
        + list(setup_trades_map["SW-3"])
    )
    _write_trade_sheet(
        ws3,
        "BTC 各右側型態獨立回測交易明細 — SW-1 (43筆) / SW-2 (28筆) / SW-3 (16筆)",
        combined_setup_trades,
        include_setup_group_col=True,
    )

    # Sheet 4: Summary table
    ws4 = wb.create_sheet(title="4_統計摘要總表")
    _write_summary_sheet(ws4, tb_res["track_b_summary"], ta_res["track_a_summary"])

    excel_out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(excel_out)

    if csv_out is not None:
        csv_out.parent.mkdir(parents=True, exist_ok=True)
        with csv_out.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "交易序號",
                    "訊號日期",
                    "入場時間",
                    "出場時間",
                    "持倉天數",
                    "進場型態",
                    "型態細項",
                    "入場價_USD",
                    "初始結構停損價_USD",
                    "初始停損距離_pct",
                    "最終移動或保本停損價_USD",
                    "盤中10pct熔斷價_USD",
                    "出場價_USD",
                    "交易結果",
                    "現貨漲跌幅_pct",
                    "單筆R倍數",
                    "投入倉位比例_pct",
                    "單筆損益_USD",
                    "對總資金影響_pct",
                    "累積總資金_USD",
                    "出場觸發原因",
                    "50W週線警示",
                    "200W週線警示",
                ]
            )
            for idx, tr in enumerate(overall_trades, start=1):
                days_held = tr.exit_idx - tr.entry_idx
                stop_dist_pct = (
                    ((tr.entry_price - tr.structural_stop) / tr.entry_price) * 100.0
                    if tr.entry_price > 0
                    else 0.0
                )
                spot_ret_pct = (
                    ((tr.exit_price - tr.entry_price) / tr.entry_price) * 100.0
                    if tr.entry_price > 0
                    else 0.0
                )
                acct_impact_pct = (
                    (tr.pnl_usd / tr.equity_at_entry) * 100.0 if tr.equity_at_entry > 0 else 0.0
                )
                cum_eq = tr.equity_at_entry + tr.pnl_usd
                writer.writerow(
                    [
                        idx,
                        tr.signal_date,
                        tr.entry_date,
                        tr.exit_date,
                        days_held,
                        tr.setup_id,
                        tr.subtype,
                        round(tr.entry_price, 2),
                        round(tr.structural_stop, 2),
                        round(stop_dist_pct, 2),
                        round(tr.final_active_stop, 2),
                        round(tr.circuit_breaker_price, 2),
                        round(tr.exit_price, 2),
                        "獲利 (WIN)" if tr.pnl_usd > 0 else "虧損 (LOSS)",
                        round(spot_ret_pct, 2),
                        round(tr.trade_r, 3),
                        round(tr.position_size_pct * 100.0, 2),
                        round(tr.pnl_usd, 2),
                        round(acct_impact_pct, 2),
                        round(cum_eq, 2),
                        EXIT_DETAIL_ZH.get(tr.exit_trigger_detail, tr.exit_trigger_detail),
                        "ACTIVE" if tr.wk_warn_50w_at_signal else "INACTIVE",
                        "ACTIVE" if tr.wk_warn_200w_at_signal else "INACTIVE",
                    ]
                )

    return excel_out, csv_out, tb_res


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export BTC right-side historical trade records to Excel (.xlsx).")
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=Path("data/snapshots/latest"),
        help="Path to verified snapshot directory",
    )
    parser.add_argument(
        "--excel-out",
        type=Path,
        default=Path("reports/btc_right_side_trades_2014_2026.xlsx"),
        help="Output path for Excel (.xlsx) workbook",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=Path("reports/btc_right_side_trades_2014_2026.csv"),
        help="Output path for companion UTF-8 BOM CSV file",
    )
    args = parser.parse_args(argv)
    xlsx_path, csv_path, tb_res = export_right_side_trades(args.snapshot_dir, args.excel_out, args.csv_out)
    print(f"Exported {len(tb_res['overall_trades'])} trades to Excel: {xlsx_path}")
    if csv_path:
        print(f"Exported companion CSV: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
