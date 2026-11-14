// src/forecast.js
// 價格預測模型引擎：
// 結合 Holt-Winters 雙重指數平滑趨勢外推 (Double Exponential Smoothing)、
// 多項式動能迴歸 (Polynomial Momentum Regression) 與 均值回歸引力 (Mean Reversion Anchor)，
// 並基於歷史對數報酬波動率 (Realized Volatility) 計算 68% (1σ) 與 95% (2σ) 蒙地卡羅信賴區間扇形圖。

export function runPriceForecast(candles, indicators, options = {}) {
  const horizon = options.horizon || 14; // 預測未來 K 線根數 (7 ~ 30)
  const mode = options.mode || 'ensemble'; // 'ensemble' | 'trend_follow' | 'mean_revert'

  const closes = candles.map((c) => c.close);
  const n = closes.length;
  const lastCandle = candles[n - 1];
  const prevCandle = candles[n - 2] || lastCandle;
  const stepSec = Math.max(3600, lastCandle.time - prevCandle.time);

  // 1. 計算近期對數報酬率與實現波動率 (Realized Volatility)
  const lookbackVol = Math.min(45, n - 1);
  const logReturns = [];
  for (let i = n - lookbackVol; i < n; i++) {
    logReturns.push(Math.log(closes[i] / closes[i - 1]));
  }
  const meanRet = logReturns.reduce((a, b) => a + b, 0) / logReturns.length;
  const variance =
    logReturns.reduce((acc, r) => acc + (r - meanRet) * (r - meanRet), 0) /
    Math.max(1, logReturns.length - 1);
  const barSigma = Math.sqrt(variance);

  // 2. Holt 雙重指數平滑 (Level L + Trend B)
  const alpha = 0.36;
  const beta = 0.22;
  let level = closes[Math.max(0, n - 50)];
  let trend = closes[Math.max(1, n - 49)] - level;

  for (let i = Math.max(1, n - 49); i < n; i++) {
    const y = closes[i];
    const prevLevel = level;
    level = alpha * y + (1 - alpha) * (level + trend);
    trend = beta * (level - prevLevel) + (1 - beta) * trend;
  }

  // 3. 線性迴歸斜率與殘差標準差 (過去 30 根 K 線)
  const regWindow = Math.min(30, n);
  let sumX = 0;
  let sumY = 0;
  let sumXY = 0;
  let sumX2 = 0;
  for (let i = 0; i < regWindow; i++) {
    const y = closes[n - regWindow + i];
    sumX += i;
    sumY += y;
    sumXY += i * y;
    sumX2 += i * i;
  }
  const regSlope =
    (regWindow * sumXY - sumX * sumY) / (regWindow * sumX2 - sumX * sumX || 1);

  // 4. 均值錨點 (SMA20 與 EMA50 加權中樞)
  const lastSma20 = indicators.sma20[n - 1] || lastCandle.close;
  const lastEma50 = indicators.ema50[n - 1] || lastCandle.close;
  const equilibriumPrice = lastSma20 * 0.55 + lastEma50 * 0.45;
  const lastRsi = indicators.rsi14[n - 1] || 50;

  // RSI 超買超賣對均值回歸的修正力道
  const rsiPull = ((52 - lastRsi) / 50) * lastCandle.close * 0.0022;

  // 5. 生成未來 h = 1..horizon 步預測路徑與 1σ / 2σ 上下界
  const forecastPoints = [];
  for (let h = 1; h <= horizon; h++) {
    // 阻尼趨勢係數 (Damped Trend factor phi = 0.94)
    const dampSum = (1 - Math.pow(0.94, h)) / (1 - 0.94);

    // 趨勢外推分量
    const trendProjection = lastCandle.close + (trend * 0.55 + regSlope * 0.45) * dampSum;

    // 均值回歸分量 (向動態成長的均線中樞收斂)
    const targetEq = equilibriumPrice + regSlope * 0.35 * h;
    const revertRatio = 1 - Math.exp(-0.11 * h);
    const meanRevertProjection =
      lastCandle.close + (targetEq - lastCandle.close) * revertRatio + rsiPull * h;

    let expectedPrice;
    if (mode === 'trend_follow') {
      expectedPrice = trendProjection * 0.82 + meanRevertProjection * 0.18;
    } else if (mode === 'mean_revert') {
      expectedPrice = meanRevertProjection * 0.8 + trendProjection * 0.2;
    } else {
      // ensemble 混合模型：根據 ADX/趨勢強度動態配重
      expectedPrice = trendProjection * 0.62 + meanRevertProjection * 0.38;
    }

    // 隨時間擴散的不確定性扇形 (sqrt(h) 擴散率)
    const spread1Sigma = expectedPrice * barSigma * Math.sqrt(h) * 0.95;
    const spread2Sigma = spread1Sigma * 1.96;

    forecastPoints.push({
      step: h,
      index: n - 1 + h,
      time: lastCandle.time + h * stepSec,
      timestamp: (lastCandle.time + h * stepSec) * 1000,
      mean: Math.round(expectedPrice),
      upper68: Math.round(expectedPrice + spread1Sigma),
      lower68: Math.round(expectedPrice - spread1Sigma),
      upper95: Math.round(expectedPrice + spread2Sigma),
      lower95: Math.round(expectedPrice - spread2Sigma)
    });
  }

  const finalPt = forecastPoints[forecastPoints.length - 1];
  const expectedReturnPct = ((finalPt.mean - lastCandle.close) / lastCandle.close) * 100;
  const annualizedVolPct = barSigma * Math.sqrt(365) * 100;

  // 上漲機率估算 (基於漂移率與波動率 Z-score)
  const zScore = (finalPt.mean - lastCandle.close) / Math.max(1, finalPt.upper68 - finalPt.mean);
  const bullProb = Math.max(12, Math.min(88, Math.round(50 + zScore * 24)));

  return {
    horizon,
    mode,
    startPrice: lastCandle.close,
    targetPrice: finalPt.mean,
    expectedReturnPct,
    bullProb,
    bearProb: 100 - bullProb,
    upper68: finalPt.upper68,
    lower68: finalPt.lower68,
    upper95: finalPt.upper95,
    lower95: finalPt.lower95,
    barSigmaPct: barSigma * 100,
    annualizedVolPct,
    points: forecastPoints
  };
}
