// src/data.js
// 負責提供即時 API 串接與高擬真多情境比特幣 K 線歷史資料生成引擎

const TIMEFRAME_MS = {
  '1H': 3600 * 1000,
  '4H': 4 * 3600 * 1000,
  '1D': 24 * 3600 * 1000,
  '1W': 7 * 24 * 3600 * 1000
};

// 可重現的偽隨機產生器，確保切換分析時圖表穩定，並支援「重新模擬」
function createRNG(seed = 421337) {
  let s = seed >>> 0;
  return function () {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/**
 * 根據指定市場情境與時間週期，生成包含經典圖表型態（雙底、頭肩底、上升三角、牛旗等）的真實感 BTC/USDT K線序列
 * @param {string} timeframe '1H' | '4H' | '1D' | '1W'
 * @param {string} scenario 'bull_breakout' | 'double_bottom' | 'head_shoulders_top' | 'triangle_squeeze'
 * @param {number} count K線數量 (預設 180)
 * @param {number} seedOffset 隨機種子偏移
 */
export function generateSyntheticBTCData(timeframe = '1D', scenario = 'bull_breakout', count = 180, seedOffset = 0) {
  const stepMs = TIMEFRAME_MS[timeframe] || TIMEFRAME_MS['1D'];
  const now = Date.now();
  const startTime = now - count * stepMs;

  const scenarioSeeds = {
    bull_breakout: 8801,
    double_bottom: 4409,
    head_shoulders_top: 9912,
    triangle_squeeze: 6623
  };
  const rng = createRNG((scenarioSeeds[scenario] || 8801) + seedOffset + stepMs / 60000);

  // 依據時間週期調整波動率
  const baseVol = {
    '1H': 0.0065,
    '4H': 0.012,
    '1D': 0.021,
    '1W': 0.042
  }[timeframe] || 0.021;

  // 構造不同情境的目標價格錨點曲線 (normalized 0..1 progress -> price)
  let anchors = [];
  if (scenario === 'bull_breakout') {
    // 頭肩底打底 -> 突破頸線 -> 牛旗整理 -> 主升段測試前高壓力
    anchors = [
      { p: 0.00, price: 82500 },
      { p: 0.12, price: 76800 }, // 左肩
      { p: 0.20, price: 83200 }, // 頸線 1
      { p: 0.30, price: 72400 }, // 頭部 (波段最低點 Swing Low)
      { p: 0.42, price: 83400 }, // 頸線 2
      { p: 0.50, price: 77500 }, // 右肩
      { p: 0.62, price: 89800 }, // 突破頸線拉升
      { p: 0.72, price: 86200 }, // 牛旗回踩支撐
      { p: 0.85, price: 96400 }, // 波段高點 Swing High
      { p: 0.93, price: 91200 }, // 回撤測試 0.382 支撐
      { p: 1.00, price: 94850 }  // 當前價格蓄勢待發
    ];
  } else if (scenario === 'double_bottom') {
    // W底型態：急跌 -> 第一底 -> 反彈頸線 -> 第二底(微高伴隨RSI背離) -> 放量突破頸線
    anchors = [
      { p: 0.00, price: 94200 },
      { p: 0.18, price: 87500 },
      { p: 0.34, price: 78200 }, // 第一底 Bottom 1
      { p: 0.52, price: 86400 }, // 頸線壓力 Neckline
      { p: 0.68, price: 78450 }, // 第二底 Bottom 2
      { p: 0.82, price: 86800 }, // 突破頸線
      { p: 0.89, price: 85100 }, // 回測頸線支撐確認
      { p: 1.00, price: 91600 }
    ];
  } else if (scenario === 'head_shoulders_top') {
    // 頭肩頂反轉：上漲 -> 左肩 -> 頭部創高 -> 跌回頸線 -> 右肩無力 -> 跌破頸線
    anchors = [
      { p: 0.00, price: 78000 },
      { p: 0.16, price: 88500 },
      { p: 0.28, price: 94600 }, // 左肩 Left Shoulder
      { p: 0.38, price: 88200 }, // 頸線 1
      { p: 0.52, price: 99400 }, // 頭部 Head
      { p: 0.64, price: 88000 }, // 頸線 2
      { p: 0.76, price: 94100 }, // 右肩 Right Shoulder
      { p: 0.88, price: 86200 }, // 跌破頸線
      { p: 0.94, price: 88100 }, // 反抽頸線壓力
      { p: 1.00, price: 83900 }
    ];
  } else {
    // triangle_squeeze: 上升三角形收斂 (高點水平壓力 ~92800，低點逐步墊高)
    anchors = [
      { p: 0.00, price: 79500 },
      { p: 0.18, price: 92600 }, // 水平壓力 1
      { p: 0.32, price: 83200 }, // 低點 1
      { p: 0.48, price: 92750 }, // 水平壓力 2
      { p: 0.62, price: 86400 }, // 低點 2 (墊高)
      { p: 0.76, price: 92680 }, // 水平壓力 3
      { p: 0.86, price: 89300 }, // 低點 3 (再墊高)
      { p: 0.94, price: 92800 }, // 測試頂部
      { p: 1.00, price: 94600 }  // 尾盤向上突破
    ];
  }

  function getTargetPrice(progress) {
    for (let i = 0; i < anchors.length - 1; i++) {
      const a = anchors[i];
      const b = anchors[i + 1];
      if (progress >= a.p && progress <= b.p) {
        const t = (progress - a.p) / (b.p - a.p || 1);
        // 平滑餘弦插值
        const smoothT = (1 - Math.cos(t * Math.PI)) / 2;
        return a.price + (b.price - a.price) * smoothT;
      }
    }
    return anchors[anchors.length - 1].price;
  }

  const candles = [];
  let prevClose = anchors[0].price;

  for (let i = 0; i < count; i++) {
    const progress = i / (count - 1);
    const target = getTargetPrice(progress);
    const time = Math.floor((startTime + i * stepMs) / 1000);

    // 結合目標牽引力與隨機漫步噪音
    const pull = (target - prevClose) * 0.26;
    const noise = (rng() - 0.495) * prevClose * baseVol;
    const open = prevClose;
    const close = Math.max(10000, Math.round((open + pull + noise) * 10) / 10);

    const bodyRange = Math.abs(close - open);
    const upperWick = (0.2 + rng() * 1.1) * (bodyRange + open * baseVol * 0.45);
    const lowerWick = (0.2 + rng() * 1.1) * (bodyRange + open * baseVol * 0.45);

    const high = Math.round((Math.max(open, close) + upperWick) * 10) / 10;
    const low = Math.round((Math.min(open, close) - lowerWick) * 10) / 10;

    // 成交量與波動度正相關
    const volatilityRatio = (high - low) / (open * baseVol);
    const baseVolume = 1400 + rng() * 1800;
    const volume = Math.round(baseVolume * (0.6 + volatilityRatio * 0.95));

    candles.push({
      index: i,
      time,
      timestamp: time * 1000,
      open,
      high,
      low,
      close,
      volume
    });

    prevClose = close;
  }

  return candles;
}

/**
 * 嘗試從公開 API (Binance / CoinGecko 相容格式) 抓取即時 BTC/USDT K 線，若環境受限則無縫回退至高擬真引擎
 */
export async function fetchLiveBTCData(timeframe = '1D', limit = 180) {
  const intervalMap = {
    '1H': '1h',
    '4H': '4h',
    '1D': '1d',
    '1W': '1w'
  };
  const interval = intervalMap[timeframe] || '1d';
  const url = `https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=${interval}&limit=${limit}`;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3500);

  try {
    const res = await fetch(url, { signal: controller.signal });
    clearTimeout(timeout);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const raw = await res.json();
    if (!Array.isArray(raw) || raw.length < 30) throw new Error('Invalid kline payload');

    return {
      source: 'LIVE_API',
      candles: raw.map((k, idx) => ({
        index: idx,
        time: Math.floor(k[0] / 1000),
        timestamp: k[0],
        open: parseFloat(k[1]),
        high: parseFloat(k[2]),
        low: parseFloat(k[3]),
        close: parseFloat(k[4]),
        volume: parseFloat(k[5])
      }))
    };
  } catch (err) {
    clearTimeout(timeout);
    return {
      source: 'SIMULATED',
      error: err.message,
      candles: generateSyntheticBTCData(timeframe, 'bull_breakout', limit)
    };
  }
}
