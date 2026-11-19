import {
  BUCKET_META, Bus, Store, api, coinsToCopper, copyTextFeedback,
  escapeHtml, getItemMeta, renderCoins, renderIconImg,
} from './core.js';
import { closeActionModal, openActionModal } from './ui.js';

let activeBucketFilter = 'all';

function injectWatchlistTabAndView() {
  if (!document.getElementById('tabBtnWatchlist')) {
    const pfTab = document.getElementById('tabBtnPortfolio');
    if (pfTab && pfTab.parentNode) {
      const btn = document.createElement('button');
      btn.id = 'tabBtnWatchlist';
      btn.className = pfTab.className.replace(/\bactive\b/g, '').trim();
      btn.innerHTML = `Радар / Инвест <span id="watchlistBadge" class="badge" style="margin-left:4px;">0</span>`;
      btn.addEventListener('click', () => Bus.emit('view:switch', 'watchlist'));
      pfTab.insertAdjacentElement('afterend', btn);
    }
  }

  if (!document.getElementById('viewWatchlist')) {
    const pfView = document.getElementById('viewPortfolio');
    if (pfView && pfView.parentNode) {
      const sec = document.createElement('section');
      sec.id = 'viewWatchlist';
      sec.style.display = 'none';
      sec.innerHTML = `
        <div class="wl-wrap">
          <div class="wl-toolbar">
            <div class="wl-tabs" id="wlFilterTabs">
              <button class="wl-pill-btn active" data-wl-filter="all">Все идеи</button>
              <button class="wl-pill-btn" data-wl-filter="fast_flip">⚡ Быстрая спекуляция</button>
              <button class="wl-pill-btn" data-wl-filter="long_invest">🏦 Долгий инвест</button>
              <button class="wl-pill-btn" data-wl-filter="watch">👁 Наблюдение</button>
            </div>
            <div style="font-size:12px; color:#9ca3af;">Клик по предмету открывает график • Зелёная метка — цена дошла до целевого входа</div>
          </div>
          <div class="panel" style="padding:0; overflow:hidden;">
            <table class="data-table" style="width:100%;">
              <thead>
                <tr>
                  <th>КАТЕГОРИЯ</th>
                  <th>ПРЕДМЕТ</th>
                  <th>ТЕКУЩАЯ ЦЕНА</th>
                  <th>ДОБАВЛЕН ПО</th>
                  <th>ДИНАМИКА (Δ)</th>
                  <th>ЦЕЛЬ ВХОДА / ВЫХОДА</th>
                  <th>ОБЪЁМ (АН)</th>
                  <th>ЗАМЕТКА</th>
                  <th>ДЕЙСТВИЯ</th>
                </tr>
              </thead>
              <tbody id="wlTableBody"></tbody>
            </table>
          </div>
        </div>
      `;
      pfView.insertAdjacentElement('afterend', sec);

      sec.querySelector('#wlFilterTabs').addEventListener('click', (e) => {
        const b = e.target.closest('[data-wl-filter]');
        if (!b) return;
        activeBucketFilter = b.dataset.wlFilter;
        sec.querySelectorAll('.wl-pill-btn').forEach((x) => x.classList.toggle('active', x === b));
        renderWatchlistTable();
      });

      sec.querySelector('#wlTableBody').addEventListener('click', async (e) => {
        const copyBtn = e.target.closest('[data-wl-copy]');
        if (copyBtn) {
          e.stopPropagation();
          copyTextFeedback(copyBtn.dataset.wlCopy, copyBtn);
          return;
        }
        const popEl = e.target.closest('[data-wl-popup]');
        if (popEl) {
          Bus.emit('chart:popup', { itemId: Number(popEl.dataset.wlPopup) });
          return;
        }
        const editBtn = e.target.closest('[data-wl-edit]');
        if (editBtn) {
          openWatchlistModal(Number(editBtn.dataset.wlEdit));
          return;
        }
        const moveBtn = e.target.closest('[data-wl-move]');
        if (moveBtn) {
          const id = Number(moveBtn.dataset.wlId);
          const item = Store.watchlistById.get(id);
          if (!item) return;
          await api.post('/api/watchlist', {
            item_id: id,
            bucket: moveBtn.dataset.wlMove,
            target_buy_copper: item.target_buy,
            target_sell_copper: item.target_sell,
            note: item.note,
          });
          await loadWatchlist();
          return;
        }
        const delBtn = e.target.closest('[data-wl-del]');
        if (delBtn) {
          await api.del(`/api/watchlist/${Number(delBtn.dataset.wlDel)}`);
          await loadWatchlist();
        }
      });
    }
  }
}

