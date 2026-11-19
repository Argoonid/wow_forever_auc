import { Bus, escapeHtml } from './core.js';

export function injectTerminalStyles() {
  if (document.getElementById('terminal-modular-styles')) return;
  const style = document.createElement('style');
  style.id = 'terminal-modular-styles';
  style.textContent = `
    * { scrollbar-width: thin; scrollbar-color: #2f3542 transparent; }
    *::-webkit-scrollbar { width: 6px; height: 6px; }
    *::-webkit-scrollbar-track { background: transparent; }
    *::-webkit-scrollbar-thumb { background: #2f3542; border-radius: 999px; }
    *::-webkit-scrollbar-thumb:hover { background: #475063; }

    input[type="number"]::-webkit-outer-spin-button,
    input[type="number"]::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
    input[type="number"] { -moz-appearance: textfield; }
    .coin-capsule input::selection, .qty-input::selection { background: rgba(56, 189, 248, 0.35); color: #fff; }
    .coin-cell { cursor: text; }

    /* Кастомный dropdown */
    .cselect-wrap { position: relative; display: inline-flex; align-items: center; vertical-align: middle; }
    .cselect-trigger {
      display: inline-flex; align-items: center; justify-content: space-between; gap: 8px;
      min-width: 72px; width: 100%; height: 30px; padding: 0 10px;
      background: #121418; color: #e4e6eb; border: 1px solid #2c303a; border-radius: 6px;
      font-size: 12.5px; font-family: inherit; cursor: pointer; user-select: none; white-space: nowrap;
      transition: border-color .15s ease, background .15s ease;
    }
    .cselect-trigger:hover { border-color: #454c5c; background: #171a20; }
    .cselect-wrap.open .cselect-trigger { border-color: #eab308; background: #171a20; }
    .cselect-arrow { width: 12px; height: 12px; opacity: .65; transition: transform .16s ease; flex-shrink: 0; }
    .cselect-wrap.open .cselect-arrow { transform: rotate(180deg); opacity: 1; }
    .cselect-menu {
      position: fixed; z-index: 10080; min-width: 96px; max-height: 240px; overflow-y: auto;
      padding: 4px; background: #15181e; border: 1px solid #2f3542; border-radius: 8px;
      box-shadow: 0 12px 32px rgba(0,0,0,.58); display: none;
    }
    .cselect-menu.open { display: block; }
    .cselect-option {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 6px 9px; border-radius: 5px; font-size: 12.5px; color: #d1d5db; cursor: pointer; white-space: nowrap;
    }
    .cselect-option:hover { background: #232834; color: #fff; }
    .cselect-option.selected { background: rgba(234,179,8,.14); color: #facc15; font-weight: 500; }

    /* Кликабельные ячейки и кнопки копирования */
    .pf-item-cell { display: inline-flex; align-items: center; gap: 7px; max-width: 100%; }
    .pf-item-trigger {
      display: inline-flex; align-items: center; gap: 9px; cursor: pointer;
      border-radius: 5px; padding: 2px 5px 2px 2px; margin: -2px 0 -2px -2px;
      transition: background .14s ease, color .14s ease;
    }
    .pf-item-trigger:hover { background: rgba(56,189,248,.10); color: #38bdf8; }
    .pf-item-trigger:hover b { text-decoration: underline; text-underline-offset: 3px; text-decoration-color: rgba(56,189,248,.55); }
    .pf-copy-btn {
      display: inline-flex; align-items: center; justify-content: center;
      width: 22px; height: 22px; padding: 0; background: transparent;
      border: 1px solid transparent; border-radius: 5px; color: #6b7280; font-size: 12px;
      cursor: pointer; transition: all .14s ease; flex-shrink: 0;
    }
    .pf-copy-btn:hover { background: #232732; border-color: #343b4a; color: #e4e6eb; }
    .pf-copy-btn.copied { color: #4ade80; border-color: rgba(74,222,128,.35); background: rgba(74,222,128,.08); }

    /* Фильтр-бар минимальной цены в сайдбаре */
    .sidebar-filter-pill {
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
      padding: 5px 8px; margin-bottom: 6px; border-radius: 6px;
      background: #14171d; border: 1px solid #262b35; font-size: 11.5px; color: #9ca3af;
    }
    .sidebar-filter-pill input[type="number"] {
      width: 52px; padding: 2px 6px; background: #0f1116; color: #facc15;
      border: 1px solid #2e3440; border-radius: 4px; font-size: 11.5px; text-align: center;
    }

    /* Всплывающее окно графика */
    .chart-popup-backdrop {
      position: fixed; inset: 0; z-index: 9500; background: rgba(7,9,13,.76);
      backdrop-filter: blur(4px); display: none; align-items: center; justify-content: center; padding: 20px;
    }
    .chart-popup-card {
      width: min(1080px, 94vw); height: min(640px, 86vh); background: #181a1f;
      border: 1px solid #2e3340; border-radius: 12px; box-shadow: 0 24px 64px rgba(0,0,0,.68);
      display: flex; flex-direction: column; overflow: hidden;
    }
    .chart-popup-header, .chart-popup-subbar {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 16px; border-bottom: 1px solid #262a34; background: #14161b; flex-wrap: wrap;
    }
    .chart-popup-subbar { padding: 8px 16px; background: #16181e; border-bottom-color: #222630; }
    .chart-popup-stage { position: relative; flex: 1; min-height: 0; width: 100%; }
    #popupChartCanvasHost { width: 100%; height: 100%; }
    #popupDrawOverlay { position: absolute; inset: 0; pointer-events: none; z-index: 4; }
    #popupCursorTooltip {
      position: absolute; display: none; pointer-events: none; z-index: 10; transform: translate(-50%,-100%);
      background: rgba(17,19,24,.94); border: 1px solid #353b49; border-radius: 7px;
      padding: 7px 10px; font-size: 12px; color: #e4e6eb; white-space: nowrap;
    }

    /* Вкладка Радар / Инвест */
    .wl-wrap { padding: 16px 20px; max-width: 1460px; margin: 0 auto; }
    .wl-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
    .wl-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
    .wl-pill-btn {
      padding: 6px 12px; border-radius: 7px; border: 1px solid #2c313d; background: #15181e;
      color: #cbd5e1; font-size: 12.5px; cursor: pointer; transition: all .14s ease;
    }
    .wl-pill-btn:hover { border-color: #4b5563; color: #fff; }
    .wl-pill-btn.active { border-color: #eab308; background: rgba(234,179,8,.14); color: #facc15; font-weight: 600; }
    .wl-badge-bucket {
      display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px;
      border-radius: 5px; font-size: 11px; font-weight: 600; white-space: nowrap;
    }
    .wl-alert-buy {
      display: inline-block; margin-left: 6px; padding: 2px 6px; border-radius: 4px;
      background: rgba(34,197,94,.18); color: #4ade80; font-size: 10.5px; font-weight: 700;
    }
    .wl-alert-sell {
      display: inline-block; margin-left: 6px; padding: 2px 6px; border-radius: 4px;
      background: rgba(56,189,248,.18); color: #38bdf8; font-size: 10.5px; font-weight: 700;
    }
    .wl-bucket-choices { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
    .wl-bucket-choice {
      flex: 1; min-width: 120px; padding: 8px 10px; border-radius: 7px; border: 1px solid #2e3340;
      background: #14171d; color: #cbd5e1; font-size: 12px; cursor: pointer; text-align: center;
    }
    .wl-bucket-choice.active { border-color: #eab308; background: rgba(234,179,8,.15); color: #facc15; font-weight: 600; }
  `;
  document.head.appendChild(style);
}

