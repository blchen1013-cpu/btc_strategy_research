// src/indicators.js
// 計算核心量化技術指標：SMA, EMA, Bollinger Bands, RSI, MACD, ATR, Volume MA 與買賣訊號掃描

export function calcSMA(values, period) {
  const result = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) {
      sum -= values[i - period];
    }
    if (i >= period - 1) {
      result[i] = sum / period;
    }
  }
  return result;
}

export function calcEMA(values, period) {
  const result = new Array(values.length).fill(null);
  const k = 2 / (period + 1);
  let ema = null;

  for (let i = 0; i < values.length; i++) {
    const val = values[i];
    if (val === null || val === undefined) continue;
    if (ema === null) {
      ema = val;
      result[i] = ema;
    } else {
      ema = val * k + ema * (1 - k);
      result[i] = ema;
    }
  }
  return result;
}

export function calcBollingerBands(closes, period = 20, stdDevMult = 2) {
  const mid = calcSMA(closes, period);
  const upper = new Array(closes.length).fill(null);
  const lower = new Array(closes.length).fill(null);
  const bandwidth = new Array(closes.length).fill(null);

  for (let i = period - 1; i < closes.length; i++) {
    const mean = mid[i];
    let sqSum = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const diff = closes[j] - mean;
      sqSum += diff * diff;
    }
    const std = Math.sqrt(sqSum / period);
    upper[i] = mean + stdDevMult * std;
    lower[i] = mean - stdDevMult * std;
    bandwidth[i] = ((upper[i] - lower[i]) / mean) * 100;
  }

  return { mid, upper, lower, bandwidth };
}

export function calcRSI(closes, period = 14) {
  const rsi = new Array(closes.length).fill(null);
  if (closes.length <= period) return rsi;

  let gains = 0;
  let losses = 0;
  for (let i = 1; i <= period; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff >= 0) gains += diff;
    else losses -= diff;
  }

  let avgGain = gains / period;
  let avgLoss = losses / period;
  rsi[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);

  for (let i = period + 1; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    const gain = diff > 0 ? diff : 0;
    const loss = diff < 0 ? -diff : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    rsi[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }

  return rsi;
}

export function calcMACD(closes, fastPeriod = 12, slowPeriod = 26, signalPeriod = 9) {
  const emaFast = calcEMA(closes, fastPeriod);
  const emaSlow = calcEMA(closes, slowPeriod);
  const macdLine = closes.map((_, i) =>
    emaFast[i] !== null && emaSlow[i] !== null ? emaFast[i] - emaSlow[i] : 0
  );
  const signalLine = calcEMA(macdLine, signalPeriod);
  const histogram = macdLine.map((m, i) => m - (signalLine[i] || 0));

  return { macdLine, signalLine, histogram };
}

export function calcATR(candles, period = 14) {
  const tr = candles.map((c, i) => {
    if (i === 0) return c.high - c.low;
    const prevClose = candles[i - 1].close;
    return Math.max(
      c.high - c.low,
      Math.abs(c.high - prevClose),
      Math.abs(c.low - prevClose)
    );
  });
  return calcSMA(tr, period);
}

/**
 * 一次計算所有指標並掃描多空買賣訊號
 */
