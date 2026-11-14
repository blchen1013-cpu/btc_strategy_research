// src/app.js
// 主程式控制器與高解析度多圖層金融 Canvas 渲染引擎

import { generateSyntheticBTCData, fetchLiveBTCData } from './data.js';
import { computeAllIndicators } from './indicators.js';
import { runClassicalAnalysis } from './classical.js';
import { runPriceForecast } from './forecast.js';
import { runStrategyBacktest } from './backtest.js';

const state = {
  timeframe: '1D',
  scenario: 'bull_breakout',
  seedOffset: 0,
  dataSource: 'SIMULATED',
  candles: [],
  indicators: null,
  classical: null,
  forecast: null,
  backtest: null,
  layers: {
    ma: true,
    bb: true,
    sr: true,
    fib: true,
    patterns: true,
    signals: true,
    forecast: true
  },
  subChart: 'macd', // 'macd' | 'rsi' | 'volume'
  hoverIndex: null,
  highlightIndex: null,
  forecastOptions: {
    horizon: 14,
    mode: 'ensemble'
  },
  backtestConfig: {
    strategyId: 'ma_cross',
    initialCapital: 10000,
    stopLossPct: 5.5,
    takeProfitPct: 14.0
  }
};

function fmtPrice(val) {
  if (val === null || val === undefined || Number.isNaN(val)) return '--';
  return '$' + Number(val).toLocaleString('en-US', { maximumFractionDigits: 1 });
}