export function closeAllCustomSelects() {
  document.querySelectorAll('.cselect-wrap.open').forEach(w => w.classList.remove('open'));
  document.querySelectorAll('.cselect-menu.open').forEach(m => m.classList.remove('open'));
}

export function cleanupDetachedSelectMenus() {
  document.querySelectorAll('.cselect-menu').forEach(menu => {
    const linked = Array.from(document.querySelectorAll('select')).some(s => s._customMenu === menu);
    if (!linked) menu.remove();
  });
}

export function upgradeCustomSelects(root = document) {
  root.querySelectorAll('select').forEach(sel => {
    if (sel.dataset.customUpgraded === '1') {
      sel._syncCustomUi?.();
      return;
    }
    sel.dataset.customUpgraded = '1';
    const inlineWidth = sel.style.width;
    sel.style.display = 'none';

    const wrap = document.createElement('div');
    wrap.className = 'cselect-wrap';
    if (inlineWidth) wrap.style.width = inlineWidth;

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'cselect-trigger';
    trigger.innerHTML = `
      <span class="cselect-label"></span>
      <svg class="cselect-arrow" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M5 7.5L10 12.5L15 7.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    `;

    const menu = document.createElement('div');
    menu.className = 'cselect-menu';
    document.body.appendChild(menu);

    const syncUi = () => {
      const opt = sel.options[sel.selectedIndex] || sel.options[0];
      trigger.querySelector('.cselect-label').textContent = opt ? opt.textContent : '';
      menu.innerHTML = '';
      Array.from(sel.options).forEach(o => {
        const item = document.createElement('div');
        const isSel = o.value === sel.value;
        item.className = 'cselect-option' + (isSel ? ' selected' : '');
        item.innerHTML = `<span>${escapeHtml(o.textContent)}</span>${isSel ? '<span>✓</span>' : ''}`;
        item.addEventListener('mousedown', (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          if (sel.value !== o.value) {
            sel.value = o.value;
            syncUi();
            sel.dispatchEvent(new Event('change', { bubbles: true }));
          }
          closeAllCustomSelects();
        });
        menu.appendChild(item);
      });
    };

    sel._syncCustomUi = syncUi;
    sel._customMenu = menu;
    sel.addEventListener('change', syncUi);
    syncUi();

    trigger.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const wasOpen = wrap.classList.contains('open');
      closeAllCustomSelects();
      if (wasOpen) return;

      syncUi();
      const rect = trigger.getBoundingClientRect();
      menu.style.minWidth = `${Math.max(rect.width, 96)}px`;
      menu.style.left = `${rect.left}px`;
      menu.classList.add('open');

      const menuH = menu.offsetHeight || 120;
      const spaceBelow = window.innerHeight - rect.bottom;
      menu.style.top = (spaceBelow < menuH + 12 && rect.top > menuH + 12)
        ? `${rect.top - menuH - 4}px`
        : `${rect.bottom + 4}px`;
      wrap.classList.add('open');
    });

    sel.parentNode.insertBefore(wrap, sel.nextSibling);
    wrap.appendChild(trigger);
  });
}

