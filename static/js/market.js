import {
  BUCKET_META, Bus, DEFAULT_MIN_PRICE_COPPER, Store, api,
  copyTextFeedback, escapeHtml, formatLocalDateTime, renderCoins, renderIconImg,
} from './core.js';

let acMatches = [];
let acIndex = -1;
let acContext = null;

function highlightSub(text, q) {
  const safe = escapeHtml(text);
  if (!q) return safe;
  const idx = text.toLowerCase().indexOf(q.toLowerCase());
  if (idx === -1) return safe;
  return `${escapeHtml(text.slice(0, idx))}<span class="ac-highlight">${escapeHtml(text.slice(idx, idx + q.length))}</span>${escapeHtml(text.slice(idx + q.length))}`;
}

export function closeAllAutocomplete() {
  const s = document.getElementById('sidebarSearchDropdown');
  const p = document.getElementById('pfItemDropdown');
  if (s) s.style.display = 'none';
  if (p) p.style.display = 'none';
  acContext = null;
  acIndex = -1;
  acMatches = [];
}

export function renderAutocomplete(dropdownEl, query, context) {
  const q = query.trim().toLowerCase();
  const matches = Store.items.filter((it) => !q || it.name.toLowerCase().includes(q)).slice(0, 25);
  acContext = context;
  acMatches = matches;
  acIndex = matches.length ? 0 : -1;

  if (!matches.length) {
    dropdownEl.innerHTML = `<div class="ac-empty">Ничего не найдено по запросу «${escapeHtml(query)}»</div>`;
    dropdownEl.style.display = 'block';
    return;
  }

  dropdownEl.innerHTML = matches.map((it, idx) => {
    const p = it.effective_price || it.market_value || 0;
    return `
      <div class="ac-item ${idx === 0 ? 'ac-active' : ''}" data-ac-idx="${idx}" data-ac-ctx="${context}">
        <div class="ac-left">
          ${renderIconImg(it.icon)}
          <span class="ac-name">${highlightSub(it.name, q)}</span>
        </div>
        <div class="ac-meta">
          ${p > 0 ? renderCoins(p) : '—'}
          <span>${it.velocity_score}/д</span>
        </div>
      </div>
    `;
  }).join('');

  dropdownEl.querySelectorAll('.ac-item').forEach((row) => {
    row.addEventListener('mousedown', (e) => {
      e.preventDefault();
      pickAc(Number(row.dataset.acIdx), row.dataset.acCtx);
    });
  });
  dropdownEl.style.display = 'block';
}

function pickAc(idx, context) {
  const it = acMatches[idx];
  if (!it) return;
  closeAllAutocomplete();
  if (context === 'sidebar') {
    document.getElementById('itemSearchInput').value = it.name;
    document.getElementById('btnClearSidebarSearch').style.display = 'block';
    Bus.emit('item:select', { itemId: it.item_id });
    renderItemList();
  } else if (context === 'portfolio') {
    Bus.emit('portfolio:pickItem', it);
  }
}

export function handleSearchKeydown(e, context) {
  const dd = document.getElementById(context === 'sidebar' ? 'sidebarSearchDropdown' : 'pfItemDropdown');
  if (!dd || dd.style.display === 'none' || !acMatches.length) return;
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    acIndex = (acIndex + 1) % acMatches.length;
    updateAcHighlight(dd);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    acIndex = (acIndex - 1 + acMatches.length) % acMatches.length;
    updateAcHighlight(dd);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (acIndex >= 0) pickAc(acIndex, context);
  } else if (e.key === 'Escape') {
    closeAllAutocomplete();
  }
}

function updateAcHighlight(dd) {
  dd.querySelectorAll('.ac-item').forEach((row, i) => {
    row.classList.toggle('ac-active', i === acIndex);
    if (i === acIndex) row.scrollIntoView({ block: 'nearest' });
  });
}

function injectMinPriceFilterControl() {
  if (document.getElementById('minPriceFilterPill')) return;
  const sortEl = document.getElementById('itemSortSelect');
  const anchor = sortEl?.closest('.cselect-wrap') || sortEl;
  if (!anchor || !anchor.parentNode) return;

  const pill = document.createElement('div');
  pill.id = 'minPriceFilterPill';
  pill.className = 'sidebar-filter-pill';
  pill.innerHTML = `
    <span>Мин. цена (отсев хлама):</span>
    <span style="display:inline-flex; align-items:center; gap:4px;">
      <input id="minPriceCopperInput" type="number" min="0" value="${Store.minPriceCopper}" />
      <span class="coin coin-copper"></span>
    </span>
  `;
  anchor.parentNode.insertBefore(pill, anchor.nextSibling);

  let timer = null;
  pill.querySelector('#minPriceCopperInput').addEventListener('input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      Store.minPriceCopper = Math.max(0, parseInt(e.target.value || '0', 10));
      await loadItems();
      await loadOpportunities();
    }, 300);
  });
}

