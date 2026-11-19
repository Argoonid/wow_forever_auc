import {
  Bus, Store, api, calcDepositClient, coinsToCopper, copyTextFeedback,
  formatAxisPrice, formatDayAxisTick, formatLocalDateTime, getItemMeta,
  iconUrl, renderCoins, setCoinsToInputs,
} from './core.js';
import { upgradeCustomSelects } from './ui.js';

function baseChartOptions() {
  return {
    localization: { locale: 'ru-RU', timeFormatter: formatLocalDateTime, priceFormatter: formatAxisPrice },
    layout: { background: { color: '#181a1f' }, textColor: '#9ca3af', fontSize: 12 },
    grid: { vertLines: { color: '#22252c' }, horzLines: { color: '#22252c' } },
    crosshair: {
      vertLine: { color: '#4b5563', width: 1, style: 2 },
      horzLine: { color: '#4b5563', width: 1, style: 2 },
    },
    rightPriceScale: { borderColor: '#2c303a', autoScale: true, scaleMargins: { top: 0.16, bottom: 0.16 } },
    timeScale: { borderColor: '#2c303a', timeVisible: true, secondsVisible: false, rightOffset: 6, tickMarkFormatter: formatDayAxisTick },
  };
}

/** Изолированный движок графика: используется и в основном окне, и во всплывающем окне поверх портфеля/радара. */
class ChartEngine {
  constructor({ hostEl, stageEl, overlayEl, tooltipEl, topBarEl, dipSummaryEl }) {
    this.hostEl = hostEl;
    this.stageEl = stageEl;
    this.overlayEl = overlayEl;
    this.tooltipEl = tooltipEl;
    this.topBarEl = topBarEl;
    this.dipSummaryEl = dipSummaryEl;

    this.chart = null;
    this.anchorSeries = null;
    this.forecastSeries = null;
    this.priceSeriesRef = null;
    this.sessionSeries = [];

    this.gaps = [];
    this.drawings = [];
    this.tradeEvents = [];
    this.exactMap = new Map();
    this.forecastMap = new Map();
    this.sessionRanges = [];
    this.defaultTopHtml = '';
    this.latestPriceCopper = 0;
  }

  mount() {
    this.destroy();
    this.hostEl.innerHTML = '';
    this.chart = LightweightCharts.createChart(this.hostEl, {
      ...baseChartOptions(),
      width: this.stageEl.clientWidth || 600,
      height: this.stageEl.clientHeight || 360,
    });
    this.anchorSeries = this.chart.addLineSeries({ visible: false, lastValueVisible: false, priceLineVisible: false });
    this.forecastSeries = this.chart.addLineSeries({
      color: '#eab308', lineWidth: 1, lineStyle: 3, pointMarkersVisible: true, pointMarkersRadius: 4,
      lastValueVisible: true, priceLineVisible: false, title: 'Прогноз 24ч',
    });
    this.chart.subscribeCrosshairMove((p) => this._onCrosshair(p));
    this.chart.timeScale().subscribeVisibleTimeRangeChange(() => this.renderOverlay());
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(() => this.renderOverlay());
    this.resize();
  }

  destroy() {
    if (this.chart) {
      this.chart.remove();
      this.chart = null;
    }
    this.anchorSeries = null;
    this.forecastSeries = null;
    this.priceSeriesRef = null;
    this.sessionSeries = [];
  }

  resize() {
    if (!this.stageEl || !this.overlayEl) return;
    const w = this.stageEl.clientWidth;
    const h = this.stageEl.clientHeight;
    if (w <= 0 || h <= 0) return;
    this.overlayEl.width = w;
    this.overlayEl.height = h;
    if (this.chart) this.chart.applyOptions({ width: w, height: h });
    this.renderOverlay();
  }

  getRefSeries() {
    return this.priceSeriesRef || this.forecastSeries;
  }