function fmtCompactPrice(val) {
  if (val === null || val === undefined || Number.isNaN(val)) return '--';
  return Number(val).toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function fmtDate(ts, tf = state.timeframe) {
  const d = new Date(ts);
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  if (tf === '1H' || tf === '4H') {
    const hh = String(d.getHours()).padStart(2, '0');
    return `${mm}/${dd} ${hh}:00`;
  }
  return `${d.getFullYear()}-${mm}-${dd}`;
}

/**
 * 重新計算所有技術指標、傳統型態/支撐壓力/斐波那契、ML走勢預測與策略回測
 */
function recomputeAll() {
  state.indicators = computeAllIndicators(state.candles);
  state.classical = runClassicalAnalysis(state.candles);
  state.forecast = runPriceForecast(state.candles, state.indicators, state.forecastOptions);
  state.backtest = runStrategyBacktest(
    state.candles,
    state.indicators,
    state.classical,
    state.backtestConfig
  );

  updateTopHeaderAndCards();
  updateCrosshairBar(state.hoverIndex ?? state.candles.length - 1);
  renderClassicalTab();
  renderSignalsTab();
  renderForecastTab();
  renderBacktestTab();
  drawAllCanvases();
}

function loadScenarioData() {
  state.dataSource = 'SIMULATED';
  state.candles = generateSyntheticBTCData(state.timeframe, state.scenario, 160, state.seedOffset);
  const badge = document.getElementById('data-source-badge');
  if (badge) {
    badge.textContent = '高擬真型態引擎';
    badge.className = 'badge badge-amber';
  }
  recomputeAll();
}

async function handleFetchLive() {
  const btn = document.getElementById('btn-fetch-live');
  if (btn) btn.textContent = '⏳ 連線中...';
  const res = await fetchLiveBTCData(state.timeframe, 160);
  state.candles = res.candles;
  state.dataSource = res.source;

  const badge = document.getElementById('data-source-badge');
  if (res.source === 'LIVE_API') {
    if (badge) {
      badge.textContent = 'LIVE API 即時行情';
      badge.className = 'badge badge-bull';
    }
    if (btn) btn.textContent = '✓ 已同步即時行情';
  } else {
    if (badge) {
      badge.textContent = '離線沙盒 (高擬真引擎)';
      badge.className = 'badge badge-amber';
    }
    if (btn) btn.textContent = '⚡ 抓取即時 API';
  }
  recomputeAll();
}

function updateTopHeaderAndCards() {
  const n = state.candles.length;
  const last = state.candles[n - 1];
  const first = state.candles[0];
  const chgPct = ((last.close - first.open) / first.open) * 100;
  const isUp = chgPct >= 0;

  const elLast = document.getElementById('stat-last-price');
  elLast.textContent = fmtPrice(last.close);
  elLast.className = `stat-value hero-price ${isUp ? 'text-bull' : 'text-bear'}`;

  const elChg = document.getElementById('stat-change');
  elChg.textContent = `${isUp ? '+' : ''}${chgPct.toFixed(2)}%`;
  elChg.className = `stat-value ${isUp ? 'text-bull' : 'text-bear'}`;

  const { swingHigh, swingLow, levels } = state.classical.fibonacci;
  document.getElementById('stat-high').textContent = fmtPrice(swingHigh.price);
  document.getElementById('stat-low').textContent = fmtPrice(swingLow.price);

  const lastAtr = state.indicators.atr14[n - 1] || 0;
  document.getElementById('stat-atr').textContent = fmtPrice(Math.round(lastAtr));

  const { score, stance } = state.indicators.summary;
  const elStance = document.getElementById('stat-stance');
  elStance.textContent = `${score} / 100 ${stance.split(' ')[0]}`;
  elStance.className = `stat-value ${score >= 52 ? 'text-bull' : score <= 46 ? 'text-bear' : 'text-amber'}`;

  // 下方 4 張重點卡片
  const r1 = state.classical.srZones.resistances[state.classical.srZones.resistances.length - 1];
  if (r1) {
    document.getElementById('card-r1-price').textContent = fmtPrice(r1.price);
    document.getElementById('card-r1-meta').textContent = `距離現價 +${Math.abs(r1.distPct).toFixed(2)}% ・ 測試 ${r1.touches} 次`;
  } else {
    document.getElementById('card-r1-price').textContent = fmtPrice(Math.round(last.close * 1.035));
    document.getElementById('card-r1-meta').textContent = '上方心理關卡壓力';
  }

  const s1 = state.classical.srZones.supports[0];
  if (s1) {
    document.getElementById('card-s1-price').textContent = fmtPrice(s1.price);
    document.getElementById('card-s1-meta').textContent = `距離現價 ${s1.distPct.toFixed(2)}% ・ 測試 ${s1.touches} 次`;
  } else {
    document.getElementById('card-s1-price').textContent = fmtPrice(Math.round(last.close * 0.965));
    document.getElementById('card-s1-meta').textContent = '下方波段防守支撐';
  }

  const fib618 = levels.find((l) => Math.abs(l.ratio - 0.618) < 0.01);
  if (fib618) {
    document.getElementById('card-fib-price').textContent = fmtPrice(fib618.price);
    document.getElementById('card-fib-meta').textContent = `距離現價 ${fib618.distPct >= 0 ? '+' : ''}${fib618.distPct.toFixed(2)}% ・ 黃金分割區`;
    document.getElementById('leg-fib618').textContent = fmtPrice(fib618.price);
  }

  const fc = state.forecast;
  document.getElementById('card-forecast-price').textContent = fmtPrice(fc.targetPrice);
  const fcSign = fc.expectedReturnPct >= 0 ? '+' : '';
  document.getElementById('card-forecast-meta').textContent = `${fc.horizon}期預期 ${fcSign}${fc.expectedReturnPct.toFixed(2)}% ・ 多頭機率 ${fc.bullProb}%`;
}

function updateCrosshairBar(idx) {
  const safeIdx = Math.max(0, Math.min(state.candles.length - 1, idx));
  const c = state.candles[safeIdx];
  if (!c) return;

  document.getElementById('ch-time').textContent = fmtDate(c.timestamp);
  document.getElementById('ch-open').textContent = fmtCompactPrice(c.open);
  document.getElementById('ch-high').textContent = fmtCompactPrice(c.high);
  document.getElementById('ch-low').textContent = fmtCompactPrice(c.low);
  const elClose = document.getElementById('ch-close');
  elClose.textContent = fmtCompactPrice(c.close);
  elClose.className = c.close >= c.open ? 'text-bull' : 'text-bear';
  document.getElementById('ch-vol').textContent = `${c.volume.toLocaleString()} BTC`;

  const { sma20, ema50, bb, macd, rsi14, volSma20 } = state.indicators;
  document.getElementById('leg-sma20').textContent = sma20[safeIdx] ? fmtCompactPrice(sma20[safeIdx]) : '--';
  document.getElementById('leg-ema50').textContent = ema50[safeIdx] ? fmtCompactPrice(ema50[safeIdx]) : '--';
  document.getElementById('leg-bb').textContent =
    bb.upper[safeIdx] && bb.lower[safeIdx]
      ? `${fmtCompactPrice(bb.upper[safeIdx])} / ${fmtCompactPrice(bb.lower[safeIdx])}`
      : '--';

  const subReadout = document.getElementById('subchart-readout');
  if (state.subChart === 'macd') {
    const m = macd.macdLine[safeIdx] || 0;
    const s = macd.signalLine[safeIdx] || 0;
    const h = macd.histogram[safeIdx] || 0;
    subReadout.textContent = `DIF: ${m.toFixed(1)} | DEA: ${s.toFixed(1)} | HIST: ${h >= 0 ? '+' : ''}${h.toFixed(1)}`;
  } else if (state.subChart === 'rsi') {
    const r = rsi14[safeIdx];
    subReadout.textContent = `RSI (14): ${r ? r.toFixed(2) : '--'}  [>70 超買 | <30 超賣]`;
  } else {
    const vma = volSma20[safeIdx];
    subReadout.textContent = `成交量: ${c.volume.toLocaleString()} BTC | 20期均量: ${vma ? Math.round(vma).toLocaleString() : '--'} BTC`;
  }
}

/* ============================================================================
 * 右側面板渲染：1. 型態、支撐壓力、斐波那契
 * ========================================================================== */
function renderClassicalTab() {
  const { patterns, srZones, fibonacci } = state.classical;

  // 1. 經典圖表型態
  document.getElementById('pattern-count-badge').textContent = `${patterns.chartPatterns.length} 個型態`;
  const patternContainer = document.getElementById('classical-patterns-list');
  patternContainer.innerHTML = patterns.chartPatterns
    .map((p) => {
      const badgeCls = p.bias === 'BULLISH' ? 'badge-bull' : p.bias === 'BEARISH' ? 'badge-bear' : 'badge-amber';
      const priceColor = p.bias === 'BULLISH' ? 'text-bull' : p.bias === 'BEARISH' ? 'text-bear' : 'text-amber';
      return `
        <div class="pattern-card">
          <div class="pattern-top">
            <span class="pattern-name">${p.name}</span>
            <span class="badge ${badgeCls}">${p.biasLabel} ・ 信心度 ${p.confidence}%</span>
          </div>
          <p class="pattern-desc">${p.description}</p>
          <div class="pattern-metrics">
            <div>
              <span class="stat-label">頸線關鍵位</span>
              <div style="font-weight:700;">${fmtPrice(p.necklinePrice)}</div>
            </div>
            <div>
              <span class="stat-label">等幅目標價</span>
              <div class="${priceColor}" style="font-weight:700;">${fmtPrice(p.targetPrice)}</div>
            </div>
            <div>
              <span class="stat-label">防守失效位</span>
              <div style="color:var(--text-secondary);">${fmtPrice(p.stopPrice)}</div>
            </div>
          </div>
        </div>
      `;
    })
    .join('');

  // 2. 支撐與壓力帶清單
  const srContainer = document.getElementById('sr-levels-list');
  const allZones = [...srZones.resistances, ...srZones.supports];
  srContainer.innerHTML = allZones
    .map((z) => {
      const isRes = z.role === 'RESISTANCE';
      const codeCls = isRes ? 'text-bear' : 'text-bull';
      const sign = z.distPct >= 0 ? '+' : '';
      return `
        <div class="level-row">
          <span class="level-code ${codeCls}">${z.code} ${isRes ? '壓力' : '支撐'}</span>
          <span style="color:var(--text-secondary); font-size:11px;">測試 ${z.touches} 次 ・ ${z.strengthLabel}</span>
          <span class="level-price">${fmtPrice(z.price)}</span>
          <span class="level-dist ${codeCls}">${sign}${z.distPct.toFixed(2)}%</span>
        </div>
      `;
    })
    .join('');

  // 3. 斐波那契回撤與擴展
  const fibBadge = document.getElementById('fib-direction-badge');
  fibBadge.textContent = fibonacci.isUptrend ? '上升波段回撤' : '下降波段反彈';
  document.getElementById('fib-swing-summary').textContent = `波段低點 ${fmtPrice(
    fibonacci.swingLow.price
  )} (${fmtDate(fibonacci.swingLow.time)}) ↔ 波段高點 ${fmtPrice(fibonacci.swingHigh.price)} (${fmtDate(
    fibonacci.swingHigh.time
  )})`;

  const fibContainer = document.getElementById('fib-levels-list');
  fibContainer.innerHTML = fibonacci.levels
    .map((lv) => {
      const rowClasses = [
        'level-row',
        lv.isGolden ? 'highlight-golden' : '',
        lv.isNearest ? 'highlight-nearest' : ''
      ]
        .filter(Boolean)
        .join(' ');
      const sign = lv.distPct >= 0 ? '+' : '';
      return `
        <div class="${rowClasses}">
          <span class="level-code" style="color:${lv.color}">${lv.ratio >= 0 ? lv.ratio.toFixed(3) : 'Ext'}</span>
          <span style="color:var(--text-secondary); font-size:11px;">
            ${lv.label} ${lv.isNearest ? '<strong class="text-cyan">(現價最近)</strong>' : ''}
          </span>
          <span class="level-price">${fmtPrice(lv.price)}</span>
          <span class="level-dist" style="color:var(--text-secondary);">${sign}${lv.distPct.toFixed(1)}%</span>
        </div>
      `;
    })
    .join('');

  // 4. 單K與組合Price Action
  const cpContainer = document.getElementById('candlestick-patterns-list');
  cpContainer.innerHTML = patterns.candlePatterns
    .map((cp) => {
      const badgeCls = cp.bias === 'BULLISH' ? 'badge-bull' : 'badge-bear';
      return `
        <div class="signal-item" data-jump-index="${cp.index}">
          <div>
            <div style="font-weight:600; font-size:12px;">${cp.name} <span style="color:var(--text-muted); font-family:var(--font-mono); font-size:11px;">@${fmtPrice(cp.price)}</span></div>
            <div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">${cp.note}</div>
          </div>
          <div style="text-align:right;">
            <span class="badge ${badgeCls}">${cp.bias === 'BULLISH' ? '看多K線' : '看空K線'}</span>
            <div style="font-size:10.5px; color:var(--text-muted); margin-top:3px; font-family:var(--font-mono);">${fmtDate(cp.time)}</div>
          </div>
        </div>
      `;
    })
    .join('');
}

/* ============================================================================
 * 右側面板渲染：2. 技術指標與買賣訊號
 * ========================================================================== */
function renderSignalsTab() {
  const { summary, sma20, ema50, rsi14, macd, bb, signals } = state.indicators;
  const n = state.candles.length - 1;

  const badge = document.getElementById('signal-score-badge');
  badge.textContent = `${summary.score} / 100 ・ ${summary.stance.split(' ')[0]}`;
  badge.className = `badge ${summary.score >= 52 ? 'badge-bull' : summary.score <= 46 ? 'badge-bear' : 'badge-amber'}`;

  document.getElementById('signal-meter-fill').style.width = `${summary.score}%`;
  document.getElementById('signal-reasons-list').innerHTML = summary.reasons
    .map((r) => `<div>• ${r}</div>`)
    .join('');

  const curRsi = rsi14[n] || 50;
  const curMacd = macd.histogram[n] || 0;
  const curBw = bb.bandwidth[n] || 0;

  document.getElementById('indicator-snapshot-grid').innerHTML = `
    <div class="kpi-cell">
      <div class="k-label">SMA 20 / EMA 50</div>
      <div class="k-val ${sma20[n] >= ema50[n] ? 'text-bull' : 'text-bear'}">
        ${fmtCompactPrice(sma20[n])} / ${fmtCompactPrice(ema50[n])}
      </div>
    </div>
    <div class="kpi-cell">
      <div class="k-label">RSI (14) 強弱指標</div>
      <div class="k-val ${curRsi > 70 ? 'text-bear' : curRsi < 30 ? 'text-bull' : 'text-amber'}">
        ${curRsi.toFixed(1)} ${curRsi > 70 ? '(超買)' : curRsi < 30 ? '(超賣)' : '(中性偏強)'}
      </div>
    </div>
    <div class="kpi-cell">
      <div class="k-label">MACD 動能柱 (Hist)</div>
      <div class="k-val ${curMacd >= 0 ? 'text-bull' : 'text-bear'}">
        ${curMacd >= 0 ? '+' : ''}${curMacd.toFixed(1)}
      </div>
    </div>
    <div class="kpi-cell">
      <div class="k-label">布林通道寬度 (Bandwidth)</div>
      <div class="k-val text-cyan">${curBw.toFixed(2)}%</div>
    </div>
  `;

  const recentSignals = [...signals].slice(-10).reverse();
  document.getElementById('signals-timeline-list').innerHTML = recentSignals
    .map((sig) => {
      const isBuy = sig.type === 'BUY';
      return `
        <div class="signal-item" data-jump-index="${sig.index}">
          <div>
            <div style="font-weight:700; font-size:12px;">
              <span class="${isBuy ? 'text-bull' : 'text-bear'}">${isBuy ? '▲ BUY' : '▼ SELL'}</span>
              — ${sig.label}
            </div>
            <div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">${sig.detail}</div>
          </div>
          <div style="text-align:right; font-family:var(--font-mono);">
            <div style="font-weight:700;">${fmtPrice(sig.close)}</div>
            <div style="font-size:10.5px; color:var(--text-muted);">${fmtDate(sig.time)}</div>
          </div>
        </div>
      `;
    })
    .join('');
}

/* ============================================================================
 * 右側面板渲染：3. 價格預測模型
 * ========================================================================== */
function renderForecastTab() {
  const fc = state.forecast;
  const isUp = fc.expectedReturnPct >= 0;

  document.getElementById('forecast-kpi-grid').innerHTML = `
    <div class="kpi-cell">
      <div class="k-label">預測中軸目標 (${fc.horizon}期後)</div>
      <div class="k-val ${isUp ? 'text-bull' : 'text-bear'}">${fmtPrice(fc.targetPrice)}</div>
    </div>
    <div class="kpi-cell">
      <div class="k-label">預期報酬率 / 多頭勝率</div>
      <div class="k-val ${isUp ? 'text-bull' : 'text-bear'}">
        ${isUp ? '+' : ''}${fc.expectedReturnPct.toFixed(2)}% (${fc.bullProb}%)
      </div>
    </div>
    <div class="kpi-cell">
      <div class="k-label">68% 信賴區間 (1σ)</div>
      <div class="k-val" style="font-size:12.5px;">${fmtCompactPrice(fc.lower68)} ~ ${fmtCompactPrice(fc.upper68)}</div>
    </div>
    <div class="kpi-cell">
      <div class="k-label">95% 極端波動區間 (2σ)</div>
      <div class="k-val" style="font-size:12.5px; color:var(--text-secondary);">${fmtCompactPrice(fc.lower95)} ~ ${fmtCompactPrice(fc.upper95)}</div>
    </div>
  `;

  // 挑出關鍵預測步數表格
  const stepMilestones = fc.points.filter(
    (pt, idx) => idx === 0 || idx === Math.floor(fc.points.length / 2) - 1 || idx === fc.points.length - 1 || (idx + 1) % 3 === 0
  );

  document.getElementById('forecast-table-container').innerHTML = `
    <table class="trade-table">
      <thead>
        <tr>
          <th>期數</th>
          <th>預測日期</th>
          <th>中軸預測價</th>
          <th>68% 區間 (1σ)</th>
          <th>95% 上界 (2σ)</th>
        </tr>
      </thead>
      <tbody>
        ${stepMilestones
          .map(
            (pt) => `
          <tr>
            <td class="text-amber">+${pt.step}</td>
            <td>${fmtDate(pt.timestamp)}</td>
            <td style="font-weight:700;">${fmtPrice(pt.mean)}</td>
            <td style="color:var(--text-secondary);">${fmtCompactPrice(pt.lower68)} – ${fmtCompactPrice(pt.upper68)}</td>
            <td class="text-cyan">${fmtCompactPrice(pt.upper95)}</td>
          </tr>
        `
          )
          .join('')}
      </tbody>
    </table>
  `;
}

/* ============================================================================
 * 右側面板渲染：4. 策略歷史回測
 * ========================================================================== */
function renderBacktestTab() {
  const bt = state.backtest;
  document.getElementById('bt-strategy-desc').textContent = bt.strategy.description;

  const alphaBadge = document.getElementById('bt-alpha-badge');
  const alphaSign = bt.alphaPct >= 0 ? '+' : '';
  alphaBadge.textContent = `超額報酬 Alpha ${alphaSign}${bt.alphaPct.toFixed(2)}%`;
  alphaBadge.className = `badge ${bt.alphaPct >= 0 ? 'badge-bull' : 'badge-amber'}`;

  const retSign = bt.totalReturnPct >= 0 ? '+' : '';
  document.getElementById('bt-kpi-grid').innerHTML = `
    <div class="kpi-cell">
      <div class="k-label">策略最終淨值 (Final Equity)</div>
      <div class="k-val ${bt.totalReturnPct >= 0 ? 'text-bull' : 'text-bear'}">
        $${bt.finalEquity.toLocaleString()} (${retSign}${bt.totalReturnPct.toFixed(2)}%)
      </div>
    </div>
    <div class="kpi-cell">
      <div class="k-label">最大回撤 (Max Drawdown)</div>
      <div class="k-val text-bear">-${bt.maxDrawdownPct.toFixed(2)}%</div>
    </div>
    <div class="kpi-cell">
      <div class="k-label">勝率 (Win Rate) / 總筆數</div>
      <div class="k-val text-amber">${bt.winRatePct.toFixed(1)}% (${bt.totalTrades} 筆)</div>
    </div>
    <div class="kpi-cell">
      <div class="k-label">夏普比率 / 獲利因子</div>
      <div class="k-val text-cyan">${bt.sharpeRatio.toFixed(2)} / PF ${bt.profitFactor.toFixed(2)}</div>
    </div>
  `;

  document.getElementById('bt-trade-count').textContent = `共 ${bt.totalTrades} 筆交易`;
  document.getElementById('bt-trades-tbody').innerHTML = bt.trades
    .map((t) => {
      const isWin = t.returnPct >= 0;
      return `
        <tr>
          <td>#${t.id}</td>
          <td>${fmtPrice(t.entryPrice)}<div style="font-size:9.5px; color:var(--text-muted);">${fmtDate(t.entryTime)}</div></td>
          <td>${fmtPrice(t.exitPrice)}<div style="font-size:9.5px; color:var(--text-muted);">${t.exitTime ? fmtDate(t.exitTime) : '持倉中'}</div></td>
          <td class="${isWin ? 'text-bull' : 'text-bear'}" style="font-weight:700;">
            ${isWin ? '+' : ''}${t.returnPct.toFixed(2)}%
          </td>
          <td style="font-family:var(--font-sans); font-size:10.5px; color:var(--text-secondary);">
            <div>進：${t.entryReason}</div>
            <div style="color:var(--text-muted);">出：${t.exitReason}</div>
          </td>
        </tr>
      `;
    })
    .join('');

  drawEquityCurveCanvas();
}

/* ============================================================================
 * 高解析度 Canvas 繪圖系統 (主圖 + 副圖 + 淨值曲線)
 * ========================================================================== */
function setupHiDPICanvas(canvas) {
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(300, Math.floor(rect.width));
  const height = Math.max(110, Math.floor(rect.height));

  if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
    canvas.width = width * dpr;
    canvas.height = height * dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width, height };
}

