import {
  Bus, Store, api, calcDepositClient, coinsToCopper, copyTextFeedback,
  escapeHtml, getItemMeta, iconUrl, renderCoins, renderIconImg,
} from './core.js';
import { closeActionModal, openActionModal } from './ui.js';
import { closeAllAutocomplete, renderAutocomplete } from './market.js';

export function selectPortfolioItem(it) {
  if (!it) return;
  document.getElementById('pfSelectedItemId').value = it.item_id;
  document.getElementById('pfItemSearchInput').value = it.name;
  const iconEl = document.getElementById('pfSelectedIcon');
  if (iconEl) {
    iconEl.dataset.fbStep = '0';
    iconEl.src = iconUrl(it.icon);
  }
  updatePortfolioFormPreview();
}

export function openPortfolioDropdown() {
  const val = document.getElementById('pfItemSearchInput').value || '';
  const selectedId = parseInt(document.getElementById('pfSelectedItemId').value || '0', 10);
  const currentMeta = Store.itemsById.get(selectedId);
  const query = (currentMeta && val === currentMeta.name) ? '' : val;
  renderAutocomplete(document.getElementById('pfItemDropdown'), query, 'portfolio');
}

export function togglePortfolioDropdown() {
  const dd = document.getElementById('pfItemDropdown');
  if (dd.style.display === 'block') closeAllAutocomplete();
  else {
    openPortfolioDropdown();
    document.getElementById('pfItemSearchInput').focus();
  }
}

export function onPortfolioSearchInput() {
  const val = document.getElementById('pfItemSearchInput').value || '';
  renderAutocomplete(document.getElementById('pfItemDropdown'), val, 'portfolio');
}

export function updatePortfolioFormPreview() {
  const itemId = parseInt(document.getElementById('pfSelectedItemId')?.value || '0', 10);
  const meta = Store.itemsById.get(itemId);
  const vendorUnit = meta?.vendor_price || 0;
  const qty = Math.max(1, parseInt(document.getElementById('pfQty')?.value || '1', 10));
  const buyUnit = coinsToCopper('pfBuyG', 'pfBuyS', 'pfBuyC');
  const listUnit = coinsToCopper('pfListG', 'pfListS', 'pfListC');
  const hours = parseInt(document.getElementById('pfHours')?.value || '8', 10);
  const el = document.getElementById('pfMathPreview');
  if (!el) return;

  if (listUnit > 0) {
    const dep = calcDepositClient(vendorUnit, qty, hours);
    const fee = Math.floor(listUnit * qty * 0.05);
    const net = (listUnit * qty) - fee - (buyUnit * qty);
    el.innerHTML = `Залог (${hours}ч): ${renderCoins(dep)} • Комиссия 5%: ${renderCoins(fee)} • Ожид. профит: ${renderCoins(net, true)}`;
  } else {
    el.innerHTML = `Вендор: ${renderCoins(vendorUnit)}/шт • Сумма закупа: ${renderCoins(buyUnit * qty)}`;
  }
}

export async function submitPortfolioBuy() {
  const itemId = parseInt(document.getElementById('pfSelectedItemId').value || '0', 10);
  const qty = Math.max(1, parseInt(document.getElementById('pfQty').value || '1', 10));
  const buyUnit = coinsToCopper('pfBuyG', 'pfBuyS', 'pfBuyC');
  const listUnit = coinsToCopper('pfListG', 'pfListS', 'pfListC');
  const hours = parseInt(document.getElementById('pfHours').value || '8', 10);
  const comment = document.getElementById('pfComment').value || '';
  if (!itemId || buyUnit <= 0) { alert('Выберите предмет и укажите цену покупки.'); return; }

  await api.post('/api/portfolio/buy', {
    item_id: itemId, qty, buy_price_copper: buyUnit, list_price_copper: listUnit, deposit_hours: hours, comment,
  });
  document.getElementById('pfComment').value = '';
  await loadPortfolio();
}