  async load(itemId, days = 0) {
    if (!this.chart) this.mount();
    const [data, drawings] = await Promise.all([
      api.get(`/api/history/${itemId}?days=${days}`),
      api.get(`/api/drawings/${itemId}`),
    ]);

    this.drawings = drawings || [];
    this.gaps = data.gaps || [];
    this.tradeEvents = data.trade_events || [];
    this.exactMap.clear();
    this.forecastMap.clear();
    this.sessionRanges = [];

    this.sessionSeries.forEach((s) => this.chart.removeSeries(s));
    this.sessionSeries = [];
    this.priceSeriesRef = null;

    this.anchorSeries.setData((data.full_timeline || []).map((ts) => ({ time: ts })));

    const sessions = data.sessions || [];
    let latestPoint = null;

    sessions.forEach((sess, idx) => {
      const isLast = idx === sessions.length - 1;
      const mktByTime = new Map((sess.market_line || []).map((m) => [m.time, m.value]));
      const mergedTicks = [];

      (sess.line || []).forEach((pt) => {
        const entry = { time: pt.time, price: pt.value, market: mktByTime.get(pt.time) || 0 };
        this.exactMap.set(pt.time, entry);
        mergedTicks.push(entry);
        latestPoint = entry;
      });

      if (mergedTicks.length > 0) {
        this.sessionRanges.push({
          start: mergedTicks[0].time,
          end: mergedTicks[mergedTicks.length - 1].time,
          ticks: mergedTicks,
        });
      }

      if (sess.market_line && sess.market_line.length > 0) {
        const mktLine = this.chart.addLineSeries({
          color: '#6b7280', lineWidth: 1, lineStyle: 2,
          lastValueVisible: isLast, priceLineVisible: false, title: isLast ? 'Рынок TSM' : '',
        });
        mktLine.setData(sess.market_line);
        this.sessionSeries.push(mktLine);
      }

      const priceLine = this.chart.addLineSeries({
        color: '#38bdf8', lineWidth: 2, pointMarkersVisible: true, pointMarkersRadius: 3.5,
        lastValueVisible: isLast, priceLineVisible: isLast, title: isLast ? 'Цена' : '',
      });
      priceLine.setData(sess.line || []);
      if (idx > 0 && sess.line?.length > 0) {
        priceLine.setMarkers([{ time: sess.line[0].time, position: 'aboveBar', color: '#6b7280', shape: 'arrowDown', text: 'СЕССИЯ' }]);
      }
      this.sessionSeries.push(priceLine);
      this.priceSeriesRef = priceLine;
    });

    const meta = getItemMeta(itemId);
    this.latestPriceCopper = latestPoint
      ? Math.round(latestPoint.price * 100)
      : (meta?.effective_price || meta?.market_value || 0);

    if (latestPoint) {
      const mktPart = latestPoint.market > 0 ? `<span class="top-metric">Рынок TSM: ${renderCoins(Math.round(latestPoint.market * 100))}</span>` : '';
      this.defaultTopHtml = `
        <span class="top-metric"><span class="tt-dot" style="background:#38bdf8;"></span>Текущая: ${renderCoins(Math.round(latestPoint.price * 100))}</span>
        ${mktPart}
        <span class="top-metric">Срез: <b>${formatLocalDateTime(latestPoint.time)}</b></span>
      `;
    } else {
      this.defaultTopHtml = `<span class="top-metric">Нет данных за выбранные сутки</span>`;
    }
    if (this.topBarEl) this.topBarEl.innerHTML = this.defaultTopHtml;

    const forecasts = data.forecast_dips || [];
    forecasts.forEach((d) => this.forecastMap.set(d.time, { price: d.value, isDip: d.is_dip }));
    this.forecastSeries.setData(forecasts.map((d) => ({ time: d.time, value: d.value })));
    this.forecastSeries.setMarkers(
      forecasts.filter((d) => d.is_dip).map((d) => ({ time: d.time, position: 'belowBar', color: '#eab308', shape: 'circle', text: 'ДНО' }))
    );

    if (this.dipSummaryEl) {
      const lowest = forecasts.reduce((min, p) => (!min || p.value < min.value ? p : min), null);
      if (lowest) {
        const hh = String(new Date(lowest.time * 1000).getHours()).padStart(2, '0');
        this.dipSummaryEl.innerHTML = `Ожидаемое дно (~${hh}:00): ${renderCoins(Math.round(lowest.value * 100))}`;
      } else {
        this.dipSummaryEl.innerHTML = `<span style="color:#6b7280;">Мало срезов для прогноза (&lt;3)</span>`;
      }
    }

    this.chart.timeScale().fitContent();
    setTimeout(() => this.resize(), 50);
    return this.latestPriceCopper;
  }