export function openWatchlistModal(itemId) {
  const meta = getItemMeta(itemId);
  const existing = Store.watchlistById.get(itemId);
  let chosenBucket = existing?.bucket || 'fast_flip';

  const currP = existing?.current_price || meta?.effective_price || Store.selectedLatestPriceCopper || 0;
  const tb = existing ? existing.target_buy : Math.round(currP * 0.85);
  const ts = existing ? existing.target_sell : Math.round(currP * 1.25);

  const split = (val) => [Math.floor(val / 10000), Math.floor((val % 10000) / 100), val % 100];
  const [bg, bs, bc] = split(tb);
  const [sg, ss, sc] = split(ts);

  const body = openActionModal(
    `Отслеживание: ${meta?.name || `Item_${itemId}`}`,
    `
      <div style="font-size:12px; color:#9ca3af; margin-bottom:8px;">Куда отправить предмет:</div>
      <div class="wl-bucket-choices" id="wlBucketPicker">
        ${Object.entries(BUCKET_META).map(([key, info]) => `
          <button type="button" class="wl-bucket-choice ${key === chosenBucket ? 'active' : ''}" data-bucket="${key}">${info.label}</button>
        `).join('')}
      </div>
      <div style="margin-bottom:10px; font-size:12px; color:var(--text-muted); display:flex; align-items:center; justify-content:space-between; gap:8px;">
        <span>Цель входа (купить ≤):</span>
        <div class="coin-capsule">
          <label class="coin-cell"><input id="wlBuyG" type="number" min="0" value="${bg}" /><span class="coin coin-gold"></span></label>
          <span class="coin-sep"></span>
          <label class="coin-cell"><input id="wlBuyS" type="number" min="0" max="99" value="${bs}" /><span class="coin coin-silver"></span></label>
          <span class="coin-sep"></span>
          <label class="coin-cell"><input id="wlBuyC" type="number" min="0" max="99" value="${bc}" /><span class="coin coin-copper"></span></label>
        </div>
      </div>
      <div style="margin-bottom:10px; font-size:12px; color:var(--text-muted); display:flex; align-items:center; justify-content:space-between; gap:8px;">
        <span>Цель выхода (продать ≥):</span>
        <div class="coin-capsule">
          <label class="coin-cell"><input id="wlSellG" type="number" min="0" value="${sg}" /><span class="coin coin-gold"></span></label>
          <span class="coin-sep"></span>
          <label class="coin-cell"><input id="wlSellS" type="number" min="0" max="99" value="${ss}" /><span class="coin coin-silver"></span></label>
          <span class="coin-sep"></span>
          <label class="coin-cell"><input id="wlSellC" type="number" min="0" max="99" value="${sc}" /><span class="coin coin-copper"></span></label>
        </div>
      </div>
      <div style="margin-bottom:14px;">
        <input id="wlNoteInput" type="text" placeholder="Заметка (например: копить под 9 декабря или флип вечером)..." value="${escapeHtml(existing?.note || '')}"
          style="width:100%; box-sizing:border-box; padding:7px 10px; background:#111318; border:1px solid #2e3340; border-radius:6px; color:#e4e6eb; font-size:12.5px;" />
      </div>
      <div style="display:flex; gap:8px; justify-content:space-between;">
        ${existing ? `<button id="wlRemoveBtn" class="danger">Убрать с радара</button>` : '<span></span>'}
        <div style="display:flex; gap:8px;">
          <button id="wlCancelBtn">Отмена</button>
          <button id="wlSaveBtn" class="btn-success">Сохранить</button>
        </div>
      </div>
    `
  );

  body.querySelector('#wlBucketPicker').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-bucket]');
    if (!btn) return;
    chosenBucket = btn.dataset.bucket;
    body.querySelectorAll('.wl-bucket-choice').forEach((b) => b.classList.toggle('active', b === btn));
  });

  body.querySelector('#wlCancelBtn').addEventListener('click', closeActionModal);
  body.querySelector('#wlRemoveBtn')?.addEventListener('click', async () => {
    await api.del(`/api/watchlist/${itemId}`);
    closeActionModal();
    await loadWatchlist();
  });
  body.querySelector('#wlSaveBtn').addEventListener('click', async () => {
    await api.post('/api/watchlist', {
      item_id: itemId,
      bucket: chosenBucket,
      target_buy_copper: coinsToCopper('wlBuyG', 'wlBuyS', 'wlBuyC'),
      target_sell_copper: coinsToCopper('wlSellG', 'wlSellS', 'wlSellC'),
      note: body.querySelector('#wlNoteInput').value || '',
    });
    closeActionModal();
    await loadWatchlist();
  });
}

