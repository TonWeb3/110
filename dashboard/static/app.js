/* ── QUOTEX1 Dashboard Frontend ─────────────────────────── */

const socket = io();
const MAX_LOGS = 300;

// ── State ────────────────────────────────────────────────
let state = {
  botRunning: false,
  connections: { telegram: false, quotex: false },
};

// Holds full loaded config so hardcoded fields are preserved on save
let _loadedConfig = {};
// False until /api/settings has been read successfully. Guards against
// saving a form that was never filled in from disk.
let _settingsLoaded = false;
// True when the server has QUOTEX_EMAIL/QUOTEX_PASSWORD set, in which case the
// Connect form may be left blank and the environment is used instead.
let _envCredentials = false;

// ── DOM helpers ──────────────────────────────────────────
const $ = id => document.getElementById(id);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls)  e.className = cls;
  if (html) e.innerHTML = html;
  return e;
};

// ── Theme ─────────────────────────────────────────────────
const htmlEl = document.documentElement;

function applyTheme(theme) {
  htmlEl.dataset.theme = theme;
  const icon = $('theme-icon');
  if (icon) icon.textContent = theme === 'dark' ? '☀' : '🌙';
  localStorage.setItem('qx1-theme', theme);
}

$('btn-theme')?.addEventListener('click', () => {
  applyTheme(htmlEl.dataset.theme === 'dark' ? 'light' : 'dark');
});

// Sync icon with whatever theme the anti-flash script already applied
applyTheme(htmlEl.dataset.theme || 'dark');

// ── Tabs ─────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const target = tab.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === target));
    document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + target));
    // Re-read config.json each time Settings is opened so the form always shows
    // the live values (e.g. after an external edit), never a stale page-load copy.
    if (target === 'settings') loadSettings();
  });
});

// ── Bot control ──────────────────────────────────────────
const btnToggle = $('btn-toggle');

btnToggle.addEventListener('click', async () => {
  if (btnToggle.classList.contains('loading')) return;
  setBtnLoading(true);

  const action = state.botRunning ? 'stop' : 'start';
  try {
    const res = await api('POST', `/api/bot/${action}`);
    if (!res.success) {
      showToast(res.message || 'Failed', 'error');
      setBtnLoading(false);
    }
    // UI updates via SocketIO state_update / bot_status
  } catch {
    showToast('Connection error', 'error');
    setBtnLoading(false);
  }
});

function setBtnLoading(on) {
  btnToggle.classList.toggle('loading', on);
  btnToggle.innerHTML = on
    ? '<div class="spinner"></div><div class="btn-text">WAIT...</div>'
    : renderBtnContent(state.botRunning);
  btnToggle.disabled = on;
}

function renderBtnContent(running) {
  return running
    ? '<div class="btn-icon">■</div><div class="btn-text">STOP</div>'
    : '<div class="btn-icon">▶</div><div class="btn-text">START</div>';
}

function updateBotButton(running) {
  state.botRunning = running;
  btnToggle.classList.remove('loading');
  btnToggle.classList.toggle('running', running);
  btnToggle.disabled = false;
  btnToggle.innerHTML = renderBtnContent(running);
  refreshAlert();
}

// ── Formatting helpers ────────────────────────────────────
const fmtMoney = (v, pct) => {
  const n = Number(v ?? 0);
  const sign = n < 0 ? '-' : '';
  return pct ? `${sign}${Math.abs(n).toFixed(2)}%` : `${sign}$${Math.abs(n).toFixed(2)}`;
};
const fmtSigned = (v, pct) => (Number(v ?? 0) >= 0 ? '+' : '') + fmtMoney(v, pct);
function fmtCountdown(secs) {
  if (secs === null || secs === undefined) return '--:--';
  const s = Math.max(0, Math.round(secs));
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}
function setBar(id, pct) {
  const b = $(id);
  if (b) b.style.width = Math.max(0, Math.min(100, pct || 0)) + '%';
}