function showListModal(pos) {
  const def = pos.list_unit > 0 ? pos.list_unit : (pos.current_ah_unit || pos.buy_unit);
  const g = Math.floor(def / 10000), s = Math.floor((def % 10000) / 100), c = def % 100;
  const isListed = pos.status === 'listed';

  const body = openActionModal(
    isListed ? `Изменить цену / перевыставить (${pos.qty} шт.)` : `Выставить на аукцион (${pos.qty} шт.)`,
    `
      <div style="margin-bottom:10px; font-size:12px; color:var(--text-muted); display:flex; align-items:center; gap:8px;">
        <span>Цена за 1 шт:</span>
        <div class="coin-capsule">
          <label class="coin-cell"><input id="mListG" type="number" min="0" value="${g}" /><span class="coin coin-gold"></span></label>
          <span class="coin-sep"></span>
          <label class="coin-cell"><input id="mListS" type="number" min="0" max="99" value="${s}" /><span class="coin coin-silver"></span></label>
          <span class="coin-sep"></span>
          <label class="coin-cell"><input id="mListC" type="number" min="0" max="99" value="${c}" /><span class="coin coin-copper"></span></label>
        </div>
      </div>
      <div style="margin-bottom:10px; font-size:12px; color:var(--text-muted); display:flex; align-items:center; gap:8px;">
        <span>Срок лота:</span>
        <select id="mHours" class="dark-select compact-select" style="width:104px;">
          <option value="2">2 часа</option>
          <option value="8" selected>8 часов</option>
          <option value="24">24 часа</option>
        </select>
      </div>
      ${isListed ? `<label style="display:flex; align-items:center; gap:6px; margin-bottom:10px; font-size:12px; color:#fca5a5; cursor:pointer;">
        <input id="mBurnPrev" type="checkbox" />
        <span>Сжечь прошлый залог (если отменял лот в игре)</span>
      </label>` : ''}
      <div id="mDepositPreview" style="margin-bottom:14px; font-size:12px;"></div>
      <div style="display:flex; gap:8px; justify-content:flex-end;">
        <button id="mCancelBtn">Отмена</button>
        <button id="mConfirmBtn" class="btn-success">Сохранить</button>
      </div>
    `
  );

  const recalc = () => {
    const hrs = parseInt(body.querySelector('#mHours').value || '8', 10);
    const dep = calcDepositClient(pos.vendor_unit, pos.qty, hrs);
    const unit = coinsToCopper('mListG', 'mListS', 'mListC');
    const fee = Math.floor(unit * pos.qty * 0.05);
    body.querySelector('#mDepositPreview').innerHTML = `Залог: ${renderCoins(dep)} • Комиссия 5%: ${renderCoins(fee)}`;
  };

  body.querySelectorAll('input, select').forEach((el) => el.addEventListener('input', recalc));
  body.querySelector('#mHours').addEventListener('change', recalc);
  body.querySelector('#mCancelBtn').addEventListener('click', closeActionModal);
  body.querySelector('#mConfirmBtn').addEventListener('click', async () => {
    const unit = coinsToCopper('mListG', 'mListS', 'mListC');
    const hrs = parseInt(body.querySelector('#mHours').value || '8', 10);
    const burn = Boolean(body.querySelector('#mBurnPrev')?.checked);
    if (unit <= 0) return;
    await api.post(`/api/portfolio/${pos.id}/list`, { list_price_copper: unit, deposit_hours: hrs, burn_previous: burn });
    closeActionModal();
    await loadPortfolio();
  });
  recalc();
}

function showSoldModal(pos) {
  const g = Math.floor(pos.list_unit / 10000), s = Math.floor((pos.list_unit % 10000) / 100), c = pos.list_unit % 100;
  const body = openActionModal(
    'Зафиксировать продажу',
    `
      <div style="margin-bottom:10px; font-size:12px; color:var(--text-muted); display:flex; align-items:center; gap:8px;">
        <span>Продано штук (из ${pos.qty}):</span>
        <input id="mSoldQty" type="number" min="1" max="${pos.qty}" value="${pos.qty}" class="qty-input" />
      </div>
      <div style="margin-bottom:12px; font-size:12px; color:var(--text-muted); display:flex; align-items:center; gap:8px;">
        <span>Цена продажи (1 шт):</span>
        <div class="coin-capsule">
          <label class="coin-cell"><input id="mSoldG" type="number" min="0" value="${g}" /><span class="coin coin-gold"></span></label>
          <span class="coin-sep"></span>
          <label class="coin-cell"><input id="mSoldS" type="number" min="0" max="99" value="${s}" /><span class="coin coin-silver"></span></label>
          <span class="coin-sep"></span>
          <label class="coin-cell"><input id="mSoldC" type="number" min="0" max="99" value="${c}" /><span class="coin coin-copper"></span></label>
        </div>
      </div>
      <div style="font-size:11.5px; color:#8b92a1; margin-bottom:14px;">Активный залог вернётся, комиссия 5% и сгоревшие залоги вычтутся из профита.</div>
      <div style="display:flex; gap:8px; justify-content:flex-end;">
        <button id="mCancelBtn">Отмена</button>
        <button id="mConfirmBtn" class="btn-success">Подтвердить продажу</button>
      </div>
    `
  );
  body.querySelector('#mCancelBtn').addEventListener('click', closeActionModal);
  body.querySelector('#mConfirmBtn').addEventListener('click', async () => {
    const soldQty = parseInt(body.querySelector('#mSoldQty').value || '1', 10);
    const sellUnit = coinsToCopper('mSoldG', 'mSoldS', 'mSoldC');
    await api.post(`/api/portfolio/${pos.id}/sold`, { sold_qty: soldQty, actual_sell_price_copper: sellUnit });
    closeActionModal();
    await loadPortfolio();
  });
}