export function computeAllIndicators(candles) {
  const closes = candles.map((c) => c.close);
  const volumes = candles.map((c) => c.volume);

  const sma20 = calcSMA(closes, 20);
  const ema50 = calcEMA(closes, 50);
  const ema200 = calcEMA(closes, 200);
  const bb = calcBollingerBands(closes, 20, 2);
  const rsi14 = calcRSI(closes, 14);
  const macd = calcMACD(closes, 12, 26, 9);
  const atr14 = calcATR(candles, 14);
  const volSma20 = calcSMA(volumes, 20);

  // 掃描關鍵買賣訊號 (Signals)
  const signals = [];
  for (let i = 21; i < candles.length; i++) {
    const c = candles[i];
    const prev = candles[i - 1];

    // 1. 黃金交叉 / 死亡交叉 (SMA20 與 EMA50)
    if (sma20[i - 1] !== null && ema50[i - 1] !== null) {
      if (sma20[i - 1] <= ema50[i - 1] && sma20[i] > ema50[i]) {
        signals.push({
          index: i,
          time: c.timestamp,
          price: c.low,
          close: c.close,
          type: 'BUY',
          category: 'MA_CROSS',
          label: '黃金交叉',
          detail: 'SMA20 向上突破 EMA50，中期多頭趨勢確立',
          strength: 86
        });
      } else if (sma20[i - 1] >= ema50[i - 1] && sma20[i] < ema50[i]) {
        signals.push({
          index: i,
          time: c.timestamp,
          price: c.high,
          close: c.close,
          type: 'SELL',
          category: 'MA_CROSS',
          label: '死亡交叉',
          detail: 'SMA20 向下跌破 EMA50，中期動能轉弱',
          strength: 84
        });
      }
    }

    // 2. RSI 超賣回升 / 超買回落
    if (rsi14[i - 1] !== null && rsi14[i] !== null) {
      if (rsi14[i - 1] < 31 && rsi14[i] >= 31 && c.close > c.open) {
        signals.push({
          index: i,
          time: c.timestamp,
          price: c.low,
          close: c.close,
          type: 'BUY',
          category: 'RSI_REV',
          label: 'RSI 超賣反彈',
          detail: `RSI 自 ${rsi14[i - 1].toFixed(1)} 超賣區勾頭向上 (${rsi14[i].toFixed(1)})`,
          strength: 78
        });
      } else if (rsi14[i - 1] > 69 && rsi14[i] <= 69 && c.close < c.open) {
        signals.push({
          index: i,
          time: c.timestamp,
          price: c.high,
          close: c.close,
          type: 'SELL',
          category: 'RSI_REV',
          label: 'RSI 超買拉回',
          detail: `RSI 自 ${rsi14[i - 1].toFixed(1)} 超買區向下轉折 (${rsi14[i].toFixed(1)})`,
          strength: 76
        });
      }
    }

    // 3. 布林通道下軌支撐吞噬 / 上軌受阻
    if (bb.lower[i] !== null && bb.upper[i] !== null) {
      if (prev.low <= bb.lower[i - 1] && c.close > prev.high && c.close > c.open) {
        // 避免與同根K線重複標記
        if (!signals.some((s) => Math.abs(s.index - i) <= 2 && s.type === 'BUY')) {
          signals.push({
            index: i,
            time: c.timestamp,
            price: c.low,
            close: c.close,
            type: 'BUY',
            category: 'BB_BOUNCE',
            label: '布林下軌支撐',
            detail: '觸及布林下軌後出現多頭吞噬確認訊號',
            strength: 74
          });
        }
      } else if (prev.high >= bb.upper[i - 1] && c.close < prev.low && c.close < c.open) {
        if (!signals.some((s) => Math.abs(s.index - i) <= 2 && s.type === 'SELL')) {
          signals.push({
            index: i,
            time: c.timestamp,
            price: c.high,
            close: c.close,
            type: 'SELL',
            category: 'BB_REJECT',
            label: '布林上軌受阻',
            detail: '觸及布林上軌後出現高檔轉折賣壓',
            strength: 73
          });
        }
      }
    }
  }

  // 計算當下綜合技術評分 (0 ~ 100, >55 偏多, <45 偏空)
  const lastIdx = candles.length - 1;
  const lastClose = closes[lastIdx];
  let score = 50;
  const reasons = [];

  if (sma20[lastIdx] && lastClose > sma20[lastIdx]) {
    score += 10;
    reasons.push('站上 SMA20 短期均線');
  } else {
    score -= 10;
    reasons.push('位於 SMA20 短期均線之下');
  }

  if (ema50[lastIdx] && lastClose > ema50[lastIdx]) {
    score += 12;
    reasons.push('站穩 EMA50 中期趨勢線');
  } else {
    score -= 12;
    reasons.push('受制於 EMA50 中期均線');
  }

  if (sma20[lastIdx] && ema50[lastIdx] && sma20[lastIdx] > ema50[lastIdx]) {
    score += 8;
    reasons.push('均線呈多頭排列 (SMA20 > EMA50)');
  } else {
    score -= 8;
  }

  const lastRsi = rsi14[lastIdx] || 50;
  if (lastRsi > 55 && lastRsi < 70) {
    score += 9;
    reasons.push(`RSI (${lastRsi.toFixed(1)}) 處於多方強勢區`);
  } else if (lastRsi >= 70) {
    score -= 4;
    reasons.push(`RSI (${lastRsi.toFixed(1)}) 進入超買警戒區`);
  } else if (lastRsi <= 30) {
    score += 8;
    reasons.push(`RSI (${lastRsi.toFixed(1)}) 進入超賣醞釀反彈區`);
  } else if (lastRsi < 45) {
    score -= 8;
    reasons.push(`RSI (${lastRsi.toFixed(1)}) 動能偏弱`);
  }

  if (macd.histogram[lastIdx] > 0) {
    score += 8;
    reasons.push('MACD 柱狀體為正值多頭動能');
  } else {
    score -= 8;
    reasons.push('MACD 柱狀體為負值空方動能');
  }

  score = Math.max(5, Math.min(95, Math.round(score)));

  return {
    sma20,
    ema50,
    ema200,
    bb,
    rsi14,
    macd,
    atr14,
    volSma20,
    signals,
    summary: {
      score,
      stance: score >= 62 ? '強勢看多 (BULLISH)' : score >= 53 ? '偏多震盪 (MODERATE BULL)' : score <= 38 ? '弱勢看空 (BEARISH)' : score <= 47 ? '偏空整理 (MODERATE BEAR)' : '多空平衡 (NEUTRAL)',
      reasons
    }
  };
}