function renderWatchlistTable() {
  const tbody = document.getElementById('wlTableBody');
  if (!tbody) return;
  const rows = activeBucketFilter === 'all'
    ? Store.watchlist
    : Store.watchlist.filter((r) => r.bucket === activeBucketFilter);

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="9" style="color:var(--text-muted); text-align:center; padding:26px;">Пока пусто — нажми «★ В радар» на графике или рядом с коллом, чтобы отслеживать позицию</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map((r) => {
    const bInfo = BUCKET_META[r.bucket] || BUCKET_META.watch;
    const deltaCol = r.delta_pct > 0 ? '#4ade80' : (r.delta_pct < 0 ? '#f87171' : '#9ca3af');
    const alertBadge = r.in_buy_zone
      ? `<span class="wl-alert-buy">🎯 ЗОНА ВХОДА</span>`
      : (r.in_sell_zone ? `<span class="wl-alert-sell">💰 ЦЕЛЬ ВЫХОДА</span>` : '');
    const altBucket = r.bucket === 'long_invest' ? 'fast_flip' : 'long_invest';
    const altLabel = altBucket === 'long_invest' ? '→ Инвест' : '→ Флип';

    return `
      <tr>
        <td><span class="wl-badge-bucket" style="background:${bInfo.bg}; color:${bInfo.color};">${bInfo.label}</span></td>
        <td>
          <div class="pf-item-cell">
            <div class="pf-item-trigger" data-wl-popup="${r.item_id}" title="Открыть график поверх">
              ${renderIconImg(r.icon)}
              <b>${escapeHtml(r.name)}</b>
            </div>
            <button class="pf-copy-btn" data-wl-copy="${escapeHtml(r.name)}" title="Скопировать">❐</button>
          </div>
        </td>
        <td>${renderCoins(r.current_price)}${alertBadge}</td>
        <td style="color:#9ca3af;">${r.added_price > 0 ? renderCoins(r.added_price) : '—'}</td>
        <td><b style="color:${deltaCol};">${r.delta_pct > 0 ? '+' : ''}${r.delta_pct}%</b></td>
        <td>
          <span style="color:#4ade80;">${r.target_buy > 0 ? renderCoins(r.target_buy) : '—'}</span>
          <span style="color:#6b7280;"> / </span>
          <span style="color:#38bdf8;">${r.target_sell > 0 ? renderCoins(r.target_sell) : '—'}</span>
        </td>
        <td style="color:#cbd5e1;">${r.current_qty} шт.</td>
        <td style="color:#9ca3af; max-width:200px;">${escapeHtml(r.note || '—')}</td>
        <td>
          <div style="display:flex; gap:4px; flex-wrap:wrap;">
            <button class="small-btn" data-wl-edit="${r.item_id}">Цели</button>
            <button class="small-btn" data-wl-move="${altBucket}" data-wl-id="${r.item_id}">${altLabel}</button>
            <button class="small-btn danger" data-wl-del="${r.item_id}">🗑</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

export async function loadWatchlist() {
  const rows = await api.get('/api/watchlist');
  Store.watchlist = rows;
  Store.watchlistById.clear();
  rows.forEach((r) => Store.watchlistById.set(r.item_id, r));

  const badge = document.getElementById('watchlistBadge');
  if (badge) badge.innerText = rows.length;
  renderWatchlistTable();
  Bus.emit('watchlist:updated', rows);
}

export function initWatchlistModule() {
  injectWatchlistTabAndView();
  Bus.on('watchlist:openModal', ({ itemId }) => openWatchlistModal(itemId));
}