  _resolvePoint(unixSec) {
    if (this.exactMap.has(unixSec)) return { kind: 'exact', ...this.exactMap.get(unixSec) };
    if (this.forecastMap.has(unixSec)) return { kind: 'forecast', ...this.forecastMap.get(unixSec) };
    for (const g of this.gaps) {
      if (unixSec > g.start_time && unixSec < g.end_time) return { kind: 'gap', hours: g.hours };
    }
    for (const sess of this.sessionRanges) {
      if (unixSec >= sess.start && unixSec <= sess.end) {
        let active = sess.ticks[0];
        for (const tk of sess.ticks) {
          if (tk.time <= unixSec) active = tk; else break;
        }
        return { kind: 'segment', ...active };
      }
    }
    return null;
  }

  _onCrosshair(param) {
    if (!param?.point || param.time === undefined || param.point.x < 0 || param.point.y < 0) {
      if (this.tooltipEl) this.tooltipEl.style.display = 'none';
      if (this.topBarEl && this.defaultTopHtml) this.topBarEl.innerHTML = this.defaultTopHtml;
      return;
    }
    const unixSec = Number(param.time);
    const info = this._resolvePoint(unixSec);
    if (!info) {
      if (this.tooltipEl) this.tooltipEl.style.display = 'none';
      return;
    }

    const timeStr = formatLocalDateTime(unixSec);
    let ttHtml = `<div class="tt-time">${timeStr}</div>`;
    let topHtml = `<span class="top-metric">Время: <b>${timeStr}</b></span>`;

    if (info.kind === 'exact' || info.kind === 'segment') {
      const pCoins = renderCoins(Math.round(info.price * 100));
      ttHtml += `<div class="tt-row"><span class="tt-dot" style="background:#38bdf8;"></span><span>Цена:</span>${pCoins}</div>`;
      topHtml += `<span class="top-metric"><span class="tt-dot" style="background:#38bdf8;"></span>Цена: ${pCoins}</span>`;
      if (info.market > 0) {
        const mCoins = renderCoins(Math.round(info.market * 100));
        ttHtml += `<div class="tt-row"><span class="tt-dot" style="background:#6b7280;"></span><span>Рынок TSM:</span>${mCoins}</div>`;
        topHtml += `<span class="top-metric">Рынок TSM: ${mCoins}</span>`;
      }
    } else if (info.kind === 'forecast') {
      const fCoins = renderCoins(Math.round(info.price * 100));
      const badge = info.isDip ? ` <span style="color:#facc15; font-weight:600;">(Окно дна)</span>` : '';
      ttHtml += `<div class="tt-row"><span class="tt-dot" style="background:#eab308;"></span><span>Прогноз:</span>${fCoins}${badge}</div>`;
      topHtml += `<span class="top-metric"><span class="tt-dot" style="background:#eab308;"></span>Прогноз: ${fCoins}${badge}</span>`;
    } else if (info.kind === 'gap') {
      ttHtml += `<div class="tt-row" style="color:#9ca3af;">Ночной разрыв (${info.hours}ч)</div>`;
      topHtml += `<span class="top-metric" style="color:#9ca3af;">Ночной разрыв (${info.hours}ч)</span>`;
    }

    if (this.topBarEl) this.topBarEl.innerHTML = topHtml;
    if (this.tooltipEl) {
      this.tooltipEl.innerHTML = ttHtml;
      this.tooltipEl.style.display = 'block';
      const clampedX = Math.max(95, Math.min(this.stageEl.clientWidth - 140, param.point.x));
      const topPos = param.point.y > 78 ? (param.point.y - 12) : (param.point.y + 68);
      this.tooltipEl.style.left = `${clampedX}px`;
      this.tooltipEl.style.top = `${topPos}px`;
    }
  }

