"""Generate TradingView Pine Script v5 for the v2 Anti-Hostile-Exchange Institutional Occam VCP Strategy."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.btc_bottom.fetch_snapshots import load_and_verify_snapshot
from scripts.btc_bottom.vcp_bull_strategy import (
    VCPPosition,
    prepare_vcp_environment,
    simulate_vcp_3tranche_strategy,
)


def date_to_int(d_str: str) -> int:
    return int(d_str.replace("-", ""))


def generate_pine_script() -> str:
    root = Path(__file__).resolve().parents[2]
    snap = root / "data" / "snapshots" / "latest"
    _, daily_bars, mvrv_rows, weekly_bars, _, _ = load_and_verify_snapshot(snap)
    env = prepare_vcp_environment(daily_bars, mvrv_rows, weekly_bars)
    sim = simulate_vcp_3tranche_strategy(env)
    records = sim["trade_records"]
    n_tr = len(records)

    lines: list[str] = [
        "// This Pine Script™ code is subject to the terms of the Mozilla Public License 2.0",
        "// © BTC Quant Analyzer — Occam's Razor VCP v2 Anti-Hostile-Exchange Institutional Strategy (Zero-Lookahead)",
        "//@version=5",
        f'indicator("BTC 奧卡姆 VCP v2 反險惡交易所實盤版 [完整 {n_tr} 筆點位 & 雙層停損 & 50W保底]", shorttitle="BTC_Occam_VCP_v2", overlay=true, max_labels_count=500, max_lines_count=500, max_boxes_count=500, max_bars_back=500)',
        "",
        "// ============================================================================",
        "// 1. 使用者顯示設定 (Inputs)",
        "// ============================================================================",
        'grp_vis = "圖表視覺化開關"',
        f'show_hist_trades  = input.bool(true,  "顯示 2015–2026 回測 {n_tr} 筆確認交易點位", group=grp_vis)',
        'show_sub_entries  = input.bool(true,  "顯示 E1+E2(2/3開盤) 與 E3(1/3回踩/3日) 獨立標記", group=grp_vis)',
        'show_exit_labels  = input.bool(true,  "顯示 X1(+2R) / X2(減半&50W保底) / X3(轉弱) 出場標籤", group=grp_vis)',
        'show_stop_lines   = input.bool(true,  "繪製內層收盤停損線(橘虛) / 外層1.6x災難防線(紅實) / +2R目標線", group=grp_vis)',
        'show_ma_lines     = input.bool(true,  "顯示核心均線 (20/30/50D EMA、200D SMA 與 50W SMA)", group=grp_vis)',
        'show_live_scanner = input.bool(true,  "開啟即時奧卡姆三支柱收盤確認與 50W 熊市保底掃描引擎", group=grp_vis)',
        'show_dashboard    = input.bool(true,  "顯示右上角 v2 策略戰情儀表板", group=grp_vis)',
        "",
        "// ============================================================================",
        "// 2. 核心均線與即時 v2 反插針掃描引擎 (100% 因果、零盤中 E1 誘多陷阱)",
        "// ============================================================================",
        "ema20  = ta.ema(close, 20)",
        "ema30  = ta.ema(close, 30)",
        "ema50  = ta.ema(close, 50)",
        "sma200 = ta.sma(close, 200)",
        "atr14  = ta.atr(14)",
        "vsma20 = ta.sma(volume, 20)",
        "",
        "// 嚴格因果週線 50W SMA (僅讀取已收盤週線，作為 X2 減半週期失效保底)",
        'wk_c1   = request.security(syminfo.tickerid, "W", close[1], lookahead=barmerge.lookahead_on)',
        'wk_c2   = request.security(syminfo.tickerid, "W", close[2], lookahead=barmerge.lookahead_on)',
        'wk_s501 = request.security(syminfo.tickerid, "W", ta.sma(close, 50)[1], lookahead=barmerge.lookahead_on)',
        'wk_s502 = request.security(syminfo.tickerid, "W", ta.sma(close, 50)[2], lookahead=barmerge.lookahead_on)',
        "wk_2below_50w = not na(wk_s501) and not na(wk_s502) and wk_c1 < wk_s501 and wk_c2 < wk_s502",
        "",
        'plot(show_ma_lines ? ema20   : na, "20D EMA (短線趨勢)",       color=color.new(#4CAF50, 30), linewidth=1)',
        'plot(show_ma_lines ? ema30   : na, "30D EMA (X3轉弱防守)",     color=color.new(#00BCD4, 30), linewidth=1)',
        'plot(show_ma_lines ? ema50   : na, "50D EMA (中期趨勢)",       color=color.new(#FF9800, 15), linewidth=2)',
        'plot(show_ma_lines ? sma200  : na, "200D SMA (日線牛熊線)",    color=color.new(#E91E63, 15), linewidth=2)',
        'plot(show_ma_lines ? wk_s501 : na, "50W SMA (X2熊市保底線)",   color=color.new(#AB47BC, 5),  linewidth=2)',
        "",
        "// 奧卡姆三支柱：嚴格於日線收盤 Close[d] 確認帶量長紅 K（徹底免疫盤中向上假插針）",
        "h5_prev   = ta.highest(high, 5)[1]",
        "l5_prev   = ta.lowest(low, 5)[1]",
        "h20_prev  = ta.highest(high, 20)[1]",
        "l20_prev  = ta.lowest(low, 20)[1]",
        "hc20_prev = ta.highest(close, 20)[1]",
        "rng5_prev  = l5_prev > 0 ? (h5_prev - l5_prev) / l5_prev : 1.0",
        "rng20_prev = l20_prev > 0 ? (h20_prev - l20_prev) / l20_prev : 1.0",
        "cr_prev    = rng20_prev > 0 ? rng5_prev / rng20_prev : 1.0",
        "",
        "live_stop_px = l5_prev - 0.25 * atr14[1]",
        "live_stop_d  = close > 0 ? (close - live_stop_px) / close : 1.0",
        "live_cat_px  = close - 1.6 * math.max(close * 0.03, close - live_stop_px)",
        "live_e3_lim  = hc20_prev * 1.003",
        "live_tp1_px  = close + 2.0 * math.max(close * 0.03, close - live_stop_px)",
        "",
        "body_pct_now  = open > 0 ? (close - open) / open : 0.0",
        "pos_bar_now   = (high - low) > 0 ? (close - low) / (high - low) : 0.0",
        "above_200_now = not na(sma200) and close > sma200",
        "fresh_50_prev = ta.lowest(close[1] - ema50[1], 5) <= 0",
        "pre_vcp_ok    = close[1] > ema20[1] and rng5_prev <= 0.08 and cr_prev <= 0.80 and live_stop_d >= 0.02 and live_stop_d <= 0.25",
        "ma_eod_ok     = close > ema20 and close > ema50 and ema20 > ema20[5] and ema50 > ema50[5]",
        "bull_eod_ok   = above_200_now or fresh_50_prev",
        "live_eod_confirmed = show_live_scanner and pre_vcp_ok and ma_eod_ok and bull_eod_ok and close > hc20_prev and body_pct_now >= 0.02 and pos_bar_now >= 0.50 and volume >= vsma20",
        "",
        'bgcolor(wk_2below_50w ? color.new(#9C27B0, 93) : (bull_eod_ok and close > ema20 ? color.new(#00E676, 95) : color.new(#FF5252, 96)), title="多空與50W保底背景色")',
        'plotshape(live_eod_confirmed and not show_hist_trades, title="即時v2收盤確認帶量長紅K", style=shape.triangleup, location=location.belowbar, color=color.new(#00E676, 0), size=size.small)',
        "",
        "// UTC 日期整數編碼 (YYYYMMDD)",
        'bar_ymd = year(time, "UTC") * 10000 + month(time, "UTC") * 100 + dayofmonth(time, "UTC")',
        "",
        "// ============================================================================",
        f"// 3. 權威回測 {n_tr} 筆 v2 反險惡交易所確認交易資料庫 (2015–2026 | 滑價 0.15%/0.30%)",
        "// ============================================================================",
    ]

    def fmt_arr(name: str, tipo: str, vals: list[Any]) -> str:
        joined = ", ".join(str(v) for v in vals)
        return f"var {tipo}[] {name} = array.from({joined})"

    t_ids, brk_ymds, end_ymds = [], [], []
    piv_pxs, stop_pxs, cat_pxs, stop_pcts, avg_eps, tp1_pxs = [], [], [], [], [], []
    e12_ymds, e12_pxs, e3_ymds, e3_pxs, e3_modes = [], [], [], [], []
    x1_ymds, x1_pxs, x1_types = [], [], []
    x2_ymds, x2_pxs, x2_types = [], [], []
    x3_ymds, x3_pxs, x3_types = [], [], []
    ret_costs, ret_eqs, trade_rs, is_wins, n_ents = [], [], [], [], []

    for tr in records:
        p: VCPPosition = tr["position"]
        t_ids.append(tr["trade_id"])
        brk_ymds.append(date_to_int(tr["breakout_date"]))
        end_ymds.append(date_to_int(tr["last_exit_date"]))

        piv_pxs.append(round(p.pivot_high, 2))
        stop_pxs.append(round(p.initial_stop, 2))
        cat_pxs.append(round(p.catastrophic_stop, 2))
        stop_pcts.append(round(p.stop_dist_pct * 100.0, 2))
        avg_ep = round(p.avg_entry_price, 2)
        avg_eps.append(avg_ep)
        r_dist = max(avg_ep * 0.03, avg_ep - p.initial_stop)
        tp1_pxs.append(round(avg_ep + 2.0 * r_dist, 2))

        e12_ymds.append(date_to_int(p.entries[0].date_utc))
        e12_pxs.append(round(p.entries[0].price, 2))
        if len(p.entries) >= 2:
            e3_ymds.append(date_to_int(p.entries[1].date_utc))
            e3_pxs.append(round(p.entries[1].price, 2))
            e3_modes.append(1 if "回踩" in p.entries[1].label else 2)
        else:
            e3_ymds.append(0)
            e3_pxs.append(0.0)
            e3_modes.append(0)

        # Exit codes:
        # 1 = X1 +2R TP, 2 = X2 Halving Peak, 3 = X3 30EMA, 4 = Inner Close Stop,
        # 5 = X3 HigherLow, 6 = Outer 1.6x Catastrophic Stop, 7 = X2 2W<50W SMA Bear Fail-Safe
        def _code(x: Any, default_code: int) -> int:
            if not x:
                return 0
            if "災難" in x.reason:
                return 6
            if "內層" in x.reason or "收盤破停損" in x.reason:
                return 4
            if "50W" in x.reason:
                return 7
            if "HigherLow" in x.reason:
                return 5
            return default_code

        x1 = p.exits.get(1)
        x1_ymds.append(date_to_int(x1.date_utc) if x1 else 0)
        x1_pxs.append(round(x1.price, 2) if x1 else 0.0)
        x1_types.append(_code(x1, 1))

        x2 = p.exits.get(2)
        x2_ymds.append(date_to_int(x2.date_utc) if x2 else 0)
        x2_pxs.append(round(x2.price, 2) if x2 else 0.0)
        x2_types.append(_code(x2, 2))

        x3 = p.exits.get(3)
        x3_ymds.append(date_to_int(x3.date_utc) if x3 else 0)
        x3_pxs.append(round(x3.price, 2) if x3 else 0.0)
        x3_types.append(_code(x3, 3))

        ret_costs.append(round(tr["ret_on_cost_pct"], 2))
        ret_eqs.append(round(tr["ret_on_equity_pct"], 2))
        trade_rs.append(round(tr["trade_r"], 2))
        is_wins.append(1 if tr["net_pnl_usd"] > 0 else 0)
        n_ents.append(tr["n_entries"])

    lines.extend([
        fmt_arr("a_id", "int", t_ids),
        fmt_arr("a_brk_ymd", "int", brk_ymds),
        fmt_arr("a_end_ymd", "int", end_ymds),
        fmt_arr("a_piv_px", "float", piv_pxs),
        fmt_arr("a_stop_px", "float", stop_pxs),
        fmt_arr("a_cat_px", "float", cat_pxs),
        fmt_arr("a_stop_pct", "float", stop_pcts),
        fmt_arr("a_avg_ep", "float", avg_eps),
        fmt_arr("a_tp1_px", "float", tp1_pxs),
        fmt_arr("a_e12_ymd", "int", e12_ymds),
        fmt_arr("a_e12_px", "float", e12_pxs),
        fmt_arr("a_e3_ymd", "int", e3_ymds),
        fmt_arr("a_e3_px", "float", e3_pxs),
        fmt_arr("a_e3_mode", "int", e3_modes),
        fmt_arr("a_x1_ymd", "int", x1_ymds),
        fmt_arr("a_x1_px", "float", x1_pxs),
        fmt_arr("a_x1_type", "int", x1_types),
        fmt_arr("a_x2_ymd", "int", x2_ymds),
        fmt_arr("a_x2_px", "float", x2_pxs),
        fmt_arr("a_x2_type", "int", x2_types),
        fmt_arr("a_x3_ymd", "int", x3_ymds),
        fmt_arr("a_x3_px", "float", x3_pxs),
        fmt_arr("a_x3_type", "int", x3_types),
        fmt_arr("a_ret_cost", "float", ret_costs),
        fmt_arr("a_ret_eq", "float", ret_eqs),
        fmt_arr("a_r", "float", trade_rs),
        fmt_arr("a_win", "int", is_wins),
        fmt_arr("a_n_ent", "int", n_ents),
        f"var int[] a_brk_time = array.new_int({n_tr}, 0)",
        "",
        "// ============================================================================",
        "// 4. 逐根 K 棒繪製：收盤確認訊號、E1+E2/E3 進場、雙層停損線與三批出場結果",
        "// ============================================================================",
        "if show_hist_trades",
        f"    for i = 0 to {n_tr - 1}",
        "        tid      = array.get(a_id, i)",
        "        b_ymd    = array.get(a_brk_ymd, i)",
        "        piv_px   = array.get(a_piv_px, i)",
        "        stop_px  = array.get(a_stop_px, i)",
        "        cat_px   = array.get(a_cat_px, i)",
        "        stop_pct = array.get(a_stop_pct, i)",
        "        avg_ep   = array.get(a_avg_ep, i)",
        "        tp1_px   = array.get(a_tp1_px, i)",
        "        e12_ymd  = array.get(a_e12_ymd, i)",
        "        e12_px   = array.get(a_e12_px, i)",
        "        e3_ymd   = array.get(a_e3_ymd, i)",
        "        e3_px    = array.get(a_e3_px, i)",
        "        e3_mode  = array.get(a_e3_mode, i)",
        "        x1_ymd   = array.get(a_x1_ymd, i)",
        "        x1_px    = array.get(a_x1_px, i)",
        "        x1_type  = array.get(a_x1_type, i)",
        "        x2_ymd   = array.get(a_x2_ymd, i)",
        "        x2_px    = array.get(a_x2_px, i)",
        "        x2_type  = array.get(a_x2_type, i)",
        "        x3_ymd   = array.get(a_x3_ymd, i)",
        "        x3_px    = array.get(a_x3_px, i)",
        "        x3_type  = array.get(a_x3_type, i)",
        "        ret_eq   = array.get(a_ret_eq, i)",
        "        tr_r     = array.get(a_r, i)",
        "        is_w     = array.get(a_win, i) == 1",
        "        n_ent    = array.get(a_n_ent, i)",
        "",
        "        // (A) 收盤確認帶量長紅K主標籤 (於確認當日 K 棒下方繪製，含雙層停損與交易結果)",
        "        if bar_ymd == b_ymd",
        "            array.set(a_brk_time, i, time)",
        '            res_badge = is_w ? "✅ WIN" : "❌ LOSS"',
        '            sign_str  = ret_eq >= 0 ? "+" : ""',
        '            e3_desc   = e3_mode == 1 ? "回踩限價 $" + str.tostring(e3_px, "#.##") : (e3_mode == 2 ? "站穩3日開盤 $" + str.tostring(e3_px, "#.##") : "未觸發")',
        '            ent_txt   = "🚀#" + str.tostring(tid) + " 確認 (" + str.tostring(n_ent) + "/3)\\n🛡️$" + str.tostring(stop_px, "#.#") + " | " + res_badge + " " + sign_str + str.tostring(tr_r, "#.#") + "R"',
        '            ent_tip   = "【v2 反險惡交易 #" + str.tostring(tid) + " 完整帳本】\\n• 結果: " + res_badge + " (對淨值: " + sign_str + str.tostring(ret_eq, "#.##") + "%, R倍數: " + sign_str + str.tostring(tr_r, "#.##") + "R)\\n• 20日收盤頸線 HC20: $" + str.tostring(piv_px, "#.##") + "\\n• 進場均價: $" + str.tostring(avg_ep, "#.##") + " (" + str.tostring(n_ent) + "/3批)\\n  - E1+E2(2/3隔日開盤): $" + str.tostring(e12_px, "#.##") + "\\n  - E3(1/3回踩/站穩3日): " + e3_desc + "\\n• 內層收盤停損 (免疫假針): $" + str.tostring(stop_px, "#.##") + " (-" + str.tostring(stop_pct, "#.##") + "%)\\n• 外層1.6x災難防線 (盤中硬停損): $" + str.tostring(cat_px, "#.##") + "\\n• X1(+2R)短期限價目標: $" + str.tostring(tp1_px, "#.##")',
        "            ent_col   = is_w ? color.new(#00897B, 5) : color.new(#546E7A, 10)",
        "            label.new(bar_index, low * 0.985, ent_txt, style=label.style_label_up, color=ent_col, textcolor=color.white, size=size.small, tooltip=ent_tip)",
        "",
        "        // (B) E1+E2 (隔日開盤2/3) 與 E3 (回踩/站穩3日1/3) 獨立標記",
        "        if show_sub_entries and bar_ymd == e12_ymd",
        '            e_lbl = (e3_ymd == e12_ymd) ? "➕#" + str.tostring(tid) + " 開盤2/3+回踩1/3\\n$" + str.tostring(e12_px, "#.#") : "➕#" + str.tostring(tid) + " E1+E2(2/3)\\n$" + str.tostring(e12_px, "#.#")',
        "            label.new(bar_index, low * 0.992, e_lbl, style=label.style_label_up, color=color.new(#43A047, 20), textcolor=color.white, size=size.tiny)",
        "        if show_sub_entries and e3_ymd > 0 and e3_ymd != e12_ymd and bar_ymd == e3_ymd",
        '            m_str = e3_mode == 1 ? "E3回踩1/3" : "E3站穩3日1/3"',
        '            label.new(bar_index, low * 0.992, "➕#" + str.tostring(tid) + " " + m_str + "\\n$" + str.tostring(e3_px, "#.#"), style=label.style_label_up, color=color.new(#2E7D32, 15), textcolor=color.white, size=size.tiny)',
        "",
        "        // (C) 出場標籤與雙層停損/目標線段",
        "        b_time = array.get(a_brk_time, i)",
        "        if show_exit_labels",
        "            if (x1_type == 4 or x1_type == 6) and bar_ymd == x1_ymd",
        '                sl_t = x1_type == 6 ? "💥#" + str.tostring(tid) + " 觸1.6x災難防線" : "🛑#" + str.tostring(tid) + " 收盤破內層停損"',
        '                label.new(bar_index, high * 1.01, sl_t + "\\n$" + str.tostring(x1_px, "#.#") + " (" + str.tostring(ret_eq, "#.##") + "%)", style=label.style_label_down, color=color.new(#D32F2F, 5), textcolor=color.white, size=size.tiny)',
        "                if show_stop_lines and b_time > 0",
        "                    line.new(b_time, stop_px, time, stop_px, xloc=xloc.bar_time, color=color.new(#FF9800, 5), width=2, style=line.style_dashed)",
        "                    line.new(b_time, cat_px,  time, cat_px,  xloc=xloc.bar_time, color=color.new(#FF1744, 0), width=2, style=line.style_solid)",
        "            else",
        "                if x1_type == 1 and bar_ymd == x1_ymd",
        '                    label.new(bar_index, high * 1.008, "🎯#" + str.tostring(tid) + " X1(+2R)\\n$" + str.tostring(x1_px, "#.#"), style=label.style_label_down, color=color.new(#00ACC1, 10), textcolor=color.white, size=size.tiny)',
        "                    if show_stop_lines and b_time > 0",
        "                        line.new(b_time, tp1_px, time, tp1_px, xloc=xloc.bar_time, color=color.new(#00E5FF, 20), width=1, style=line.style_dashed)",
        "                if (x3_type == 3 or x3_type == 5) and bar_ymd == x3_ymd",
        '                    x3_r = x3_type == 5 ? "X3破HL" : "X3破30EMA"',
        '                    label.new(bar_index, high * 1.012, "📉#" + str.tostring(tid) + " " + x3_r + "\\n$" + str.tostring(x3_px, "#.#"), style=label.style_label_down, color=color.new(#FB8C00, 10), textcolor=color.white, size=size.tiny)',
        "                    if show_stop_lines and b_time > 0",
        "                        line.new(b_time, stop_px, time, stop_px, xloc=xloc.bar_time, color=color.new(#FF9800, 15), width=2, style=line.style_dashed)",
        "                        line.new(b_time, cat_px,  time, cat_px,  xloc=xloc.bar_time, color=color.new(#FF1744, 20), width=1, style=line.style_solid)",
        "                if (x2_type == 4 or x2_type == 6) and x1_type != 4 and x1_type != 6 and bar_ymd == x2_ymd",
        '                    label.new(bar_index, high * 1.01, "🛑#" + str.tostring(tid) + " X2觸停損\\n$" + str.tostring(x2_px, "#.#"), style=label.style_label_down, color=color.new(#C62828, 10), textcolor=color.white, size=size.tiny)',
    ])

    # Group X2 Halving Peak exits AND X2 2W<50W SMA fail-safe exits cleanly by date!
    x2_halv_groups: dict[int, list[tuple[int, float, str]]] = defaultdict(list)
    x2_50w_groups: dict[int, list[tuple[int, float, str]]] = defaultdict(list)
    for tr in records:
        p = tr["position"]
        x2 = p.exits.get(2)
        if x2 and "50W" in x2.reason:
            x2_50w_groups[date_to_int(x2.date_utc)].append((tr["trade_id"], round(x2.price, 2), x2.reason))
        elif x2 and "停損" not in x2.reason and "災難" not in x2.reason:
            x2_halv_groups[date_to_int(x2.date_utc)].append((tr["trade_id"], round(x2.price, 2), x2.reason))

    lines.append("")
    lines.append("    // (D) X2 減半理論高點收割 ＆ 雙週破50W熊市保底 群組標籤")
    lines.append("    if show_exit_labels")
    for ymd_val, items in sorted(x2_halv_groups.items()):
        id_list_str = ",".join(f"#{x[0]}" for x in items)
        px_val = items[0][1]
        reason_str = items[0][2]
        lines.append(f"        if bar_ymd == {ymd_val}")
        lines.append(
            f'            label.new(bar_index, high * 1.03, "🏆 X2 減半理論高點收割 ({id_list_str})\\n@ $" + str.tostring({px_val}, "#.##") + "\\n{reason_str}", style=label.style_label_down, color=color.new(#FFD600, 0), textcolor=color.black, size=size.normal)'
        )
    for ymd_val, items in sorted(x2_50w_groups.items()):
        id_list_str = ",".join(f"#{x[0]}" for x in items)
        px_val = items[0][1]
        lines.append(f"        if bar_ymd == {ymd_val}")
        lines.append(
            f'            label.new(bar_index, high * 1.025, "🛡️ X2 雙週破50W保底鎖利 ({id_list_str})\\n@ $" + str.tostring({px_val}, "#.##") + "\\n躲過後續暴跌", style=label.style_label_down, color=color.new(#9C27B0, 5), textcolor=color.white, size=size.small)'
        )

    lines.extend([
        "",
        "// ============================================================================",
        "// 5. 即時動態 v2 收盤確認指令線段與警報 (Live Signals & Alerts)",
        "// ============================================================================",
        'plot(live_eod_confirmed ? live_e3_lim  : na, "即時E3頸線回踩限價",      color=color.new(#29B6F6, 0), style=plot.style_linebr, linewidth=2)',
        'plot(live_eod_confirmed ? live_stop_px : na, "即時內層收盤停損線",      color=color.new(#FF9800, 0), style=plot.style_linebr, linewidth=2)',
        'plot(live_eod_confirmed ? live_cat_px  : na, "即時外層1.6x災難硬停損線", color=color.new(#FF1744, 0), style=plot.style_linebr, linewidth=2)',
        'plot(live_eod_confirmed ? live_tp1_px  : na, "即時X1(+2R)短期限價目標",   color=color.new(#00E676, 0), style=plot.style_linebr, linewidth=1)',
        "",
        "if live_eod_confirmed and barstate.islast",
        '    label.new(bar_index, low * 0.98, "🔔 v2 帶量長紅K突破確認！明日00:00 UTC買入 E1+E2 (2/3)\\nE3回踩限價(1/3): $" + str.tostring(live_e3_lim, "#.##") + "\\n內層收盤停損: $" + str.tostring(live_stop_px, "#.##") + " | 外層1.6x硬停損: $" + str.tostring(live_cat_px, "#.##") + "\\nX1(+2R)目標: $" + str.tostring(live_tp1_px, "#.##"), style=label.style_label_up, color=color.new(#00897B, 0), textcolor=color.white, size=size.normal)',
        "",
        'alertcondition(live_eod_confirmed, title="1_v2收盤確認帶量長紅K(明日開盤進2/3)", message="【BTC奧卡姆VCP v2】今日收盤確認帶量長紅K有效突破！明日00:00 UTC買入E1+E2(2/3)、掛出E3回踩限價(1/3)、外層1.6x災難硬停損與X1(+2R)限價止盈。")',
        'alertcondition(wk_2below_50w and not wk_2below_50w[1], title="2_X2雙週收盤跌破50W_SMA熊市保底", message="【BTC奧卡姆VCP v2】警告：BTC已連續2週收盤跌破50W SMA！請啟動X2長線底倉熊市保底出場。")',
        "",
        "// ============================================================================",
        "// 6. 右上角 v2 策略戰情儀表板 (Dashboard Table)",
        "// ============================================================================",
        "var table dash = table.new(position.top_right, 2, 9, bgcolor=color.new(#0D1117, 10), border_color=color.new(#30363D, 0), border_width=1)",
        "if show_dashboard and barstate.islast",
        '    table.cell(dash, 0, 0, "BTC 奧卡姆 VCP v2 反險惡版", text_color=color.white, bgcolor=color.new(#1B365D, 0), text_size=size.small)',
        '    table.cell(dash, 1, 0, "2014–2026 嚴格因果實盤", text_color=color.new(#FFD600, 0), bgcolor=color.new(#1B365D, 0), text_size=size.small)',
        '    table.cell(dash, 0, 1, "一、50W 週線結構狀態", text_color=color.silver, text_size=size.tiny)',
        '    table.cell(dash, 1, 1, wk_2below_50w ? "🟣 雙週破50W (觸發X2熊市保底)" : "🟢 50W 結構正常 (允許持有X2)", text_color=wk_2below_50w ? color.new(#CE93D8, 0) : color.new(#00E676, 0), text_size=size.tiny)',
        '    table.cell(dash, 0, 2, "二、今日收盤確認訊號", text_color=color.silver, text_size=size.tiny)',
        '    table.cell(dash, 1, 2, live_eod_confirmed ? ("🔔 確認突破！明日開盤買2/3 | 停損 $" + str.tostring(live_stop_px, "#.#")) : "無新確認突破 (空手/持倉等待)", text_color=live_eod_confirmed ? color.new(#00E676, 0) : color.gray, text_size=size.tiny)',
        '    table.cell(dash, 0, 3, "總交易單數 / 勝率", text_color=color.silver, text_size=size.tiny)',
        f'    table.cell(dash, 1, 3, "{sim["n_trades"]} 筆 (0假E1) / 勝率 {sim["win_rate_pct"]}%", text_color=color.white, text_size=size.tiny)',
        '    table.cell(dash, 0, 4, "2014–2026 總報酬 (CAGR)", text_color=color.silver, text_size=size.tiny)',
        f'    table.cell(dash, 1, 4, "+{sim["tot_ret_pct"]:,.1f}% (CAGR {sim["cagr_pct"]}%)", text_color=color.new(#00E676, 0), text_size=size.tiny)',
        '    table.cell(dash, 0, 5, "最大回撤 / Calmar / Sharpe", text_color=color.silver, text_size=size.tiny)',
        f'    table.cell(dash, 1, 5, "MaxDD {sim["max_dd_pct"]}% | Calmar {sim["calmar"]} | Sh {sim["sharpe"]}", text_color=color.new(#FFB74D, 0), text_size=size.tiny)',
        '    table.cell(dash, 0, 6, "中位數 R / +3R封頂 EV", text_color=color.silver, text_size=size.tiny)',
        f'    table.cell(dash, 1, 6, "Median +{sim["median_r"]}R / Cap3R +{sim["capped_3r_ev"]}R", text_color=color.new(#4FC3F7, 0), text_size=size.tiny)',
        '    table.cell(dash, 0, 7, "險惡交易所極壓測試", text_color=color.silver, text_size=size.tiny)',
        '    table.cell(dash, 1, 7, "每日±插針+高滑價僅衰減 7.2% (Sh 1.52)", text_color=color.new(#80CBC4, 0), text_size=size.tiny)',
        '    table.cell(dash, 0, 8, "雙層停損與滑價基準", text_color=color.silver, text_size=size.tiny)',
        '    table.cell(dash, 1, 8, "收盤破P_stop + 盤中1.6x防線 | 滑價0.15/0.30%", text_color=color.new(#FFD600, 0), text_size=size.tiny)',
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    out_path = root / "reports" / "btc_vcp_3tranche_tradingview.pine"
    pine_code = generate_pine_script()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(pine_code, encoding="utf-8")
    print(f"[OK] Generated v2 Institutional Pine Script v5: {out_path} ({len(pine_code.splitlines())} lines, {len(pine_code)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