export async function loadItems() {
  const rows = await api.get(`/api/items?min_price=${Store.minPriceCopper}`);
  Store.items = rows;
  Store.itemsById.clear();
  rows.forEach((r) => Store.itemsById.set(r.item_id, r));
  renderItemList();
  Bus.emit('items:loaded', rows);
  return rows;
}

export function renderItemList() {
  const query = (document.getElementById('itemSearchInput')?.value || '').trim().toLowerCase();
  const sortMode = document.getElementById('itemSortSelect')?.value || 'velocity';

  const hiddenCount = Store.items.filter((i) => i.is_hidden).length;
  const hiddenBtn = document.getElementById('btnToggleHidden');
  if (hiddenBtn) {
    hiddenBtn.innerText = Store.showHiddenItems ? `Скрыть обратно (${hiddenCount})` : `Скрытые: ${hiddenCount}`;
    hiddenBtn.classList.toggle('active', Store.showHiddenItems);
  }

  const filtered = Store.items.filter((it) => {
    if (!query && !Store.showHiddenItems && it.is_hidden) return false;
    if (query && !it.name.toLowerCase().includes(query)) return false;
    return true;
  });

  filtered.sort((a, b) => {
    if (a.is_active_pool !== b.is_active_pool) return b.is_active_pool - a.is_active_pool;
    if (sortMode === 'price') return (b.effective_price || 0) - (a.effective_price || 0);
    if (sortMode === 'name') return a.name.localeCompare(b.name, 'ru');
    return b.velocity_score - a.velocity_score;
  });

  const listEl = document.getElementById('itemList');
  if (!listEl) return;
  listEl.innerHTML = filtered.map((it) => {
    const wl = Store.watchlistById.get(it.item_id);
    const wlDot = wl ? `<span title="${BUCKET_META[wl.bucket]?.label || ''}" style="color:${BUCKET_META[wl.bucket]?.color || '#facc15'}; font-size:11px;">★</span>` : '';
    const sel = it.item_id === Store.selectedItemId ? ' selected' : '';
    const hid = it.is_hidden ? ' hidden-item' : '';
    const priceTag = it.effective_price > 0 ? renderCoins(it.effective_price) : '';

    return `
      <div class="item-row${sel}${hid}" data-item-id="${it.item_id}">
        <div class="item-left">
          ${renderIconImg(it.icon)}
          <span class="item-name ${it.is_active_pool ? 'active-pool-text' : ''}">${wlDot} ${escapeHtml(it.name)}</span>
        </div>
        <div class="item-right" style="gap:6px;">
          <span style="font-size:11px; color:#cbd5e1;">${priceTag}</span>
          <span style="color:#8b92a1; font-size:11px;">${it.velocity_score}/д</span>
          <button class="hide-item-btn" data-hide-id="${it.item_id}" title="${it.is_hidden ? 'Вернуть' : 'Скрыть'}">${it.is_hidden ? '👁' : '✕'}</button>
        </div>
      </div>
    `;
  }).join('');
}

export function onSidebarSearchInput() {
  const val = document.getElementById('itemSearchInput').value || '';
  document.getElementById('btnClearSidebarSearch').style.display = val ? 'block' : 'none';
  renderItemList();
  const dd = document.getElementById('sidebarSearchDropdown');
  if (val.trim()) renderAutocomplete(dd, val, 'sidebar');
  else dd.style.display = 'none';
}

export function clearSidebarSearch() {
  document.getElementById('itemSearchInput').value = '';
  document.getElementById('btnClearSidebarSearch').style.display = 'none';
  closeAllAutocomplete();
  renderItemList();
}

export function toggleShowHidden() {
  Store.showHiddenItems = !Store.showHiddenItems;
  renderItemList();
}

export async function loadOpportunities() {
  const ops = await api.get(`/api/opportunities?min_price=${Store.minPriceCopper}`);
  const tbody = document.getElementById('opsBody');
  if (!tbody) return;
  if (!ops.length) {
    tbody.innerHTML = `<tr><td colspan="9" style="color:var(--text-muted); text-align:center;">Сейчас цены близки к медиане — ждём вечернего слива (17:00–22:00)</td></tr>`;
    return;
  }
  tbody.innerHTML = ops.map((o) => `
    <tr>
      <td><span class="tag">${escapeHtml(o.tag)}</span></td>
      <td>
        <div class="pf-item-cell">
          <div class="pf-item-trigger" data-op-open="${o.item_id}" title="Открыть график">
            ${renderIconImg(o.icon)}
            <b>${escapeHtml(o.name)}</b>
          </div>
          <button class="pf-copy-btn" data-copy-text="${escapeHtml(o.name)}" title="Скопировать">❐</button>
          <button class="pf-copy-btn" data-wl-add="${o.item_id}" title="Отправить в радар" style="color:#facc15;">★</button>
        </div>
      </td>
      <td style="color:#9ca3af; font-size:12px;">${escapeHtml(o.reason)}</td>
      <td>${renderCoins(o.buy)}</td>
      <td>${renderCoins(o.sell)}</td>
      <td style="color:#4ade80; font-weight:600;">+${o.roi}%</td>
      <td>${o.qty} шт.</td>
      <td><b>${renderCoins(o.total_profit)}</b></td>
      <td>${o.daily_sales}</td>
    </tr>
  `).join('');
}