  renderOverlay() {
    const refSeries = this.getRefSeries();
    if (!this.overlayEl || !this.chart || !refSeries) return;
    const ctx = this.overlayEl.getContext('2d');
    ctx.clearRect(0, 0, this.overlayEl.width, this.overlayEl.height);

    const toXY = (pt) => {
      const x = this.chart.timeScale().timeToCoordinate(pt.time);
      const y = refSeries.priceToCoordinate(pt.price);
      return (x === null || y === null) ? null : { x, y };
    };

    const plotH = ctx.canvas.height - 28;
    const maxRight = ctx.canvas.width - 65;

    this.gaps.forEach((g) => {
      const x1 = this.chart.timeScale().timeToCoordinate(g.start_time);
      const x2 = this.chart.timeScale().timeToCoordinate(g.end_time);
      if (x1 === null || x2 === null || x2 <= x1 + 2) return;
      const left = Math.max(0, x1);
      const width = Math.min(maxRight, x2) - left;
      if (width <= 2) return;
      ctx.save();
      ctx.fillStyle = 'rgba(10,11,14,0.62)';
      ctx.fillRect(left, 0, width, plotH);
      ctx.strokeStyle = 'rgba(107,114,128,0.45)';
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(left, 0); ctx.lineTo(left, plotH);
      ctx.moveTo(left + width, 0); ctx.lineTo(left + width, plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#9ca3af';
      ctx.font = '10px Inter, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(width >= 46 ? `ГАП ${g.hours}ч` : `${g.hours}ч`, left + width / 2, 14);
      ctx.restore();
    });

    this.drawings.forEach((d) => {
      const pts = d.points || [];
      if (!pts.length) return;
      const c = toXY(pts[0]);
      if (!c) return;
      ctx.save();
      if (d.tool_type === 'hline') {
        ctx.strokeStyle = '#eab308';
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(0, c.y); ctx.lineTo(maxRight, c.y); ctx.stroke();
        ctx.fillStyle = '#eab308';
        ctx.font = '11px Inter, sans-serif';
        ctx.fillText(`Уровень: ${formatAxisPrice(pts[0].price)}`, 10, c.y - 4);
      } else if (d.tool_type === 'note' && d.note_text) {
        ctx.fillStyle = '#facc15';
        ctx.beginPath(); ctx.arc(c.x, c.y, 4, 0, Math.PI * 2); ctx.fill();
        ctx.font = '11px Inter, sans-serif';
        const w = ctx.measureText(d.note_text).width + 16;
        const bx = Math.min(ctx.canvas.width - w - 70, Math.max(6, c.x - w / 2));
        const by = Math.max(6, c.y - 28);
        ctx.fillStyle = 'rgba(32,35,42,0.95)';
        ctx.fillRect(bx, by, w, 20);
        ctx.strokeStyle = '#eab308';
        ctx.strokeRect(bx, by, w, 20);
        ctx.fillStyle = '#fef08a';
        ctx.fillText(d.note_text, bx + 8, by + 14);
      }
      ctx.restore();
    });

    this.tradeEvents.forEach((ev) => {
      const c = toXY({ time: ev.time, price: ev.price_silver });
      if (!c) return;
      ctx.save();
      const isBuy = ev.type === 'buy';
      const isSold = ev.type === 'sold';
      const color = isBuy ? '#22c55e' : (isSold ? '#38bdf8' : '#eab308');
      const label = isBuy
        ? `КУПЛЕНО ${formatAxisPrice(ev.price_silver)} ×${ev.qty}`
        : (isSold ? `ПРОДАНО ${formatAxisPrice(ev.price_silver)} ×${ev.qty}` : `ЛОТ ${formatAxisPrice(ev.price_silver)} ×${ev.qty}`);
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(c.x, c.y, 5, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = '#111215'; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.font = 'bold 10px Inter, sans-serif';
      const tw = ctx.measureText(label).width + 10;
      const by = isBuy ? c.y + 8 : c.y - 24;
      ctx.fillStyle = 'rgba(17,18,21,0.9)'; ctx.fillRect(c.x - tw / 2, by, tw, 17);
      ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.strokeRect(c.x - tw / 2, by, tw, 17);
      ctx.fillStyle = color; ctx.textAlign = 'center'; ctx.fillText(label, c.x, by + 12);
      ctx.restore();
    });
  }
}

/* === ОСНОВНОЙ ГРАФИК + БЫСТРАЯ ПОКУПКА + ВСПЛЫВАЮЩЕЕ ОКНО === */

let mainEngine = null;
let popupEngine = null;
let mainDaysRange = 0;
let popupDaysRange = 0;
let popupItemId = null;
let popupResizeObs = null;

let activeTool = 'none';
let pendingNotePoint = null;

function ensurePopupDom() {
  if (document.getElementById('chartPopupBackdrop')) return;
  const el = document.createElement('div');
  el.id = 'chartPopupBackdrop';
  el.className = 'chart-popup-backdrop';
  el.innerHTML = `
    <div class="chart-popup-card" role="dialog" aria-modal="true">
      <div class="chart-popup-header">
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <img id="popupChartIcon" class="wow-icon" src="" style="width:26px; height:26px;" onerror="window.__iconFallback(this)" />
          <h3 id="popupChartTitle" style="margin:0; font-size:15.5px; color:#f3f4f6;">—</h3>
          <button id="popupCopyBtn" class="small-btn">Копировать</button>
          <button id="popupWatchBtn" class="small-btn" style="border-color:#eab308; color:#facc15;">★ В радар</button>
          <button id="popupJumpBtn" class="small-btn">В рынок ↗</button>
        </div>
        <div style="display:flex; align-items:center; gap:12px;">
          <div id="popupTopPriceBar" style="display:flex; align-items:center; gap:12px; font-size:12.5px; color:#cbd5e1;"></div>
          <button id="popupCloseBtn" class="small-btn" style="padding:3px 9px;">✕</button>
        </div>
      </div>
      <div class="chart-popup-subbar">
        <div style="display:flex; align-items:center; gap:6px;">
          <span style="font-size:12px; color:#8b92a1;">Таймлайн:</span>
          <button class="small-btn popup-day-btn" data-pdays="1">1 сутки</button>
          <button class="small-btn popup-day-btn" data-pdays="3">3 суток</button>
          <button class="small-btn popup-day-btn" data-pdays="7">7 суток</button>
          <button class="small-btn popup-day-btn active" data-pdays="0">Всё время</button>
        </div>
        <div id="popupDipSummary" style="font-size:12px; color:#facc15;"></div>
      </div>
      <div class="chart-popup-stage" id="popupChartStage">
        <div id="popupChartCanvasHost"></div>
        <canvas id="popupDrawOverlay"></canvas>
        <div id="popupCursorTooltip"></div>
      </div>
    </div>
  `;
  el.addEventListener('click', (e) => { if (e.target === el) closePopupChart(); });
  document.body.appendChild(el);

  el.querySelector('#popupCloseBtn').addEventListener('click', closePopupChart);
  el.querySelector('#popupCopyBtn').addEventListener('click', (e) => {
    const meta = getItemMeta(popupItemId);
    if (meta?.name) copyTextFeedback(meta.name, e.currentTarget);
  });
  el.querySelector('#popupWatchBtn').addEventListener('click', () => {
    if (popupItemId) Bus.emit('watchlist:openModal', { itemId: popupItemId });
  });
  el.querySelector('#popupJumpBtn').addEventListener('click', () => {
    if (!popupItemId) return;
    const id = popupItemId;
    closePopupChart();
    Bus.emit('view:switch', 'market');
    Bus.emit('item:select', { itemId: id });
  });
  el.querySelectorAll('.popup-day-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      popupDaysRange = Number(btn.dataset.pdays);
      el.querySelectorAll('.popup-day-btn').forEach((b) => b.classList.toggle('active', b === btn));
      if (popupItemId && popupEngine) popupEngine.load(popupItemId, popupDaysRange);
    });
  });
}