// ── Metrics ───────────────────────────────────────────────
function updateMetrics(s) {
  // Remember whether the server holds Quotex credentials in its environment,
  // so config.json can be deployed without them and the Connect form can
  // accept blank fields instead of demanding a retype.
  if (typeof s.quotex_env_credentials === 'boolean') {
    _envCredentials = s.quotex_env_credentials;
  }
  const isPct = s.risk_mode === 'percent';

  // Balance + account type
  const bal = s.balance;
  $('metric-balance').textContent = bal === null || bal === undefined ? '—' : fmtMoney(bal, false);
  const badge = $('acct-badge');
  const live  = (s.account_type || 'demo') === 'live';
  badge.textContent = live ? 'LIVE' : 'DEMO';
  badge.className   = 'acct-badge' + (live ? ' live' : '');
  $('metric-balance-sub').textContent = bal === null || bal === undefined
    ? 'Connect Quotex to see your balance'
    : (live ? 'Real account' : 'Practice account');

  // P&L
  const pnlEl = $('metric-pnl');
  const pnl   = Number(s.daily_pnl ?? 0);
  pnlEl.textContent = fmtSigned(pnl, isPct);
  pnlEl.className   = 'metric-value ' + (pnl >= 0 ? 'text-green' : 'text-red');
  const dayOpen = s.day_open_balance;
  $('metric-pnl-sub').textContent =
    dayOpen === null || dayOpen === undefined
      ? (isPct ? 'Daily P&L (% of balance)' : 'Realized daily P&L')
      : `since today's open ${fmtMoney(dayOpen, false)}`;

  // Win rate
  const wins = s.wins ?? 0, losses = s.losses ?? 0, total = wins + losses;
  $('metric-winrate').textContent = total ? Math.round((wins / total) * 100) + '%' : '—';
  $('metric-winrate-sub').textContent = total ? `${wins}W / ${losses}L settled` : 'No settled trades yet';

  // Daily trades vs limit
  const trades = s.daily_trades ?? 0, maxTrades = s.max_daily_trades ?? 0;
  $('metric-trades').textContent = trades;
  $('metric-trades-sub').textContent = maxTrades ? `of ${maxTrades} allowed today` : 'today';
  setBar('bar-trades', maxTrades ? (trades / maxTrades) * 100 : 0);

  // Daily loss vs limit
  const loss = Number(s.daily_loss ?? 0), maxLoss = Number(s.max_daily_loss ?? 0);
  const lossOn = !!s.max_daily_loss_enabled;
  const lossEl = $('metric-loss');
  lossEl.textContent = fmtMoney(loss, isPct);
  lossEl.className   = 'metric-value ' + (loss > 0 ? 'text-red' : '');
  $('metric-loss-sub').textContent = lossOn
    ? (loss >= maxLoss && maxLoss > 0 ? `LIMIT HIT — ${fmtMoney(maxLoss, isPct)}` : `limit ${fmtMoney(maxLoss, isPct)}`)
    : 'Limit disabled';
  setBar('bar-loss', lossOn && maxLoss ? (loss / maxLoss) * 100 : 0);

  // In flight
  const pending = s.pending || [];
  const activeTrades = s.active_trades ?? 0;
  $('metric-signals').textContent = pending.length + activeTrades;
  $('metric-signals-sub').textContent =
    `${pending.length} scheduled · ${activeTrades}/${s.max_concurrent_trades ?? 1} live`;

  setQueue(pending);
  renderLastTrade(s.last_trade);
}

// ── Upcoming entries (with a locally-ticking countdown) ───
let _queue = [];        // [{asset, direction, entry_time, expiry, seconds_left, _at}]

function setQueue(pending) {
  const now = Date.now();
  _queue = (pending || []).map(p => ({ ...p, _at: now }));
  renderQueue();
}

