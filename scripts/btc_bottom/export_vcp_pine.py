"""Generate TradingView Pine Script v5 for the BTC VCP 3-Tranche Bull Strategy."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

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
    sim = simulate_vcp_3tranche_strategy(env, risk_per_trade_pct=0.08, max_alloc_per_trade=0.90)
    records = sim["trade_records"]

    # Prepare compact arrays for the 38 trades in Pine Script
    # Per-trade arrays:
    # t_id, setup_ymd, brk_ymd, last_exit_ymd, x3_end_ymd, pivot_px, trig_px, stop_px, avg_ep, tp1_px,
    # e1_ymd, e1_px, e2_ymd, e2_px, e3_ymd, e3_px, e3_mode (0=none, 1=pullback_limit, 2=hold3d_open4),
    # x1_ymd, x1_px, x2_ymd, x2_px, x3_ymd, x3_px,
    # ret_cost_pct, ret_eq_pct, trade_r, is_win (1 or 0), n_entries
    n_tr = len(records)

    lines: list[str] = [
        "// This Pine Script™ code is subject to the terms of the Mozilla Public License 2.0",
        "// © BTC Quant Analyzer — VCP 3-Tranche Bull Cycle Strategy (Zero-Lookahead)",
        "//@version=5",
        'indicator("BTC 牛市 VCP 三進三出 + 減半理論高點策略 [完整歷史點位 & 即時掛單提示]", shorttitle="BTC_VCP_三進三出", overlay=true, max_labels_count=500, max_lines_count=500, max_boxes_count=500, max_bars_back=500)',
        "",
        "// ============================================================================",
        "// 1. 使用者顯示設定 (Inputs)",
        "// ============================================================================",
        'grp_vis = "圖表視覺化開關"',
        'show_hist_trades  = input.bool(true,  "顯示 2015–2026 回測 38 筆完整交易點位", group=grp_vis)',
        'show_armed_order  = input.bool(true,  "顯示「何時預掛觸價單 (Setup Day)」提示標籤", group=grp_vis)',
        'show_sub_entries  = input.bool(true,  "顯示 E2 / E3 加碼批次獨立標記", group=grp_vis)',
        'show_exit_labels  = input.bool(true,  "顯示 X1(+2R) / X2(減半高點) / X3(轉弱) 出場標籤", group=grp_vis)',
        'show_stop_lines   = input.bool(true,  "繪製每筆 VCP 頸線觸價線、初始硬停損線與 +2R 目標線", group=grp_vis)',
        'show_ma_lines     = input.bool(true,  "顯示策略核心均線 (30D EMA / 50D EMA / 200D SMA)", group=grp_vis)',
        'show_live_scanner = input.bool(true,  "開啟未來/即時動態 VCP 掃描與掛單提示引擎", group=grp_vis)',
        'show_dashboard    = input.bool(true,  "顯示右上角策略戰情儀表板", group=grp_vis)',
        "",
        "// ============================================================================",
        "// 2. 核心均線與即時動態 VCP 掃描引擎 (供當下與未來新 K 棒即時監控)",
        "// ============================================================================",
        "ema30  = ta.ema(close, 30)",
        "ema50  = ta.ema(close, 50)",
        "sma200 = ta.sma(close, 200)",
        "atr14  = ta.atr(14)",
        "vsma20 = ta.sma(volume, 20)",
        "",
        'plot(show_ma_lines ? ema30  : na, "30D EMA (X3 轉弱防守線)", color=color.new(#00BCD4, 15), linewidth=1)',
        'plot(show_ma_lines ? ema50  : na, "50D EMA (中期趨勢線)",   color=color.new(#FF9800, 15), linewidth=2)',
        'plot(show_ma_lines ? sma200 : na, "200D SMA (牛熊分界年線)", color=color.new(#E91E63, 10), linewidth=2)',
        "",
        "// 週線 21W EMA 與因果牛市狀態",
        'wk_close = request.security(syminfo.tickerid, "W", close[1], lookahead=barmerge.lookahead_on)',
        'wk_ema21 = request.security(syminfo.tickerid, "W", ta.ema(close, 21)[1], lookahead=barmerge.lookahead_on)',
        "d_bull_ok = close > sma200 and close > ema50 and ema50 > ema50[5]",
        "w_bull_ok = not na(wk_ema21) and wk_close > wk_ema21 and close > ema50",
        "is_bull_regime = d_bull_ok or w_bull_ok",
        'bgcolor(is_bull_regime ? color.new(#00E676, 95) : color.new(#FF5252, 96), title="牛熊狀態背景色")',
        "",
        "// 即時 VCP 36 日箱體收縮偵測 (供最新 K 棒預掛單提示)",
        "base_len = 36",
        "piv_hi_live  = ta.highest(high, base_len)[1]",
        "outer_lo_live = ta.lowest(low, base_len)[1]",
        "tight_lo_live = ta.lowest(low, 10)",
        "d1_live = piv_hi_live > 0 ? (piv_hi_live - outer_lo_live) / piv_hi_live : 0.0",
        "d2_live = piv_hi_live > 0 ? (piv_hi_live - tight_lo_live) / piv_hi_live : 0.0",
        "c_ratio_live = d1_live > 0 ? d2_live / d1_live : 1.0",
        "min_vr_live  = ta.lowest(volume / vsma20, 10)",
        "live_vcp_armed = show_live_scanner and is_bull_regime and close <= piv_hi_live and (piv_hi_live - close) / piv_hi_live <= 0.08 and d1_live >= 0.07 and d1_live <= 0.40 and tight_lo_live > outer_lo_live and d2_live >= 0.02 and d2_live <= 0.16 and c_ratio_live <= 0.75 and min_vr_live <= 0.85",
        "live_trig_px = piv_hi_live * 1.001",
        "live_stop_px = tight_lo_live - 0.25 * atr14",
        "live_tp1_px  = live_trig_px + 2.0 * math.max(live_trig_px * 0.03, live_trig_px - live_stop_px)",
        "",
        "// UTC 日期整數編碼 (YYYYMMDD)，確保跨時區與跨交易所精確對齊",
        'bar_ymd = year(time, "UTC") * 10000 + month(time, "UTC") * 100 + dayofmonth(time, "UTC")',
        "",
        "// ============================================================================",
        f"// 3. 權威回測 38 筆 VCP 三進三出交易資料庫 (2015–2026)",
        "// ============================================================================",
    ]

    def fmt_arr(name: str, tipo: str, vals: list[Any]) -> str:
        joined = ", ".join(str(v) for v in vals)
        return f"var {tipo}[] {name} = array.from({joined})"

    t_ids, setup_ymds, brk_ymds, end_ymds, x3_ymds = [], [], [], [], []
    piv_pxs, trig_pxs, stop_pxs, stop_pcts, avg_eps, tp1_pxs = [], [], [], [], [], []
    e1_ymds, e1_pxs, e2_ymds, e2_pxs, e3_ymds, e3_pxs, e3_modes = [], [], [], [], [], [], []
    x1_ymds, x1_pxs, x1_types = [], [], []
    x2_ymds, x2_pxs, x2_types = [], [], []
    x3_pxs, x3_types = [], []
    ret_costs, ret_eqs, trade_rs, is_wins, n_ents = [], [], [], [], []

    for tr in records:
        p: VCPPosition = tr["position"]
        t_ids.append(tr["trade_id"])
        setup_ymds.append(date_to_int(tr["setup_date"]))
        brk_ymds.append(date_to_int(tr["breakout_date"]))
        end_ymds.append(date_to_int(tr["last_exit_date"]))

        piv_pxs.append(round(p.pivot_high, 2))
        trig_pxs.append(round(p.pivot_high * 1.001, 2))
        stop_pxs.append(round(p.initial_stop, 2))
        stop_pcts.append(round(p.stop_dist_pct * 100.0, 2))
        avg_ep = round(p.avg_entry_price, 2)
        avg_eps.append(avg_ep)
        r_dist = max(avg_ep * 0.03, avg_ep - p.initial_stop)
        tp1_pxs.append(round(avg_ep + 2.0 * r_dist, 2))

        e_map = {i + 1: e for i, e in enumerate(p.entries)}
        e1_ymds.append(date_to_int(e_map[1].date_utc))
        e1_pxs.append(round(e_map[1].price, 2))

        if 2 in e_map:
            e2_ymds.append(date_to_int(e_map[2].date_utc))
            e2_pxs.append(round(e_map[2].price, 2))
        else:
            e2_ymds.append(0)
            e2_pxs.append(0.0)

        if 3 in e_map:
            e3_ymds.append(date_to_int(e_map[3].date_utc))
            e3_pxs.append(round(e_map[3].price, 2))
            e3_modes.append(1 if "回踩" in e_map[3].label else 2)
        else:
            e3_ymds.append(0)
            e3_pxs.append(0.0)
            e3_modes.append(0)

        # Exits: code 1 = +2R TP1, 2 = Halving Peak, 3 = 30D EMA / HL Weakness, 4 = Hard Initial Stop
        x1 = p.exits.get(1)
        x1_ymds.append(date_to_int(x1.date_utc) if x1 else 0)
        x1_pxs.append(round(x1.price, 2) if x1 else 0.0)
        x1_types.append(4 if (x1 and "硬停損" in x1.reason) else 1)

        x2 = p.exits.get(2)
        x2_ymds.append(date_to_int(x2.date_utc) if x2 else 0)
        x2_pxs.append(round(x2.price, 2) if x2 else 0.0)
        x2_types.append(4 if (x2 and "硬停損" in x2.reason) else 2)

        x3 = p.exits.get(3)
        x3_ymds.append(date_to_int(x3.date_utc) if x3 else 0)
        x3_pxs.append(round(x3.price, 2) if x3 else 0.0)
        if x3 and "硬停損" in x3.reason:
            x3_types.append(4)
        elif x3 and "HigherLow" in x3.reason:
            x3_types.append(5)
        else:
            x3_types.append(3)

        ret_costs.append(round(tr["ret_on_cost_pct"], 2))
        ret_eqs.append(round(tr["ret_on_equity_pct"], 2))
        trade_rs.append(round(tr["trade_r"], 2))
        is_wins.append(1 if tr["net_pnl_usd"] > 0 else 0)
        n_ents.append(tr["n_entries"])

    lines.extend([
        fmt_arr("a_id", "int", t_ids),
        fmt_arr("a_setup_ymd", "int", setup_ymds),
        fmt_arr("a_brk_ymd", "int", brk_ymds),
        fmt_arr("a_end_ymd", "int", end_ymds),
        fmt_arr("a_piv_px", "float", piv_pxs),
        fmt_arr("a_trig_px", "float", trig_pxs),
        fmt_arr("a_stop_px", "float", stop_pxs),
        fmt_arr("a_stop_pct", "float", stop_pcts),
        fmt_arr("a_avg_ep", "float", avg_eps),
        fmt_arr("a_tp1_px", "float", tp1_pxs),
        fmt_arr("a_e1_ymd", "int", e1_ymds),
        fmt_arr("a_e1_px", "float", e1_pxs),
        fmt_arr("a_e2_ymd", "int", e2_ymds),
        fmt_arr("a_e2_px", "float", e2_pxs),
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
        "// 4. 逐根 K 棒繪製：何時掛觸價單、三批進場、初始硬停損線、三批出場與交易結果",
        "// ============================================================================",
        "if show_hist_trades",
        f"    for i = 0 to {n_tr - 1}",
        "        tid       = array.get(a_id, i)",
        "        s_ymd     = array.get(a_setup_ymd, i)",
        "        b_ymd     = array.get(a_brk_ymd, i)",
        "        piv_px    = array.get(a_piv_px, i)",
        "        trig_px   = array.get(a_trig_px, i)",
        "        stop_px   = array.get(a_stop_px, i)",
        "        stop_pct  = array.get(a_stop_pct, i)",
        "        avg_ep    = array.get(a_avg_ep, i)",
        "        tp1_px    = array.get(a_tp1_px, i)",
        "        e1_px     = array.get(a_e1_px, i)",
        "        e2_ymd    = array.get(a_e2_ymd, i)",
        "        e2_px     = array.get(a_e2_px, i)",
        "        e3_ymd    = array.get(a_e3_ymd, i)",
        "        e3_px     = array.get(a_e3_px, i)",
        "        e3_mode   = array.get(a_e3_mode, i)",
        "        x1_ymd    = array.get(a_x1_ymd, i)",
        "        x1_px     = array.get(a_x1_px, i)",
        "        x1_type   = array.get(a_x1_type, i)",
        "        x2_ymd    = array.get(a_x2_ymd, i)",
        "        x2_px     = array.get(a_x2_px, i)",
        "        x2_type   = array.get(a_x2_type, i)",
        "        x3_ymd    = array.get(a_x3_ymd, i)",
        "        x3_px     = array.get(a_x3_px, i)",
        "        x3_type   = array.get(a_x3_type, i)",
        "        ret_eq    = array.get(a_ret_eq, i)",
        "        tr_r      = array.get(a_r, i)",
        "        is_w      = array.get(a_win, i) == 1",
        "        n_ent     = array.get(a_n_ent, i)",
        "",
        "        // (A) 何時掛觸價單 (Setup Day: VCP 收縮完成當晚收盤，預掛隔日 E1 Stop-Market 觸價單)",
        "        if show_armed_order and bar_ymd == s_ymd",
        '            arm_txt = "⏳ #" + str.tostring(tid) + " 預掛觸價單\\n觸發價: $" + str.tostring(trig_px, "#.##") + "\\n硬停損: $" + str.tostring(stop_px, "#.##")',
        '            arm_tip = "【VCP 波動收縮完成 — 預掛單通知】\\n• 交易編號: #" + str.tostring(tid) + "\\n• 前高頸線: $" + str.tostring(piv_px, "#.##") + "\\n• 預掛 E1 觸價買單 (Stop-Market): $" + str.tostring(trig_px, "#.##") + "\\n• 初始硬停損價 (跌破全砍): $" + str.tostring(stop_px, "#.##") + " (-" + str.tostring(stop_pct, "#.##") + "%)"',
        "            label.new(bar_index, low, arm_txt, style=label.style_label_up, color=color.new(#1E88E5, 15), textcolor=color.white, size=size.small, tooltip=arm_tip)",
        "",
        "        // (B) E1 突破進場主標籤 (含三批進場摘要、初始停損與最終交易結果徽章)",
        "        if bar_ymd == b_ymd",
        "            array.set(a_brk_time, i, time)",
        '            res_badge = is_w ? "✅ 獲利 WIN" : "❌ 停損 LOSS"',
        '            sign_str  = ret_eq >= 0 ? "+" : ""',
        '            e3_desc   = e3_mode == 1 ? "回踩限價 $" + str.tostring(e3_px, "#.##") : (e3_mode == 2 ? "站穩3日第4日開盤 $" + str.tostring(e3_px, "#.##") : "未觸發")',
        '            e2_desc   = e2_ymd > 0 ? "隔日開盤 $" + str.tostring(e2_px, "#.##") : "未帶量(取消)"',
        '            ent_txt   = "🚀 #" + str.tostring(tid) + " E1突破 $" + str.tostring(e1_px, "#.#") + " (" + str.tostring(n_ent) + "/3批)\\n🛑硬停損 $" + str.tostring(stop_px, "#.#") + " | " + res_badge + " " + sign_str + str.tostring(ret_eq, "#.##") + "% (" + sign_str + str.tostring(tr_r, "#.##") + "R)"',
        '            ent_tip   = "【交易 #" + str.tostring(tid) + " 完整三進三出帳本】\\n• 交易結果: " + res_badge + " (總淨值影響: " + sign_str + str.tostring(ret_eq, "#.##") + "%, R倍數: " + sign_str + str.tostring(tr_r, "#.##") + "R)\\n• 進場均價: $" + str.tostring(avg_ep, "#.##") + " (共進場 " + str.tostring(n_ent) + "/3 批)\\n  - E1(盤中觸價): $" + str.tostring(e1_px, "#.##") + "\\n  - E2(帶量隔日開盤): " + e2_desc + "\\n  - E3(回踩/站穩3日): " + e3_desc + "\\n• 初始硬停損價: $" + str.tostring(stop_px, "#.##") + " (-" + str.tostring(stop_pct, "#.##") + "%)\\n• X1(+2R)短期目標價: $" + str.tostring(tp1_px, "#.##")',
        "            ent_col   = is_w ? color.new(#00897B, 5) : color.new(#546E7A, 10)",
        "            label.new(bar_index, low * 0.985, ent_txt, style=label.style_label_up, color=ent_col, textcolor=color.white, size=size.small, tooltip=ent_tip)",
        "",
        "        // (C) E2 與 E3 加碼批次獨立標記",
        "        if show_sub_entries and e2_ymd > 0 and bar_ymd == e2_ymd",
        '            e2_t = (e3_ymd == e2_ymd) ? "➕ #" + str.tostring(tid) + " E2開盤+E3回踩\\n$" + str.tostring(e2_px, "#.#") + " / $" + str.tostring(e3_px, "#.#") : "➕ #" + str.tostring(tid) + " E2帶量開盤\\n$" + str.tostring(e2_px, "#.#")',
        "            label.new(bar_index, low * 0.992, e2_t, style=label.style_label_up, color=color.new(#43A047, 20), textcolor=color.white, size=size.tiny)",
        "        if show_sub_entries and e3_ymd > 0 and e3_ymd != e2_ymd and bar_ymd == e3_ymd",
        '            m_str = e3_mode == 1 ? "E3回踩限價" : "E3站穩3日開盤"',
        '            label.new(bar_index, low * 0.992, "➕ #" + str.tostring(tid) + " " + m_str + "\\n$" + str.tostring(e3_px, "#.#"), style=label.style_label_up, color=color.new(#2E7D32, 15), textcolor=color.white, size=size.tiny)',
        "",
        "        // (D) 出場批次標籤與水平停損/目標線段 (X1 短期+2R / X3 技術轉弱 / 初始硬停損全砍)",
        "        b_time = array.get(a_brk_time, i)",
        "        if show_exit_labels",
        "            // 若觸發初始硬停損全砍 (x1_type == 4 且在 x1_ymd)",
        "            if x1_type == 4 and bar_ymd == x1_ymd",
        '                sl_batches = (x3_type == 4 and x3_ymd == x1_ymd) ? "X1+X2+X3 全砍" : "X1+X2 剩餘全砍"',
        '                label.new(bar_index, high * 1.01, "🛑 #" + str.tostring(tid) + " 跌破硬停損 (" + sl_batches + ")\\n@ $" + str.tostring(x1_px, "#.#") + " | 結果 " + str.tostring(ret_eq, "#.##") + "%", style=label.style_label_down, color=color.new(#D32F2F, 5), textcolor=color.white, size=size.small)',
        "                if show_stop_lines and b_time > 0",
        "                    line.new(b_time, stop_px, time, stop_px, xloc=xloc.bar_time, color=color.new(#FF1744, 0), width=2, style=line.style_solid)",
        "                    line.new(b_time, trig_px, time, trig_px, xloc=xloc.bar_time, color=color.new(#29B6F6, 25), width=1, style=line.style_dashed)",
        "                    line.new(b_time, avg_ep,  time, x1_px,   xloc=xloc.bar_time, color=color.new(#FF5252, 20), width=1, style=line.style_dotted)",
        "            else",
        "                // X1 (+2.0R 短期即時獲利)",
        "                if x1_type == 1 and bar_ymd == x1_ymd",
        '                    label.new(bar_index, high * 1.008, "🎯 #" + str.tostring(tid) + " X1短期+2R獲利\\n@ $" + str.tostring(x1_px, "#.#"), style=label.style_label_down, color=color.new(#00ACC1, 10), textcolor=color.white, size=size.tiny)',
        "                    if show_stop_lines and b_time > 0",
        "                        line.new(b_time, tp1_px, time, tp1_px, xloc=xloc.bar_time, color=color.new(#00E5FF, 20), width=1, style=line.style_dashed)",
        "                // X3 (30D EMA 或波段 Higher Low 技術面弱勢提早出場)",
        "                if x3_type != 4 and bar_ymd == x3_ymd",
        '                    x3_r_txt = x3_type == 5 ? "X3破HigherLow出" : "X3破30EMA轉弱出"',
        '                    label.new(bar_index, high * 1.012, "📉 #" + str.tostring(tid) + " " + x3_r_txt + "\\n@ $" + str.tostring(x3_px, "#.#"), style=label.style_label_down, color=color.new(#FB8C00, 10), textcolor=color.white, size=size.tiny)',
        "                    if show_stop_lines and b_time > 0",
        "                        line.new(b_time, stop_px, time, stop_px, xloc=xloc.bar_time, color=color.new(#FF1744, 15), width=2, style=line.style_solid)",
        "                        line.new(b_time, trig_px, time, trig_px, xloc=xloc.bar_time, color=color.new(#29B6F6, 30), width=1, style=line.style_dashed)",
        "                        line.new(b_time, avg_ep,  time, x3_px,   xloc=xloc.bar_time, color=is_w ? color.new(#00E676, 20) : color.new(#FF5252, 20), width=1, style=line.style_dotted)",
        "                // 若僅 X2 在後續被硬停損洗出 (例如 #13 2019 小牛市 X2 在 2020/03/12 觸及初始硬停損)",
        "                if x2_type == 4 and x1_type != 4 and bar_ymd == x2_ymd",
        '                    label.new(bar_index, high * 1.01, "🛑 #" + str.tostring(tid) + " X2長線倉觸初始硬停損\\n@ $" + str.tostring(x2_px, "#.#"), style=label.style_label_down, color=color.new(#C62828, 10), textcolor=color.white, size=size.tiny)',
    ])

    # Group X2 Halving Theoretical Peak exits by date so multiple positions exiting on the same cycle peak day render a single clean trophy banner!
    x2_peak_groups: dict[int, list[tuple[int, float, str]]] = defaultdict(list)
    for tr in records:
        p = tr["position"]
        x2 = p.exits.get(2)
        if x2 and "硬停損" not in x2.reason:
            x2_peak_groups[date_to_int(x2.date_utc)].append((p.trade_id, round(x2.price, 2), x2.reason))

    lines.append("")
    lines.append("    // (E) X2 事前推論「牛市減半理論高點」總收割標籤 (合併同日出場之長線底倉)")
    lines.append("    if show_exit_labels")
    for ymd_val, items in sorted(x2_peak_groups.items()):
        id_list_str = ",".join(f"#{x[0]}" for x in items)
        px_val = items[0][1]
        reason_str = items[0][2]
        lines.append(f"        if bar_ymd == {ymd_val}")
        lines.append(
            f'            label.new(bar_index, high * 1.03, "🏆 X2 減半理論高點總收割 ({id_list_str})\\n@ $" + str.tostring({px_val}, "#.##") + "\\n{reason_str}", style=label.style_label_down, color=color.new(#FFD600, 0), textcolor=color.black, size=size.normal)'
        )

    lines.extend([
        "",
        "// ============================================================================",
        "// 5. 即時動態 VCP 預掛單線段與警報 (Live Dynamic Order Lines & Alerts)",
        "// ============================================================================",
        "plot(live_vcp_armed ? live_trig_px : na, \"即時VCP預掛觸價線 (E1 Trigger)\", color=color.new(#29B6F6, 0), style=plot.style_linebr, linewidth=2)",
        "plot(live_vcp_armed ? live_stop_px : na, \"即時VCP初始硬停損線 (Hard Stop)\", color=color.new(#FF1744, 0), style=plot.style_linebr, linewidth=2)",
        "plot(live_vcp_armed ? live_tp1_px  : na, \"即時VCP短期+2R目標線 (X1 TP1)\",  color=color.new(#00E676, 0), style=plot.style_linebr, linewidth=1)",
        "",
        "if live_vcp_armed and barstate.islast",
        '    label.new(bar_index, low * 0.98, "🔔 今日VCP收縮成立！請預掛 E1 觸價單\\n觸發價: $" + str.tostring(live_trig_px, "#.##") + "\\n初始硬停損: $" + str.tostring(live_stop_px, "#.##") + "\\nX1(+2R)目標: $" + str.tostring(live_tp1_px, "#.##"), style=label.style_label_up, color=color.new(#1E88E5, 0), textcolor=color.white, size=size.normal)',
        "",
        'alertcondition(live_vcp_armed, title="1_VCP預掛觸價單通知", message="【BTC VCP三進三出】今日日線收盤確認VCP收縮完成，請於前高頸線上方預掛 E1 (1/3倉位) Stop-Market 觸價買單並設定初始硬停損！")',
        'alertcondition(live_vcp_armed[1] and high >= live_trig_px[1] and low > live_stop_px[1], title="2_E1盤中突破觸價成交", message="【BTC VCP三進三出】E1 盤中突破前高頸線觸價成交！請留意今日收盤是否帶量站穩以決定明日開盤 E2 加碼。")',
        "",
        "// ============================================================================",
        "// 6. 右上角策略戰情儀表板 (Dashboard Table)",
        "// ============================================================================",
        "var table dash = table.new(position.top_right, 2, 9, bgcolor=color.new(#0D1117, 10), border_color=color.new(#30363D, 0), border_width=1)",
        "if show_dashboard and barstate.islast",
        '    table.cell(dash, 0, 0, "BTC VCP 三進三出減半策略", text_color=color.white, bgcolor=color.new(#1B365D, 0), text_size=size.small)',
        '    table.cell(dash, 1, 0, "2014–2026 實證 & 即時狀態", text_color=color.new(#FFD600, 0), bgcolor=color.new(#1B365D, 0), text_size=size.small)',
        '    table.cell(dash, 0, 1, "一、當前牛熊確認狀態", text_color=color.silver, text_size=size.tiny)',
        '    table.cell(dash, 1, 1, is_bull_regime ? "🟢 牛市確認 (允許掃描VCP)" : "🔴 熊市休眠/冷卻 (空手觀望)", text_color=is_bull_regime ? color.new(#00E676, 0) : color.new(#FF5252, 0), text_size=size.tiny)',
        '    table.cell(dash, 0, 2, "二、今日 VCP 預掛單訊號", text_color=color.silver, text_size=size.tiny)',
        '    table.cell(dash, 1, 2, live_vcp_armed ? ("⏳ 預掛E1觸價 $" + str.tostring(live_trig_px, "#.#") + " | 停損 $" + str.tostring(live_stop_px, "#.#")) : "無新VCP收縮完成 (等待中)", text_color=live_vcp_armed ? color.new(#29B6F6, 0) : color.gray, text_size=size.tiny)',
        '    table.cell(dash, 0, 3, "回測總交易 / 勝率", text_color=color.silver, text_size=size.tiny)',
        f'    table.cell(dash, 1, 3, "{sim["n_trades"]} 筆 / 勝率 {sim["win_rate_pct"]}%", text_color=color.white, text_size=size.tiny)',
        '    table.cell(dash, 0, 4, "2014–2026 累積總報酬", text_color=color.silver, text_size=size.tiny)',
        f'    table.cell(dash, 1, 4, "+{sim["tot_ret_pct"]}% (CAGR {sim["cagr_pct"]}%)", text_color=color.new(#00E676, 0), text_size=size.tiny)',
        '    table.cell(dash, 0, 5, "最大回撤 / 單筆最差", text_color=color.silver, text_size=size.tiny)',
        f'    table.cell(dash, 1, 5, "MaxDD {sim["max_dd_pct"]}% / 單筆最差 {sim["worst_trade_eq_pct"]}%", text_color=color.new(#FFB74D, 0), text_size=size.tiny)',
        '    table.cell(dash, 0, 6, "Sharpe / Calmar 比", text_color=color.silver, text_size=size.tiny)',
        f'    table.cell(dash, 1, 6, "Sharpe {sim["sharpe"]} / Calmar {sim["calmar"]}", text_color=color.white, text_size=size.tiny)',
        '    table.cell(dash, 0, 7, "單筆中位數 R / 封頂+3R期望", text_color=color.silver, text_size=size.tiny)',
        f'    table.cell(dash, 1, 7, "Median +{sim["median_r"]}R / Capped +{sim["capped_3r_ev"]}R", text_color=color.new(#4DD0E1, 0), text_size=size.tiny)',
        '    table.cell(dash, 0, 8, "三輪減半理論高點 X2 實績", text_color=color.silver, text_size=size.tiny)',
        '    table.cell(dash, 1, 8, "17/12/06 $13.5k | 21/10/08 $53.9k | 25/10/05 $123.5k", text_color=color.new(#FFD600, 0), text_size=size.tiny)',
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    out_pine = root / "reports" / "btc_vcp_3tranche_tradingview.pine"
    code = generate_pine_script()
    out_pine.write_text(code, encoding="utf-8")
    print(f"[OK] Generated TradingView Pine Script ({len(code.splitlines())} lines, {len(code)} bytes) -> {out_pine}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