export async function loadCallHistory() {
  const data = await api.get('/api/calls/history?limit=40');
  const s = data.summary;
  const statsEl = document.getElementById('callHistoryStats');
  if (statsEl) {
    statsEl.innerHTML = `
      Всего коллов: <b style="color:#e4e6eb;">${s.total}</b> •
      Цель взята: <b style="color:#4ade80;">${s.hits}</b> •
      Отскок: <b style="color:#38bdf8;">${s.partials}</b> •
      Ожидание: <b style="color:#facc15;">${s.pending}</b> •
      Точность (Hit Rate): <b style="color:#4ade80;">${s.hit_rate}%</b>
    `;
  }
  const tbody = document.getElementById('callHistoryBody');
  if (!tbody) return;
  if (!data.items?.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="color:var(--text-muted); text-align:center;">История коллов начнёт заполняться с текущего среза</td></tr>`;
    return;
  }
  const statusMap = {
    hit: `<span class="tag tag-sold">ЦЕЛЬ ВЗЯТА ✓</span>`,
    partial: `<span class="tag tag-holding">ОТСКОК В ПЛЮС</span>`,
    pending: `<span class="tag">ОЖИДАНИЕ ⏳</span>`,
    missed: `<span class="tag tag-vendored">НЕ ОТСКОЧИЛ ✕</span>`,
  };
  tbody.innerHTML = data.items.map((c) => {
    const col = c.actual_roi > 0 ? '#4ade80' : (c.actual_roi < 0 ? '#f87171' : '#9ca3af');
    return `
      <tr>
        <td style="color:#9ca3af; font-size:12px;">${formatLocalDateTime(c.called_at)}</td>
        <td>${statusMap[c.status] || escapeHtml(c.status)}</td>
        <td>
          <div class="pf-item-cell">
            ${renderIconImg(c.icon)}
            <b class="copy-name" data-copy-text="${escapeHtml(c.name)}">${escapeHtml(c.name)}</b>
          </div>
        </td>
        <td><span style="font-size:11.5px; color:#cbd5e1;">${escapeHtml(c.tag)}</span></td>
        <td>${renderCoins(c.buy_price)}</td>
        <td>${renderCoins(c.predicted_sell)}</td>
        <td>${c.max_observed > 0 ? renderCoins(c.max_observed) : '—'}</td>
        <td><span style="color:#9ca3af;">+${c.predicted_roi}%</span> / <b style="color:${col};">${c.actual_roi > 0 ? '+' : ''}${c.actual_roi}%</b></td>
      </tr>
    `;
  }).join('');
}

export async function copyAiDigest() {
  const btn = document.getElementById('btnCopyAiDigest');
  const data = await api.get('/api/calls/digest');
  await copyTextFeedback(data.digest, btn);
}

export function initMarketModule() {
  Store.minPriceCopper = DEFAULT_MIN_PRICE_COPPER;
  injectMinPriceFilterControl();

  // Делегирование кликов по списку предметов
  document.getElementById('itemList')?.addEventListener('click', async (e) => {
    const hideBtn = e.target.closest('[data-hide-id]');
    if (hideBtn) {
      e.stopPropagation();
      const id = Number(hideBtn.dataset.hideId);
      const res = await api.post(`/api/items/${id}/toggle-hide`);
      const target = Store.itemsById.get(id);
      if (target) target.is_hidden = res.is_hidden;
      renderItemList();
      return;
    }
    const row = e.target.closest('[data-item-id]');
    if (row) {
      const id = Number(row.dataset.itemId);
      Bus.emit('item:select', { itemId: id });
      renderItemList();
    }
  });

  // Делегирование кликов по таблице коллов и истории
  const marketView = document.getElementById('viewMarket');
  marketView?.addEventListener('click', (e) => {
    const copyEl = e.target.closest('[data-copy-text]');
    if (copyEl) {
      e.stopPropagation();
      copyTextFeedback(copyEl.dataset.copyText, copyEl);
      return;
    }
    const wlBtn = e.target.closest('[data-wl-add]');
    if (wlBtn) {
      e.stopPropagation();
      Bus.emit('watchlist:openModal', { itemId: Number(wlBtn.dataset.wlAdd) });
      return;
    }
    const openEl = e.target.closest('[data-op-open]');
    if (openEl) {
      Bus.emit('item:select', { itemId: Number(openEl.dataset.opOpen) });
      renderItemList();
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.search-wrap') && !e.target.closest('.pf-combobox-wrap')) {
      closeAllAutocomplete();
    }
  });

  Bus.on('item:select', () => renderItemList());
  Bus.on('watchlist:updated', () => renderItemList());
}