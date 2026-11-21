export const DEFAULT_MIN_PRICE_COPPER = 80;

const QUESTIONMARK_ICON = 'https://wow.zamimg.com/images/wow/icons/medium/inv_misc_questionmark.jpg';

export const BUCKET_META = {
  fast_flip:   { label: '⚡ Быстрая спекуляция', short: 'Флип',   color: '#38bdf8', bg: 'rgba(56,189,248,0.14)' },
  long_invest: { label: '🏦 Долгий инвест',      short: 'Инвест', color: '#a78bfa', bg: 'rgba(167,139,250,0.16)' },
  watch:       { label: '👁 Наблюдение',         short: 'Радар',  color: '#facc15', bg: 'rgba(250,204,21,0.14)' },
};

export const Store = {
  items: [],
  itemsById: new Map(),
  watchlist: [],
  watchlistById: new Map(),
  portfolioMeta: new Map(),

  minPriceCopper: DEFAULT_MIN_PRICE_COPPER,
  showHiddenItems: false,
  selectedItemId: null,
  selectedItemName: '',
  selectedVendorCopper: 0,
  selectedLatestPriceCopper: 0,
};

export const Bus = {
  _listeners: new Map(),
  on(event, fn) {
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event).add(fn);
    return () => this._listeners.get(event)?.delete(fn);
  },
  emit(event, payload) {
    const set = this._listeners.get(event);
    if (!set) return;
    set.forEach((fn) => {
      try { fn(payload); } catch (e) { console.error(`[Bus:${event}]`, e); }
    });
  },
};

export const api = {
  async get(url) {
    const res = await fetch(url);
    return res.json();
  },
  async post(url, body = {}) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return res.json();
  },
  async del(url) {
    const res = await fetch(url, { method: 'DELETE' });
    return res.json();
  },
};