export function openActionModal(title, bodyHtml) {
  const titleEl = document.getElementById('modalTitle');
  const bodyEl = document.getElementById('modalBody');
  const modal = document.getElementById('actionModal');
  if (!modal || !bodyEl) return;
  if (titleEl) titleEl.innerText = title;
  bodyEl.innerHTML = bodyHtml;
  cleanupDetachedSelectMenus();
  upgradeCustomSelects(bodyEl);
  modal.style.display = 'flex';
  return bodyEl;
}

export function closeActionModal() {
  closeAllCustomSelects();
  const modal = document.getElementById('actionModal');
  if (modal) modal.style.display = 'none';
}

export function initGlobalUx() {
  injectTerminalStyles();

  const actionModal = document.getElementById('actionModal');
  if (actionModal && !actionModal.dataset.backdropBound) {
    actionModal.dataset.backdropBound = '1';
    actionModal.addEventListener('click', (e) => {
      if (e.target === actionModal) closeActionModal();
    });
  }

  // Автовыделение числа в любом числовом input при клике / Tab
  document.addEventListener('focusin', (e) => {
    const t = e.target;
    if (t?.tagName === 'INPUT' && t.type === 'number') {
      t.dataset.justFocused = '1';
      try { t.select(); } catch (_) {}
    }
  });

  document.addEventListener('mouseup', (e) => {
    const t = e.target;
    if (t?.tagName === 'INPUT' && t.type === 'number') {
      if (t.dataset.justFocused === '1' || t.closest('.coin-capsule') || t.classList.contains('qty-input')) {
        e.preventDefault();
        try { t.select(); } catch (_) {}
        delete t.dataset.justFocused;
      }
    }
  });

  document.addEventListener('focusout', (e) => {
    const t = e.target;
    if (t?.tagName === 'INPUT' && t.type === 'number') {
      delete t.dataset.justFocused;
      if (t.value.trim() === '') {
        t.value = t.min !== '' ? t.min : '0';
        t.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.cselect-wrap') && !e.target.closest('.cselect-menu')) {
      closeAllCustomSelects();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (document.querySelector('.cselect-menu.open')) {
      closeAllCustomSelects();
      return;
    }
    Bus.emit('ui:escape');
    const actMod = document.getElementById('actionModal');
    if (actMod && actMod.style.display === 'flex') closeActionModal();
  });

  window.addEventListener('scroll', closeAllCustomSelects, true);
  window.addEventListener('resize', closeAllCustomSelects);
  upgradeCustomSelects(document);
}