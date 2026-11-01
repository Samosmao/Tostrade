/* XAU Signal: self-contained client. Market data and secrets stay on the server. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = { user: null, csrf: '', providerConfigured: false, cacheSeconds: 120, interval: '1m', snapshot: null, authMode: 'register', requestId: 0 };
  const authDialog = $('authDialog');
  const settingsDialog = $('settingsDialog');
  const chartContainer = $('chartContainer');
  const chartTooltip = $('chartTooltip');
  const ns = 'http://www.w3.org/2000/svg';
  const money = (value) => Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const shortTime = (value) => new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value));

  async function api(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.method && options.method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRF-Token'] = state.csrf;
    }
    const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options, headers });
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('The server returned an unexpected response.'); }
    if (!response.ok) throw new Error(data.error || 'Something went wrong. Please try again.');
    return data;
  }

  function setButtonText(button, text) {
    if (button.firstChild && button.firstChild.nodeType === Node.TEXT_NODE) button.firstChild.nodeValue = text + ' ';
    else button.prepend(document.createTextNode(text + ' '));
  }

  function renderAccount() {
    const member = !!state.user;
    const name = member ? state.user.name : 'Guest trader';
    $('profileName').textContent = name;
    $('profileSub').textContent = member ? 'Your workspace' : 'Explore the demo';
    $('avatar').textContent = member ? (state.user.name.trim().charAt(0).toUpperCase() || 'M') : 'G';
    $('settingsAvatar').textContent = $('avatar').textContent;
    $('settingsName').textContent = name;
    $('settingsEmail').textContent = member ? state.user.email : 'You’re exploring the public demo.';
    $('settingsAccountAction').textContent = member ? 'Sign out' : 'Sign in';
    setButtonText($('topAccount'), member ? 'Account' : 'Sign in');
    setButtonText($('primaryAccount'), member ? 'My account' : 'Create account');
    if (state.providerConfigured) {
      $('providerStatus').textContent = 'KEY CONFIGURED';
      $('providerDescription').textContent = member
        ? 'A key is configured on this server. The dashboard requests provider candles when available.'
        : 'A key is configured on this server. Sign in to request provider candles; the public preview stays simulated.';
    } else {
      $('providerStatus').textContent = 'NOT CONNECTED';
      $('providerDescription').textContent = 'The dashboard currently uses simulated prices. Add an API key on the server to request provider data.';
    }
  }

  function updateClock() {
    $('utcClock').textContent = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date()) + ' UTC';
  }

  function closeDialog(dialog) { if (dialog.open) dialog.close(); }
  function openSettings() {
    closeDialog(authDialog);
    renderAccount();
    if (!settingsDialog.open) settingsDialog.showModal();
  }
  function setAuthMode(mode) {
    state.authMode = mode;
    const register = mode === 'register';
    $('nameField').hidden = !register;
    $('nameInput').required = register;
    $('passwordInput').autocomplete = register ? 'new-password' : 'current-password';
    $('authTitle').replaceChildren();
    const line = document.createTextNode(register ? 'Make the market' : 'Welcome back');
    const br = document.createElement('br');
    const em = document.createElement('em');
    em.textContent = register ? 'make more sense.' : 'to your workspace.';
    $('authTitle').append(line, br, em);
    $('authIntro').textContent = register ? 'Create an account to access your personal signal dashboard.' : 'Sign in to return to your gold market dashboard.';
    $('authSwitchText').textContent = register ? 'Already have an account?' : 'New to XAU Signal?';
    $('switchAuth').textContent = register ? 'Sign in' : 'Create an account';
    setButtonText($('authSubmit'), register ? 'Create account' : 'Sign in');
    $('authError').hidden = true;
  }
  function openAuth(mode) {
    closeDialog(settingsDialog);
    setAuthMode(mode);
    if (!authDialog.open) authDialog.showModal();
  }

  $('topAccount').addEventListener('click', () => state.user ? openSettings() : openAuth('login'));
  $('primaryAccount').addEventListener('click', () => state.user ? openSettings() : openAuth('register'));
  $('profileButton').addEventListener('click', () => state.user ? openSettings() : openAuth('register'));
  document.querySelectorAll('[data-open-settings]').forEach((button) => button.addEventListener('click', openSettings));
  document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => closeDialog(button.closest('dialog'))));
  [authDialog, settingsDialog].forEach((dialog) => dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); }));
  $('switchAuth').addEventListener('click', () => setAuthMode(state.authMode === 'register' ? 'login' : 'register'));
  $('settingsAccountAction').addEventListener('click', async () => {
    if (!state.user) { openAuth('login'); return; }
    $('settingsAccountAction').disabled = true;
    try {
      const result = await api('/api/auth/logout', { method: 'POST', body: '{}' });
      state.user = null; state.csrf = result.csrf;
      closeDialog(settingsDialog); renderAccount(); await loadMarket(true);
    } catch (err) {
      $('settingsEmail').textContent = err.message;
    } finally { $('settingsAccountAction').disabled = false; }
  });

  $('authForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!$('authForm').reportValidity()) return;
    const register = state.authMode === 'register';
    const payload = { email: $('emailInput').value.trim(), password: $('passwordInput').value };
    if (register) payload.name = $('nameInput').value.trim();
    $('authError').hidden = true;
    $('authSubmit').disabled = true;
    try {
      const result = await api(register ? '/api/auth/register' : '/api/auth/login', { method: 'POST', body: JSON.stringify(payload) });
      state.user = result.user; state.csrf = result.csrf;
      $('passwordInput').value = '';
      closeDialog(authDialog); renderAccount(); await loadMarket(true);
    } catch (err) {
      $('authError').textContent = err.message;
      $('authError').hidden = false;
    } finally { $('authSubmit').disabled = false; }
  });

  document.querySelectorAll('[data-nav]').forEach((link) => link.addEventListener('click', () => {
    document.querySelectorAll('[data-nav]').forEach((item) => item.classList.remove('active'));
    link.classList.add('active');
  }));
  document.querySelectorAll('[data-timeframe]').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.timeframe === state.interval) return;
    state.interval = button.dataset.timeframe;
    updateTimeframeButtons();
    loadMarket(true);
  }));
  $('refreshButton').addEventListener('click', () => loadMarket(false));

  function updateTimeframeButtons() {
    document.querySelectorAll('[data-timeframe]').forEach((button) => {
      const selected = button.dataset.timeframe === state.interval;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  }
  function timeframeLabel(interval) {
    return ({ '1m': '1 MIN', '5m': '5 MIN', '15m': '15 MIN', '1h': '1 HOUR', '4h': '4 HOUR' })[interval] || interval.toUpperCase();
  }
  function clearValues(message) {
    ['currentPrice', 'priceChange', 'lastClosed', 'metricSignal', 'metricTrend', 'metricRsi', 'metricAtr', 'conditionTrend', 'conditionRsi', 'conditionPrice'].forEach((id) => { $(id).textContent = '—'; });
    $('metricSignal').className = '';
    $('metricTrendSub').textContent = 'Fast & slow moving averages';
    $('metricRsiSub').textContent = 'Wilder’s RSI';
    $('metricSignalSub').textContent = 'Based on the last closed candle';
    $('recentSignals').textContent = message ? 'No current data to scan.' : 'Scanning closed candles…';
    $('priceLabel').textContent = 'LAST CLOSED PRICE';
    $('signalHeadline').textContent = message || 'Reading the market';
    $('signalReason').textContent = message ? 'Check the connection details or try another timeframe.' : 'Calculating the latest closed candle.';
    $('signalBadge').className = 'signal-badge wait';
    $('signalBadge').replaceChildren(document.createElement('span'), document.createTextNode(' WAIT'));
    $('signalSymbol').textContent = '—'; $('signalSymbol').className = 'signal-symbol wait';
    $('signalFocus').className = 'signal-focus';
    const placeholder = document.createElement('div'); placeholder.className = 'chart-placeholder'; placeholder.textContent = message || 'Loading price chart…';
    chartContainer.replaceChildren(placeholder, chartTooltip); chartTooltip.hidden = true;
  }

  async function loadMarket(clearFirst = false) {
    const id = ++state.requestId;
    const interval = state.interval;
    if (clearFirst) clearValues();
    $('refreshButton').disabled = true;
    try {
      const endpoint = state.user ? '/api/market' : '/api/demo-market';
      const data = await api(endpoint + '?interval=' + encodeURIComponent(interval));
      if (id !== state.requestId) return;
      state.snapshot = data;
      renderMarket(data);
    } catch (err) {
      if (id !== state.requestId) return;
      state.snapshot = null;
      clearValues('Market data unavailable');
      renderSourceError(err.message);
    } finally {
      if (id === state.requestId) $('refreshButton').disabled = false;
    }
  }

  function renderSourceError(message) {
    $('topStatus').className = 'top-status error';
    $('topStatusText').textContent = 'DATA UNAVAILABLE';
    $('dataNotice').className = 'data-notice error-notice';
    $('noticeTitle').textContent = 'Market data unavailable';
    $('noticeText').textContent = message;
    $('sidebarSource').textContent = 'Feed unavailable';
    $('sidebarSourceDetail').textContent = 'Check the provider key, plan and connection.';
    $('providerAttribution').hidden = true;
    $('chartSource').textContent = 'UNAVAILABLE';
    $('chartBottomText').textContent = 'No market prices are displayed';
  }

  function renderSource(source) {
    const demo = source.mode === 'demo';
    const stale = source.stale;
    const cache = source.poll_seconds || state.cacheSeconds;
    const fetched = shortTime(source.retrieved_at) + ' UTC';
    $('topStatus').className = 'top-status' + (!demo && !stale ? ' live' : '');
    $('topStatusText').textContent = demo ? 'SIMULATED DATA' : stale ? 'STALE PROVIDER DATA' : 'POLLED PROVIDER DATA';
    $('dataNotice').className = 'data-notice' + (!demo && !stale ? ' live-notice' : '');
    $('noticeTitle').textContent = demo ? 'You’re viewing a demo market' : stale ? 'Provider data is stale' : 'Data provided by Twelve Data';
    $('noticeText').textContent = demo
      ? 'Prices and signals are simulated, not live market data.'
      : stale
        ? 'Last closed candle ' + shortTime(source.last_closed_at) + ' UTC; signal forced to WAIT. Last poll ' + fetched + '.'
        : 'Polled, not streaming. Last poll ' + fetched + '; cached up to ' + cache + ' seconds. Provider latency may vary.';
    $('sidebarSource').textContent = demo ? 'Demo market' : stale ? 'Stale data' : 'Twelve Data';
    $('sidebarSourceDetail').textContent = demo ? 'Explore with simulated data, or connect a provider.' : stale ? 'Waiting for a fresh provider candle.' : 'Polled, not streaming. Last poll ' + fetched + '.';
    $('providerAttribution').hidden = demo;
    $('chartSource').textContent = demo ? 'SIMULATED' : stale ? 'STALE PROVIDER DATA' : 'TWELVE DATA · POLLED';
    $('chartSubtitle').textContent = source.forming ? 'Last candle forming · indicators use closed candles' : 'Closed candles · USD per troy ounce';
    $('chartBottomText').textContent = demo ? 'Demo candles · not market prices' : stale ? 'Stale candles · check the timestamp' : (source.forming ? 'Last candle forming · ' : 'Closed candles · ') + cache + 's cache';
  }

  function renderRecentSignals(data) {
    const scalp = data.interval === '1m';
    $('recentScan').hidden = !scalp;
    if (!scalp) return;
    const list = $('recentSignals');
    list.replaceChildren();
    if (data.source.stale) {
      list.textContent = 'Unavailable while data is stale.';
      return;
    }
    const entries = data.recent_signals || [];
    if (!entries.length) {
      list.textContent = 'No BUY / SELL matches in these bars.';
      return;
    }
    [...entries].reverse().forEach((entry) => {
      const row = document.createElement('div'); row.className = 'recent-signal-row';
      const detail = document.createElement('span');
      detail.textContent = shortTime(entry.closed_at) + ' UTC · $' + money(entry.price);
      const side = document.createElement('strong');
      side.className = entry.signal.toLowerCase(); side.textContent = entry.signal;
      row.append(detail, side); list.append(row);
    });
  }

  function renderMarket(data) {
    renderSource(data.source);
    const a = data.analysis, latest = data.latest;
    $('priceLabel').textContent = data.source.forming ? 'LAST POLLED PRICE' : 'LAST CLOSED PRICE';
    $('currentPrice').textContent = money(latest.price);
    const change = Number(latest.change_percent);
    const changeDigits = change !== 0 && Math.abs(change) < 0.0005 ? 4 : change !== 0 && Math.abs(change) < 0.01 ? 3 : 2;
    $('priceChange').textContent = (change >= 0 ? '+' : '') + change.toFixed(changeDigits) + '%';
    $('priceChange').classList.toggle('negative', change < 0);
    $('lastClosed').textContent = shortTime(data.source.last_closed_at);
    $('priceComparison').textContent = data.source.forming ? 'Forming candle vs last close' : 'Compared with previous ' + data.interval + ' candle';
    $('metricSignal').textContent = a.signal;
    $('metricSignal').className = a.signal.toLowerCase();
    $('metricSignalSub').textContent = 'Based on the last closed ' + data.interval + ' candle';
    $('metricTrend').textContent = a.trend;
    $('trendIndicator').className = 'metric-indicator ' + (a.trend === 'Bullish' ? 'positive' : a.trend === 'Bearish' ? 'negative' : '');
    $('metricTrendSub').textContent = 'EMA 9: ' + money(a.ema9) + '  ·  21: ' + money(a.ema21);
    $('metricRsi').textContent = Number(a.rsi14).toFixed(2);
    $('metricRsiSub').textContent = a.rsi_zone;
    const rsiBuy = data.interval === '1m' ? [52, 68] : [55, 70];
    const rsiSell = data.interval === '1m' ? [32, 48] : [30, 45];
    $('rsiIndicator').className = 'metric-indicator ' + (a.rsi14 >= rsiBuy[0] && a.rsi14 <= rsiBuy[1] ? 'positive' : a.rsi14 >= rsiSell[0] && a.rsi14 <= rsiSell[1] ? 'negative' : '');
    $('metricAtr').textContent = money(a.atr14);
    const signalClass = a.signal.toLowerCase();
    $('signalFocus').className = 'signal-focus ' + signalClass;
    $('signalBadge').className = 'signal-badge ' + signalClass;
    $('signalBadge').replaceChildren(document.createElement('span'), document.createTextNode(' ' + a.signal));
    $('signalSymbol').className = 'signal-symbol ' + signalClass;
    $('signalSymbol').textContent = a.signal === 'BUY' ? '↗' : a.signal === 'SELL' ? '↘' : '—';
    $('signalHeadline').textContent = data.source.stale ? 'Waiting for fresh data' : a.signal === 'BUY' ? 'Bullish alignment' : a.signal === 'SELL' ? 'Bearish alignment' : 'Waiting for alignment';
    $('signalReason').textContent = a.reason;
    $('trendLabel').textContent = data.interval === '1m' ? '5m trend filter' : 'Trend direction';
    $('trendDescription').textContent = data.interval === '1m' ? '5m EMA 9 versus EMA 21' : 'EMA 9 versus EMA 21';
    $('conditionTrend').textContent = data.interval === '1m' ? (a.bias5m || 'Unavailable') : a.trend;
    $('conditionRsi').textContent = Number(a.rsi14).toFixed(2);
    $('conditionPrice').textContent = '$' + money(latest.closed_price);
    $('analysisInterval').textContent = data.interval;
    $('chartTimeframe').textContent = timeframeLabel(data.interval) + ' TIMEFRAME';
    renderRecentSignals(data);
    drawChart(data);
  }

  function svgNode(type, attributes = {}, text = '') {
    const element = document.createElementNS(ns, type);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text) element.textContent = text;
    return element;
  }
  function axisTime(time, interval) {
    const date = new Date(time);
    if (interval === '4h') return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric', hour: '2-digit', hour12: false }).format(date).replace(',', '');
    return new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
  }
  function drawChart(data) {
    const bars = data.candles.slice(-66);
    const width = 820, height = 260, left = 5, right = 78, top = 12, bottom = 31;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const maxRaw = Math.max(...bars.map((b) => Math.max(b.high, b.ema9, b.ema21)));
    const minRaw = Math.min(...bars.map((b) => Math.min(b.low, b.ema9, b.ema21)));
    const padding = Math.max((maxRaw - minRaw) * 0.12, 0.25);
    const high = maxRaw + padding, low = minRaw - padding;
    const y = (v) => top + (high - v) / (high - low) * plotHeight;
    const priceY = y(data.latest.price);
    const step = plotWidth / bars.length;
    const x = (i) => left + (i + .5) * step;
    const svg = svgNode('svg', { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': `XAU/USD ${data.interval} candlestick chart. Last observed price ${money(data.latest.price)} US dollars per ounce.` });
    svg.append(svgNode('title', {}, data.source.forming ? 'XAU/USD chart: the final candle and its EMA lines are provisional' : 'Closed-candle XAU/USD chart with EMA 9 and EMA 21'));
    for (let i = 0; i <= 4; i++) {
      const yy = top + (plotHeight / 4) * i;
      svg.append(svgNode('line', { x1: left, x2: left + plotWidth, y1: yy, y2: yy, stroke: '#2b362c', 'stroke-width': 1, 'stroke-dasharray': '3 5' }));
      if (Math.abs(yy - priceY) > 14) {
        svg.append(svgNode('text', { x: width - 2, y: yy + 3, 'text-anchor': 'end', fill: '#748677', 'font-size': 9, 'font-family': 'DM Mono, monospace' }, money(high - ((high - low) / 4) * i)));
      }
    }
    for (let i = 0; i < 5; i++) {
      const index = Math.round((bars.length - 1) * i / 4);
      const xx = x(index);
      svg.append(svgNode('line', { x1: xx, x2: xx, y1: top, y2: top + plotHeight, stroke: '#222c23', 'stroke-width': 1 }));
      svg.append(svgNode('text', { x: xx, y: height - 7, 'text-anchor': i === 0 ? 'start' : i === 4 ? 'end' : 'middle', fill: '#718073', 'font-size': 9, 'font-family': 'DM Mono, monospace' }, axisTime(bars[index].time, data.interval)));
    }
    function movingAverage(key, color) {
      const d = bars.map((bar, index) => (index === 0 ? 'M' : 'L') + x(index).toFixed(2) + ' ' + y(bar[key]).toFixed(2)).join(' ');
      svg.append(svgNode('path', { d, fill: 'none', stroke: color, 'stroke-width': 1.5, opacity: .85, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    }
    movingAverage('ema21', '#809ba1'); movingAverage('ema9', '#d8bc7e');
    bars.forEach((bar, index) => {
      const bullish = bar.close >= bar.open;
      const color = bullish ? '#83c8aa' : '#d78e80';
      const candleWidth = Math.max(3.2, Math.min(7.5, step * .58));
      svg.append(svgNode('line', { x1: x(index), x2: x(index), y1: y(bar.high), y2: y(bar.low), stroke: color, 'stroke-width': 1.15, opacity: bar.closed === false ? .5 : .9 }));
      const bodyY = Math.min(y(bar.open), y(bar.close));
      const bodyHeight = Math.max(1.7, Math.abs(y(bar.open) - y(bar.close)));
      svg.append(svgNode('rect', { x: x(index) - candleWidth / 2, y: bodyY, width: candleWidth, height: bodyHeight, fill: color, opacity: bar.closed === false ? .5 : 1, rx: .4 }));
    });
    svg.append(svgNode('line', { x1: left, x2: width - right + 5, y1: priceY, y2: priceY, stroke: '#92caaa', 'stroke-width': .8, 'stroke-dasharray': '4 4', opacity: .65 }));
    svg.append(svgNode('rect', { x: width - right + 7, y: priceY - 9, width: right - 8, height: 18, fill: '#86c8a4', rx: 2 }));
    svg.append(svgNode('text', { x: width - 5, y: priceY + 3, 'text-anchor': 'end', fill: '#101c14', 'font-size': 9, 'font-weight': 700, 'font-family': 'DM Mono, monospace' }, money(data.latest.price)));
    chartTooltip.hidden = true;
    chartContainer.replaceChildren(svg, chartTooltip);
  }

  chartContainer.addEventListener('mousemove', (event) => {
    if (!state.snapshot || !chartContainer.querySelector('svg')) return;
    const rect = chartContainer.getBoundingClientRect();
    const chartX = (event.clientX - rect.left) / rect.width * 820;
    const plotWidth = 820 - 5 - 78;
    const bars = state.snapshot.candles.slice(-66);
    const index = Math.max(0, Math.min(bars.length - 1, Math.floor((chartX - 5) / plotWidth * bars.length)));
    const bar = bars[index];
    if (!bar || chartX < 5 || chartX > 820 - 78) { chartTooltip.hidden = true; return; }
    chartTooltip.textContent = shortTime(bar.time) + ' UTC' + (bar.closed === false ? ' (FORMING)' : ' (CLOSED)') + '  ·  O ' + money(bar.open) + '  H ' + money(bar.high) + '  L ' + money(bar.low) + '  C ' + money(bar.close);
    chartTooltip.hidden = false;
    const left = Math.max(0, Math.min(event.clientX - rect.left + 12, rect.width - chartTooltip.offsetWidth - 4));
    const top = Math.max(0, Math.min(event.clientY - rect.top - 38, rect.height - chartTooltip.offsetHeight - 4));
    chartTooltip.style.left = left + 'px'; chartTooltip.style.top = top + 'px';
  });
  chartContainer.addEventListener('mouseleave', () => { chartTooltip.hidden = true; });

  function expireOldSignal() {
    const data = state.snapshot;
    if (!data || data.source.mode === 'demo' || data.source.stale) return;
    if (Date.now() - Date.parse(data.source.last_closed_at) >= data.source.max_age_seconds * 1000) {
      data.source.stale = true;
      data.analysis.signal = 'WAIT';
      data.analysis.reason = 'The last closed candle is stale; no current entry is issued.';
      data.analysis.bias5m = 'Unavailable';
      data.recent_signals = [];
      renderMarket(data);
    }
  }

  async function init() {
    updateClock(); setInterval(updateClock, 30000);
    setInterval(expireOldSignal, 10000);
    try {
      const session = await api('/api/session');
      state.user = session.user; state.csrf = session.csrf; state.providerConfigured = session.providerConfigured; state.cacheSeconds = session.cacheSeconds;
      renderAccount();
      await loadMarket();
    } catch (err) { clearValues('Could not connect to the server'); renderSourceError(err.message); }
    setInterval(() => loadMarket(false), 60000);
  }
  init();
})();
