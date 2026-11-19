import { Bus, Store, copyTextFeedback } from './core.js';
import { closeActionModal, closeAllCustomSelects, initGlobalUx } from './ui.js';
import {
  cancelChartNote, clearAllDrawings, confirmChartNote, initChartsModule,
  selectDrawTool, setMainDaysRange, submitQuickTrade, toggleQuickTradeForm, updateQuickTradePreview,
} from './charts.js';
import {
  clearSidebarSearch, closeAllAutocomplete, copyAiDigest, handleSearchKeydown,
  initMarketModule, loadCallHistory, loadItems, loadOpportunities, onSidebarSearchInput,
  renderItemList, toggleShowHidden,
} from './market.js';
import {
  initPortfolioModule, loadPortfolio, onPortfolioSearchInput, openPortfolioDropdown,
  submitPortfolioBuy, togglePortfolioDropdown, updatePortfolioFormPreview,
} from './portfolio.js';
import { initWatchlistModule, loadWatchlist } from './watchlist.js';

function switchView(view) {
  closeAllAutocomplete();
  closeAllCustomSelects();

  const views = {
    market:    { el: 'viewMarket',    btn: 'tabBtnMarket',    display: 'grid' },
    portfolio: { el: 'viewPortfolio', btn: 'tabBtnPortfolio', display: 'block' },
    watchlist: { el: 'viewWatchlist', btn: 'tabBtnWatchlist', display: 'block' },
  };

  Object.entries(views).forEach(([key, cfg]) => {
    const sec = document.getElementById(cfg.el);
    const btn = document.getElementById(cfg.btn);
    if (sec) sec.style.display = key === view ? cfg.display : 'none';
    if (btn) btn.classList.toggle('active', key === view);
  });

  if (view === 'portfolio') loadPortfolio();
  else if (view === 'watchlist') loadWatchlist();
  else setTimeout(() => Bus.emit('chart:resize'), 50);
}

function setupBrowserLifecycleSync() {
  const pingAlive = () => {
    fetch('/api/lifecycle/ping', { method: 'POST', keepalive: true }).catch(() => {});
  };

  let channel = null;
  if (typeof BroadcastChannel !== 'undefined') {
    channel = new BroadcastChannel('forever_terminal_lifecycle');
    channel.addEventListener('message', (e) => {
      // Другая вкладка закрылась, но эта ещё жива — отменяем остановку трея
      if (e.data === 'closing') pingAlive();
    });
  }

  pingAlive();
  window.addEventListener('pageshow', pingAlive);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') pingAlive();
  });

  window.addEventListener('pagehide', (event) => {
    if (event.persisted) return;
    if (channel) {
      try { channel.postMessage('closing'); } catch (_) {}
      channel.close();
    }
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/lifecycle/closing', '');
    } else {
      fetch('/api/lifecycle/closing', { method: 'POST', keepalive: true }).catch(() => {});
    }
  });
}

// Тонкий мост только для статических onclick/oninput в index.html
Object.assign(window, {
  switchView,
  copyCurrentItemName: () => copyTextFeedback(Store.selectedItemName, document.getElementById('btnCopyTitle')),
  copyTextFeedback,
  onSidebarSearchInput,
  clearSidebarSearch,
  handleSearchKeydown,
  renderItemList,
  toggleShowHidden,
  setDaysRange: setMainDaysRange,
  selectTool: selectDrawTool,
  confirmChartNote,
  cancelChartNote,
  clearAllDrawings,
  toggleQuickTradeForm,
  updateQuickTradePreview,
  submitQuickTrade,
  openPortfolioDropdown,
  togglePortfolioDropdown,
  onPortfolioSearchInput,
  updatePortfolioFormPreview,
  submitPortfolioBuy,
  closeModal: closeActionModal,
  copyAiDigest,
});

async function boot() {
  setupBrowserLifecycleSync();

  initGlobalUx();
  initChartsModule();
  initMarketModule();
  initPortfolioModule();
  initWatchlistModule();

  Bus.on('view:switch', switchView);

  await loadWatchlist();
  const items = await loadItems();
  const firstVisible = items.find((i) => !i.is_hidden) || items[0];
  if (firstVisible) {
    Bus.emit('item:select', { itemId: firstVisible.item_id });
  }

  await Promise.all([loadOpportunities(), loadCallHistory(), loadPortfolio()]);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}