function renderQueue() {
  const box = $('queue');
  if (!box) return;
  if (!_queue.length) {
    box.innerHTML = '<div class="queue-empty">No signals scheduled — waiting for the next channel message.</div>';
    return;
  }
  box.innerHTML = _queue.map(q => {
    const left = q.seconds_left === null || q.seconds_left === undefined
      ? null
      : q.seconds_left - (Date.now() - q._at) / 1000;
    const firing = left !== null && left <= 0;
    const up  = q.direction === 'call';
    return `<div class="queue-item${firing ? ' firing' : ''}">
        <span class="dir-pill ${up ? 'up' : 'down'}">${up ? '▲ CALL' : '▼ PUT'}</span>
        <span class="queue-asset">${escHtml(q.asset || '')}</span>
        <span class="queue-meta">${escHtml(q.expiry || '')} @ ${escHtml(q.entry_time || '—')}</span>
        <span class="queue-count">${firing ? 'FIRING' : fmtCountdown(left)}</span>
      </div>`;
  }).join('');
}

setInterval(() => { if (_queue.length) renderQueue(); }, 1000);

// ── Last trade ────────────────────────────────────────────
function renderLastTrade(t) {
  const box = $('last-trade');
  if (!box) return;
  if (!t) {
    box.innerHTML = '<div class="queue-empty">No trades placed yet.</div>';
    return;
  }
  const up = t.direction === 'call';
  const res = t.result;
  const resCls = res === 'win' ? 'win' : res === 'loss' ? 'loss' : 'pending';
  const resTxt = res === 'win' ? 'WIN' : res === 'loss' ? 'LOSS'
               : res === 'unknown' ? 'UNKNOWN' : 'IN PROGRESS';
  const profit = (t.profit === null || t.profit === undefined) ? '' : fmtSigned(t.profit, false);
  box.innerHTML = `<div class="queue-item">
      <span class="dir-pill ${up ? 'up' : 'down'}">${up ? '▲ CALL' : '▼ PUT'}</span>
      <span class="queue-asset">${escHtml(t.asset || '')}</span>
      <span class="queue-meta">$${Number(t.amount ?? 0).toFixed(2)} · ${t.duration ?? 60}s · ${escHtml(t.at || '')}${t.latency != null ? ` · fill ${t.latency}s` : ''}</span>
      <span class="res-pill ${resCls}">${resTxt}${profit ? ' ' + profit : ''}</span>
    </div>`;
}

// ── Header status ─────────────────────────────────────────
function updateHeaderStatus(running) {
  const pill = $('header-status');
  pill.className = 'header-status ' + (running ? 'running' : 'stopped');
  pill.innerHTML = `<div class="status-dot ${running ? 'pulse' : ''}"></div>${running ? 'RUNNING' : 'STOPPED'}`;
}

// ── Alert banner ──────────────────────────────────────────
let _serverAlert = null;   // last real error from bot_state.json

function setAlert(msg) {
  const banner = $('alert-banner');
  if (msg) {
    $('alert-text').textContent = msg;
    banner.classList.add('visible');
  } else {
    banner.classList.remove('visible');
  }
}

/**
 * Decide what (if anything) to show in the alert banner.
 * Priority:
 *   1. Real server error alert (login failures, health monitor errors)
 *   2. Dynamic connection warning when bot is running but connections missing
 *   3. Nothing
 */
function refreshAlert() {
  // Real error from the bot always takes priority
  if (_serverAlert) {
    setAlert(_serverAlert);
    return;
  }
  // Dynamic connection warnings (only relevant when bot is running)
  if (state.botRunning) {
    const tg = state.connections.telegram;
    const qx = state.connections.quotex;
    if (!tg && !qx) {
      setAlert('Telegram and Quotex are not connected — configure both in Settings to enable trading.');
      return;
    }
    if (!qx) {
      setAlert('Quotex not connected — go to Settings → Quotex Account and click Connect.');
      return;
    }
    if (!tg) {
      setAlert('Telegram not connected — go to Settings → Connections and connect Telegram.');
      return;
    }
  }
  setAlert(null);
}

document.querySelector('.alert-close')?.addEventListener('click', () => {
  _serverAlert = null;
  setAlert(null);
});

// ── Activity log ──────────────────────────────────────────
const logPanel = $('log-panel');

