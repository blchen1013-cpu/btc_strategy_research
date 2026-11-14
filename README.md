# BTC // QUANT — 比特幣全方位技術分析與量化回測終端

專為比特幣（BTC/USDT）走勢分析打造的互動式網頁分析終端，整合**傳統技術分析（型態學、支撐壓力、斐波那契）**、**量化技術指標**、**買賣訊號掃描**、**機器學習/時間序列走勢預測**與**交易策略歷史回測**。

## 核心功能

### 1. 即時與歷史 K 線圖 (Candlestick & Sub-charts)
- **多時間週期切換**：支援 `1H`、`4H`、`1D`、`1W` 週期切換。
- **雙資料源引擎**：支援一鍵從公開加密貨幣 API 抓取即時歷史 K 線，並內建高擬真多週期歷史/模擬行情引擎（含突破、盤整、趨勢行情切換），無須 API Key 即可離線完整操作。
- **互動式十字游標 (Crosshair)**：即時檢視任意 K 線之開、高、低、收、成交量與各項指標數值。

### 2. 技術指標系統 (Technical Indicators)
- **主圖疊加指標**：`SMA 20`、`EMA 50`、`EMA 200`、**布林通道 (Bollinger Bands 20, 2)**。
- **副圖指標切換**：
  - **MACD (12, 26, 9)**：DIF、DEA 與紅綠動能柱狀圖。
  - **RSI (14)**：相對強弱指標，標示 `70` 超買區與 `30` 超賣區。
  - **成交量 (Volume)**：多空量能柱與 20 期均量線。

### 3. 傳統技術分析：型態、支撐壓力、斐波那契
- **自動支撐與壓力區間 (Support & Resistance Zones)**：透過局部波段高低點聚類 (Pivot Clustering) 與成交量權重，自動標繪關鍵壓力帶（Resistance）與支撐帶（Support），並計算距離現價百分比與測試次數。
- **斐波那契回撤與擴展 (Fibonacci Retracement & Extension)**：自動抓取近期顯著波段高點 (Swing High) 與低點 (Swing Low)，在圖表上繪製 `0.0`、`0.236`、`0.382`、`0.5`、`0.618`（黃金分割）、`0.786`、`1.0` 及 `1.272`、`1.618` 關鍵點位與價格清單。
- **經典圖表型態辨識 (Classical Chart Patterns)**：自動掃描並於 K 線圖上直接繪製型態輪廓、頸線與目標價：
  - 雙底 / 雙頂 (Double Bottom / Double Top)
  - 頭肩頂 / 頭肩底 (Head & Shoulders / Inverse H&S)
  - 上升三角形 / 下降三角形 / 對稱收斂三角形 (Ascending / Descending / Symmetrical Triangle)
  - 牛旗 / 熊旗通道 (Bullish / Bearish Flag)

### 4. 智慧買賣訊號提示 (Signal Scanner)
- 自動偵測並於 K 線圖標註 `BUY` / `SELL` 標籤：
  - **MA 黃金交叉 / 死亡交叉** (SMA20 × EMA50)
  - **RSI 超賣背離 / 超買反轉** (RSI < 30 回升 / RSI > 70 回落)
  - **MACD 柱狀體翻紅 / 翻綠交叉**
  - **布林通道下軌支撐反彈 / 上軌突破壓力**
  - **關鍵支撐位測試 / 壓力位突破**
- 綜合多空儀表板（Bullish / Bearish Confluence Score）。

### 5. 價格預測模型 (ML & Time-Series Forecast)
- 結合 **Holt-Winters 指數平滑趨勢外推**、**多項式動能迴歸 (Polynomial Momentum Regression)** 與 **歷史波動率蒙地卡羅置信區間 (Monte Carlo Volatility Cone)**。
- 在 K 線圖右側直接繪製未來 **7 至 30 根 K 線** 的預測中軸路徑，以及 **68% (1σ)** 與 **95% (2σ)** 機率區間扇形圖。
- 支援調整預測期數與模型權重（趨勢追蹤 vs. 均值回歸）。

### 6. 交易策略歷史回測 (Strategy Backtester)
- 內建四大經典策略並支援自訂參數（初始資金、手續費率、停損/停利比例）：
  1. **雙均線交叉策略 (MA Crossover)**
  2. **RSI 極端均值回歸策略 (RSI Mean Reversion)**
  3. **MACD 動能突破策略 (MACD Momentum)**
  4. **布林通道 + 支撐壓力共振策略 (BB + S/R Confluence)**
- 產出完整績效指標：**總報酬率 (Total Return)**、**最大回撤 (Max Drawdown)**、**勝率 (Win Rate)**、**夏普比率 (Sharpe Ratio)**、**獲利因子 (Profit Factor)**、**資產淨值曲線 (Equity Curve)** 與 **逐筆交易明細表**。

## 專案結構

```text
bitcoin-analyzer/
├── index.html          # 主應用程式介面與互動佈局
├── src/
│   ├── styles.css      # 專業金融終端深色主題設計系統 (Vanilla CSS)
│   ├── data.js         # 即時 API 串接與多行情歷史 K 線模擬引擎
│   ├── indicators.js   # MA, EMA, Bollinger, RSI, MACD, ATR 計算模組
│   ├── classical.js    # 傳統技術分析：支撐壓力聚類、斐波那契、K線與圖表型態辨識
│   ├── forecast.js     # 價格預測模型：時間序列外推與波動率信賴區間
│   ├── backtest.js     # 策略回測引擎、淨值曲線與績效統計計算
│   └── app.js          # 高解析度 Canvas K線渲染引擎與 UI 狀態控制
└── package.json
```

## 快速啟動

```bash
# 使用 Python 內建伺服器啟動
npm start
# 或直接開啟
python3 -m http.server 3000
```
接著於瀏覽器開啟 `http://localhost:3000`。