function ensureMainChartWatchButton() {
  if (document.getElementById('btnChartToWatchlist')) return;
  const copyBtn = document.getElementById('btnCopyTitle');
  if (!copyBtn || !copyBtn.parentNode) return;
  const btn = document.createElement('button');
  btn.id = 'btnChartToWatchlist';
  btn.className = 'small-btn';
  btn.style.cssText = 'border-color:#eab308; color:#facc15;';
  btn.textContent = '★ В радар';
  btn.addEventListener('click', () => {
    if (Store.selectedItemId) Bus.emit('watchlist:openModal', { itemId: Store.selectedItemId });
  });
  copyBtn.insertAdjacentElement('afterend', btn);
}

export function refreshChartWatchButtonLabel() {
  const btn = document.getElementById('btnChartToWatchlist');
  if (!btn || !Store.selectedItemId) return;
  const wl = Store.watchlistById.get(Store.selectedItemId);
  btn.textContent = wl ? '★ На радаре (изм.)' : '★ В радар';
}

export async function loadMainChart(itemId) {
  const meta = getItemMeta(itemId);
  Store.selectedItemId = itemId;
  Store.selectedItemName = meta?.name || `Item_${itemId}`;
  Store.selectedVendorCopper = meta?.vendor_price || 0;

  const titleEl = document.getElementById('chartTitle');
  const iconEl = document.getElementById('chartIcon');
  if (titleEl) titleEl.innerText = Store.selectedItemName;
  if (iconEl) {
    iconEl.dataset.fbStep = '0';
    const rawIcon = meta?.icon || 'inv_misc_questionmark';
    const slug = rawIcon.startsWith('http') ? rawIcon.split('/').pop().replace('.jpg', '').toLowerCase() : rawIcon.toLowerCase();
    iconEl.dataset.altCdn = `https://render.worldofwarcraft.com/eu/icons/56/${slug}.jpg`;
    iconEl.onerror = () => window.__iconFallback(iconEl);
    iconEl.src = iconUrl(rawIcon);
  }

  refreshChartWatchButtonLabel();
  const latestCopper = await mainEngine.load(itemId, mainDaysRange);
  Store.selectedLatestPriceCopper = latestCopper;

  const qtBox = document.getElementById('quickTradeBox');
  if (qtBox && qtBox.style.display === 'block') prefillQuickTrade();
}