let activePositionsMap = new Map();

function renderItemCell(p) {
  Store.portfolioMeta.set(p.item_id, { item_id: p.item_id, name: p.name, icon: p.icon, vendor_price: p.vendor_unit });
  return `
    <div class="pf-item-cell">
      <div class="pf-item-trigger" data-popup-item="${p.item_id}" title="Открыть график поверх">
        ${renderIconImg(p.icon)}
        <b>${escapeHtml(p.name)}</b>
      </div>
      <button class="pf-copy-btn" data-copy-item="${p.item_id}" title="Скопировать название">❐</button>
    </div>
  `;
}

export async function loadPortfolio() {
  const data = await api.get('/api/portfolio');
  const s = data.summary;
  activePositionsMap.clear();
  data.active.forEach((p) => activePositionsMap.set(p.id, p));

  document.getElementById('portfolioBadge').innerText = s.active_count;
  document.getElementById('headerPortfolioMini').innerHTML = `
    <span>В товаре: ${renderCoins(s.invested_total)}</span>
    <span>Сгорело залогов: ${renderCoins(-s.burned_deposits)}</span>
    <span>Чистый профит: ${renderCoins(s.realized_pnl, true)}</span>
  `;

  document.getElementById('portfolioKpiRow').innerHTML = `
    <div class="kpi-card">
      <div class="kpi-title">Заморожено в товаре</div>
      <div class="kpi-value">${renderCoins(s.invested_total)}</div>
      <div style="font-size:11px; color:var(--text-muted); margin-top:4px;">На руках: ${renderCoins(s.invested_holding)} • На АН: ${renderCoins(s.invested_listed)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Активные залоги (вернутся)</div>
      <div class="kpi-value">${renderCoins(s.active_deposits)}</div>
      <div style="font-size:11px; color:var(--text-muted); margin-top:4px;">Возвращаются на почту при выкупе</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Сгорело на непродажах</div>
      <div class="kpi-value">${renderCoins(-s.burned_deposits)}</div>
      <div style="font-size:11px; color:var(--text-muted); margin-top:4px;">Учтено в чистом профите</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Зафиксированный чистый профит</div>
      <div class="kpi-value">${renderCoins(s.realized_pnl, true)}</div>
      <div style="font-size:11px; color:var(--text-muted); margin-top:4px;">После вычета 5% АН (${renderCoins(s.total_ah_fees)})</div>
    </div>
  `;

  const activeBody = document.getElementById('pfActiveBody');
  activeBody.innerHTML = data.active.length === 0
    ? `<tr><td colspan="9" style="color:var(--text-muted); text-align:center;">Активных позиций пока нет</td></tr>`
    : data.active.map((p) => {
        const badge = p.status === 'listed'
          ? `<span class="tag">НА АУКЦИОНЕ (${p.deposit_hours}ч)</span>`
          : `<span class="tag tag-holding">В СУМКЕ</span>`;
        return `
          <tr>
            <td>${badge}</td>
            <td>${renderItemCell(p)}</td>
            <td>${p.qty} шт.</td>
            <td>${renderCoins(p.buy_unit)} <span style="color:#8b92a1;">(${renderCoins(p.cost_total)})</span></td>
            <td>${p.list_unit > 0 ? renderCoins(p.list_unit) : '<span style="color:#8b92a1;">Не выставлен</span>'}</td>
            <td>
              ${renderCoins(p.active_deposit)} / <span style="color:#f87171;">${renderCoins(p.burned_deposit)}</span>
              ${p.burned_deposit > 0 ? `<button class="small-btn" style="padding:1px 5px; margin-left:4px;" data-pf-act="reset-burned" data-pos-id="${p.id}" title="Сбросить залог">↺</button>` : ''}
            </td>
            <td>${p.list_unit > 0 ? renderCoins(p.expected_net, true) : '—'}</td>
            <td style="color:#9ca3af; max-width:180px;">${escapeHtml(p.comment || '—')}</td>
            <td>
              <div style="display:flex; gap:4px; flex-wrap:wrap;">
                <button class="small-btn" data-pf-act="list" data-pos-id="${p.id}">Выставить</button>
                ${p.list_unit > 0 ? `<button class="small-btn btn-success" data-pf-act="sold" data-pos-id="${p.id}">Продано ✓</button>` : ''}
                ${p.status === 'listed' ? `<button class="small-btn btn-warn" data-pf-act="expire" data-pos-id="${p.id}">Не продано ✕</button>` : ''}
                <button class="small-btn" data-pf-act="vendor" data-pos-id="${p.id}">Вендору</button>
                <button class="small-btn danger" data-pf-act="delete" data-pos-id="${p.id}">🗑</button>
              </div>
            </td>
          </tr>
        `;
      }).join('');

  const closedBody = document.getElementById('pfClosedBody');
  closedBody.innerHTML = data.closed.length === 0
    ? `<tr><td colspan="9" style="color:var(--text-muted); text-align:center;">Закрытых сделок пока нет</td></tr>`
    : data.closed.map((p) => `
        <tr>
          <td>${p.status === 'sold' ? `<span class="tag tag-sold">ПРОДАНО</span>` : `<span class="tag tag-vendored">ВЕНДОРУ</span>`}</td>
          <td>${renderItemCell(p)}</td>
          <td>${p.qty} шт.</td>
          <td>${renderCoins(p.buy_unit)}</td>
          <td>${renderCoins(p.sell_unit)}</td>
          <td>${renderCoins(p.ah_fee)}</td>
          <td>${renderCoins(p.burned_deposit)}</td>
          <td><b>${renderCoins(p.realized_pnl, true)}</b></td>
          <td style="color:#9ca3af;">${escapeHtml(p.comment || '—')}</td>
        </tr>
      `).join('');
}