export function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function extractIconSlug(rawIcon) {
  const clean = String(rawIcon || '').trim();
  if (!clean) return 'inv_misc_questionmark';
  const withoutQuery = clean.split(/[?#]/)[0];
  const filePart = withoutQuery.split('/').pop() || '';
  const slug = filePart.replace(/\.(jpg|jpeg|png|webp)$/i, '').toLowerCase();
  return slug || 'inv_misc_questionmark';
}

export function iconUrl(rawIcon) {
  const slug = extractIconSlug(rawIcon);
  return `https://wow.zamimg.com/images/wow/icons/medium/${slug}.jpg`;
}

export function normalizeIconName(iconName) {
  if (!iconName) return 'inv_misc_questionmark';
  const cleaned = String(iconName).trim().split('?')[0].replace(/\/+$/, '');
  const base = cleaned.includes('/') ? cleaned.slice(cleaned.lastIndexOf('/') + 1) : cleaned;
  return base.replace(/\.(jpg|jpeg|png)$/i, '').toLowerCase() || 'inv_misc_questionmark';
}

export function renderIconImg(iconName, cls = 'wow-icon') {
  const safe = normalizeIconName(iconName);
  return `<img class="${cls}" src="/api/icon/${encodeURIComponent(safe)}.jpg" loading="lazy" decoding="async" onerror="this.onerror=null;this.src='${QUESTIONMARK_ICON}'" alt="" />`;
}


// Трёхуровневый фолбэк: Wowhead CDN -> Blizzard Render CDN -> вопросительный знак
window.__iconFallback = function (img) {
  const step = Number(img.dataset.fbStep || '0');
  const altCdn = img.dataset.altCdn || '';
  if (step === 0 && altCdn && img.src !== altCdn) {
    img.dataset.fbStep = '1';
    img.src = altCdn;
  } else {
    img.onerror = null;
    img.src = QUESTIONMARK_ICON;
  }
};

export function coinsToCopper(gId, sId, cId) {
  const g = Math.max(0, parseInt(document.getElementById(gId)?.value || '0', 10));
  const s = Math.max(0, parseInt(document.getElementById(sId)?.value || '0', 10));
  const c = Math.max(0, parseInt(document.getElementById(cId)?.value || '0', 10));
  return g * 10000 + s * 100 + c;
}

export function setCoinsToInputs(gId, sId, cId, totalCopper) {
  const total = Math.max(0, Math.round(totalCopper || 0));
  const gEl = document.getElementById(gId);
  const sEl = document.getElementById(sId);
  const cEl = document.getElementById(cId);
  if (gEl) gEl.value = Math.floor(total / 10000);
  if (sEl) sEl.value = Math.floor((total % 10000) / 100);
  if (cEl) cEl.value = total % 100;
}

export function calcDepositClient(vendorUnitCopper, qty, hours) {
  if (!vendorUnitCopper || vendorUnitCopper <= 0 || qty <= 0) return 0;
  const rates = { 2: 0.15, 8: 0.30, 24: 0.60 };
  return Math.max(1, Math.floor(vendorUnitCopper * qty * (rates[hours] || 0.30)));
}

export function renderCoins(totalCopper, signed = false) {
  const sign = signed && totalCopper > 0 ? '+' : (totalCopper < 0 ? '-' : '');
  const c = Math.max(0, Math.round(Math.abs(totalCopper || 0)));
  const gold = Math.floor(c / 10000);
  const silver = Math.floor((c % 10000) / 100);
  const copper = c % 100;
  const parts = [];
  if (gold > 0) parts.push(`<span class="coin-part">${gold}<span class="coin coin-gold"></span></span>`);
  if (silver > 0 || gold > 0) {
    const sVal = gold > 0 ? String(silver).padStart(2, '0') : silver;
    parts.push(`<span class="coin-part">${sVal}<span class="coin coin-silver"></span></span>`);
  }
  const cVal = (silver > 0 || gold > 0) ? String(copper).padStart(2, '0') : copper;
  parts.push(`<span class="coin-part">${cVal}<span class="coin coin-copper"></span></span>`);
  const colorStyle = signed ? (totalCopper > 0 ? 'color:#4ade80;' : (totalCopper < 0 ? 'color:#f87171;' : '')) : '';
  return `<span class="money-wrap" style="${colorStyle}">${sign}${parts.join('')}</span>`;
}

export function formatAxisPrice(silverVal) {
  const totalCopper = Math.max(0, Math.round(silverVal * 100));
  const g = Math.floor(totalCopper / 10000);
  const s = Math.floor((totalCopper % 10000) / 100);
  const c = totalCopper % 100;
  if (g > 0) return `${g}g ${String(s).padStart(2, '0')}s`;
  if (s > 0) return `${s}s ${String(c).padStart(2, '0')}c`;
  return `${c}c`;
}

export function formatLocalDateTime(unixSec) {
  return new Date(unixSec * 1000).toLocaleString('ru-RU', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

export function formatDayAxisTick(unixSec, tickMarkType) {
  const d = new Date(unixSec * 1000);
  const dayMonth = d.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' });
  if (tickMarkType <= 2) return dayMonth;
  const hh = String(d.getHours()).padStart(2, '0');
  return (hh === '00' || hh === '12') ? `${dayMonth} ${hh}:00` : `${hh}:00`;
}

export async function copyTextFeedback(text, btnEl) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    if (btnEl) {
      const orig = btnEl.dataset.origText || btnEl.innerText;
      btnEl.dataset.origText = orig;
      const isCompact = btnEl.classList.contains('pf-copy-btn');
      btnEl.innerText = isCompact ? '✓' : '✓ Скопировано';
      btnEl.classList.add('copied');
      clearTimeout(btnEl._copyTimer);
      btnEl._copyTimer = setTimeout(() => {
        btnEl.innerText = orig;
        btnEl.classList.remove('copied');
      }, 1100);
    }
  } catch (_) {}
}

export function getItemMeta(itemId) {
  return Store.itemsById.get(itemId) || Store.portfolioMeta.get(itemId) || Store.watchlistById.get(itemId) || null;
}