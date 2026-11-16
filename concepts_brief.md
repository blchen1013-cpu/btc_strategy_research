# 比特幣雙軌抄底分析與歷史回測規格書 (BTC Bottom-Fishing Analysis Concepts Brief)

* **Author:** blchen1013-cpu (via EGM Elephant-Goldfish Brainstorm)
* **Date:** 2026-11-14
* **Status:** Step 3 Resolved — Ready for Step 4 Alignment Gate

## Overview

本規格書定義一套僅使用**真實歷史市場與鏈上資料**的比特幣（BTC/USD）雙軌抄底分析與因果回測驗證系統，現階段僅產出分析與驗證報告（**不修改** [`index.html`](file:///home/antigravity/bitcoin-analyzer/index.html) 與 [`src/*`](file:///home/antigravity/bitcoin-analyzer/src/app.js) 網頁介面）。

系統將為兩個**完全獨立、互不共攤資金與風險額度**的部位，各自輸出明確的條件式行動結論（**「現在買」**、**「等更低價位（附單一核心重押價與區間）」**或**「等右側結構確認（附觸發條件與部位比例）」**）：
1. **Track A — 長線部位（1–3 年持有、左側抄底、不設停損）**：以估值硬門檻 `L1` 判定是否進入價值區，若未觸發則給出單一核心重押價與價值區間；同時揭露歷史進場後最大潛在浮虧（MAE），並以歷史週期回測驗證「超過期限未跌到目標價時，切換週線右側確認進場」的條件與表現。
2. **Track B — 波段部位（數個月持有、日線右側確認、僅依結構失效與趨勢破壞出場）**：以日線右側結構 `S1` 確認進場、採用尾端緩衝部位公式將單筆實際最大回撤嚴格控制在 `10%` 內，並以 2014–2026 真實多週期日線 OHLCV 回測計算 `S1` 在「有無 `S3` 週線破位警示」下的實際勝率與期望值（EV）。

---

## Analysis & Resolved Architecture Decisions

針對 7 位無記憶 Goldfish 審查代理（`egm-design`、`egm-zoom-out`、`egm-codebase-details`、`egm-comprehension`、`egm-implementation-readiness`、`egm-structural`、`egm-inventive-catalyst`）提出的所有阻礙與矛盾，經決策者授權拍板定案如下：

### 1. 資料來源、UTC 對齊與現有程式碼禁用邊界
* **禁用現有 [`src/*`](file:///home/antigravity/bitcoin-analyzer/src/app.js) 模組**：[`src/data.js`](file:///home/antigravity/bitcoin-analyzer/src/data.js) 依賴 HTTP 451 受阻的 Binance API 且失敗會靜默退回模擬資料；[`src/classical.js`](file:///home/antigravity/bitcoin-analyzer/src/classical.js) 使用 `rightBars = 5` 未來窺視樞紐點（lookahead bias）與合成通道；[`src/indicators.js`](file:///home/antigravity/bitcoin-analyzer/src/indicators.js) 於第 0 根初始化 EMA；[`src/backtest.js`](file:///home/antigravity/bitcoin-analyzer/src/backtest.js) 使用固定百分比停損、`252` 美股年化與硬編碼預設指標。本分析與回測腳本必須完全獨立開發，並強制 **`fail-closed`**：任何 API 失敗立即報錯中止，並將原始資料存為不可變本地快照，記錄 `fetched_at_utc`、時間區間與 SHA-256 雜湊清單。
* **已驗證多週期真實資料組合**：
  * **日線多週期 OHLCV（Track B 回測與日線技術面）**：Yahoo Finance `BTC-USD`（`2014-09-17` 至 `2026-11-14`，4,442 根日線）與 Bitstamp 分頁 OHLCV 交叉校對重疊區間與最新 Kraken 日線（721 根）。
  * **週線 OHLCV（Track A 估值與 S3 判斷）**：Kraken Weekly（`2013-10-03` 至今 685 週，注意為週四 `00:00 UTC` 起算），並於週線計算 `200W SMA` 前段暖機時以 Blockchain.info `sampled=false`（`2009-01-03` 至 `2026-11-13`，每日嚴格 86,400 秒間隔，過濾早期 `$0` 紀錄）做點位對齊拼接，同時以歷史日線重採樣驗證週四 vs. 週一週線邊界敏感度。
  * **鏈上與情緒**：CoinMetrics `CapMVRVCur`（`2010-07-18` 至 `2026-11-13`，5,963 筆）；Alternative.me 恐懼貪婪指數（`2018-02-01` 起，不回填 2018 年前缺失值）。
* **嚴格已收盤 K 線與無未來函數規範**：
  * 固定 `--as-of UTC` 截斷點，剔除任何尚未收盤的當日／當週 K 線（例如 `2026-11-12` 起始的未收盤週線不參與 `200W SMA`、`21W EMA`、`S3` 收盤確認，僅將即時現價用於與目標價比對）。
  * 跨週期對齊必須嚴格滿足 `last_closed_week.close_ts <= daily_bar.close_ts`；所有樞紐點（Swing Pivot）於 `t + rightBars` 之後才可視為已確認。

### 2. 資金隔離與「10% 最大回撤」精確數學定義（解決 Blocker 1）
* **雙資金池完全隔離（Disjoint Sleeves）**：`Capital_A`（Track A 長線資金池）與 `Capital_B`（Track B 波段資金池）相互獨立、各自以 `100%` 該資金池為單位計算曝險與損益，絕不混用資金池造成槓桿或額度互相排擠。
* **Track B 尾端緩衝部位規模與雙層保護**：
  * 有效停損距離須先通過最低波動門檻防洗盤：`stop_dist% = max(3.0%, 1.5 * ATR14 / entry_price, (entry_price - structural_stop) / entry_price)`；結構失效價若與進場價夾角過近則取 `max` 下限或暫不進場。
  * 考慮雙邊手續費（0.2%）與日線歷史 99% 跳空穿透緩衝（`gap_buffer%`，依 2014–2026 日線實際穿價分佈實測校準）：
    `position_size_pct = min(100%, (10% - 0.2%) / (stop_dist% + gap_buffer%))`
  * **平時出場僅看日線收盤結構失效**（避免盤中插針震盪洗出），但外加**盤中硬性 10% 權益熔斷防護**：若盤中價格觸及 `entry_price * (1 - 9.8% / position_size_pct)` 直接市價熔斷平倉，確保極端黑天鵝單日閃崩也不會突破 Track B 單筆 10% 最大帳面回撤紅線。回測時同時報告「單筆最大虧損」與「連續停損造成的 Track-level 累計最大回撤」。

### 3. Track A 狀態機與執行規則（解決 Blocker 2）
* **「單一重押價 vs. 區間」解耦**：
  * **重押價值區間 `[Zone_Low, Zone_High]`**：由客觀結構（對數通道下緣、宏觀週線 S/R 聚類、波段斐波那契 0.618–0.786、歷次熊市減幅區間）交集形成之左側價值帶。
  * **單一核心重押價 `P_anchor`**：落在 `[Zone_Low, Zone_High]` 內、共識密度最高的單一主掛單錨點價格（供執行「單筆重押」；報告亦附註若欲防踏空之建議參考配置，但不改變單一重押主結論）。
* **L1 與修飾指標的優先權（嚴格遵守不否決原則）**：
  * `L1` 硬門檻（週線或已收盤日線滿足 ≥ 2/3）：(1) `Price <= 200W SMA * 1.10`；(2) `CoinMetrics CapMVRVCur <= 1.0`；(3) 距本輪週期 ATH 跌幅達到擴展視窗校準後的週期底部下緣（明確公式於 `design_draft.md` 鎖定，嚴禁事後窺視）。
  * 若 `L1 < 2/3`：結論一律為 **「等（WAIT）」**，且計算出的 `[Zone_Low, Zone_High]` 與 `P_anchor` 必須保證在該價位時 `L1` 可望達標（依當時 Realized Cap 與 200W SMA 動態漂移帶驗算）。
  * 若 `L1 >= 2/3`：技術修飾指標**不得否決**買進資格。若現價已經 `<= Zone_High`，主結論立即給出 **「現在買（進入左側價值區）」**；若現價仍明顯高於 `Zone_High`（例如差距 > 5%），則給出「`L1` 估值達標，現價偏離技術支撐區上緣，掛限價 `Zone_High / P_anchor`」。
* **時間期限回退（Time-Expiry Fallback）與雙鑰匙橋接**：
  * **計時起點 `t_0`**：本輪週期實證確認之歷史最高點（Cycle ATH）日期。
  * **期限 `T_max`**：由專案歷史回測統計 2013–2015、2017–2018、2021–2022 三次週期高點至週期最低點的時間分佈（並對照減半時間點）校準得出。
  * **期限後觸發規則（估值部分橋接）**：若過了 `T_max` 仍未跌至左邊目標價，原左側限價單失效並切換至**週線右側確認進場**（週線出現因果 Higher Low 且週線收盤站上 `21W EMA`）；為防止牛市無止盡追高或被假突破騙線，此回退需滿足**雙鑰匙條件**：觸發時至少滿足 `L1` 三項條件中的 **1 項**（或本輪已出現過 ≥ 75% 校準熊市跌幅的回調），否則維持觀望。回測腳本將實證對比 2013–2026 歷史上所有觸發此週線右側訊號的表現（含牛市假突破與熊底反轉）。

### 4. Track B 完整生命週期、出場規則與 `S3` 負期望值處置（解決 Blocker 3）
* **因果 `S1` 進場確認（日線收盤三擇一，並加 Signal Freshness 過濾避免追高）**：
  1. **結構均線轉強**：因果確認的日線 Higher Low（`right_bars = 5`）＋ 日線收盤重新站上 SMA 暖機初始化之 `50D EMA`（且現價偏離止損不超過可接受延伸倍數）。
  2. **經典底部頸線突破**：因果偵測之雙底（Double Bottom）或頭肩底（Inv H&S）日線收盤有效突破頸線。
  3. **威科夫因果代理（Spring → SOS → LPS）**：跌破區間支撐（SC–AR 箱體下緣）後快速放量收回箱內（Spring），隨後放量強勢突破（SOS）並於縮量回測箱體上緣/中軸守住收陽（LPS）。
* **完整波段出場與移動停利規則（讓 Win Rate 與 EV 可精算）**：
  1. **保本上移**：浮盈觸及 `+1.5R` 時，停損自動上移至進場成本價（含雙邊手續費）。
  2. **結構移動停利**：每當日線形成新的因果 Higher Low（高於前一防守低點），結構失效價跟隨上移至該低點下方緩衝區。
  3. **趨勢破壞出場**：日線收盤**連續 2 日跌破 `50D EMA`**，或日線收盤跌破最新結構移動停利位時平倉。同時計算目標阻力/R 倍數對照敏感度。
* **`S3` 定義與負期望值防護機制**：
  * **`S3` 明確定義**：過去 `W = 12` 週內曾發生週線收盤跌破關鍵長期均線（回測分別驗證 `50W SMA` 與 `200W SMA` 兩版），且尚未出現連續 2 週收盤站回該均線上方以重置警示。
  * **負 EV 自動護欄**：回測腳本將分別計算 `S1` 在 **`S3` 亮燈期間** vs. **`S3` 熄燈期間** 的總交易數 `N`、勝率、盈虧比、平均 R 倍數與組合 EV。若實證顯示 `EV(S1 | S3 active) <= 0`，實戰報告中當 `S3` 亮燈時，Track B 將自動由「正常開單」降級為 **「僅列觀察 / 風險預算減半（≤ 5%）或暫停開倉」**。

### 5. 7 大分析維度的嚴格角色分工（解決 Blocker 4 / YAGNI）
| 維度 | 使用資料來源 | 在系統中的角色分工 | 能否參與因果歷史回測 |
| :--- | :--- | :--- | :--- |
| **1. 長期估值（200W SMA / Mayer / ATH 跌幅 / 對數成長通道 LGC）** | Kraken Weekly + Blockchain.info (擴展視窗) | **Track A 硬門檻 (`L1`) ＋ 價值區間下界校準** | 可（嚴格擴展視窗，無未來函數） |
| **2. 減半週期位置** | 區塊鏈減半時間軸 + BTC 週線 | **Track A 期限 `T_max` 參考錨點 ＋ 週期階段定位** | 可（描述性統計對照歷次週期） |
| **3. 歷史熊市類比** | Blockchain.info + Kraken Weekly + Yahoo Daily | **Track A 進場後潛在最大浮虧 (MAE) 與見底時長評估** | 可（歷史樣本統計） |
| **4. 鏈上估值 (MVRV)** | CoinMetrics `CapMVRVCur` | **Track A 硬門檻 (`L1`) ＋ Realized Cap 動態價位換算** | 可（T-1 日終對齊） |
| **5. 市場情緒 (Fear & Greed)** | Alternative.me `fng` (`2018-02`+) | **Track A 恐慌極值輔助參考（非否決）** | 僅 2018+ 週期輔助驗證 |
| **6. 中短期量化指標 (RSI / MACD / BB / EMA)** | Yahoo/Bitstamp Daily + Kraken Weekly | **Track B 進場 (`50D EMA`)、出場與 `S3` 警示核心** | 可（100% 因果回測） |
| **7. 傳統技術與結構學派（S/R / 斐波 / 頸線 / 威科夫 / 艾略特波浪）** | Yahoo/Bitstamp Daily + Kraken Weekly | **因果 S/R、斐波、頸線、Wyckoff 代理進回測；波浪理論（主選+替代+失效位）與完整 Wyckoff 圖譜專責當前盤面結構與區間微調** | 幾何/代理規則進回測；主觀波浪僅作當下報告定性結構分析 |

---

## Goals vs. Non-Goals

* **In-Scope Goals:**
  * 建立完全獨立、不可變快照驅動、`fail-closed` 的真實資料擷取與 BTC/USD 雙軌因果分析回測腳本。
  * 在真實 2014–2026 日線與 2013–2026 週線歷史上，產出 Track A 期限回退與歷史浮虧 (MAE) 實證、以及 Track B `S1` 在有無 `S3` 警示下的勝率與期望值 (EV) 統計表。
  * 產出包含完整 7 大維度、明確雙軌結論與實戰價位指令的分析報告。
* **Out-of-Scope Non-Goals:**
  * 本階段**不修改**任何前端網頁介面（[`index.html`](file:///home/antigravity/bitcoin-analyzer/index.html) 及 [`src/*`](file:///home/antigravity/bitcoin-analyzer/src/app.js)）。
  * **嚴禁**使用模擬資料或靜默失敗退回機制。
  * **不進行**無統計根據的艾略特波浪自動化歷史調參。

---

## Alternatives Weighed

* **沿用現有 [`src/*`](file:///home/antigravity/bitcoin-analyzer/src/app.js) 模組進行回測：**
  * *Rejected by audit & author*: 現有程式含 Binance HTTP 451 綁定、模擬資料靜默退回、未來窺視樞紐點與固定停損假設，完全不符真實決策要求。
* **允許 Track A 與 Track B 共用資金且無盤中熔斷線：**
  * *Rejected by author*: 共用資金與無熔斷線會導致「無停損長線」與「最大 10% 回撤波段」邏輯直接衝突；拆分為獨立資金池 `Capital_A` / `Capital_B` 並於 Track B 加上尾端緩衝公式與盤中 10% 硬熔斷線後，風險數學完全自洽。
* **將艾略特波浪納入全自動 12 年歷史參數優化：**
  * *Rejected by audit & author*: 主觀多浪解法無法在回測中消除後見之明偏差，改為「因果幾何/指標負責回測與門檻，波浪/完整威科夫負責當下盤面結構拆解與明確失效價標定」。

---

## Key Decisions & Staging for Next Steps

* 所有 Step 3 的 4 項 Goldfish Blockers 均已鎖定明確規則並無邏輯衝突。
* 下一步準備進入 `egm-draft`（依本定案撰寫含精確公式、資料結構與檔案清單的詳細設計文件 `design_draft.md` 並做最終審核），或經決策者確認後直接推進實證腳本與報告產出。