export async function openPopupChart(itemId) {
  ensurePopupDom();
  const meta = getItemMeta(itemId);
  popupItemId = itemId;
  popupDaysRange = 0;

  const backdrop = document.getElementById('chartPopupBackdrop');
  backdrop.querySelector('#popupChartTitle').innerText = meta?.name || `Item_${itemId}`;
  const iconEl = backdrop.querySelector('#popupChartIcon');
  const rawIcon = meta?.icon || 'inv_misc_questionmark';
  const slug = rawIcon.startsWith('http') ? rawIcon.split('/').pop().replace('.jpg', '').toLowerCase() : rawIcon.toLowerCase();
  iconEl.dataset.fbStep = '0';
  iconEl.dataset.altCdn = `https://render.worldofwarcraft.com/eu/icons/56/${slug}.jpg`;
  iconEl.src = iconUrl(rawIcon);

  backdrop.querySelectorAll('.popup-day-btn').forEach((b) => b.classList.toggle('active', b.dataset.pdays === '0'));
  backdrop.style.display = 'flex';

  const stage = document.getElementById('popupChartStage');
  if (!popupEngine) {
    popupEngine = new ChartEngine({
      hostEl: document.getElementById('popupChartCanvasHost'),
      stageEl: stage,
      overlayEl: document.getElementById('popupDrawOverlay'),
      tooltipEl: document.getElementById('popupCursorTooltip'),
      topBarEl: document.getElementById('popupTopPriceBar'),
      dipSummaryEl: document.getElementById('popupDipSummary'),
    });
  }
  popupEngine.mount();
  popupResizeObs?.disconnect();
  popupResizeObs = new ResizeObserver(() => popupEngine?.resize());
  popupResizeObs.observe(stage);

  await popupEngine.load(itemId, 0);
}