function addLog(time, msg, level = 'INFO') {
  const entry = el('div', 'log-entry');
  entry.innerHTML = `<span class="log-time">${time}</span><span class="log-msg ${level}">${escHtml(msg)}</span>`;
  logPanel.appendChild(entry);

  // Trim old entries
  while (logPanel.children.length > MAX_LOGS) {
    logPanel.removeChild(logPanel.firstChild);
  }
  // Auto-scroll if near bottom
  if (logPanel.scrollHeight - logPanel.scrollTop - logPanel.clientHeight < 60) {
    logPanel.scrollTop = logPanel.scrollHeight;
  }
}

/**
 * Load the tail of the log FILE so a page reload shows history instead of an
 * empty console. Live lines keep streaming in over SocketIO afterwards.
 */
async function loadLogHistory(lines = 300) {
  const res = await api('GET', `/api/logs?lines=${lines}`);
  if (!res || !Array.isArray(res.lines)) return;
  logPanel.innerHTML = '';
  if (!res.lines.length) {
    addLog(now(), res.exists ? `${res.file} is empty` : 'No log file yet — start the bot', 'INFO');
    return;
  }
  res.lines.forEach(l => addLog(l.time, l.message, l.level));
  addLog(now(), `— ${res.lines.length} earlier lines loaded from ${res.file} —`, 'INFO');
  logPanel.scrollTop = logPanel.scrollHeight;
}

$('btn-clear-log')?.addEventListener('click', () => {
  logPanel.innerHTML = '';
  addLog(now(), 'View cleared — the log file is untouched. Reload to see history again.', 'INFO');
});

$('btn-download-log')?.addEventListener('click', () => {
  window.location.href = '/api/logs/download';
});

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── SocketIO events ───────────────────────────────────────
socket.on('connect', () => addLog(now(), 'Dashboard connected', 'INFO'));
socket.on('disconnect', () => addLog(now(), 'Dashboard disconnected', 'WARNING'));

socket.on('state_update', s => {
  const running = s.bot_running ?? false;
  if (running !== state.botRunning) updateBotButton(running);
  updateHeaderStatus(running);
  updateMetrics(s);
  // Store real server errors; connection warnings are handled dynamically by refreshAlert()
  _serverAlert = s.alert || null;
  if (s.connections) updateConnectionBadges(s.connections);
  // refreshAlert() is called inside updateConnectionBadges → always runs last
});

socket.on('bot_status', d => {
  updateBotButton(d.running);
  updateHeaderStatus(d.running);
});

socket.on('log', d => addLog(d.time || now(), d.message, d.level || 'INFO'));


// ── Connection badges ─────────────────────────────────────
function updateConnectionBadges(conn) {
  state.connections = conn;
  setConnBadge('tg-badge',     conn.telegram, 'Connected', 'Offline');
  setConnBadge('qx-badge',     conn.quotex,   'Connected', 'Offline');
  setConnBadge('tg-badge-hdr', conn.telegram, 'Connected', 'Offline');
  setConnBadge('qx-badge-hdr', conn.quotex,   'Connected', 'Offline');
  refreshAlert();
}

function setConnBadge(id, ok, yesLabel, noLabel) {
  const el = $(id);
  if (!el) return;
  el.className = 'conn-badge ' + (ok ? 'connected' : 'disconnected');
  el.innerHTML = `<div class="status-dot${ok ? ' pulse' : ''}"></div>${ok ? yesLabel : noLabel}`;
}

// ── Telegram auth modal ───────────────────────────────────
let tgPhone = '';

$('btn-tg-connect')?.addEventListener('click', () => {
  if (state.connections.telegram) {
    disconnectTelegram();
  } else {
    openModal('modal-telegram');
    showTgStep('step-phone');
  }
});

$('btn-tg-send-code')?.addEventListener('click', async () => {
  const phone = $('tg-phone').value.trim();
  if (!phone) { showFieldError('tg-phone', 'Enter your phone number'); return; }
  tgPhone = phone;
  setBtnBusy('btn-tg-send-code', true, 'Sending...');

  const res = await api('POST', '/api/telegram/connect', { phone });
  setBtnBusy('btn-tg-send-code', false, 'Send Code');

  if (res.success) {
    showTgStep('step-code');
  } else {
    showToast(res.message || 'Failed to send code', 'error');
  }
});