function drawAllCanvases() {
  drawMainChart();
  drawSubChart();
  drawEquityCurveCanvas();
}

function drawMainChart() {
  const canvas = document.getElementById('main-chart-canvas');
  if (!canvas || state.candles.length === 0) return;
  const { ctx, width, height } = setupHiDPICanvas(canvas);

  ctx.clearRect(0, 0, width, height);

  const candles = state.candles;
  const n = candles.length;
  const forecastLen = state.layers.forecast ? state.forecast.points.length : 4;
  const totalSlots = n + forecastLen;

  const pad = { top: 22, right: 76, bottom: 26, left: 12 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  // 計算價格上下邊界
  let minPrice = Infinity;
  let maxPrice = -Infinity;
  for (const c of candles) {
    if (c.low < minPrice) minPrice = c.low;
    if (c.high > maxPrice) maxPrice = c.high;
  }
  if (state.layers.bb) {
    for (let i = 0; i < n; i++) {
      if (state.indicators.bb.lower[i]) minPrice = Math.min(minPrice, state.indicators.bb.lower[i]);
      if (state.indicators.bb.upper[i]) maxPrice = Math.max(maxPrice, state.indicators.bb.upper[i]);
    }
  }
  if (state.layers.forecast && state.forecast) {
    for (const fp of state.forecast.points) {
      minPrice = Math.min(minPrice, fp.lower95);
      maxPrice = Math.max(maxPrice, fp.upper95);
    }
  }

  const priceMargin = (maxPrice - minPrice) * 0.075;
  minPrice -= priceMargin;
  maxPrice += priceMargin;

  const xForIndex = (i) => pad.left + ((i + 0.5) / totalSlots) * plotW;
  const yForPrice = (p) => pad.top + ((maxPrice - p) / (maxPrice - minPrice || 1)) * plotH;

  // 1. 繪製背景價格網格與右側座標軸
  ctx.strokeStyle = 'rgba(28, 37, 54, 0.65)';
  ctx.lineWidth = 1;
  ctx.font = '11px "JetBrains Mono", monospace';
  ctx.fillStyle = '#64748b';

  const gridRows = 6;
  for (let r = 0; r <= gridRows; r++) {
    const y = pad.top + (r / gridRows) * plotH;
    const p = maxPrice - (r / gridRows) * (maxPrice - minPrice);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
    ctx.fillText('$' + Math.round(p).toLocaleString(), width - pad.right + 6, y + 4);
  }

  // 時間垂直網格線
  const timeStep = Math.max(15, Math.floor(n / 6));
  for (let i = 0; i < n; i += timeStep) {
    const x = xForIndex(i);
    ctx.beginPath();
    ctx.moveTo(x, pad.top);
    ctx.lineTo(x, height - pad.bottom);
    ctx.stroke();
    ctx.fillStyle = '#64748b';
    ctx.fillText(fmtDate(candles[i].timestamp), x - 28, height - 8);
  }

  // 2. 圖層：支撐與壓力價格帶 (Support & Resistance Zones)
  if (state.layers.sr) {
    for (const z of state.classical.srZones.allZones) {
      const yTop = yForPrice(z.upper);
      const yBot = yForPrice(z.lower);
      const yMid = yForPrice(z.price);
      const isRes = z.role === 'RESISTANCE';

      ctx.fillStyle = isRes ? 'rgba(244, 63, 94, 0.08)' : 'rgba(16, 185, 129, 0.08)';
      ctx.fillRect(pad.left, yTop, plotW, Math.max(2, yBot - yTop));

      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = isRes ? 'rgba(244, 63, 94, 0.55)' : 'rgba(16, 185, 129, 0.55)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.left, yMid);
      ctx.lineTo(width - pad.right, yMid);
      ctx.stroke();
      ctx.restore();

      ctx.fillStyle = isRes ? '#fb7185' : '#34d399';
      ctx.font = '600 10.5px "JetBrains Mono", monospace';
      ctx.fillText(`${z.code} $${z.price.toLocaleString()}`, pad.left + 6, yMid - 4);
    }
  }

  // 3. 圖層：斐波那契回撤點位 (Fibonacci Retracement)
  if (state.layers.fib) {
    const fib = state.classical.fibonacci;
    // 繪製 Golden Pocket (0.618 ~ 0.65) 高亮帶
    const gpTop = yForPrice(Math.max(fib.goldenPocket.upper, fib.goldenPocket.lower));
    const gpBot = yForPrice(Math.min(fib.goldenPocket.upper, fib.goldenPocket.lower));
    ctx.fillStyle = 'rgba(245, 158, 11, 0.11)';
    ctx.fillRect(pad.left, gpTop, plotW, Math.max(3, gpBot - gpTop));

    for (const lv of fib.levels) {
      if (lv.isExtension) continue; // 主圖保持清晰，僅畫 0.0 ~ 1.0 回撤線
      const y = yForPrice(lv.price);
      if (y < pad.top || y > height - pad.bottom) continue;

      ctx.save();
      ctx.strokeStyle = lv.isGolden ? 'rgba(245, 158, 11, 0.75)' : 'rgba(148, 163, 184, 0.28)';
      ctx.lineWidth = lv.isGolden ? 1.4 : 1;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(xForIndex(Math.min(fib.swingLow.index, fib.swingHigh.index)), y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      ctx.restore();

      ctx.fillStyle = lv.color;
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.fillText(`Fib ${lv.ratio.toFixed(3)} ($${lv.price.toLocaleString()})`, width - pad.right - 138, y - 3);
    }
  }

  // 4. 圖層：布林通道 (Bollinger Bands)
  if (state.layers.bb) {
    const { upper, lower } = state.indicators.bb;
    ctx.save();
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < n; i++) {
      if (upper[i] === null) continue;
      const x = xForIndex(i);
      const y = yForPrice(upper[i]);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
    }
    for (let i = n - 1; i >= 0; i--) {
      if (lower[i] === null) continue;
      ctx.lineTo(xForIndex(i), yForPrice(lower[i]));
    }
    ctx.closePath();
    ctx.fillStyle = 'rgba(168, 85, 247, 0.055)';
    ctx.fill();

    // 上下軌邊線
    const drawBandLine = (arr) => {
      ctx.beginPath();
      let st = false;
      for (let i = 0; i < n; i++) {
        if (arr[i] === null) continue;
        const x = xForIndex(i);
        const y = yForPrice(arr[i]);
        if (!st) {
          ctx.moveTo(x, y);
          st = true;
        } else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = 'rgba(168, 85, 247, 0.42)';
      ctx.lineWidth = 1;
      ctx.stroke();
    };
    drawBandLine(upper);
    drawBandLine(lower);
    ctx.restore();
  }

  // 5. 圖層：移動平均線 (SMA 20 & EMA 50)
  if (state.layers.ma) {
    const drawMA = (arr, color, widthPx) => {
      ctx.beginPath();
      let st = false;
      for (let i = 0; i < n; i++) {
        if (arr[i] === null) continue;
        const x = xForIndex(i);
        const y = yForPrice(arr[i]);
        if (!st) {
          ctx.moveTo(x, y);
          st = true;
        } else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = widthPx;
      ctx.stroke();
    };
    drawMA(state.indicators.sma20, '#38bdf8', 1.6);
    drawMA(state.indicators.ema50, '#f59e0b', 1.7);
  }

  // 6. 繪製 K 線實體與上下影線 (Candlesticks)
  const barSlotW = plotW / totalSlots;
  const candleW = Math.max(2.5, Math.min(11, barSlotW * 0.72));

  for (let i = 0; i < n; i++) {
    const c = candles[i];
    const x = xForIndex(i);
    const yOpen = yForPrice(c.open);
    const yClose = yForPrice(c.close);
    const yHigh = yForPrice(c.high);
    const yLow = yForPrice(c.low);
    const isBull = c.close >= c.open;
    const color = isBull ? '#10b981' : '#f43f5e';

    ctx.strokeStyle = color;
    ctx.lineWidth = 1.15;
    ctx.beginPath();
    ctx.moveTo(x, yHigh);
    ctx.lineTo(x, yLow);
    ctx.stroke();

    const topY = Math.min(yOpen, yClose);
    const bodyH = Math.max(1.5, Math.abs(yClose - yOpen));
    ctx.fillStyle = color;
    ctx.fillRect(x - candleW / 2, topY, candleW, bodyH);
  }

  // 7. 圖層：傳統圖表型態幾何連線與頸線 (Classical Chart Patterns)
  if (state.layers.patterns) {
    const chartPatterns = state.classical.patterns.chartPatterns;
    for (const pat of chartPatterns) {
      const patColor = pat.bias === 'BEARISH' ? '#fb7185' : '#fbbf24';

      // 繪製轉折折線 (例如 左肩 -> 頸線1 -> 頭部 -> 頸線2 -> 右肩)
      if (pat.points && pat.points.length >= 2) {
        ctx.save();
        ctx.strokeStyle = patColor;
        ctx.lineWidth = 2;
        ctx.beginPath();
        pat.points.forEach((pt, idx) => {
          const px = xForIndex(pt.index);
          const py = yForPrice(pt.price);
          if (idx === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        });
        ctx.stroke();

        // 繪製轉折圓點與標籤
        pat.points.forEach((pt) => {
          const px = xForIndex(pt.index);
          const py = yForPrice(pt.price);
          ctx.fillStyle = '#070a0f';
          ctx.beginPath();
          ctx.arc(px, py, 4.5, 0, Math.PI * 2);
          ctx.fill();
          ctx.strokeStyle = patColor;
          ctx.lineWidth = 2;
          ctx.stroke();

          ctx.fillStyle = '#f8fafc';
          ctx.font = '600 10.5px "Inter", sans-serif';
          const offsetY = py > height / 2 ? 15 : -9;
          ctx.fillText(pt.tag, px - 20, py + offsetY);
        });
        ctx.restore();
      }

      // 繪製頸線 (Neckline)
      if (pat.neckline) {
        ctx.save();
        ctx.setLineDash([6, 4]);
        ctx.strokeStyle = '#38bdf8';
        ctx.lineWidth = 1.6;
        const x1 = xForIndex(pat.neckline.startIndex);
        const y1 = yForPrice(pat.neckline.startPrice);
        const x2 = xForIndex(pat.neckline.endIndex);
        const y2 = yForPrice(pat.neckline.endPrice);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.fillStyle = '#38bdf8';
        ctx.font = '600 10.5px "Inter", sans-serif';
        ctx.fillText(`頸線 $${pat.necklinePrice.toLocaleString()}`, x2 - 85, y2 - 6);
        ctx.restore();
      }

      // 繪製收斂趨勢線 (Trendlines)
      if (pat.trendlines) {
        for (const tl of pat.trendlines) {
          ctx.save();
          ctx.strokeStyle = tl.color;
          ctx.lineWidth = 1.8;
          ctx.beginPath();
          ctx.moveTo(xForIndex(tl.startIndex), yForPrice(tl.startPrice));
          ctx.lineTo(xForIndex(tl.endIndex), yForPrice(tl.endPrice));
          ctx.stroke();
          ctx.restore();
        }
      }
    }
  }

  // 8. 圖層：買賣訊號標記 (BUY / SELL Markers)
  if (state.layers.signals) {
    const recentSigs = state.indicators.signals.slice(-10);
    for (const sig of recentSigs) {
      const x = xForIndex(sig.index);
      const isBuy = sig.type === 'BUY';
      const y = isBuy ? yForPrice(sig.price) + 14 : yForPrice(sig.price) - 14;

      ctx.save();
      ctx.fillStyle = isBuy ? '#10b981' : '#f43f5e';
      ctx.beginPath();
      if (isBuy) {
        ctx.moveTo(x, y - 8);
        ctx.lineTo(x - 6, y + 3);
        ctx.lineTo(x + 6, y + 3);
      } else {
        ctx.moveTo(x, y + 8);
        ctx.lineTo(x - 6, y - 3);
        ctx.lineTo(x + 6, y - 3);
      }
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }
  }

  // 9. 圖層：ML 時間序列價格預測扇形圖 (Forecast Cone)
  if (state.layers.forecast && state.forecast) {
    const pts = state.forecast.points;
    const lastCandle = candles[n - 1];
    const startX = xForIndex(n - 1);
    const startY = yForPrice(lastCandle.close);

    // 95% 信賴區間 (2σ)
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(startX, startY);
    for (const fp of pts) ctx.lineTo(xForIndex(fp.index), yForPrice(fp.upper95));
    for (let i = pts.length - 1; i >= 0; i--) {
      ctx.lineTo(xForIndex(pts[i].index), yForPrice(pts[i].lower95));
    }
    ctx.closePath();
    ctx.fillStyle = 'rgba(6, 182, 212, 0.08)';
    ctx.fill();

    // 68% 信賴區間 (1σ)
    ctx.beginPath();
    ctx.moveTo(startX, startY);
    for (const fp of pts) ctx.lineTo(xForIndex(fp.index), yForPrice(fp.upper68));
    for (let i = pts.length - 1; i >= 0; i--) {
      ctx.lineTo(xForIndex(pts[i].index), yForPrice(pts[i].lower68));
    }
    ctx.closePath();
    ctx.fillStyle = 'rgba(6, 182, 212, 0.17)';
    ctx.fill();

    // 預測中軸路徑
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = '#22d3ee';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(startX, startY);
    for (const fp of pts) {
      ctx.lineTo(xForIndex(fp.index), yForPrice(fp.mean));
    }
    ctx.stroke();

    // 終點目標標籤
    const endPt = pts[pts.length - 1];
    const endX = xForIndex(endPt.index);
    const endY = yForPrice(endPt.mean);
    ctx.fillStyle = '#22d3ee';
    ctx.beginPath();
    ctx.arc(endX, endY, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.font = '700 11px "JetBrains Mono", monospace';
    ctx.fillText(`預測 $${endPt.mean.toLocaleString()}`, endX - 68, endY - 8);
    ctx.restore();
  }

  // 10. 繪製最新價格水平線與右側高亮價格牌
  const lastC = candles[n - 1];
  const lastY = yForPrice(lastC.close);
  ctx.save();
  ctx.setLineDash([2, 2]);
  ctx.strokeStyle = lastC.close >= lastC.open ? 'rgba(16, 185, 129, 0.7)' : 'rgba(244, 63, 94, 0.7)';
  ctx.beginPath();
  ctx.moveTo(pad.left, lastY);
  ctx.lineTo(width - pad.right, lastY);
  ctx.stroke();

  ctx.fillStyle = lastC.close >= lastC.open ? '#10b981' : '#f43f5e';
  ctx.fillRect(width - pad.right + 2, lastY - 9, pad.right - 4, 18);
  ctx.fillStyle = '#070a0f';
  ctx.font = '700 11px "JetBrains Mono", monospace';
  ctx.fillText('$' + Math.round(lastC.close).toLocaleString(), width - pad.right + 6, lastY + 4);
  ctx.restore();

  // 11. 十字游標或定位高亮線 (Crosshair)
  const activeIdx = state.hoverIndex ?? state.highlightIndex;
  if (activeIdx !== null && activeIdx >= 0 && activeIdx < n) {
    const cx = xForIndex(activeIdx);
    const cy = yForPrice(candles[activeIdx].close);
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'rgba(241, 245, 249, 0.45)';
    ctx.beginPath();
    ctx.moveTo(cx, pad.top);
    ctx.lineTo(cx, height - pad.bottom);
    ctx.moveTo(pad.left, cy);
    ctx.lineTo(width - pad.right, cy);
    ctx.stroke();
    ctx.restore();
  }
}

function drawSubChart() {
  const canvas = document.getElementById('sub-chart-canvas');
  if (!canvas || state.candles.length === 0) return;
  const { ctx, width, height } = setupHiDPICanvas(canvas);
  ctx.clearRect(0, 0, width, height);

  const candles = state.candles;
  const n = candles.length;
  const forecastLen = state.layers.forecast ? state.forecast.points.length : 4;
  const totalSlots = n + forecastLen;

  const pad = { top: 12, right: 76, bottom: 18, left: 12 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const xForIndex = (i) => pad.left + ((i + 0.5) / totalSlots) * plotW;

  if (state.subChart === 'macd') {
    const { macdLine, signalLine, histogram } = state.indicators.macd;
    let maxAbs = 1;
    for (let i = 0; i < n; i++) {
      maxAbs = Math.max(maxAbs, Math.abs(macdLine[i]), Math.abs(signalLine[i]), Math.abs(histogram[i]));
    }
    maxAbs *= 1.15;
    const zeroY = pad.top + plotH / 2;
    const yForVal = (v) => zeroY - (v / maxAbs) * (plotH / 2);

    // 零軸
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.25)';
    ctx.beginPath();
    ctx.moveTo(pad.left, zeroY);
    ctx.lineTo(width - pad.right, zeroY);
    ctx.stroke();

    const barW = Math.max(2, (plotW / totalSlots) * 0.65);
    for (let i = 0; i < n; i++) {
      const h = histogram[i];
      const x = xForIndex(i);
      const y = yForVal(h);
      ctx.fillStyle = h >= 0 ? 'rgba(16, 185, 129, 0.65)' : 'rgba(244, 63, 94, 0.65)';
      ctx.fillRect(x - barW / 2, Math.min(zeroY, y), barW, Math.max(1, Math.abs(y - zeroY)));
    }

    const drawLine = (arr, color) => {
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const x = xForIndex(i);
        const y = yForVal(arr[i]);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.stroke();
    };
    drawLine(macdLine, '#38bdf8');
    drawLine(signalLine, '#f59e0b');
  } else if (state.subChart === 'rsi') {
    const rsi = state.indicators.rsi14;
    const yForRsi = (v) => pad.top + ((100 - v) / 100) * plotH;

    // 70 與 30 超買超賣區間背景
    const y70 = yForRsi(70);
    const y30 = yForRsi(30);
    ctx.fillStyle = 'rgba(56, 189, 248, 0.06)';
    ctx.fillRect(pad.left, y70, plotW, y30 - y70);

    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = 'rgba(244, 63, 94, 0.45)';
    ctx.beginPath();
    ctx.moveTo(pad.left, y70);
    ctx.lineTo(width - pad.right, y70);
    ctx.stroke();

    ctx.strokeStyle = 'rgba(16, 185, 129, 0.45)';
    ctx.beginPath();
    ctx.moveTo(pad.left, y30);
    ctx.lineTo(width - pad.right, y30);
    ctx.stroke();
    ctx.restore();

    ctx.fillStyle = '#64748b';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.fillText('70', width - pad.right + 6, y70 + 3);
    ctx.fillText('30', width - pad.right + 6, y30 + 3);

    ctx.beginPath();
    let st = false;
    for (let i = 0; i < n; i++) {
      if (rsi[i] === null) continue;
      const x = xForIndex(i);
      const y = yForRsi(rsi[i]);
      if (!st) {
        ctx.moveTo(x, y);
        st = true;
      } else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = '#a855f7';
    ctx.lineWidth = 1.7;
    ctx.stroke();
  } else {
    // Volume 成交量與均量線
    let maxVol = 1;
    for (const c of candles) maxVol = Math.max(maxVol, c.volume);
    maxVol *= 1.1;
    const yForVol = (v) => pad.top + plotH - (v / maxVol) * plotH;
    const barW = Math.max(2, (plotW / totalSlots) * 0.68);

    for (let i = 0; i < n; i++) {
      const c = candles[i];
      const x = xForIndex(i);
      const y = yForVol(c.volume);
      ctx.fillStyle = c.close >= c.open ? 'rgba(16, 185, 129, 0.55)' : 'rgba(244, 63, 94, 0.55)';
      ctx.fillRect(x - barW / 2, y, barW, pad.top + plotH - y);
    }

    const vma = state.indicators.volSma20;
    ctx.beginPath();
    let st = false;
    for (let i = 0; i < n; i++) {
      if (!vma[i]) continue;
      const x = xForIndex(i);
      const y = yForVol(vma[i]);
      if (!st) {
        ctx.moveTo(x, y);
        st = true;
      } else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = '#f59e0b';
    ctx.lineWidth = 1.4;
    ctx.stroke();
  }
}

function drawEquityCurveCanvas() {
  const canvas = document.getElementById('bt-equity-canvas');
  if (!canvas || !state.backtest) return;
  const { ctx, width, height } = setupHiDPICanvas(canvas);
  ctx.clearRect(0, 0, width, height);

  const curve = state.backtest.equityCurve;
  if (!curve || curve.length === 0) return;

  let minEq = Infinity;
  let maxEq = -Infinity;
  for (const pt of curve) {
    minEq = Math.min(minEq, pt.strategyEquity, pt.buyHoldEquity);
    maxEq = Math.max(maxEq, pt.strategyEquity, pt.buyHoldEquity);
  }
  const margin = (maxEq - minEq) * 0.1 || 500;
  minEq -= margin;
  maxEq += margin;

  const pad = { top: 10, right: 10, bottom: 16, left: 10 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const xFor = (i) => pad.left + (i / Math.max(1, curve.length - 1)) * plotW;
  const yFor = (v) => pad.top + ((maxEq - v) / (maxEq - minEq || 1)) * plotH;

  // Buy & Hold 基準線 (虛線)
  ctx.save();
  ctx.setLineDash([3, 3]);
  ctx.beginPath();
  curve.forEach((pt, i) => {
    const x = xFor(i);
    const y = yFor(pt.buyHoldEquity);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = '#64748b';
  ctx.lineWidth = 1.3;
  ctx.stroke();
  ctx.restore();

  // 策略淨值曲線
  ctx.beginPath();
  curve.forEach((pt, i) => {
    const x = xFor(i);
    const y = yFor(pt.strategyEquity);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = '#10b981';
  ctx.lineWidth = 2;
  ctx.stroke();

  // 圖例說明
  ctx.font = '10.5px "Inter", sans-serif';
  ctx.fillStyle = '#10b981';
  ctx.fillText('● 策略淨值', 12, 18);
  ctx.fillStyle = '#94a3b8';
  ctx.fillText('--- 買入持有 (Buy & Hold)', 84, 18);
}

/* ============================================================================
 * DOM 事件綁定與互動控制
 * ========================================================================== */
function bindEvents() {
  // 1. 情境切換
  document.getElementById('scenario-select').addEventListener('change', (e) => {
    state.scenario = e.target.value;
    loadScenarioData();
  });

  // 2. 隨機擾動樣本與抓取即時 API
  document.getElementById('btn-resimulate').addEventListener('click', () => {
    state.seedOffset += 17;
    loadScenarioData();
  });

  document.getElementById('btn-fetch-live').addEventListener('click', () => {
    handleFetchLive();
  });

  // 3. 時間週期切換
  document.querySelectorAll('#timeframe-group .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#timeframe-group .seg-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.timeframe = btn.dataset.tf;
      if (state.dataSource === 'LIVE_API') {
        handleFetchLive();
      } else {
        loadScenarioData();
      }
    });
  });

  // 4. 圖層疊加開關
  document.querySelectorAll('#overlay-group .layer-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      const layer = btn.dataset.layer;
      state.layers[layer] = !state.layers[layer];
      btn.classList.toggle('active', state.layers[layer]);
      drawMainChart();
      drawSubChart();
    });
  });

  // 5. 副圖切換
  document.querySelectorAll('#subchart-group .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#subchart-group .seg-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.subChart = btn.dataset.sub;
      const titleMap = {
        macd: 'MACD (12, 26, 9) — 動能柱與訊號線交叉',
        rsi: 'RSI (14) — 相對強弱指標 [70 超買 / 30 超賣]',
        volume: 'Volume — 多空成交量與 20 期均量線'
      };
      document.getElementById('subchart-title').textContent = titleMap[state.subChart];
      updateCrosshairBar(state.hoverIndex ?? state.candles.length - 1);
      drawSubChart();
    });
  });

  // 6. 右側分頁切換
  document.querySelectorAll('.inspector-tabs .tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.inspector-tabs .tab-btn').forEach((b) => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('active'));
      btn.classList.add('active');
      const panel = document.getElementById(btn.dataset.tab);
      if (panel) panel.classList.add('active');
      if (btn.dataset.tab === 'tab-backtest') {
        setTimeout(drawEquityCurveCanvas, 30);
      }
    });
  });

  // 7. 預測參數調整
  document.getElementById('forecast-horizon').addEventListener('change', (e) => {
    state.forecastOptions.horizon = Number(e.target.value);
    state.forecast = runPriceForecast(state.candles, state.indicators, state.forecastOptions);
    updateTopHeaderAndCards();
    renderForecastTab();
    drawMainChart();
    drawSubChart();
  });

  document.getElementById('forecast-mode').addEventListener('change', (e) => {
    state.forecastOptions.mode = e.target.value;
    state.forecast = runPriceForecast(state.candles, state.indicators, state.forecastOptions);
    updateTopHeaderAndCards();
    renderForecastTab();
    drawMainChart();
  });

  // 8. 策略回測執行
  const triggerBacktest = () => {
    state.backtestConfig = {
      strategyId: document.getElementById('bt-strategy').value,
      initialCapital: Number(document.getElementById('bt-capital').value) || 10000,
      stopLossPct: Number(document.getElementById('bt-sl').value) || 5.5,
      takeProfitPct: Number(document.getElementById('bt-tp').value) || 14.0
    };
    state.backtest = runStrategyBacktest(
      state.candles,
      state.indicators,
      state.classical,
      state.backtestConfig
    );
    renderBacktestTab();
  };

  document.getElementById('btn-run-backtest').addEventListener('click', triggerBacktest);
  document.getElementById('bt-strategy').addEventListener('change', triggerBacktest);

  // 9. 點擊訊號定位 K 線
  document.addEventListener('click', (e) => {
    const item = e.target.closest('[data-jump-index]');
    if (item) {
      const idx = Number(item.dataset.jumpIndex);
      state.highlightIndex = idx;
      updateCrosshairBar(idx);
      drawMainChart();
    }
  });

  // 10. 主圖滑鼠十字游標互動
  const mainCanvas = document.getElementById('main-chart-canvas');
  mainCanvas.addEventListener('mousemove', (e) => {
    const rect = mainCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const padLeft = 12;
    const padRight = 76;
    const plotW = rect.width - padLeft - padRight;
    const forecastLen = state.layers.forecast ? state.forecast.points.length : 4;
    const totalSlots = state.candles.length + forecastLen;
    const rawIdx = Math.floor(((x - padLeft) / plotW) * totalSlots);

    if (rawIdx >= 0 && rawIdx < state.candles.length) {
      state.hoverIndex = rawIdx;
      updateCrosshairBar(rawIdx);
      drawMainChart();
    }
  });

  mainCanvas.addEventListener('mouseleave', () => {
    state.hoverIndex = null;
    updateCrosshairBar(state.candles.length - 1);
    drawMainChart();
  });

  window.addEventListener('resize', () => {
    drawAllCanvases();
  });
}

// 啟動應用程式
window.addEventListener('DOMContentLoaded', () => {
  bindEvents();
  loadScenarioData();
});