export function initPortfolioModule() {
  const pfView = document.getElementById('viewPortfolio');
  pfView?.addEventListener('click', async (e) => {
    const copyBtn = e.target.closest('[data-copy-item]');
    if (copyBtn) {
      e.stopPropagation();
      const meta = getItemMeta(Number(copyBtn.dataset.copyItem));
      if (meta?.name) copyTextFeedback(meta.name, copyBtn);
      return;
    }
    const popEl = e.target.closest('[data-popup-item]');
    if (popEl) {
      Bus.emit('chart:popup', { itemId: Number(popEl.dataset.popupItem) });
      return;
    }
    const actBtn = e.target.closest('[data-pf-act]');
    if (!actBtn) return;
    const posId = Number(actBtn.dataset.posId);
    const act = actBtn.dataset.pfAct;
    const pos = activePositionsMap.get(posId);

    if (act === 'list' && pos) showListModal(pos);
    else if (act === 'sold' && pos) showSoldModal(pos);
    else if (act === 'expire') { await api.post(`/api/portfolio/${posId}/expire`); await loadPortfolio(); }
    else if (act === 'vendor') { await api.post(`/api/portfolio/${posId}/vendor`); await loadPortfolio(); }
    else if (act === 'reset-burned') { await api.post(`/api/portfolio/${posId}/reset-burned`); await loadPortfolio(); }
    else if (act === 'delete') { await api.del(`/api/portfolio/${posId}`); await loadPortfolio(); }
  });

  Bus.on('portfolio:refresh', loadPortfolio);
  Bus.on('portfolio:pickItem', selectPortfolioItem);
  Bus.on('items:loaded', (items) => {
    const selId = parseInt(document.getElementById('pfSelectedItemId')?.value || '0', 10);
    if (!selId && items.length) {
      const first = items.find((i) => !i.is_hidden) || items[0];
      if (first) selectPortfolioItem(first);
    }
  });
}