$('btn-tg-verify')?.addEventListener('click', async () => {
  const code = $('tg-code').value.trim();
  if (!code) { showFieldError('tg-code', 'Enter the code'); return; }
  setBtnBusy('btn-tg-verify', true, 'Verifying...');

  const res = await api('POST', '/api/telegram/verify', { phone: tgPhone, code });
  setBtnBusy('btn-tg-verify', false, 'Verify');

  if (res.success) {
    showTgStep('step-success');
    state.connections.telegram = true;
    updateConnectionBadges(state.connections);
    setTimeout(() => closeModal('modal-telegram'), 2000);
  } else if (res.needs_password) {
    showTgStep('step-password');
  } else {
    showToast(res.message || 'Invalid code', 'error');
  }
});

$('btn-tg-password')?.addEventListener('click', async () => {
  const pw = $('tg-password').value;
  if (!pw) { showFieldError('tg-password', 'Enter your password'); return; }
  setBtnBusy('btn-tg-password', true, 'Verifying...');

  const res = await api('POST', '/api/telegram/password', { password: pw });
  setBtnBusy('btn-tg-password', false, 'Submit');

  if (res.success) {
    showTgStep('step-success');
    state.connections.telegram = true;
    updateConnectionBadges(state.connections);
    setTimeout(() => closeModal('modal-telegram'), 2000);
  } else {
    showToast(res.message || 'Wrong password', 'error');
  }
});

function showTgStep(id) {
  document.querySelectorAll('#modal-telegram .modal-step').forEach(s => {
    s.classList.toggle('active', s.id === id);
  });
}

async function disconnectTelegram() {
  await api('POST', '/api/telegram/disconnect');
  state.connections.telegram = false;
  updateConnectionBadges(state.connections);
  showToast('Telegram disconnected', 'info');
}

// ── Quotex connect modal ──────────────────────────────────

function showQxStep(id) {
  document.querySelectorAll('#modal-quotex .modal-step').forEach(s => {
    s.classList.toggle('active', s.id === id);
  });
}

$('btn-qx-connect')?.addEventListener('click', () => {
  if (state.connections.quotex) {
    disconnectQuotex();
  } else {
    openModal('modal-quotex');
    showQxStep('qx-step-form');
    // Pre-fill from the Settings form fields (always reflects the latest saved values)
    setValue('qx-email',    $('s-qx-email')?.value    || '');
    setValue('qx-password', $('s-qx-password')?.value || '');
  }
});

$('btn-qx-save')?.addEventListener('click', async () => {
  const email    = $('qx-email').value.trim();
  const password = $('qx-password').value;
  // Blank is legitimate when the server holds the credentials in its
  // environment — that is how config.json is deployed without them.
  if (!_envCredentials) {
    if (!email)    { showFieldError('qx-email',    'Enter your Quotex email');    return; }
    if (!password) { showFieldError('qx-password', 'Enter your Quotex password'); return; }
  }

  showQxStep('qx-step-testing');

  // /api/quotex/connect may block for up to 10 minutes (OTP wait) — don't await here;
  // the result comes back via SocketIO events (quotex_otp_required / connection_update).
  api('POST', '/api/quotex/connect', { email, password }).then(res => {
    if ($('qx-step-testing').classList.contains('active') ||
        $('qx-step-pin').classList.contains('active')) {
      if (res.success) {
        _onQxConnected(email, password);
      } else {
        $('quotex-error-msg').textContent = res.message || 'Login failed — check your credentials.';
        showQxStep('qx-step-failed');
      }
    }
  });
});

// SocketIO: Quotex requests a PIN from the user's email
socket.on('quotex_otp_required', d => {
  showQxStep('qx-step-pin');
  setValue('qx-pin', '');
  $('qx-pin')?.focus();
});