export function closePopupChart() {
  const backdrop = document.getElementById('chartPopupBackdrop');
  if (backdrop) backdrop.style.display = 'none';
  popupResizeObs?.disconnect();
  popupEngine?.destroy();
}

export function setMainDaysRange(days) {
  mainDaysRange = days;
  document.querySelectorAll('.day-btn').forEach((b) => b.classList.remove('active'));
  document.getElementById(`days-${days}`)?.classList.add('active');
  if (Store.selectedItemId) loadMainChart(Store.selectedItemId);
}

export function selectDrawTool(tool) {
  activeTool = tool;
  pendingNotePoint = null;
  document.getElementById('notePromptBar').style.display = 'none';
  document.querySelectorAll('.toolbar button[id^="tool-"]').forEach((b) => b.classList.remove('active'));
  document.getElementById(`tool-${tool}`)?.classList.add('active');
  const hints = {
    none: '',
    hline: 'Кликните на график, чтобы зафиксировать горизонтальный ценовой уровень',
    note: 'Кликните на точку графика, к которой хотите привязать текстовый комментарий',
  };
  document.getElementById('toolHintBar').innerText = hints[tool] || '';
  document.getElementById('drawOverlay').classList.toggle('drawing-active', tool !== 'none');
  mainEngine?.renderOverlay();
}

export async function confirmChartNote() {
  if (!pendingNotePoint || !Store.selectedItemId) return;
  const text = (document.getElementById('noteInputText').value || '').trim();
  if (!text) return;
  const payload = { item_id: Store.selectedItemId, tool_type: 'note', points: [pendingNotePoint], note_text: text };
  await api.post('/api/drawings', payload);
  mainEngine.drawings.push(payload);
  pendingNotePoint = null;
  document.getElementById('notePromptBar').style.display = 'none';
  selectDrawTool('none');
}

export function cancelChartNote() {
  pendingNotePoint = null;
  document.getElementById('notePromptBar').style.display = 'none';
  selectDrawTool('none');
}

export async function clearAllDrawings() {
  if (!Store.selectedItemId) return;
  await api.del(`/api/drawings/${Store.selectedItemId}`);
  mainEngine.drawings = [];
  mainEngine.renderOverlay();
}

export function prefillQuickTrade() {
  setCoinsToInputs('qtBuyG', 'qtBuyS', 'qtBuyC', Store.selectedLatestPriceCopper);
  setCoinsToInputs('qtListG', 'qtListS', 'qtListC', 0);
  updateQuickTradePreview();
}

export function toggleQuickTradeForm() {
  const box = document.getElementById('quickTradeBox');
  const opening = box.style.display === 'none' || box.style.display === '';
  box.style.display = opening ? 'block' : 'none';
  if (opening) {
    prefillQuickTrade();
    upgradeCustomSelects(box);
    const qtyEl = document.getElementById('qtQty');
    if (qtyEl) { qtyEl.focus(); qtyEl.select(); }
  }
}

