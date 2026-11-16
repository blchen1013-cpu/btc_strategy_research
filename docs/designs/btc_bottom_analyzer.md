# BTC/USD 雙軌抄底決策分析與因果回測系統

*   **Author:** blchen1013-cpu
*   **Status:** Approved (Round 3 Resolved)
*   **Date:** 2026-11-14

--------------------------------------------------------------------------------

## Overview

本設計定義一套與現有網頁前端隔離的比特幣（BTC/USD）雙軌抄底分析與因果回測系統。系統先從公開市場與鏈上端點封存不可變日線／週線離線快照，再透過純因果規則計算當下買賣決策、目標價值區間、結構停損與 2010–2026 歷史實證表現，產出涵蓋 7 個分析維度的 Markdown 決策報告。

資金劃分為兩個獨立資金池：**Track A（`Capital_A`）** 執行 1–3 年長線左側估值抄底，不設停損並以週線右側「雙鑰匙」作為超時回退；**Track B（`Capital_B`）** 執行數個月日線右側波段交易，採結構失效出場並將單筆初始最大風險控制在資金池的 10% 以內。

## The Problem

現行網頁應用（[`src/data.js`](file:///home/antigravity/bitcoin-analyzer/src/data.js)、[`src/indicators.js`](file:///home/antigravity/bitcoin-analyzer/src/indicators.js)、[`src/classical.js`](file:///home/antigravity/bitcoin-analyzer/src/classical.js)、[`src/forecast.js`](file:///home/antigravity/bitcoin-analyzer/src/forecast.js)、[`src/backtest.js`](file:///home/antigravity/bitcoin-analyzer/src/backtest.js)、[`src/app.js`](file:///home/antigravity/bitcoin-analyzer/src/app.js)）無法支撐真實資金的跨週期抄底決策，原因如下：

1.  **資料覆蓋短與模擬替換風險**：[`fetchLiveBTCData`](file:///home/antigravity/bitcoin-analyzer/src/data.js#L166-L207) 僅請求單一 `api.binance.com/api/v3/klines` 端點（`limit=180`；由 [`handleFetchLive`](file:///home/antigravity/bitcoin-analyzer/src/app.js#L99-L121) 以 `160` 根呼叫），請求受限或非 2xx 回應時拋錯並退回至以 `Date.now()` 產生的 [`generateSyntheticBTCData`](file:///home/antigravity/bitcoin-analyzer/src/data.js#L27-L161) 合成行情，缺乏橫跨多輪牛熊週期與鏈上估值的真實歷史資料。
2.  **指標初始化與分析目標落差**：[`calcEMA`](file:///home/antigravity/bitcoin-analyzer/src/indicators.js#L19-L36) 直接於第 0 根以收盤價啟動，使 [`calcMACD`](file:///home/antigravity/bitcoin-analyzer/src/indicators.js#L88-L98) 於暖機前期即輸出非空數值，[`calcATR`](file:///home/antigravity/bitcoin-analyzer/src/indicators.js#L100-L111) 採簡單均線而非 Wilder 平滑，且全系統缺少週線長期估值均線；[`runPriceForecast`](file:///home/antigravity/bitcoin-analyzer/src/forecast.js#L7-L133) 結合短線阻尼趨勢外推、30 根線性迴歸與均值回歸，目標為短期預測而非週期估值。
3.  **樞紐點未來窺視與型態失真**：[`findSwingPivots`](file:///home/antigravity/bitcoin-analyzer/src/classical.js#L12-L49) 在中點即讀取右側 5 根未來 K 線標記高低點；[`computeSupportResistanceZones`](file:///home/antigravity/bitcoin-analyzer/src/classical.js#L54-L157) 以全序列最後收盤價計算聚類容差；[`detectClassicalPatterns`](file:///home/antigravity/bitcoin-analyzer/src/classical.js#L234-L556) 在圖表型態數量不足兩筆時強制補入通道或區間結構，且每類圖表型態至多保留一筆匹配。
4.  **單資金池回測假設失真**：[`runStrategyBacktest`](file:///home/antigravity/bitcoin-analyzer/src/backtest.js#L28-L268) 以單一資金池執行全倉複利與忽略跳空的固定百分比停損、最後兩根不開新倉、以 `sqrt(252)` 美股日曆年化任意週期夏普比率（總報酬率未年化），且於 `grossLoss == 0` 且 `grossProfit > 0` 時回傳預設常數 `profitFactor = 3.8`、於零波動時回傳 `sharpeRatio = 1.2`。

## Goals

*   **離線可重現的真實資料快照**：以強制校驗機制擷取跨多輪牛熊週期（2010–2026）的公開現貨、日線 OHLCV、鏈上 MVRV／實質價格與輔助情緒資料，按來源性質分層截斷並封存為具 SHA-256 清單的不可變離線快照。
*   **純因果序列與無前瞻擴展計算**：明確隔離已收盤歷史序列與可選盤中即時現價；跨週期運算僅讀取截止當日已收盤之週線與已生效鏈上讀數；所有樞紐點、對數成長通道、跌幅下限與波段型態皆嚴格以當期以前資訊遞推。
*   **雙資金池互斥且窮盡之決策輸出**：為 `Capital_A` 與 `Capital_B` 分別建立狀態機與決策矩陣，產出單一明確行動指令、核心重押價／價值帶、結構失效與熔斷價位、以及建議部位比例。
*   **Track A 歷史表現實證**：統計歷輪熊市左側估值達標與超時 `Dual-Key` 週線右側回退的進場表現，完整記錄進場後最深帳面浮虧（MAE）以及 1 年、2 年、3 年與下一輪週期高點前報酬。
*   **Track B 勝率、期望值與 10% 風險驗證**：於 2014–2026 真實日線 OHLCV 驗證三類右側進場型態（`SW-1/2/3`）、跳空尾端部位公式、盤中熔斷與移動停利規則；統計 `WK-Warn` 警示亮燈與熄燈下的實證勝率、盈虧比與期望值，並以無前瞻影子帳本驗證警示降級與連敗節流規則。
*   **7 維度整合決策報告**：產出可獨立運作（確定性結構骨架與失效界線）且支援以雜湊綁定 `--narrative-overlay` 補充主／替代艾略特浪型與威科夫定性相位的 Markdown 報告。

## Non-Goals

*   **不修改網頁前端**：不更動 [`index.html`](file:///home/antigravity/bitcoin-analyzer/index.html)、[`src/styles.css`](file:///home/antigravity/bitcoin-analyzer/src/styles.css) 以及 `src/` 目錄下任何既有檔案。
*   **不串接交易所實盤下單**：不建立帳號憑證、錢包簽章或自動委託功能。
*   **不對主觀多浪計數做自動化歷史回測優化**：僅將具確定性規則的幾何、均線與威科夫代理訊號送入歷史回測；艾略特主／替代數浪不參與歷史評分或優化，僅可透過時間／雜湊綁定疊加檔呈現當下結構分析。

## Open Questions

*   無（策略架構、狀態機規則與數值公式基線已鎖定；實際歷史校準值與績效統計於執行離線快照後生成）。

## Glossary & Decision Authority

### 核心術語定義

| 術語 | 定義 |
| :--- | :--- |
| `Capital_A` / `Capital_B` | 互不共享投資額度與部位計算的獨立資金池：`Capital_A` 專責長線左側重押（最高 100% 現貨、無停損）；`Capital_B` 專責日線右側波段（最高滿倉受手續費約束、單筆初始風險上限 10%）。 |
| `L1` | Track A 左側買進硬門檻，含長期週均線折價、鏈上 MVRV 低估與遞減校準歷史跌幅達標三項，必須同時滿足至少 2 項才啟動左側買進邏輯。 |
| `[Zone_Low, Zone_High]` 與 `P_anchor` | 在符合 `L1 >= 2/3` 的價格空間內，依估值與結構交會區間界定之左側買進價值帶 `[Zone_Low, Zone_High]`，及該區間內分數最高之單一主限價錨點 `P_anchor`。 |
| `t_0` 與 `T_max` | `t_0` 為本輪經時間與回撤雙條件確認的週期歷史高點日期；`T_max` 為依過往已完成重大熊市週期持續天數推算的左側等待期限。 |
| `Dual-Key Fallback` | Track A 超過 `T_max` 且尚未於左側價位買進時啟用的週線右側機制：必須同時達成週線右側轉強構造鑰匙與基本估值／深度修正緩衝鑰匙方允許買進。 |
| `SW-Entry` (`SW-1 / SW-2 / SW-3`) | Track B 日線右側進場訊號（區分於支撐代號 `S1–S3`）：分別對應 Higher Low 收復 `50D EMA`、雙底／頭肩底突破頸線、以及因果威科夫箱體下破收回 `Spring -> SOS -> LPS`。 |
| `WK-Warn` | Track B 週線破位警示狀態：週線收盤跌破長期週均線（主規則 `50W SMA`，對照組 `200W SMA`）時亮燈，須待連續 2 週收盤站回方熄燈。 |
| `1R` / `EV_R` / `MAE` | `1R` 為進場時部位承受至有效停損線的美元風險；`EV_R` 為已完結交易扣除雙邊手續費後的平均淨 R 倍數；`MAE` 為持倉期間觸及之最深帳面浮虧比例。 |

### 7 個分析維度系統授權分工

| 分析維度 | 系統內授權角色 | 參與自動化歷史回測？ |
| :--- | :--- | :--- |
| 1. 長期估值（`200W SMA` / `Mayer` / `ATH` 回撤 / 對數通道） | Track A `L1` 硬門檻、價值帶界定與診斷輸出 | 是（擴展視窗） |
| 2. 減半週期位置 | 週期階段定位與 `T_max` 參考對照 | 是（描述性統計） |
| 3. 歷史熊市類比 | Track A 遞減跌幅門檻校準與 MAE 歷史對照 | 是（僅讀已完結週期） |
| 4. 鏈上估值（`MVRV` / 實質價格 `Realized_Price`） | Track A `L1` 硬門檻與價值帶交會計算 | 是（嚴格因果日終對齊） |
| 5. 市場情緒（Fear & Greed） | 報告極端情緒參考指標（無決策否決權） | 2018-02 起輔助紀錄 |
| 6. 中短期技術指標（日／週均線、ATR、RSI、MACD、布林、量能） | Track B `SW-Entry`、出場、`WK-Warn` 核心；`RSI/MACD/BB/Mayer` 專作診斷輸出 | `EMA / ATR / 量能` 進回測 |
| 7. 傳統結構與型態（因果 S/R、對數斐波、頸線、威科夫、波浪） | 因果 S/R、對數斐波、頸線、`SW-3` 與結構骨架進引擎；主觀波浪由可選疊加檔呈現 | 幾何與代理規則進回測；主觀波浪不進回測 |

## Critical User Journeys (CUJs)

### CUJ 1: 離線快照封存與來源校驗

*   **Given**: 操作者指定目標路徑與基準時間，且本機可存取公開市場與鏈上歷史 API。
*   **When**: 執行快照擷取指令抓取日線 OHLCV、鏈上 `CapMVRVCur / PriceUSD`、交叉校驗 OHLCV 與可選情緒序列，並進行時序對齊與週線重採樣。
*   **Then**: 系統將不可變歷史序列連同逐檔及正規化清單 SHA-256 寫入快照目錄；若主來源缺漏、時間戳偏移越界或重疊價差超限立即報錯中止，輔助即時現價僅獨立於雜湊清單外按即時需求記錄。

### CUJ 2: Track A 長線抄底決策與超期回退驗證

*   **Given**: 系統已驗證並載入橫跨 2010–2026 的日線、週線與鏈上歷史快照。
*   **When**: `track_a` 與 `backtest_engine` 計算擴展視窗 `L1` 門檻、`[Zone_Low, Zone_High]`、`P_anchor` 與 `Dual-Key` 狀態，並重演歷輪左側進場、限價掛單與超時右側回退。
*   **Then**: 為 `Capital_A` 輸出明確的當下與回測持倉狀態結論、掛單目標區間，並附帶各輪已完結週期的 MAE 與遠期／週期高點前實證報酬統計。

### CUJ 3: Track B 日線右側波段回測與警示降級控制

*   **Given**: 系統已載入 `2014-09-17` 起完整日線 OHLCV 及無未來確認的週線 `WK-Warn` 序列。
*   **When**: `track_b` 與 `backtest_engine` 模擬 `SW-1/2/3` 次日開盤進場、歷史尾端跳空緩衝縮倉、盤中 10% 熔斷與移動出場，並以無前瞻影子 1R 帳本追蹤 `WK-Warn` 亮燈期間的歷史期望值。
*   **Then**: 輸出各型態與警示狀態分組之回測勝率、盈虧比與淨 `EV_R`，依據固定門檻自動輸出 `Capital_B` 當期允許風險（`10% / 5% / 0%`）、進場參考價、結構停損與熔斷價位。

### CUJ 4: 雙軌整合 7 維度報告產出

*   **Given**: 離線驗證通過且雙軌運算完成，並可選載入通過快照時間與清單雜湊校驗的主觀浪型疊加檔。
*   **When**: 報告生成器彙整兩組資金池決策結論、歷史實證表格與 7 個分析維度數據。
*   **Then**: 產出結構化摘要 JSON 與一份數據可溯源至離線快照、明確分離長線與波段決策的 7 維度 Markdown 報告。

## Risks & Mitigations

*   **非官方圖表端點與鏈上日終發布時間落差**：
    *   *風險*：Yahoo Finance 圖表端點受上游欄位異動或限流影響；CoinMetrics 日終鏈上指標通常於 UTC 換日後延遲數小時發布，若強綁單一截斷時間會使 Track B 無故丟失最新已收盤日線或影響即時比對。
    *   *緩解*：採「主來源雙核心（Yahoo 日線 + CoinMetrics `CapMVRVCur, PriceUSD`）」與「近期交易所重疊核對（Bitstamp 日線）」分級（主來源異常時強制中止；12 年收盤一致度仰賴 CoinMetrics 對 Yahoo 長序列核對，近 `1,000` 日另有 Bitstamp 核對且逾時或歷史重放重疊不足時降級標記而不阻斷）；對齊截斷採分軌獨立水位線（容許最多 1 日發布差），並將盤中即時現價自快照雜湊邊界抽離，僅於請求當日現貨模式時寫入對照欄。
*   **早年無完整高低量序列與小樣本統計偏差**：
    *   *風險*：`2014-09-17` 以前缺乏真實交易所日線高低點與成交量，且全史已完成重大熊市週期僅 4 輪（`M <= 4`，其中 `M < 2` 為早年暖機期）；對數成長通道下緣界線建立於高度自相關的對數趨勢殘差上，並非嚴格獨立同分佈信賴區間。
    *   *緩解*：限定 Track B 策略僅在 `2014-09-17` 起真實 OHLCV 區間執行；2010–2014 現貨僅供 Track A `200W SMA` 暖機、週期高點統計與對數趨勢線使用；於報告第 2 節明示對數通道下緣之經驗邊界性質，並將早年暖機期與後續樣本外週期並列對照。
*   **極端波動率下熔斷價位歸零與因果閘門永久鎖死**：
    *   *風險*：市場劇跌期間日內波動放大，若動態跳空緩衝缺乏上限，與停損距離相加可能突破 100% 導致熔斷價位小於等於零；在因果警示回測中，一旦負期望值觸發 `0%` 停單，若實盤不再追蹤理論交易將造成無法解除封鎖的狀態死鎖。
    *   *緩解*：對單筆進場之停損距離與跳空緩衝總和設定嚴格上限（超限即取消進場訊號），使盤中熔斷價維持正數保護；同時維持一條獨立且始終記帳的「基準 `1R` 影子帳本」，供實證期望值轉正時自動恢復開倉資格。

## Technical Plan (Architecture)

### 1. 模組邊界、資料流與分軌水位線

系統由線上快照抓取與純離線分析回測兩階段組成，各模組依「資料擷取／純指標／長線 Track A／波段 Track B／回測／報告」分層隔離：

```text
 [線上快照抓取 (唯一允許 HTTP 存取階段)]
   Yahoo Finance Daily (2014-09-17+, Primary OHLCV) ────────┐
   CoinMetrics Daily (2010-07-18+, CapMVRVCur & PriceUSD) ──┼─> fetch_snapshots.py
   Bitstamp Daily (<= 1,000d, Exchange OHLCV Cross-Check) ──┤     ├─ 分來源水位線截斷 & 連續性檢核
   Alternative.me F&G (2018-02-01+, Optional Diagnostic) ───┤     ├─ 12 年與近期雙重誤差比對
   Kraken Ticker (Optional LIVE runtime only -> unhashed) ──┘     └─ ISO 週一週線重採樣 & manifest.json
                                                                           │
 [純離線分析 & 回測 (嚴格禁止連網; --as-of 嚴格重截)]                     ▼
   analyze_and_report.py <--(可選: SHA-256 + 時戳綁定 narrative_overlay.json)
     ├─ indicators.py       (純滾動技術指標、因果樞紐點與相對百分比因果 S/R、WK-Warn、擴展 LGC)
     ├─ track_a.py          (Cycle_ATH / t_0、擴展 L1/T_max、價值帶與 P_anchor、Dual-Key、Track A 狀態)
     ├─ track_b.py          (SW-1/2/3 型態偵測、日線尾端緩衝倉位、Capital_B 狀態)
     ├─ backtest_engine.py  (Track A 多路徑回測 + Track B 基準/分組/影子因果降級與連敗敏感度)
     └─ output              (analysis_summary.json + 7 維度決策報告 + LIVE 現價對照欄)
```

*   **資料源整併與時間基準統一**：以 **CoinMetrics Community API（單次多欄位請求 `CapMVRVCur,PriceUSD`）** 同時提供 `2010-07-18` 起每日連續鏈上比率、一致之實質價格（`Realized_Price = PriceUSD / CapMVRVCur`）、`2014-09-17` 以前日線暖機序列以及 12 年跨源比對基準，並搭配 Bitstamp 單次非分頁 OHLCV 執行近期交易所核對。
*   **標準 ISO 週一 UTC 週線重採樣**：於合併後之連續日線序列按週一 `00:00:00 UTC` 7 日完整週桶合成週線（丟棄首尾未滿 7 日殘週），使 `200W SMA` 早於 Yahoo OHLCV 起點即完成暖機。
*   **雙水位線與即時報價抽離**：快照清單區分 OHLCV 與 MVRV 有效水位線，確保 Track B 不受鏈上數小時發布延遲牽連。盤中即時現價（`live_spot`）獨立封存於雜湊清單之外（僅允許由 `fetch_snapshots.py` 抓取與更新）；離線分析依 `--as-of` 與快照邊界切換 `LIVE` 與 `HISTORICAL_REPLAY`，後者強制屏蔽即時現價並一律採用當期收盤價。

### 2. 因果基準與無前瞻擴展計算核心

*   **嚴格因果對齊不變式**：於任意日線基準時點，系統僅讀取當時已完全收盤之週線、已生效之鏈上紀錄與右側延遲已確認之樞紐點；歷史重放模式於分析入口先執行全域資料截斷再進行任何指標計算。
*   **擴展週期狀態與遞減校準原則**：
    *   逐日更新累積最高價並追蹤週期峰值日期：創嚴格新高即時重置起算日 `t_0` 狀態與波谷；平高不重置既有起算日，於滿足時間與回撤門檻後鎖定本輪 `t_0`。
    *   凡歷經深幅回撤且峰值至谷底跨度達標（排除短期牛市中繼急跌）並於後續創高突圍之循環，始登記為「已完結重大熊市週期」。跌幅下限 `Calibrated_DD_Floor` 與待機期限 `T_max` 皆僅取用截至當期已完成登記之過往週期遞推。
    *   對數成長通道下界 `LGC_Lower_Band` 與支撐壓力聚類皆採嚴格因果擴展／近期視窗計算，其中因果 S/R 依當前價格採相對百分比容差與週線時間視窗限縮，避免早年低價樞紐吞併近期價格結構。

### 3. Track A (`Capital_A`) 左側估值決策與超期回退架構

*   **`L1` 硬門檻（三取二）**：結合（1）收盤價貼近或低於 `200W SMA * 1.10`、（2）鏈上 `CapMVRVCur <= 1.00`、（3）自 `Cycle_ATH` 回撤達當期 `Calibrated_DD_Floor`，三項達兩項始具左側重押資格。
*   **價值帶與核心錨點合成**：於滿足投影 `L1 >= 2/3` 之價格空間內，取「`200W` 均線帶、實質價格帶、對數通道下緣帶與對數斐波那契 `0.618–0.786` 回撤帶」的最高共識交會段作為 `[Zone_Low, Zone_High]`，並結合因果週線支撐樞紐權重選出單一最高分限價錨點 `P_anchor`。
*   **`Dual-Key Fallback` 超期機制**：若距 `t_0` 經過天數達 `T_max` 仍未左側進場，撤銷無限期等待左側之限價立場，切換為須同時達成「構造鑰匙（兩個皆嚴格形成於 `t_0` 之後且未跌破之最新週線 Higher Low ＋ 週線收盤站上 `21W EMA`）」與「估值鑰匙（`L1 >= 1/3` 或累積最大回撤達 `75%` 校準熊市深度）」的週線右側進場規則。
*   **互斥且窮盡之 `Capital_A` 狀態矩陣**（`effective_spot` 於 `LIVE` 且具即時現價時取 `live_spot`，否則取 `Close[t]`；`track_a.action` 固定輸出對應空白資金立場之 `A2–A7` 枚舉，`backtest_cycle_deployed` 標記主路徑週期持倉狀態 `A1`）：

| 狀態組 | `Deployed_In_Cycle` | `L1` (已收盤) 與 `Zone_High` 條件 | `Elapsed >= T_max` | `Dual-Key` | `Capital_A` 唯一結論 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| A1 | `True`（主回測已重押） | 任意 | 任意 | 任意 | **`HOLD_CYCLE_POSITION`**（維持本輪現貨持倉至創高突圍重啟週期；空白資金欄位依 A2–A7 輸出當期動作） |
| A2 | `False`（待進場） | `L1 >= 2/3` 且（`Zone_High is None` 或 `effective_spot <= Zone_High`） | 任意 | 任意 | **`BUY_NOW_LEFT_ZONE`**（左側估值與價值帶同步達標，執行單筆重押並附 `P_anchor` 參考） |
| A3 | `False`（待進場） | `L1 >= 2/3` 且 `Zone_High` 非空且 `effective_spot > Zone_High` | `False`（或 `t_0` 未定） | 任意 | **`BUY_LIMIT_PULLBACK`**（估值達標但現價高於共振帶，於 `Zone_High` 至 `P_anchor` 掛限價單） |
| A4 | `False`（待進場） | `L1 < 2/3` 且 `LIVE` 具即時價且 `Zone_High` 非空且 `effective_spot <= Zone_High` | `False`（或 `t_0` 未定） | 任意 | **`INTRADAY_ALERT_PENDING_CLOSE`**（盤中現價跌入價值帶，待 UTC 收盤確認 `L1 >= 2/3`） |
| A5 | `False`（待進場） | `L1 < 2/3` 且（非 `LIVE` 無即時價、或 `Zone_High is None`、或 `effective_spot > Zone_High`） | `False`（或 `t_0` 未定） | 任意 | **`WAIT_LEFT_TARGET`**（維持現金，目標價值帶 `[Zone_Low, Zone_High]` 與核心價 `P_anchor`） |
| A6 | `False`（待進場） | 非 A2 條件（未觸發左側買進） | `True` | `True` | **`BUY_NOW_DUAL_KEY_FALLBACK`**（逾越左側期限，週線右側結構與基本估值雙鑰匙同步成立買進） |
| A7 | `False`（待進場） | 非 A2 條件（未觸發左側買進） | `True` | `False` | **`WAIT_WEEKLY_RIGHT_SIDE`**（逾越左側期限，待 `Dual-Key` 雙條件成立或價格跌入 A2 左側買進區） |

### 4. Track B (`Capital_B`) 右側波段、尾端緩衝與 `WK-Warn` 防護架構

*   **右側三型態確認（`SW-Entry`，僅限 `2014-09-17+` 真實日線）**：分別涵蓋日線未破且單次消費的 Higher Low 收復 `50D EMA`（`SW-1`）、確認未破底之雙底／頭肩底首度放量突破頸線（`SW-2`）、以及針對固定歷史 `(SC, AR)` 箱體依序發生下破收回 `Spring`、放量突破 `SOS` 並於回踩守住箱體中軸收陽之 `LPS`（`SW-3`）。「當日有效進場觸發」須同時滿足結構停損與綜合停損距離不逾上限、非當日平倉冷卻日、且通過跳空緩衝上限保護；未通過防禦檢查時原訊號作廢並路由至 `WAIT_SIGNAL`。
*   **跳空尾端部位規模與正熔斷保護**：以因果擴展統計過往日線單日負報酬對 `ATR14` 的第 99 百分位倍數計算當期跳空緩衝，將單筆實際名目風險比例上限（含雙邊手續費與跳空穿透緩衝）鎖定於資金池的 10% 內並限定總緩衝上限，保證盤中強制停損觸發線 `circuit_breaker_price` 嚴格保持正數。
*   **K 線執行與出場優先序**：進場採訊號日次日開盤撮合，結構停損於進場時鎖定為 `structural_stop`。每個持倉交易日依序執行：（1）結算前日收盤已排程之開盤平倉（同日收盤禁止反手開新單）；（2）盤中檢查極端跳空與 10% 權益熔斷線（同日亦禁止收盤重開新單）；（3）依昨日既有 `active_stop` 與趨勢狀態（持有滿 10 日或進場後曾收盤站上 `EMA50` 後出現連續 2 日收盤跌破）排程次日開盤平倉；（4）未排程出場時，於收盤淨浮盈達 `+1.5R` 上移保本停損，並隨新確認之 Higher Low 依確認日凍結 ATR 上移結構移動停損。
*   **`WK-Warn` 狀態與影子帳本門檻判定**：週線收盤跌破 `50W SMA`（報告並列 `200W SMA` 對照）即亮燈，待連續 2 週站回方熄燈。回測除產出固定 10% 風險基準、依型態與亮燈／熄燈分組統計外，亦運行單部位順序進出場之「基準 `1R` 影子帳本」執行無前瞻因果降級與三連敗減半敏感度模擬。
*   **互斥且窮盡之 `Capital_B` 行動決策矩陣（共 8 條唯一互斥規則 `B1–B7b`）**：

| 狀態組 | 持倉狀態 | `WK-Warn (50W)` | `SW-Entry` 當日有效進場觸發 | 累計 `ACTIVE` 閉合表現（因果歷史指標） | `Capital_B` 唯一結論 | 單筆風險預算 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| B1 | 持倉中（已排程出場） | 任意 | 不適用 | 不適用 | **`EXIT_NEXT_OPEN`**（已觸發收盤結構或趨勢破壞，於次日開盤平倉） | 既定持倉結清 |
| B2 | 持倉中（續抱） | 任意 | 不適用 | 不適用 | **`HOLD_POSITION`**（維持持倉並公告最新 `active_stop` 與 `circuit_breaker_price`） | 已進場鎖定 |
| B3 | 空倉 | `INACTIVE` | 無（含被防禦拒單或冷卻日） | 不適用 | **`WAIT_SIGNAL`**（無有效右側買進訊號，空手觀望） | `10.0%` 待命 |
| B4 | 空倉 | `INACTIVE` | 有效觸發 | 不適用 | **`BUY_NEXT_OPEN`**（警示未亮燈且結構與風控通過，次日開盤買進） | `10.0%` |
| B5 | 空倉 | `ACTIVE` | 任意 | `N_active >= 15` 且 `EV_R_active <= 0.0` | **`WATCHLIST_BLOCKED`**（週線破位期實證負期望值，強制停開新單等待雙週站回） | `0.0%` |
| B6 | 空倉 | `ACTIVE` | 無（含被防禦拒單或冷卻日） | `N_active < 15` 或 `EV_R_active > 0.0` | **`WAIT_SIGNAL`**（處於破位觀察期且未封鎖，待有效右側訊號出現再行評估） | 依條件 `5.0% / 10.0%` 待命 |
| B7a | 空倉 | `ACTIVE` | 有效觸發 | `N_active < 15` 或（`EV_R_active > 0.0` 且（`EV_R_active <= 0.5 * max(0, EV_R_inactive)` 或 `EV_R_active < 0.15`）） | **`BUY_REDUCED_RISK`**（樣本未足或警示期實證期望值偏低，依跳空緩衝公式減半風險進場） | `5.0%` |
| B7b | 空倉 | `ACTIVE` | 有效觸發 | `N_active >= 15` 且 `EV_R_active >= 0.15` 且 `EV_R_active > 0.5 * max(0, EV_R_inactive)` | **`BUY_NEXT_OPEN`**（警示期間具足夠實證正期望值，依跳空緩衝部位公式正常進場） | `10.0%` |

## Alternatives Considered

### Alternative A: 擴充現有 [`data.js`](file:///home/antigravity/bitcoin-analyzer/src/data.js)、[`indicators.js`](file:///home/antigravity/bitcoin-analyzer/src/indicators.js)、[`classical.js`](file:///home/antigravity/bitcoin-analyzer/src/classical.js) 與 [`backtest.js`](file:///home/antigravity/bitcoin-analyzer/src/backtest.js) 執行回測

*   **Pros**: 可重複使用既有部分技術指標計算迴圈，減少前端既有函式重寫量。
*   **Cons**: 現有前端模組綁定模擬行情回退、未來窺視樞紐、全倉複利假設與以 `sqrt(252)` 美股日曆年化任意週期夏普比率，在既有架構上大幅改寫極易違反不修改網頁前端（含 [`app.js`](file:///home/antigravity/bitcoin-analyzer/src/app.js) 與頁面樣式）的範圍邊界。
*   **Rejection Rationale**: 改於獨立目錄 `scripts/btc_bottom/` 下以 Python 3.11 標準庫建構離線系統，徹底隔離既有前端並確保所有回測統計源自純因果規則。

### Alternative B: 合併單一資金池與全帳戶統一 10% 停損

*   **Pros**: 帳號淨值曲線與風險統計單一，不需分離兩組行動矩陣。
*   **Cons**: 統一的 10% 停損限制將導致長線左側部位在價值區下方震盪時被頻繁停損出局，無法執行「1–3 年不設停損並承受帳面浮虧」的投資目標。
*   **Rejection Rationale**: 維持完全隔離的 `Capital_A` 與 `Capital_B` 雙資金池與對應決策表，分別以無停損估值機制與 10% 風險上限服務兩種持有期完全不同的策略需求。

### Alternative C: 保留第三方週線 API 與多交易所額外歷史端點

*   **Pros**: 不需撰寫 7 日週一重採樣程序，且近期價格有額外多所交叉來源。
*   **Cons**: 第三方週線端點受 720 根滑動視窗截斷與非標準週四桶邊界影響；而引入過多異構單點指數（如與 Yahoo 存在 1 日收盤時戳錯位的市場點位匯流）會增加資料時序錯位風險。
*   **Rejection Rationale**: 精簡為 Yahoo Finance Daily（2014–2026 OHLCV）、CoinMetrics（2010–2026 鏈上指標與早年日線暖機）與 Bitstamp（近期交易所 OHLCV 校驗），統一按標準 ISO 週一 `00:00 UTC` 重採樣週線。

### Alternative D: 在純離線腳本內替任意歷史時間點自動推算艾略特主／替代數浪

*   **Pros**: 執行單一支 Python 腳本即可於所有歷史重放時間自動產生主／替代波浪標籤與說明。
*   **Cons**: 艾略特波浪具備多解主觀性，缺乏統計基準的自動多浪標籤易於過擬合或在圖表轉折不明處強行套入浪級。
*   **Rejection Rationale**: 採用確定性結構骨架與可選主觀疊加註記分離設計：Python 引擎產出因果樞紐、斐波與失效界線（`STRUCTURAL_ONLY`），完整主／替代多浪計數則透過驗證雜湊匹配的 `--narrative-overlay` JSON 選擇性合成於當前分析報告。

## Non-Functional Requirements

*   **零外部相依標準庫相容**：系統與所有測試必須 100% 採用目標執行環境之 Python 3.11 標準庫（`urllib.request`、`json`、`csv`、`hashlib`、`math`、`statistics`、`bisect`、`datetime`、`pathlib`、`unittest`），不依賴 `pandas`、`numpy`、`scipy`、`requests` 或 `pytest`。
*   **離線可重現與效能預算**：分析與回測執行器嚴禁建立網路連線；歷史序列總未壓縮容量維持於 `5 MB` 以下；設計目標為單次離線執行 16 年日／週分析與雙軌回測於 `10` 秒內完成。
*   **HTTP 請求防護與資源上限**：資料擷取階段所有遠端請求須夾帶標準 `User-Agent`，並強制單檔讀取上限、固定請求逾時、有界重試次數與分頁迭代上限。

--------------------------------------------------------------------------------

## Detailed Implementation Plan

本節定義所有數學公式、資料綱要、端點契約、模組邊界與驗證測試規格；實作程式碼限於下列目標清單，嚴格禁止更動前端頁面與樣式檔案。

### Incremental Phasing & Checkpoints

*   **Phase 1 — 快照擷取與驗證層**：實作 `scripts/btc_bottom/__init__.py`、`scripts/btc_bottom/fetch_snapshots.py`、`data/snapshots/.gitkeep` 與獨立測試 `tests/test_snapshots.py`。*檢查點*：`python3 -m unittest tests/test_snapshots.py` 全數模擬測試通過，且成功抓取並驗證本機離線快照（`--verify-only`）。
*   **Phase 2 — 因果指標與雙軌狀態邏輯層**：實作 `scripts/btc_bottom/indicators.py`、`scripts/btc_bottom/track_a.py`、`scripts/btc_bottom/track_b.py` 與獨立測試 `tests/test_indicators_and_tracks.py`。*檢查點*：嚴格前綴因果一致性、指標暖機索引、`Cycle_ATH`/`t_0`、價值帶合成、相對百分比因果 S/R 與 `SW-1/2/3`（含 `(SC, AR)` 配對箱體）單元測試全過。
*   **Phase 3 — 雙軌回測與 7 維度報告生成層**：實作 `scripts/btc_bottom/backtest_engine.py`、`scripts/btc_bottom/analyze_and_report.py`、`reports/.gitkeep` 與測試 `tests/test_backtest_and_report.py`。*檢查點*：全部 3 組測試套件通過、離線 `--snapshot-dir` 生成 JSON／報告通過有限數與時序防窺視自檢、且 `git status --porcelain index.html src/` 為空。

### Authoritative Mathematical Formulas & Tie-Breaking Rules

#### 1. 純技術指標、因果樞紐、因果 S/R 與 `WK-Warn` (`indicators.py`)
*   **`calc_sma(values, n)`**：前 `n - 1` 項或視窗內含 `None` 時回傳 `None`，其餘輸出連續 `n` 項算術平均。據此於 `macro_daily` 計算 `SMA200D` 並定義 **`mayer_multiple = Close[t] / SMA200D[t]`**（若 `SMA200D[t] is None` 則為 `None`），於 `daily_ohlcv` 計算 **`vol_ratio_20 = Vol[i] / Vol_SMA20[i]`**（若 `daily_ohlcv` 為空、`Vol_SMA20[i] is None` 或為 `0.0` 則為 `None`）。
*   **`calc_ema_sma_seeded(values, n)`**：從左掃描找尋首 `n` 個非 `None` 數值，記第 `n` 個非 `None` 元素之陣列索引為 `k`；索引 `< k` 一律回傳 `None`，索引 `k` 初始化為該前 `n` 個非 `None` 值之平均，索引 `> k` 以 `alpha = 2 / (n + 1)` 依 `EMA[i] = val * alpha + EMA[i-1] * (1 - alpha)` 遞推（連續非空序列中 `k = n - 1`）。
*   **`calc_wilder_atr(bars, n=14)`**：`TR[0] = High[0] - Low[0]`；`i >= 1` 時 `TR[i] = max(High[i] - Low[i], abs(High[i] - Close[i-1]), abs(Low[i] - Close[i-1]))`。於索引 **`n - 1` (`13`)** 初始化 `ATR[13] = sum(TR[0..13]) / 14`，`0..12` 回傳 `None`，`i >= 14` 採 `ATR[i] = (ATR[i-1] * 13 + TR[i]) / 14`。
*   **`calc_wilder_rsi(closes, n=14)`**：價差 `d[i] = Close[i] - Close[i-1]` 自 `i = 1` 起計算；於索引 **`n` (`14`)** 計算首 14 筆價差 `i in [1..14]` 之 `avg_gain = sum(max(d,0))/14` 與 `avg_loss = sum(max(-d,0))/14`，`0..13` 回傳 `None`；其後 `i >= 15` 以 `(prev * 13 + cur) / 14` 遞推；`avg_loss == 0.0` 時回傳 `100.0`。
*   **`calc_macd(closes, fast=12, slow=26, sig=9)`**：`DIF[i] = EMA12[i] - EMA26[i]`（自索引 **`25`** 起非 `None`）；`DEA = calc_ema_sma_seeded(DIF, 9)`（首 9 個非 `None` `DIF` 為 `[25..33]`，故 `DEA` 與 `Histogram = DIF - DEA` 自索引 **`33`** 起有效，此前均為 `None`）。
*   **`calc_bollinger(closes, n=20, k=2.0)`**：`0..18` 回傳 `None`；`i >= 19` 使用 `statistics.pstdev(closes[i-19 : i+1])` 計算 `mid ± 2.0 * sd`。
*   **`compute_causal_pivots(bars, left_bars, right_bars)`**：當索引 `p in [left_bars .. N - 1 - right_bars]` 嚴格滿足 `High[p] > High[j]`（對所有 `j in [p-left_bars .. p+right_bars], j != p`）形成因果波段高點，`Low[p] < Low[j]` 形成低點。回傳 `Pivot(idx=p, confirm_idx=p + right_bars, price, kind, ts)`；所有邏輯於基準索引 `i` 僅讀 `confirm_idx <= i`。日線採 `(5, 5)`，週線結構低點採 `(3, 3)`。
*   **`compute_causal_sr_zones(daily_bars_prefix, weekly_pivots_prefix, i, atr14_i)`**（近端時間窗 ＋ 相對百分比聚類）：
    *   候選樞紐點池：日線已確認 `(5, 5)` 樞紐中滿足 `max(0, i - 365) <= p.idx` 且 `p.confirm_idx <= i` 者（權重 `1.0`），加上已收盤週線確認 `(3, 3)` 樞紐中滿足 `bar_close_ts - 1095 * 86400 <= p.ts <= bar_close_ts`（近 3 年）者（權重 `2.0`）。
    *   因果相對聚類：設相對百分比容差 `tol_pct = max(0.015, 0.75 * ((atr14_i / Close[i]) if atr14_i else 0.02))`。將候選點依 `price` 由低至高排序並單趟合併：若 `abs(pt.price - cur_cluster.center) / cur_cluster.center <= tol_pct` 則併入並重算加權平均中心價 `center = sum(w * p) / sum(w)`。
    *   測試次數與排序：對每個聚類定義 `band_half = max(0.5 * (max_p - min_p), 0.005 * center)`、`band_low = center - band_half`、`band_high = center + band_half`，統計近一年 K 線 `[max(0, i-365) .. i]` 滿足 `bar.high >= band_low and bar.low <= band_high` 之次數 `touches`，評分 `score = total_weight + 0.25 * touches`。對 `center > Close[i]` 按 `(-score, abs(center - Close[i]), center)` 取前 3 筆並依 `center` 升序命名 `R1..R3`；對 `center <= Close[i]` 按同排序鍵取前 3 筆並依 `center` 降序命名 `S1..S3`。每筆輸出 `{code, price: center, band_low, band_high, touches, score, dist_pct}`。
*   **`compute_expanding_lgc(macro_daily)`**（Welford/Chan 線上中心化遞推）：
    *   自變量 `x_t = log10(max(1, (date_utc[t] - date(2009, 1, 3)).days))`，應變量 `y_t = log10(Close[t])`。
    *   逐日維護 `(n, mean_x, mean_y, C_xx, C_xy, C_yy)`：`n += 1; dx = x - mean_x; dy = y - mean_y; mean_x += dx / n; mean_y += dy / n; dx2 = x - mean_x; dy2 = y - mean_y; C_xx += dx * dx2; C_xy += dx * dy2; C_yy += dy * dy2`。
    *   當 `n >= 365` 且 `C_xx > 0`：`slope = C_xy / C_xx`、`intercept = mean_y - slope * mean_x`、`sse = max(0.0, C_yy - slope * C_xy)`、`sigma = sqrt(sse / (n - 2))`，輸出 `LGC_Lower_Band[t] = 10 ** (intercept + slope * x_t - 1.645 * sigma)`；`n < 365` 回傳 `None`。
*   **`compute_wk_warn_series(weekly_rows, ma_period=50)`**：
    *   計算每週 `SMA(ma_period)`。初始 `active = False, reclaim_cnt = 0, active_weeks = 0, episode_start_date = None`。依序對週線 `w`：若 `SMA[w] is None` 維持 `False`；若 `Close[w] < SMA[w]`：若原 `not active` 則設 `episode_start_date = w.open_date_utc`，設 `active = True, reclaim_cnt = 0, active_weeks += 1`；否則若 `active` 遞增 `reclaim_cnt += 1`、`active_weeks += 1`，當 `reclaim_cnt >= 2` 立即重設 `active = False, reclaim_cnt = 0, active_weeks = 0, episode_start_date = None`。

#### 2. Track A 週期狀態、價值帶與回測參數 (`fetch_snapshots.py` / `track_a.py`)
*   **`build_macro_daily_series(mvrv_rows, daily_ohlcv_rows, weekly_ohlcv_rows=None)`**（於 Phase 1 `fetch_snapshots.py` 定義並供 Phase 2 `track_a.py` 匯入）：
    *   依 UTC 日期對齊併合 `mvrv.csv` 與 `daily_ohlcv.csv`：凡嚴格早於 `daily_ohlcv[0].date_utc`（或 `daily_ohlcv` 為空）之 `mvrv` 日期，填充 `open = high = low = close = price_usd`、`volume = 0.0`、`is_ohlcv = False`；自 `daily_ohlcv[0]` 起採 Yahoo OHLCV 數值並設 `is_ohlcv = True`。
    *   `weekly_ohlcv.csv` 重採樣規則：按 ISO 週一 `00:00:00 UTC` 將 `macro_daily` 切分為 7 日完整週桶（丟棄任何日數 `< 7` 之首尾殘週），取 `open = days[0].open`、`high = max(d.high)`、`low = min(d.low)`、`close = days[-1].close`、`volume = sum(d.volume)`（CM 純現貨日貢獻 `0.0`）、`is_ohlcv = true` 若且唯若該週全部 7 天皆為 `True`（含 `2014-09-15` 交界週為 `false`）。
*   **`Cycle_ATH`、`t_0` 與已完結重大熊市週期登記**：
    *   定義 `Cycle_ATH[t] := Rolling_ATH[t]`。首日 `Rolling_ATH = High[0], Peak_Date = date[0], Trough = Low[0], Trough_Date = date[0], t_0 = None`。
    *   當 `High[t] > Rolling_ATH`（嚴格創新高）：若此前 `t_0 is not None`、`max_dd = (Trough - Rolling_ATH) / Rolling_ATH <= -0.50` 且 **`t_to_trough_days = (Trough_Date - Peak_Date).days >= 90`**，立即將該筆循環寫入「已完結重大熊市週期表」（欄位：`peak_date, ath, trough_date, trough, max_dd, t_to_trough_days, breakout_date`）。隨後重置 `Rolling_ATH = High[t], Peak_Date = date[t], Trough = Low[t], Trough_Date = date[t], t_0 = None`。
    *   平高平手（`High[t] == Rolling_ATH`）：維持原 `Peak_Date` 與現行 `t_0`。
    *   低點更新與 `t_0` 確認：若 `High[t] <= Rolling_ATH`，遇嚴格新低 `Low[t] < Trough` 更新 `Trough = Low[t], Trough_Date = date[t]`（同低保留最先觸底日）；若 `t_0 is None` 且 `(date[t] - Peak_Date).days >= 60` 且 `(Close[t] - Rolling_ATH) / Rolling_ATH <= -0.25`，鎖定 `t_0 = Peak_Date`。
*   **無前瞻校準與 `L1`**：
    *   令 `completed_cycles` 為截至 `t`（含）已完結重大熊市週期清單，`M = len(completed_cycles)`，`dd = [c.max_dd for c in completed_cycles]`，`D = [c.t_to_trough_days for c in completed_cycles]`。
    *   `M < 2` 時 `Calibrated_DD_Floor = -0.80`（`early_burn_in = True`）；`M >= 2` 時 `Calibrated_DD_Floor = min(-0.55, max(-0.85, max(dd[-2:]) + 0.05))`（`early_burn_in = False`）。
    *   `T_max`：`M == 0 -> 365`；`M == 1 -> max(365, D[0])`；`M >= 2 -> max(365, round(mean(D) + stdev(D)))`（`statistics.stdev` 樣本標準差）。
    *   `L1`（需有效對齊讀數）：(1) `SMA200W` 非空且 `Close[t] <= 1.10 * SMA200W`；(2) 嚴格取 `mvrv.effective_ts <= bar.close_ts` 最新筆，`CapMVRVCur <= 1.00`；(3) `(Close[t] - Cycle_ATH[t]) / Cycle_ATH[t] <= Calibrated_DD_Floor[t]`。
*   **價值帶 `[Zone_Low, Zone_High]` 與 `P_anchor` 網格合成**：
    *   `Realized_Price = cm_price_usd / cap_mvrv_cur`（來自同一已生效 CoinMetrics 日紀錄）。
    *   建構有效子帶清單（跳過底座為 `None` 者，其中 `Low` 取最近已完結重大熊市週期 `trough`，無前低取 `macro_daily[0].low`；設 `fib_low = Low**0.786 * Cycle_ATH**0.214`）：`(a) [0.85*SMA200W, 1.10*SMA200W]`（權重 `1.0`）、`(b) [0.80*Realized_Price, 1.00*Realized_Price]`（權重 `1.0`）、`(c) [0.90*LGC_Lower_Band, 1.10*LGC_Lower_Band]`（權重 `0.8`）、`(d)` 對數斐波 `[fib_low, Low**0.618 * Cycle_ATH**0.382]`（權重 `0.8`）。
    *   對數網格：`anchors = [x for x in (SMA200W, Realized_Price, LGC_Lower_Band, fib_low, Cycle_ATH * (1.0 + Calibrated_DD_Floor)) if x is not None and x > 0]`、`P_min = max(0.01, 0.25 * min(anchors))`、`P_max = 1.15 * max(所有有效子帶上界, Cycle_ATH)`，幾何步長因子 `1.005`。
    *   合格投影集 `C`：過濾使虛擬代入 `projected_L1(P) >= 2` 之點（`P <= 1.10*SMA200W`、`P / Realized_Price <= 1.00`、`(P - Cycle_ATH)/Cycle_ATH <= Calibrated_DD_Floor`）。若 `C` 為空則回傳 `(None, None, None)`。
    *   網格計分：於 `C` 上計算 `Hits(P) = sum(I(P in band) for band in valid_bands)`，及 `Score(P) = weighted_sum_of_valid_bands + (0.4 if P 落在任一已確認週線 (3,3) 低點 ±3.5% 內 else 0.0)`（對該時點前約數十筆已確認週低點直接線性比對）。
    *   取 `max_hits = max(Hits(P))`。若 `max_hits == 0`，設 `[Zone_Low, Zone_High] = [0.85 * max(C), max(C)]`；否則抽出所有 `Hits == max_hits` 之連續區間段，依序取 `(max_score_in_seg, seg_hi)` 最大者作為 `[Zone_Low, Zone_High]`。無論 `max_hits > 0` 或 `== 0`，於 `C ∩ [Zone_Low, Zone_High]` 內取所有 `Score == max_score` 的最大連續格點段 `run`，依鍵值 `(len(run), run[-1])` 選出最長段（長度平手取最高價段），取幾何中點 `P_anchor = sqrt(run[0] * run[-1])`。回測迴圈僅於 `L1 >= 2/3` 與報告基準日呼叫本函式。
*   **`Dual-Key Fallback` 雙鑰匙確定性規則**：
    *   記 `t_0_ts` 為 `Peak_Date` 當日 `00:00:00 UTC` Unix 秒數；在最新已收盤週線 `w`（須滿足 `w.week_open_ts > t_0_ts`）：
        *   **構造鑰匙**：取截止週 `w` 所有已確認 `(3,3)` 週線因果低點 `weekly_lows`（`confirm_week_idx <= w`）；必須至少存在前低與最新低點（`len >= 2`），且 **`weekly_lows[-2].week_open_ts > t_0_ts`**、`p_last.price > weekly_lows[-2].price`、自 `p_last.idx` 至週 `w` 無任一週收盤 `< p_last.price`，且 `weekly_close[w] > EMA21W[w]`。
        *   **估值鑰匙**：取週線 `w` 結束當日（`w.week_close_ts - 86400` 對應日線基準）之日線閉盤狀態：滿足 `L1 >= 1/3`，**或**當時累積低點回撤 `(Trough_w - Cycle_ATH_w) / Cycle_ATH_w <= 0.75 * Calibrated_DD_Floor_w`。
*   **Track A 實盤／重放狀態判別與回測路徑**：
    *   `effective_spot = live_spot if (replay_mode == "LIVE" and live_spot is not None) else Close[t]`；當 `Zone_High is None` 時（無法合成左側區間），`evaluate_track_a_action` 對 `L1 >= 2/3` 判定 `BUY_NOW_LEFT_ZONE`、對 `L1 < 2/3` 且未逾期判定 `WAIT_LEFT_TARGET`、對已逾期判定 `A6/A7`。
    *   每輪由 `Peak_Date` 標記之獨立 ATH 週期於回測路徑至多觸發 1 次買進，且每次嚴格創新高 `High[t] > Rolling_ATH` 重置 `Deployed_In_Cycle = False`；序列最末根 `t == N - 1` 因尚無 `Open[t+1]` 不寫入回測成交事件。分別模擬三條路徑：
        *   `left_market`（`L1 >= 2/3 and (Zone_High is None or Close[t] <= Zone_High[t])` 次日 `Open[t+1]` 成交）；
        *   `p_anchor_limit`（每逢 `L1 >= 2/3` 且 `P_anchor` 非空日掛限價 `P_anchor[t]`，次日若 `Low[t+1] <= P_anchor[t]` 以 `min(Open[t+1], P_anchor[t])` 成交）；
        *   `dual_key`（於該週期尚未由 `left_market` 買進，且 `Elapsed >= T_max` 且當日對齊 `Dual-Key == True` 之首日，次日 `Open[t+1]` 成交）。
        *   `track_a.backtest_cycle_deployed` 與 `backtest_cycle_entry` 追蹤 `left_market + dual_key` 主合流路徑；統一扣單邊 `0.10%` 手續費，淨報酬百分比公式為 **`((exit_px * (1.0 - 0.001)) / (entry_px * (1.0 + 0.001)) - 1.0) * 100.0`**。
    *   **前瞻報酬與 MAE 統計邊界**：`ret_1y_pct` (`+365d`)、`ret_2y_pct` (`+730d`)、`ret_3y_pct` (`+1095d`) 取對應索引收盤價計算淨報酬（越界填 `None`）。`Next Cycle Peak` 定義為：自進場後先嚴格突破進場時之 `Cycle_ATH`（設為 `breakout_idx`），其後若存在峰值晚於 `breakout_idx` 且已構成重大熊市 downturn（即後續已登記為 `completed_cycles` 之循環，**或**當前未完結回撤已滿足 `max_dd <= -0.50` 且 `t_to_trough_days >= 90`），以該後續峰值 `ath` 依上述淨報酬公式計算；否則標記 `None`（排除未滿 90 天之牛市中繼回撤）。`mae_pct = ((min_low - entry_price) / entry_price) * 100.0`（不計手續費），統計區間為 `[entry_idx .. breakout_idx]`（尚未突破前高則迄 `N - 1`）。

#### 3. Track B (`Capital_B`) 右側波段、資金倉位與因果降級 (`track_b.py` & `backtest_engine.py`)
*   **通用前置門檻**：僅於 `2014-09-17+` 計算，需 `i >= 49`、`EMA50[i-1]`、`EMA50[i]`、`ATR14[i]` 與 `Vol_SMA20[i]` 皆非 `None`，且定義 `stop_dist_to_close = (Close[i] - structural_stop) / Close[i]`，要求 `0.0 < stop_dist_to_close <= 0.25`。同日觸發多型態時取 `SW-3 > SW-2 > SW-1` 為主標籤，並將全數匹配清單寫入 `active_setups_on_bar`。
*   **`SW-1` (Higher Low ＋ 50D EMA)**：取 `confirm_idx <= i` 最新已確認 `(5,5)` 低點 `p_cur` 與前一低點 `p_prev`，滿足 `p_cur.price > p_prev.price`、`i - p_cur.confirm_idx <= 25`、`p_cur.idx` 尚未被此前已成交 `SW-1` 進場消費、區間 `[p_cur.idx+1 .. i]` 全日收盤 `>= p_cur.price`、當日收陽破前高（`Close[i] > Open[i] and Close[i] > High[i-1]`），且滿足站上交叉（`Close[i-1] <= EMA50[i-1] and Close[i] > EMA50[i]`）或上方回踩（`Low[i] <= 1.015 * EMA50[i] and Close[i] > EMA50[i]`）。`structural_stop = p_cur.price - 0.25 * ATR14[i]`，`entry_anchor_low = p_cur.price`。
*   **`SW-2` (雙底／頭肩底頸線突破)**：僅搜尋 `[max(0, i-120) .. i]` 內已確認 `(5,5)` 樞紐，取候選匹配中**最新右腳／右肩索引（同索引先比較長跨度，再比 `Inv H&S > Double Bottom`）**：
    *   *雙底*：低點配對 `(L1, L2)` 滿足 `L2.idx - L1.idx >= 10`、`|L1.price - L2.price| / min(L1.price, L2.price) <= 0.05`、存在 `(L1.idx, L2.idx)` 之間已確認高點（`Neckline = max(H.price)`）；`L1.idx..i` 無收盤 `< min(L1.price, L2.price)`；`L2.idx+1..i-1` 無收盤 `> Neckline`；第 `i` 日首度 `Close[i] > Neckline and Vol[i] > 1.10 * Vol_SMA20[i]`。`structural_stop = L2.price - 0.25 * ATR14[i]`，`entry_anchor_low = L2.price`。
    *   *頭肩底*：三元組 `(LS, Head, RS)` 滿足 `Head.idx - LS.idx >= 8`、`RS.idx - Head.idx >= 8`、`Head.price < min(LS.price, RS.price) * 0.975`、`|LS.price - RS.price| / min(LS.price, RS.price) <= 0.065`；`(LS.idx, Head.idx)` 內最高已確認高點為 `Peak1`，`(Head.idx, RS.idx)` 內最高已確認高點為 `Peak2`（皆須存在），`Neckline = max(Peak1.price, Peak2.price)`；`Head.idx..i` 無收盤 `< Head.price` 且 `RS.idx..i` 無收盤 `< RS.price`；`RS.idx+1..i-1` 無收盤 `> Neckline`；第 `i` 日首度 `Close[i] > Neckline and Vol[i] > 1.10 * Vol_SMA20[i]`。`structural_stop = RS.price - 0.25 * ATR14[i]`，`entry_anchor_low = RS.price`。
*   **`SW-3` (因果威科夫箱體配對代理)**：針對 `[max(0, i-90) .. i]` 內所有已確認 `(5,5)` 配對 `(SC_low, AR_high)` 要求 `SC.idx <= i - 15`、`SC.idx < AR.idx <= i - 5`、`AR.confirm_idx < i - 1`、`(AR.price - SC.price) / SC.price >= 0.12` 且於 `[SC.idx .. AR.confirm_idx]` 無收盤 `< 0.85 * SC.price`：
    *   於 `[AR.confirm_idx .. i-2]` 尋最早 `Spring` 日 `s`（`Low[s] < 0.995 * SC.price and Low[s] >= 0.85 * SC.price`，且於 `s` 或 `s+1` 收盤 `> SC.price`，記收回日 `s_rec`；`AR.confirm_idx..s_rec` 無收盤 `< 0.85 * SC.price`）。
    *   於 `[max(19, s_rec + 1) .. i - 1]` 尋最早 `SOS` 日 `q`（`Vol_SMA20[q]` 非 `None`、`Close[q] > AR.price and Vol[q] > 1.20 * Vol_SMA20[q]`，`s_rec..q` 無收盤 `< 0.85 * SC.price`）。
    *   於第 `i` 日驗證 `LPS`：`1 <= i - q <= 15`、`min(Low[q+1..i]) <= 1.02 * AR.price`、`q+1..i` 全數收盤 `>= box_mid = 0.5 * (SC.price + AR.price)`、且 `Close[i] > Open[i] and Close[i] > Close[i-1]`。
    *   多組箱體匹配時擇**最大 `AR.idx`（平手選最大 `SC.idx`）**；`structural_stop = min(min(Low[q+1..i]), box_mid) - 0.25 * ATR14[i]`，`entry_anchor_low = min(Low[q+1..i])`。
*   **跳空緩衝 `gap_buffer_pct[i]`、部位規模與完整帳戶記帳**：
    *   逐日維護有序串列 `tail_mults`（透過 `bisect.insort` 插入 `u in [15..i]` 之 `max(0.0, (Close[u-1]-Close[u])/Close[u-1]) / max(1e-6, ATR14[u-1]/Close[u-1])`）；`len < 90` 採先驗 `1.5`，否則取 `0.99 * (len - 1)` 線性插值百分位。設 `gap_buffer_pct[i] = max(0.02, mult_i * (ATR14[i] / Close[i]))`。
    *   於計算當期預估（`entry_ref_price = effective_spot`）或回測成交（`entry_price = Open[i+1]`）時：
        *   `stop_dist_pct = max(0.03, 1.5 * ATR14[i] / entry_price, (entry_price - structural_stop) / entry_price)`。
        *   進場防禦拒單：若 `entry_price <= structural_stop`、`stop_dist_pct > 0.25` 或 `stop_dist_pct + gap_buffer_pct[i] >= 0.75`，撤銷委託並使 `evaluate_track_b_action` 回傳 `WAIT_SIGNAL`（若同時屬 `B5` 則維持 `WATCHLIST_BLOCKED`；sizing 欄位皆為 `null`）。
        *   `position_size_pct = min(1.0 / (1.0 + fee_oneway), (risk_budget_eff - 2 * fee_oneway) / (stop_dist_pct + gap_buffer_pct[i]))`（`fee_oneway = 0.001`）。
        *   `circuit_breaker_price = entry_price * (1.0 - (risk_budget_eff - 2 * fee_oneway) / position_size_pct)`（總緩衝 `< 0.75` 保證 `circuit_breaker_price >= 0.25 * entry_price > 0`）。
        *   精確損益與 `1R`：`notional_in = equity_at_entry * position_size_pct`、`buy_fee = notional_in * fee_oneway`、`qty = notional_in / entry_price`、`1R_usd = notional_in * stop_dist_pct`；出場扣 `sell_fee = qty * exit_price * fee_oneway`，`trade_pnl_usd = (qty * exit_price - sell_fee) - (notional_in + buy_fee)`、`trade_r = trade_pnl_usd / 1R_usd`。
*   **逐日精確執行順序（自 `d = i + 1` 起；進場初始化 `active_stop = structural_stop`、`has_reclaimed_ema50 = False`）**：
    1.  若昨日已排程 `pending_exit_next_open == True`：以 `Open[d]` 平倉結束持倉，設 `same_day_exit_cooldown = True`（當日收盤不開新單）。
    2.  盤中熔斷檢查（續抱中）：`Open[d] <= circuit_breaker_price` 以 `Open[d]` 平倉，否則 `Low[d] <= circuit_breaker_price` 以 `circuit_breaker_price` 觸發當日平倉；平倉時亦設 `same_day_exit_cooldown = True`。
    3.  收盤平倉判定（讀取更新前既有 `active_stop`，`bars_held = d - i`）：若 `Close[d] < active_stop` 或（`d >= i + 2` 且 `(has_reclaimed_ema50 or bars_held >= 10)` 且 `Close[d] < EMA50[d] and Close[d-1] < EMA50[d-1]`），標記 `pending_exit_next_open = True`（若已達序列最末根則列為 `EXIT_NEXT_OPEN`、淨值以期末 `Close[d]` 計價並歸類 `open_trades_count = 1`）。
    4.  若仍續抱且無待平倉排程：更新 `has_reclaimed_ema50 |= (Close[d] > EMA50[d])`；若收盤扣除雙邊手續費淨浮盈達 `+1.5R`（`((qty * Close[d] * (1.0 - fee_oneway)) - (notional_in + buy_fee)) / 1R_usd >= 1.5`），更新 `active_stop = max(active_stop, entry_price * (1.0 + 2 * fee_oneway))`；凡確認於當日的新日線低點（`p.confirm_idx == d` 且 `p.idx > i + 1` 且 `p.price > entry_anchor_low`），以確認日凍結 ATR 更新 `active_stop = max(active_stop, p.price - 0.25 * ATR14[d])`。
    *   統計指標：`ev_r = mean(trade_r)`（`0.0` 若空）、`win_rate_pct = 100.0 * count(trade_pnl_usd > 0) / n_closed`（`0.0` 若空）、`payoff_ratio = mean_win_r / abs(mean_loss_r)`（任一側為空或分母為 `0` 時為 `None`）、`single_trade_worst_pct = min(trade_pnl_usd / equity_at_entry * 100.0)`（`0.0` 若空）；`track_max_dd_pct` 依每日收盤市值淨值曲線 `cash + qty * Close[d]` 計算，`mtm_stats` 額外記錄 `{mtm_total_return_pct, mtm_max_dd_pct, open_trades_count}`。
*   **無前瞻影子帳本、分組口徑與連敗節流驗證**：
    *   **基準 `1R` 影子帳本**：在 `2014-09-17+` 獨立運行一組**單部位順序持有**模擬器（固定 `risk_budget = 0.10`、非複利固定單位名目風險、同實盤 `Open[i+1]`／冷卻／熔斷／Step 1–4 出場／`0.10%` 手續費），每筆影子交易固定標記 `wk_warn_50w_at_signal = wk_warn_50w[signal_idx_i]`。第 `i` 日 `evaluate_track_b_action` 與 `backtest_wk_gated_causal` 僅讀取 `exit_idx < i` 之已平倉影子交易統計 `N_active`、`EV_R_active` 與 `EV_R_inactive`（空集合設 `0.0`），依 `Capital_B` 決策表 **8 條互斥規則（`B1–B7b`）** 決定 `risk_budget_eff`。
    *   **分組模擬口徑**：`backtest_by_setup:{SW-1,SW-2,SW-3}` 各別運行獨立單一型態基準回測（`risk_budget = 0.10`），具備完整日淨值與 `track_max_dd_pct`；`backtest_by_wk_warn_50w:{ACTIVE,INACTIVE}` 與 `backtest_by_wk_warn_200w:{ACTIVE,INACTIVE}` 將 `backtest_overall` 已平倉交易依訊號日 `wk_warn_at_signal` 切分，產出 `{n_closed, win_rate_pct, payoff_ratio, ev_r, trade_compounded_return_pct, trade_curve_max_dd_pct, single_trade_worst_pct}`。
    *   **連敗減半組（`backtest_streak_throttled`）**：維護單條不重疊之因果閉合序列（當 `effective_budget > 0` 有實際開單時只讀該筆實際平倉結果；當被 `0.0` 封鎖無實際持倉時讀同時段平倉之影子交易結果，不重複計入同訊號）。若最近連續 3 筆閉合皆 `trade_r < 0.0` 設 `streak_budget = 0.05`，直至出現一筆 `trade_r >= 1.5` 恢復 `0.10`；開倉預算取 `min(wk_budget_causal, streak_budget)`。

#### 4. 減半週期與結構骨架 (`analyze_and_report.py` / `track_a.py`)
*   **無前瞻減半曆法計算**：內建已發生減半日 `['2012-11-28', '2016-07-09', '2020-05-11', '2024-04-20']`，過濾 `known = [d for d in HALVING_DATES if d <= eff_date]`：
    *   `len(known) == 0`：`era_status = "PRE_HALVING_ERA"`、`last_halving_date = None`、`days_since_halving = None`、`next_est_date = "2012-11-28"`、`cycle_progress_pct = None`。
    *   `len(known) == 1`：`era_status = "ERA_1"`、`next_est_date = None`、`cycle_progress_pct = None`（不引用未來間隔）。
    *   `len(known) >= 2`：`era_status = f"ERA_{len(known)}"`，依 `known` 內部相鄰實測間隔均值 `est_len = round(mean(intervals))`（`len == 4` 時 `[1319, 1402, 1440]` 之歷史平均 **`1,387` 日**）推算 `next_est_date = known[-1] + est_len`（第 5 次為 `2028-02-06`）與 `cycle_progress_pct = round(days_since_halving / est_len * 100.0, 2)`。
    *   `historical_cycle_offsets`：僅納截斷前已完成重大熊市週期，每筆取該週期峰值前最近減半日計算 `{peak_date, trough_date, halving_date, days_from_halving_to_peak, days_from_halving_to_trough}`（早於首輪減半則減半欄位為 `null`）。

### Target Files to Create/Modify

1.  **`scripts/btc_bottom/__init__.py`** (`Create`, Phase 1): 空套件標記檔，支援 `python3 -m scripts.btc_bottom.<module>`。
2.  **`scripts/btc_bottom/fetch_snapshots.py`** (`Create`, Phase 1): 唯一具備 HTTP 請求權限之 CLI 工具，負責資料抓取、水位線截斷、ISO 週一重採樣、`build_macro_daily_series`、跨源比對、`manifest.json` 與可選 `live_spot.json` 寫入及雜湊驗真。CLI 參數：`--out-dir`（預設 `data/snapshots/<YYYYMMDD_HHMMSSZ>`；新抓取僅允許空／不存在／僅含 `.gitkeep` 之目錄，若既有目錄已含 `manifest.json` 或其他資料檔即拒絕覆寫）、`--as-of`（預設執行當下 `now_utc`；接受 ISO-8601 UTC 或 `YYYY-MM-DD`，後者解析為 `min(now_utc, YYYY-MM-DDT23:59:59Z)`，未來超過 `now_utc + 60s` 立即中止）、`--verify-only`（必附 `--out-dir` 驗證 `manifest_sha256`、檔案列數／雜湊、等距時間戳與有限數）、`--refresh-live-spot`（搭配 `--out-dir` 於既有有效快照上單獨更新未入雜湊之 `live_spot.json`，前提 `ohlcv_effective_as_of_ts == floor_utc_day(now_utc)`）。
3.  **`scripts/btc_bottom/indicators.py`** (`Create`, Phase 2): 實作 `calc_sma`, `calc_ema_sma_seeded`, `calc_wilder_atr`, `calc_wilder_rsi`, `calc_macd`, `calc_bollinger`, `compute_causal_pivots`, `compute_causal_sr_zones`, `compute_expanding_lgc`, `compute_wk_warn_series`。
4.  **`scripts/btc_bottom/track_a.py`** (`Create`, Phase 2): 實作 `track_causal_cycle_peaks`, `evaluate_l1`, `synthesize_track_a_zone`, `evaluate_dual_key`, `evaluate_track_a_action`, `compute_halving_diagnostics`（並自 `fetch_snapshots.py` 匯入 `build_macro_daily_series`）。
5.  **`scripts/btc_bottom/track_b.py`** (`Create`, Phase 2): 實作 `detect_causal_sw_setups`, `compute_expanding_gap_buffers`, `compute_position_sizing`, `evaluate_track_b_action`。
6.  **`scripts/btc_bottom/backtest_engine.py`** (`Create`, Phase 3): 實作 `run_track_a_backtest` 與 `run_track_b_backtest`（含基準 `10%` 統計、`by_setup`、`by_wk_warn_50w/200w`、無前瞻 `wk_gated_causal` 與 `streak_throttled` 對照組）。
7.  **`scripts/btc_bottom/analyze_and_report.py`** (`Create`, Phase 3): 純離線分析與報告生成器（嚴禁匯入或發起任何 HTTP 請求）。參數：`--snapshot-dir`、`--as-of`（省略預設為 `manifest["requested_as_of_utc"]`，若傳入晚於快照基準立即中止；早於快照終日強制進入 `HISTORICAL_REPLAY` 且屏蔽 `live_spot`）、`--report-out`（預設 `reports/btc_bottom_analysis_<effective_ohlcv_date>.md`）、`--json-out`（預設 `<snapshot-dir>/analysis_summary.json`）、`--narrative-overlay <path.json>`。執行前必檢 `manifest_sha256` 與全域有限數守衛（區分合法可空欄位與非空必填數值欄）。
8.  **`tests/test_snapshots.py`、`tests/test_indicators_and_tracks.py`、`tests/test_backtest_and_report.py`** (`Create`, Phases 1–3): 嚴格無真實網路依賴（HTTP 測試皆以 `unittest.mock` 替換回應並使用 `tempfile.TemporaryDirectory` 建立測試沙盒）。
9.  **`data/snapshots/.gitkeep` & `reports/.gitkeep`** (`Create`, Phase 1/3): 追蹤輸出資料夾；執行期生成產物對應路徑規範 `data/snapshots/**` 與 `reports/btc_bottom_analysis_*.md`。

### Exact Endpoints, Canonical Hashing & Data Schemas

*   **網路請求上限與端點定義**：所有 HTTP 請求夾帶 `User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36`，逾時 `20s`、單一回應大小上限 `50 MB`（流式分塊讀取驗證）、指數退避重試最多 `3` 次（間隔 `1s, 2s, 4s`），`CoinMetrics` 分頁迴圈上限 `20` 頁。

| 資料源 | 必要級別 | 請求端點與參數 | JSON 欄位與時戳解析 | 寫入／截斷規則 |
| :--- | :--- | :--- | :--- | :--- |
| Yahoo Finance Daily | 主來源 (`fail-closed`) | `https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?period1=1410912000&period2=<int_req_as_of_ts>&interval=1d` | `chart.result[0].timestamp` (`open_ts`) 及 `indicators.quote[0].{open,high,low,close,volume}` | **先篩選 `open_ts + 86400 <= req_as_of_ts` 剔除盤中未收盤棒**，再驗嚴格 `86400s`、有限正價、`vol >= 0` -> `daily_ohlcv.csv` |
| CoinMetrics Community | 主來源 (`fail-closed`) | `https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc&metrics=CapMVRVCur,PriceUSD&frequency=1d&page_size=10000` (跟隨 `next_page_url`) | `data[]` 中 `time` (`YYYY-MM-DDT00:00:00.000000000Z` -> `open_ts`)、`CapMVRVCur`、`PriceUSD` (字串轉 float) | `effective_ts = open_ts + 86400 <= req_as_of_ts`；驗嚴格 `86400s` 與全正數有限浮點 -> `mvrv.csv` |
| Bitstamp Daily | 校驗 (`DEGRADED` on timeout) | `https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=1000` | `data.ohlc[]` (`int(timestamp)`=`open_ts`, `close`) | 重疊 `< 30` 筆（歷史視窗外）跳過且不標降級；否則算 `abs(bs - yh) / yh * 100`，中位 `> 0.50%` 或 P99（`0.99*(n-1)` 插值）`> 1.50%` -> `fail-closed` |
| Alternative.me F&G | 輔助診斷 (`UNAVAILABLE` on fail) | `https://api.alternative.me/fng/?limit=0` | `data[]` (`value`, `value_classification`, `int(timestamp)`) | 過濾整數秒 `ts < ohlcv_effective_as_of_ts` 並依 `date_utc` 升序排列；遇官方缺日容許間隔 -> `fng.csv` |
| Kraken Ticker | 僅 `LIVE` 盤中 (`UNAVAILABLE` on fail) | `https://api.kraken.com/0/public/Ticker?pair=XBTUSD` | `result.XXBTZUSD.c[0]` (最新成交價) | 僅當 `req_as_of_date == now_utc.date()`（若傳 ISO 時戳須與 `now_utc` 差 `<= 3600s`）**且** `ohlcv_effective_as_of_ts == floor_utc_day(now_utc)` 時擷取；其餘寫 `mode="HISTORICAL_REPLAY"`, `ticker_last=null` -> `live_spot.json` |

*   **水位線、12 年跨源驗證與路徑規範化**：
    *   `ohlcv_effective_as_of_ts = min(floor_utc_day(req_as_of_ts), last_yahoo_close_ts)`；`mvrv_effective_as_of_ts = min(floor_utc_day(req_as_of_ts), last_cm_effective_ts)`。若 `abs(ohlcv_effective_as_of_ts - mvrv_effective_as_of_ts) > 86400` 立即 `fail-closed` 中止；並以各自水位線對 `daily_ohlcv.csv`、`mvrv.csv`、`weekly_ohlcv.csv` 做精確截斷。
    *   全域 `CoinMetrics PriceUSD vs Yahoo Close` 重疊檢核（`2014-09-17 .. min_close`）：相對誤差 `abs(cm - yh) / yh * 100` 須滿足中位差 `<= 0.50%`、P99（`0.99*(n-1)` 插值）`<= 3.00%`。若主來源與未跳過之 `Bitstamp` 皆通過則 `cross_check_status = "VERIFIED"`；僅當有效視窗內 `Bitstamp` 連線逾時／失敗設為 `"DEGRADED"`。
    *   路徑規範化與快照防覆寫保護：CLI 傳入之 `--out-dir`/`--report-out`/`--json-out` 經 `Path.resolve()` 規範化；新快照目標目錄如已含 `manifest.json` 或非 `.gitkeep` 檔案一律拒絕覆寫以確保快照不可變性。
*   **CSV／JSON 資料綱要與正規化雜湊（`manifest_sha256`）**：
    *   `daily_ohlcv.csv`：`open_ts(int),close_ts(int),date_utc(YYYY-MM-DD),open,high,low,close,volume`（自 `2014-09-17` 起；若 `--as-of < 2014-09-17` 僅寫表頭）。
    *   `mvrv.csv`：`date_utc(YYYY-MM-DD),open_ts(int),effective_ts(int),cap_mvrv_cur(float),price_usd(float),realized_price(float)`。
    *   `weekly_ohlcv.csv`：`week_open_ts(int),week_close_ts(int),open_date_utc(YYYY-MM-DD),open,high,low,close,volume,is_ohlcv(true|false)`。
    *   `fng.csv`：`date_utc(YYYY-MM-DD),fng_value(int),fng_class(str)`（升序排列；失敗或早於 `2018-02-01` 時僅寫表頭）。
    *   `live_spot.json`（不納入 `manifest.json` 雜湊）：`{"mode":"LIVE"|"HISTORICAL_REPLAY","live_spot_status":"OK"|"UNAVAILABLE"|"HISTORICAL_REPLAY","fetched_at_utc":str|null,"ticker_last":float|null,"source":"kraken_ticker"|null}`。
    *   `manifest.json`：含 `schema_version("1.0")`, `requested_as_of_utc`, `ohlcv_effective_as_of_utc`, `ohlcv_effective_as_of_ts`, `mvrv_effective_as_of_utc`, `mvrv_effective_as_of_ts`, `cross_check_status("VERIFIED"|"DEGRADED")`, `fng_status("OK"|"UNAVAILABLE")`, `overlap_diffs:{cm_vs_yh_median_pct,cm_vs_yh_p99_pct,bs_median_pct|null,bs_p99_pct|null}`, `files:{"daily_ohlcv.csv":{"rows":int,"sha256":str}, "mvrv.csv":{...}, "weekly_ohlcv.csv":{...}, "fng.csv":{...}}`（`rows` 為不含表頭之資料列數）及 `manifest_sha256`。正規化算法：排除 `"manifest_sha256"` 鍵後以 `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")` 計算 SHA-256 十六進位字串並寫入 `"manifest_sha256"`；`--verify-only` 與離線分析必雙重驗證檔案列數、間隔連續性、有限正價與 `manifest_sha256`。
    *   `narrative_overlay.json` (可選)：`{"snapshot_ohlcv_effective_utc":str, "manifest_sha256":str, "wyckoff_phase":str, "wyckoff_events":[{"event":str,"date_utc":str,"price":float,"note":str}], "elliott_primary":{"degree":str,"current_wave":str,"summary":str,"invalidation_price":float}, "elliott_alternate":{"degree":str,"current_wave":str,"summary":str,"invalidation_price":float}, "invalidation_notes":str}`。兩項綁定鍵須同時匹配 `manifest_sha256` 與 **截斷後實際 `effective_ohlcv_utc`**，不合即 `fail-closed` 中止。
    *   `analysis_summary.json`（以下除註明可為 `null` 欄位外，全數浮點必為有限數）：
        *   `meta`: `{requested_as_of_utc, effective_ohlcv_utc, effective_mvrv_utc, mvrv_lag_days, replay_mode, live_spot_price|null, live_spot_status, live_spot_age_sec|null, narrative_status("STRUCTURAL_ONLY"|"VERIFIED_OVERLAY"), manifest_sha256}`。
        *   `dimensions_snapshot`:
            *   `valuation`: `{close, sma200w|null, mayer_multiple|null, cycle_ath, ath_dd_pct, lgc_lower_band|null}`。
            *   `halving`: `{era_status, last_halving_date|null, days_since_halving|null, next_est_date|null, cycle_progress_pct|null, historical_cycle_offsets:[{peak_date,trough_date,halving_date|null,days_from_halving_to_peak|null,days_from_halving_to_trough|null}]}`。
            *   `sentiment`: 若無列、`UNAVAILABLE` 或距 `effective_ohlcv_date` 逾 `7` 日均輸出 `{date_utc:null, fng_value:null, fng_class:"UNAVAILABLE", fng_stale_days:null}`，否則填補並記 `fng_stale_days` (`0..7`)。
            *   `momentum_diag`（早於 `2014-09-17` 無 `daily_ohlcv` 時全為 `null`）: `{ema50|null, rsi14|null, macd_dif|null, macd_dea|null, macd_hist|null, bb_upper|null, bb_mid|null, bb_lower|null, atr14|null, vol_ratio_20|null}`。
            *   `structural_skeleton`（無 `daily_ohlcv` 時 `sr_*` 與 `recent_daily_pivots` 為空陣列）: `{sr_supports:[{code,price,band_low,band_high,touches,score,dist_pct}], sr_resistances:[...], macro_log_fib:{low, ath, levels:[{ratio,label,price,dist_pct}]}, recent_daily_pivots:[{date_utc,price,kind}], recent_weekly_pivots:[{date_utc,price,kind}], macro_invalidation_levels:{cycle_ath, zone_low|null, p_anchor|null, dual_key_weekly_hl|null, track_b_active_stop|null, track_b_circuit_breaker|null}}`（斐波固定 `[0.236, 0.382, 0.500, 0.618, 0.786]` 對數比例，樞紐至多各留最近 `10` 筆）。
        *   `track_a`: `{action("BUY_NOW_LEFT_ZONE"|"BUY_LIMIT_PULLBACK"|"INTRADAY_ALERT_PENDING_CLOSE"|"WAIT_LEFT_TARGET"|"BUY_NOW_DUAL_KEY_FALLBACK"|"WAIT_WEEKLY_RIGHT_SIDE"), backtest_cycle_deployed(bool), backtest_cycle_entry:{path,date,price}|null, l1_score(int), l1_flags:{sma200w_ok,mvrv_ok,dd_ok}, realized_price, zone_low|null, zone_high|null, p_anchor|null, t0_date|null, t_max_days, elapsed_days|null, calibrated_dd_floor_pct, dual_key_active(bool), backtest_stats:{left_market_events, p_anchor_limit_events, dual_key_events, summary_all, summary_oos}}`（事件含 `{entry_date, entry_price, early_burn_in, mae_pct, ret_1y_pct|null, ret_2y_pct|null, ret_3y_pct|null, ret_next_cycle_peak_pct|null}`；`summary_all`/`summary_oos` 記 `{primary_n_events, primary_mean_mae_pct|null, primary_worst_mae_pct|null, primary_mean_ret_1y_pct|null, primary_mean_ret_2y_pct|null, primary_mean_ret_3y_pct|null, primary_mean_ret_next_peak_pct|null, limit_n_events, limit_mean_mae_pct|null}`）。
        *   `track_b`: `{action("EXIT_NEXT_OPEN"|"HOLD_POSITION"|"WAIT_SIGNAL"|"BUY_NEXT_OPEN"|"BUY_REDUCED_RISK"|"WATCHLIST_BLOCKED"), wk_warn_50w_active, wk_warn_200w_active, active_setups_on_bar, rejected_reason|null, risk_budget_eff|null, position_size_pct|null, entry_ref_price|null, structural_stop|null, active_stop|null, circuit_breaker_price|null, gap_buffer_pct|null, backtest_overall, backtest_by_setup:{SW-1,SW-2,SW-3}, backtest_by_wk_warn_50w:{ACTIVE,INACTIVE}, backtest_by_wk_warn_200w:{ACTIVE,INACTIVE}, backtest_wk_gated_causal, backtest_streak_throttled}`（其中 `BUY_*` 填入預估 `entry_ref_price = effective_spot` 與進場 sizing 欄；`HOLD_POSITION`/`EXIT_NEXT_OPEN` 填入現行 `active_stop`, `structural_stop`, `circuit_breaker_price`, `position_size_pct` 且 `entry_ref_price` 填成交進場價；`WAIT_SIGNAL`/`WATCHLIST_BLOCKED` 設上述價位／倉位欄位為 `null`。連續淨值回測組含 `closed_stats:{n_closed, win_rate_pct, payoff_ratio|null, ev_r, total_return_pct, track_max_dd_pct, single_trade_worst_pct}` 與 `mtm_stats:{mtm_total_return_pct, mtm_max_dd_pct, open_trades_count}`；`by_wk_warn_*` 分組含 `closed_stats:{n_closed, win_rate_pct, payoff_ratio|null, ev_r, trade_compounded_return_pct, trade_curve_max_dd_pct, single_trade_worst_pct}`）。

### Markdown 報告輸出章節規格 (`analyze_and_report.py`)
產出報告必須依序包含：1. 執行摘要與當期雙資金池行動建議表（並列 `live_spot` vs `Close[t]` 價差與 `track_a` 空白資金／同週期已持倉狀態）；2. 維度 1 & 4（長期估值與鏈上 MVRV / Realized Price 對照，須明確標註 `LGC_Lower_Band` 為基於自相關對數殘差之擴展經驗邊界）；3. 維度 2 & 3（減半時序位置與歷輪熊市類比／Track A 歷史 MAE 與遠期／高點前實證表）；4. 維度 5 & 6（情緒讀數與日／週技術指標診斷表）；5. 維度 7（因果相對 S/R 區間、對數斐波位、樞紐階梯與可選 `--narrative-overlay` 主／替代波浪及明確失效點位）；6. Track B 實證勝率、期望值與 `WK-Warn (50W/200W)` 分組及影子因果減碼檢定表（並列表達「僅已完結交易統計」與「含期末市值 MTM 報酬與最大回撤」）。

### Verification Plan

*   **觸動範圍檢查（Phase 1–3 全程）**：執行 `git status --porcelain index.html src/`，輸出必須完全為空。
*   **分階段無網路單元測試**：
    1.  `python3 -m unittest tests/test_snapshots.py -v`（Mock HTTP 驗證 Yahoo `/v8/finance/chart` 先剔除盤中未收盤棒再驗間隔、CoinMetrics `data[]` 分頁、`Bitstamp` 重疊超限 `fail-closed`、`> 86400s` 水位差中止、首尾未滿 7 日殘週丟棄、`build_macro_daily_series` 2010–2014 補平與交界週 `is_ohlcv=false`、`manifest_sha256` 驗真與防覆寫、F&G 缺日容錯、`LIVE` 當日 UTC 對齊 vs `HISTORICAL_REPLAY` 分離）。
    2.  `python3 -m unittest tests/test_indicators_and_tracks.py -v`（前綴長度因果不變性、`RSI(14)` idx 14 / `ATR(14)` idx 13 / `MACD(33)`、`Cycle_ATH` 嚴格創新高與平高不重置 + `t_to_trough >= 90d` 過濾 `2021-04`、`P_min=0.01`、因果 S/R 近端週視窗與相對百分比聚類不被 2010 極低價吞併、`SW-1` 單次消耗與不破底、`SW-2` 頸線首破、`SW-3` 配對 `(SC, AR)` 箱體不被覆蓋、Track A `A1–A7` 與 Track B `B1–B7b` 互斥窮盡矩陣及防禦拒單路由 `WAIT_SIGNAL`）。
    3.  `python3 -m unittest tests/test_backtest_and_report.py -v`（`Track A` 正符號淨報酬公式、左側／限價／`Dual-Key`／`Next Cycle Peak` 不因 `< 90d` 急跌提早截斷、`Track B` `Open[d]` 跳空、`structural_stop` 初始 `active_stop`、`+1.5R` 收盤淨浮盈保本、`gap_buffer >= 0.75` 拒單、`circuit_breaker_price >= 0.25 * entry`、Step 1 & 2 同日冷卻禁止開新單、`WK-Warn` 2 週熄燈、單部位順序影子 `1R` 帳本解鎖、統一不重複連敗 `min()`、`analyze_and_report.py` 無網路匯入、`narrative_overlay.json` 截斷後日期與雜湊雙重防錯與有限數守衛）。
    *   總整執行 `python3 -m unittest discover -s tests -p "test_*.py" -v` 須 100% 通過。
*   **端到端實機快照與離線重放檢核**：
    1.  `python3 -m scripts.btc_bottom.fetch_snapshots --out-dir data/snapshots/latest`
    2.  `python3 -m scripts.btc_bottom.fetch_snapshots --verify-only --out-dir data/snapshots/latest`
    3.  `python3 -m scripts.btc_bottom.analyze_and_report --snapshot-dir data/snapshots/latest`（驗證 `analysis_summary.json` 及 `reports/btc_bottom_analysis_<effective_ohlcv_date>.md` 生成且無 `NaN`）。

--------------------------------------------------------------------------------

*Drafted with EGM*