$('btn-qx-pin')?.addEventListener('click', async () => {
  const pin = ($('qx-pin')?.value || '').replace(/\s/g, '');
  if (!pin) { showFieldError('qx-pin', 'Enter the PIN from your email'); return; }
  setBtnBusy('btn-qx-pin', true, 'Submitting…');

  const res = await api('POST', '/api/quotex/pin', { pin });
  setBtnBusy('btn-qx-pin', false, 'Submit PIN');

  if (res.success) {
    // PIN submitted — go back to "testing" spinner while connect continues
    showQxStep('qx-step-testing');
  } else {
    showToast(res.message || 'Failed to submit PIN', 'error');
  }
});

// SocketIO: Quotex connected successfully (fired after OTP accepted)
socket.on('connection_update', d => {
  if (d.telegram !== undefined) state.connections.telegram = d.telegram;
  if (d.quotex   !== undefined) {
    state.connections.quotex = d.quotex;
    if (d.quotex && document.getElementById('modal-quotex')?.classList.contains('open')) {
      const email    = $('qx-email')?.value    || '';
      const password = $('qx-password')?.value || '';
      _onQxConnected(email, password);
    }
  }
  updateConnectionBadges(state.connections);
});

function _onQxConnected(email, password) {
  showQxStep('qx-step-success');
  state.connections.quotex = true;
  updateConnectionBadges(state.connections);
  if (!email && !password) {
    // Connected using the server's environment credentials — there is nothing
    // to mirror into the Settings fields, and blanking them would be wrong.
    return;
  }
  setValue('s-qx-email',    email);
  setValue('s-qx-password', password);
  _loadedConfig.quotex = { ...(_loadedConfig.quotex || {}), email, password };
  setTimeout(() => closeModal('modal-quotex'), 2000);
}

async function disconnectQuotex() {
  await api('POST', '/api/quotex/disconnect');
  state.connections.quotex = false;
  updateConnectionBadges(state.connections);
  showToast('Quotex credentials cleared', 'info');
}

// ── Settings ──────────────────────────────────────────────
async function loadSettings() {
  const cfg = await api('GET', '/api/settings');
  // The server always returns the EFFECTIVE settings (config.json overlaid on
  // the defaults), so the form is populated even when the file is imperfect —
  // and saving is how the user repairs it. Only a genuine transport failure,
  // where the current settings are unknown, leaves the form blank.
  if (!cfg || cfg.success === false || !cfg.trading) {
    _settingsLoaded = false;
    showToast('Could not reach the server for settings: ' +
              ((cfg && cfg.message) || 'no response') +
              ' — fields left blank so nothing is overwritten.', 'error');
    return;
  }
  _settingsLoaded = true;
  if (cfg.warning) showToast(cfg.warning, 'error');
  _loadedConfig = cfg;  // preserve hardcoded fields for save

  const t = cfg.telegram || {};
  const q = cfg.quotex   || {};
  const tr = cfg.trading  || {};
  const lo = cfg.logging  || {};

  // Telegram (session_name is not exposed in the UI)
  setValue('s-api-id',   t.api_id   || '');
  setValue('s-api-hash', t.api_hash || '');

  // Quotex
  setValue('s-qx-email',    q.email    || '');
  setValue('s-qx-password', q.password || '');
  setValue('s-early-entry', q.early_entry_seconds ?? 0.5);
  setValue('s-late-grace',  q.late_entry_grace_seconds ?? 5);

  // Trading
  setValue('s-account-type', tr.account_type || 'demo');
  setValue('s-risk-mode',    tr.risk_mode    || 'fixed');
  applyRiskModeUnits(tr.risk_mode || 'fixed');
  setValue('s-risk-amount',  tr.risk_amount  ?? 1);
  setValue('s-max-trades',   tr.max_daily_trades   ?? 10);
  setValue('s-max-loss',     tr.max_daily_loss     ?? 50);
  setChecked('s-max-loss-enabled', tr.max_daily_loss_enabled ?? true);
  toggleMaxLossField(tr.max_daily_loss_enabled ?? true);
  setValue('s-max-concurrent', tr.max_concurrent_trades ?? 1);

  // Martingale
  setChecked('s-martingale-enabled', tr.martingale_enabled || false);
  setValue('s-martingale-mult',   tr.martingale_multiplier ?? 2.0);
  setValue('s-martingale-steps',  tr.martingale_steps  ?? 2);
  toggleMartingaleFields(tr.martingale_enabled || false);

  // Logging
  setValue('s-log-level', lo.log_level || 'INFO');
  setValue('s-log-file',  lo.log_file  || 'quotex_bot.log');

  // Channels
  renderChannels(t.channels || []);
}