export function updateQuickTradePreview() {
  const qty = Math.max(1, parseInt(document.getElementById('qtQty')?.value || '1', 10));
  const buyUnit = coinsToCopper('qtBuyG', 'qtBuyS', 'qtBuyC');
  const listUnit = coinsToCopper('qtListG', 'qtListS', 'qtListC');
  const hours = parseInt(document.getElementById('qtHours')?.value || '8', 10);
  const el = document.getElementById('qtMathPreview');
  if (!el) return;
  if (listUnit > 0) {
    const dep = calcDepositClient(Store.selectedVendorCopper, qty, hours);
    const fee = Math.floor(listUnit * qty * 0.05);
    const net = (listUnit * qty) - fee - (buyUnit * qty);
    el.innerHTML = `Залог (${hours}ч): ${renderCoins(dep)} • Комиссия 5%: ${renderCoins(fee)} • Ожид. профит: ${renderCoins(net, true)}`;
  } else {
    el.innerHTML = `Будет записано в сумку (на руках): ${renderCoins(buyUnit * qty)}`;
  }
}

export async function submitQuickTrade() {
  if (!Store.selectedItemId) return;
  const qty = Math.max(1, parseInt(document.getElementById('qtQty').value || '1', 10));
  const buyUnit = coinsToCopper('qtBuyG', 'qtBuyS', 'qtBuyC');
  const listUnit = coinsToCopper('qtListG', 'qtListS', 'qtListC');
  const hours = parseInt(document.getElementById('qtHours').value || '8', 10);
  const comment = document.getElementById('qtComment').value || '';
  if (buyUnit <= 0) { alert('Укажите цену покупки.'); return; }

  await api.post('/api/portfolio/buy', {
    item_id: Store.selectedItemId, qty, buy_price_copper: buyUnit, list_price_copper: listUnit, deposit_hours: hours, comment,
  });
  document.getElementById('quickTradeBox').style.display = 'none';
  document.getElementById('qtComment').value = '';
  Bus.emit('portfolio:refresh');
  await loadMainChart(Store.selectedItemId);
}

export function initChartsModule() {
  ensureMainChartWatchButton();
  mainEngine = new ChartEngine({
    hostEl: document.getElementById('chart'),
    stageEl: document.querySelector('.chart-wrapper'),
    overlayEl: document.getElementById('drawOverlay'),
    tooltipEl: document.getElementById('cursorTooltip'),
    topBarEl: document.getElementById('topPriceBar'),
    dipSummaryEl: document.getElementById('dipSummary'),
  });
  mainEngine.mount();

  window.addEventListener('resize', () => mainEngine?.resize());

  document.getElementById('drawOverlay').addEventListener('click', async (e) => {
    if (activeTool === 'none' || !Store.selectedItemId) return;
    const rect = e.target.getBoundingClientRect();
    const time = mainEngine.chart.timeScale().coordinateToTime(e.clientX - rect.left);
    const price = mainEngine.getRefSeries().coordinateToPrice(e.clientY - rect.top);
    if (time === null || price === null) return;
    const pt = { time: Number(time), price: Number(price.toFixed(2)) };

    if (activeTool === 'hline') {
      const payload = { item_id: Store.selectedItemId, tool_type: 'hline', points: [pt], note_text: '' };
      await api.post('/api/drawings', payload);
      mainEngine.drawings.push(payload);
      selectDrawTool('none');
    } else if (activeTool === 'note') {
      pendingNotePoint = pt;
      document.getElementById('notePromptCoords').innerText = `${formatLocalDateTime(pt.time)} • ${formatAxisPrice(pt.price)}:`;
      document.getElementById('notePromptBar').style.display = 'flex';
      document.getElementById('noteInputText').value = '';
      document.getElementById('noteInputText').focus();
    }
  });

  Bus.on('item:select', ({ itemId }) => loadMainChart(itemId));
  Bus.on('chart:popup', ({ itemId }) => openPopupChart(itemId));
  Bus.on('chart:resize', () => mainEngine?.resize());
  Bus.on('watchlist:updated', refreshChartWatchButtonLabel);
  Bus.on('ui:escape', () => {
    const pop = document.getElementById('chartPopupBackdrop');
    if (pop && pop.style.display === 'flex') closePopupChart();
  });
}