function renderChannels(channels) {
  const list = $('channel-list');
  list.innerHTML = '';
  (channels.length ? channels : [{ enabled: true, identifier: '' }]).forEach((ch, i) => {
    const item = el('div', 'channel-item');
    item.innerHTML = `
      <label class="toggle" title="Enable/Disable">
        <input type="checkbox" ${ch.enabled ? 'checked' : ''} data-ch="${i}" class="ch-enabled">
        <span class="toggle-track"></span>
      </label>
      <input type="text" value="${ch.identifier || ''}" placeholder="Channel name, @username or numeric ID"
             data-ch="${i}" class="ch-id" style="flex:1;">
      <button class="btn btn-danger btn-sm" onclick="removeChannel(${i})">✕</button>`;
    list.appendChild(item);
  });
}

function addChannel() {
  const channels = getChannelsFromDOM();
  channels.push({ enabled: true, identifier: '' });
  renderChannels(channels);
}

function removeChannel(idx) {
  const channels = getChannelsFromDOM();
  channels.splice(idx, 1);
  renderChannels(channels.length ? channels : [{ enabled: true, identifier: '' }]);
}

function getChannelsFromDOM() {
  const items = document.querySelectorAll('.channel-item');
  return Array.from(items).map(item => ({
    enabled:    item.querySelector('.ch-enabled').checked,
    identifier: item.querySelector('.ch-id').value.trim(),
  }));
}

$('s-martingale-enabled')?.addEventListener('change', function () {
  toggleMartingaleFields(this.checked);
});

function toggleMartingaleFields(on) {
  $('martingale-fields').style.display = on ? '' : 'none';
}

// Daily-loss limit on/off — grey out the amount field when disabled.
$('s-max-loss-enabled')?.addEventListener('change', function () {
  toggleMaxLossField(this.checked);
});

function toggleMaxLossField(on) {
  const inp = $('s-max-loss');
  if (!inp) return;
  inp.disabled = !on;
  inp.style.opacity = on ? '' : '0.45';
}

// When risk mode is "% of balance", risk amount, daily loss (and P&L) are all
// percentages — relabel the settings inputs so the units match.
$('s-risk-mode')?.addEventListener('change', function () {
  applyRiskModeUnits(this.value);
});

function applyRiskModeUnits(mode) {
  const isPct = mode === 'percent';
  const ra = $('lbl-risk-amount');
  const ml = $('lbl-max-loss');
  if (ra) ra.textContent = isPct ? 'Risk Amount (% of balance)' : 'Risk Amount ($)';
  if (ml) ml.textContent = isPct ? 'Max Daily Loss (%)'         : 'Max Daily Loss ($)';
}

async function saveSettings() {
  if (!_settingsLoaded) {
    // Otherwise this would write the form's fallback values (risk 1, 10 trades,
    // martingale off) straight over the real configuration.
    showToast('Settings were never loaded — refusing to save and overwrite ' +
              'config.json. Reload the page first.', 'error');
    return;
  }
  const btn = $('btn-save');
  btn.classList.add('btn-saving');
  btn.textContent = 'Saving...';
  btn.disabled = true;

  const cfg = buildConfigFromForm();
  const res = await api('POST', '/api/settings', cfg);

  btn.classList.remove('btn-saving');
  btn.textContent = 'Save Settings';
  btn.disabled = false;

  if (res.success) showToast('Settings saved', 'success');
  else             showToast('Save failed: ' + (res.message || ''), 'error');
}

function buildConfigFromForm() {
  return {
    telegram: {
      // Only the fields the UI edits are sent. The server deep-merges this over
      // the existing config.json, so session_name is preserved on disk.
      api_id:   parseInt($('s-api-id').value)  || 0,
      api_hash: $('s-api-hash').value.trim(),
      channels: getChannelsFromDOM(),
    },
    quotex: {
      email:               $('s-qx-email').value.trim(),
      password:            $('s-qx-password').value,
      early_entry_seconds: Math.min(5, Math.max(0, parseFloat($('s-early-entry').value) || 0)),
      late_entry_grace_seconds: Math.min(60, Math.max(0, parseFloat($('s-late-grace').value) || 5)),
    },
    trading: {
      account_type:          $('s-account-type').value,
      risk_mode:             $('s-risk-mode').value,
      risk_amount:           parseFloat($('s-risk-amount').value) || 1,
      max_daily_trades:      parseInt($('s-max-trades').value)   || 10,
      max_daily_loss:        parseFloat($('s-max-loss').value)   || 50,
      max_daily_loss_enabled: $('s-max-loss-enabled').checked,
      max_concurrent_trades: parseInt($('s-max-concurrent').value) || 1,
      martingale_enabled:    $('s-martingale-enabled').checked,
      martingale_multiplier: parseFloat($('s-martingale-mult').value)  || 2.0,
      martingale_steps:      parseInt($('s-martingale-steps').value)   || 2,
    },
    logging: {
      log_level: $('s-log-level').value,
      log_file:  $('s-log-file').value.trim() || 'quotex_bot.log',
    },
  };
}

// ── Modal helpers ─────────────────────────────────────────
function openModal(id) {
  $(id).classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeModal(id) {
  $(id).classList.remove('open');
  document.body.style.overflow = '';
}
// Close on backdrop click
document.querySelectorAll('.modal-overlay').forEach(overlay => {
  overlay.addEventListener('click', e => {
    if (e.target === overlay) closeModal(overlay.id);
  });
});
document.querySelectorAll('.modal-close').forEach(btn => {
  btn.addEventListener('click', () => closeModal(btn.closest('.modal-overlay').id));
});

// ── Toast notifications ───────────────────────────────────
function showToast(msg, type = 'info') {
  const toast = el('div', `toast-popup ${type}`, escHtml(msg));
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3500);
}

// ── API helper ────────────────────────────────────────────
async function api(method, url, body) {
  try {
    const opts = { method, headers: { 'Content-Type': 'application/json' }, cache: 'no-store' };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      // Without this an error body looked like a config object, and every
      // "?? default" below quietly filled the form with invented values.
      return { success: false, _httpError: res.status,
               message: data.error || data.message || ('HTTP ' + res.status) };
    }
    return data;
  } catch (e) {
    return { success: false, message: e.message };
  }
}

// ── Misc helpers ──────────────────────────────────────────
function now() {
  return new Date().toLocaleTimeString('en-GB', { hour12: false });
}
function setValue(id, val) {
  const e = $(id);
  if (e) e.value = val;
}
function setChecked(id, val) {
  const e = $(id);
  if (e) e.checked = !!val;
}
function setBtnBusy(id, busy, label) {
  const e = $(id);
  if (!e) return;
  e.disabled = busy;
  e.textContent = label;
}
function showFieldError(id, msg) {
  const e = $(id);
  if (e) { e.focus(); e.style.borderColor = 'var(--red)'; setTimeout(() => e.style.borderColor = '', 2000); }
  showToast(msg, 'error');
}

// ── Init ──────────────────────────────────────────────────
(async () => {
  await loadSettings();
  await loadLogHistory();

  // Initial status fetch
  const status = await api('GET', '/api/status');
  if (status) {
    _serverAlert = status.alert || null;
    updateBotButton(status.bot_running || false);
    updateHeaderStatus(status.bot_running || false);
    updateMetrics(status);
    if (status.connections) updateConnectionBadges(status.connections);
    // refreshAlert() is called inside updateConnectionBadges
  }
})();
