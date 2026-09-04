// Reads the base path rendered by layout.html.j2's <meta name="omx-base-path"> tag.
function getBasePath() {
  const m = document.querySelector('meta[name="omx-base-path"]');
  return (m && m.getAttribute('content')) || '';
}
function getThemeColors() {
  const style = getComputedStyle(document.documentElement);
  return {
    background: style.getPropertyValue('--bg-terminal').trim() || '#111',
    foreground: style.getPropertyValue('--fg-terminal').trim() || '#fff'
  };
}
const initialColors = getThemeColors();
const term = new Terminal({ convertEol: true, theme: { background: initialColors.background, foreground: initialColors.foreground } });

window.addEventListener('theme-changed', () => {
  const colors = getThemeColors();
  term.options.theme = { background: colors.background, foreground: colors.foreground };
});

const fitAddon = (window.FitAddon) ? new window.FitAddon.FitAddon() : null;
if (fitAddon) term.loadAddon(fitAddon);
const termEl = document.getElementById('term');
term.open(termEl);
function fitTerminal() { try { fitAddon && fitAddon.fit(); } catch (_) {} }
window.fitTerminal = fitTerminal; // Expose for layout sidebar toggle
window.addEventListener('load', () => { fitTerminal(); setTimeout(fitTerminal, 0); });
window.addEventListener('resize', () => { fitTerminal(); fitActionTerminal(); });
try {
  const ro = new ResizeObserver(() => { fitTerminal(); fitActionTerminal(); });
  ro.observe(document.getElementById('term-container'));
  // Opening/closing the pane does not resize #term-container itself, so the
  // pane needs its own observer to re-fit after a display or width change.
  ro.observe(document.getElementById('actionTermPane'));
} catch (_) {}

const qs = new URLSearchParams(window.location.search);
const bannerEl = document.getElementById('banner');
const portDisplayName = document.getElementById('portDisplayName');
const portDisplayDesc = document.getElementById('portDisplayDesc');
const selectedPortName = (qs.get('port') || '').trim();
const connectBtn = document.getElementById('connect');
const logsBtn = document.getElementById('logsButton');
let ws; let ports = []; let currentConnectedPort = null; let portIsUp = false;
// Debounce timer to delay showing WS-down banner during fast reconnects
let wsDownTimer = null;
// Debounce timer to delay showing Port-down (yellow) banner to avoid flicker during quick switches
let portDownTimer = null;

function updatePortDisplay() {
  if (!portDisplayName || !portDisplayDesc) return;
  const p = ports.find(x => x.name === selectedPortName);
  portDisplayName.textContent = selectedPortName || 'No console selected';
  portDisplayDesc.textContent = (p && p.description) ? p.description : '';
}
function populatePorts(list) {
  ports = Array.isArray(list) ? list : [];
  updatePortDisplay();
}
async function loadPorts() {
  try {
    // Build an absolute URL from origin to avoid inheriting any userinfo (username:password) from the page URL
    const url = new URL(getBasePath() + '/api/ports', window.location.origin);
    const res = await fetch(url.toString(), { cache: 'no-store', credentials: 'same-origin' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    populatePorts(data.ports || []);
  } catch (e) {
    console.warn('Failed to load ports list:', e);
  }
}

// --- Info overlay helpers ---
const infoOverlay = document.getElementById('infoOverlay');
const infoBody = document.getElementById('infoBody');
const infoToggle = document.getElementById('infoToggle');
const infoClose = document.getElementById('infoClose');
let infoTimer = null;
// Minimal mode tracking per port (updated from server control frames)
let clientMode = 'read-only';
// This WS connection's own client_id, learned from the server's initial client_mode
// frame; passed to Port Actions run requests so the runner can self-demote/restore us.
let myClientId = null;
// Ctrl+E menu wiring
const ctrlMenu = document.getElementById('ctrlMenu');
const ctrlClose = document.getElementById('ctrlClose');
const ctrlReqRW = document.getElementById('ctrlReqRW');
const ctrlReconnect = document.getElementById('ctrlReconnect');
const ctrlDisconnect = document.getElementById('ctrlDisconnect');
const menuToggle = document.getElementById('menuToggle');
function showCtrlMenu() { ctrlMenu.style.display = 'block'; }
function hideCtrlMenu() { ctrlMenu.style.display = 'none'; }
ctrlClose.addEventListener('click', hideCtrlMenu);
ctrlReqRW.addEventListener('click', () => { try { ws && ws.send('OMXCTRL ' + JSON.stringify({ type: 'request_rw' })); } catch (_) {} });
ctrlReconnect.addEventListener('click', () => { try { abortSlowPaste('Reconnecting'); if (isConnected()) ws.close(1000, 'Client requested reconnect'); connectSelected(); } catch (_) {} });
ctrlDisconnect.addEventListener('click', () => { try { abortSlowPaste('Disconnecting'); if (isConnected()) ws.close(1000, 'Client requested disconnect'); } catch (_) {} hideCtrlMenu(); });
if (menuToggle) menuToggle.addEventListener('click', () => { if (ctrlMenu.style.display === 'none') { closeRoMenu(); closeViewersMenu(); showCtrlMenu(); } else hideCtrlMenu(); });
const ctrlReleaseRW = document.getElementById('ctrlReleaseRW');
const ctrlForceTake = document.getElementById('ctrlForceTake');
const roIndicator = document.getElementById('roIndicator');
const roIndicatorWrap = document.getElementById('roIndicatorWrap');
const roMenu = document.getElementById('roMenu');
const roMenuInfo = document.getElementById('roMenuInfo');
let lastRwHolders = [];
let lastMaxRwUsers = null;
function updateRoMenuInfo(holders, maxRwUsers) {
  lastRwHolders = Array.isArray(holders) ? holders : [];
  if (maxRwUsers !== undefined) lastMaxRwUsers = maxRwUsers;
  if (!roMenuInfo) return;
  if (lastMaxRwUsers === 0) {
    roMenuInfo.textContent = 'Read-write is disabled on this port';
  } else if (lastRwHolders.length) {
    roMenuInfo.textContent = 'Held by: ' + lastRwHolders.join(', ');
  } else {
    roMenuInfo.textContent = 'No current read-write holder';
  }
  roMenuInfo.style.display = '';
}
function closeRoMenu() { if (roMenu) roMenu.style.display = 'none'; }
if (roIndicator) roIndicator.addEventListener('click', (e) => {
  e.stopPropagation();
  const opening = roMenu && roMenu.style.display === 'none';
  if (opening) { hideCtrlMenu(); closeViewersMenu(); }
  if (roMenu) roMenu.style.display = opening ? 'block' : 'none';
  // Refresh holder info from server whenever the dropdown is opened
  if (opening) { try { ws && isConnected() && ws.send('OMXCTRL ' + JSON.stringify({ type: 'query_rw_holders' })); } catch (_) {} }
});
document.addEventListener('click', () => closeRoMenu());
const roMenuReqRW = document.getElementById('roMenuReqRW');
const roMenuForceRW = document.getElementById('roMenuForceRW');
if (roMenuReqRW) roMenuReqRW.addEventListener('click', () => { closeRoMenu(); try { ws && ws.send('OMXCTRL ' + JSON.stringify({ type: 'request_rw' })); } catch (_) {} });
if (roMenuForceRW) roMenuForceRW.addEventListener('click', () => { closeRoMenu(); try { ws && ws.send('OMXCTRL ' + JSON.stringify({ type: 'force_promote' })); } catch (_) {} });

// Ambient viewer-presence badge (GitHub issue #48): a small always-visible
// "N others" chip, driven purely by 'presence' control frames the server
// broadcasts on attach/detach/promote/demote - no polling, no toasts.
const viewersBadgeWrap = document.getElementById('viewersBadgeWrap');
const viewersBadge = document.getElementById('viewersBadge');
const viewersCount = document.getElementById('viewersCount');
const viewersMenu = document.getElementById('viewersMenu');
const viewersMenuList = document.getElementById('viewersMenuList');
function closeViewersMenu() { if (viewersMenu) viewersMenu.style.display = 'none'; }
function updateViewersBadge(viewers) {
  const list = Array.isArray(viewers) ? viewers : [];
  const others = Math.max(0, list.length - 1);
  if (viewersBadgeWrap) viewersBadgeWrap.style.display = others > 0 ? '' : 'none';
  if (viewersCount) viewersCount.textContent = String(others);
  if (viewersBadge) viewersBadge.title = others === 1 ? '1 other viewer' : `${others} other viewers`;
  if (viewersMenuList) {
    viewersMenuList.innerHTML = '';
    const canTakeNow = clientMode !== 'read-write';
    list.forEach((v) => {
      const row = document.createElement('div');
      const mine = !!myClientId && v.client_id === myClientId;
      // "<muxcon-server>/username@<ip>" for a federated remote viewer, or just
      // "username@<ip>" for one local to this server (server_id omitted - implied).
      const who = `${v.username || 'unknown'}@${v.ip || 'unknown'}`;
      const label = `${v.server_id ? `${v.server_id}/${who}` : who} (${v.mode === 'read-write' ? 'read-write' : 'read-only'})`;
      row.textContent = mine ? `${label} (me)` : label;
      // Per-holder takeover (issue #59 Part 2): named "Take control" for a
      // read-write holder that is not the viewer itself, on a LOCAL port
      // (federated remote entries carry server_id; their takeover has no
      // local client_id to target). The server re-checks entitlement and
      // demotes that one holder, restoring it if the promotion fails.
      if (canTakeNow && !mine && v.mode === 'read-write' && v.client_id && !v.server_id) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'ro-menu-item';
        btn.textContent = 'Take control';
        btn.style.marginTop = '2px';
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          closeViewersMenu();
          try { ws && ws.send('OMXCTRL ' + JSON.stringify({ type: 'force_promote', client_id: v.client_id })); } catch (_) {}
        });
        row.appendChild(btn);
      }
      viewersMenuList.appendChild(row);
    });
    viewersMenuList.style.display = list.length ? '' : 'none';
  }
}
if (viewersBadge) viewersBadge.addEventListener('click', (e) => {
  e.stopPropagation();
  const opening = viewersMenu && viewersMenu.style.display === 'none';
  if (opening) { hideCtrlMenu(); closeRoMenu(); }
  if (viewersMenu) viewersMenu.style.display = opening ? 'block' : 'none';
});
document.addEventListener('click', () => closeViewersMenu());
function updateCtrlMenuButtons() {
  const isRW = (clientMode === 'read-write');
  if (ctrlReqRW) ctrlReqRW.style.display = isRW ? 'none' : '';
  if (ctrlForceTake) ctrlForceTake.style.display = isRW ? 'none' : '';
  if (ctrlReleaseRW) ctrlReleaseRW.style.display = isRW ? '' : 'none';
  if (roIndicatorWrap) roIndicatorWrap.style.display = isRW ? 'none' : '';
  document.body.classList.toggle('mode-readonly', !isRW);
}
if (ctrlReleaseRW) ctrlReleaseRW.addEventListener('click', () => { try { ws && ws.send('OMXCTRL ' + JSON.stringify({ type: 'release_rw' })); } catch (_) {} });
if (ctrlForceTake) ctrlForceTake.addEventListener('click', () => { try { ws && ws.send('OMXCTRL ' + JSON.stringify({ type: 'force_promote' })); } catch (_) {} });

// Slow paste UI elements and state
const slowPasteText = document.getElementById('slowPasteText');
const slowPasteRate = document.getElementById('slowPasteRate');
const slowPasteStart = document.getElementById('slowPasteStart');
const slowPasteStop = document.getElementById('slowPasteStop');
const slowPasteProgress = document.getElementById('slowPasteProgress');
const slowPasteStatus = document.getElementById('slowPasteStatus');
const slowPaste = {
  active: false,
  codepoints: [],
  idx: 0,
  tokens: 0,
  lastTs: 0,
  timerId: null,
  rate: 30,
  // Completion announce debounce: wait for idle RX window
  waitMs: 1000,
  lastRxTs: 0,
  completeTimerId: null,
  waitingIdle: false,
};
function clearSlowPasteCompletionTimer() {
  try { if (slowPaste.completeTimerId) clearTimeout(slowPaste.completeTimerId); } catch (_) {}
  slowPaste.completeTimerId = null;
}
function scheduleSlowPasteCompletionAnnounce() {
  if (!slowPaste.waitingIdle) return;
  clearSlowPasteCompletionTimer();
  slowPaste.completeTimerId = setTimeout(() => {
    // If still waiting and no new RX within window, announce complete
    const now = performance.now();
    if (!slowPaste.waitingIdle) return;
    if (now - slowPaste.lastRxTs >= slowPaste.waitMs) {
      slowPaste.waitingIdle = false;
      clearSlowPasteCompletionTimer();
      try { term.write(`\r\n[Slow paste complete]` + '\r\n'); } catch (_) {}
    } else {
      // Data arrived; reschedule
      scheduleSlowPasteCompletionAnnounce();
    }
  }, slowPaste.waitMs);
}
function resetSlowPasteUI() {
  slowPasteProgress.value = 0;
  slowPasteProgress.max = 100;
  slowPasteStatus.textContent = '0%';
  slowPasteStart.disabled = false;
  slowPasteStop.style.display = 'none';
  slowPasteStart.style.display = '';
  slowPasteText.disabled = false;
  slowPasteRate.disabled = false;
}
function updateSlowPasteUI() {
  const sent = slowPaste.idx;
  const total = slowPaste.codepoints.length || 1;
  slowPasteProgress.max = total;
  slowPasteProgress.value = sent;
  const pct = Math.floor((sent / total) * 100);
  slowPasteStatus.textContent = `${pct}%`;
}
function abortSlowPaste(reason) {
  if (!slowPaste.active) return;
  try { if (slowPaste.timerId) clearInterval(slowPaste.timerId); } catch (_) {}
  slowPaste.active = false;
  slowPaste.timerId = null;
  slowPaste.tokens = 0; slowPaste.idx = 0; slowPaste.codepoints = [];
  slowPaste.waitingIdle = false; clearSlowPasteCompletionTimer();
  resetSlowPasteUI();
  if (reason) {
    try { term.write(`\r\n[Slow paste aborted: ${reason}]\r\n`); } catch (_) {}
  }
}
function finishSlowPaste() {
  if (slowPaste.timerId) { try { clearInterval(slowPaste.timerId); } catch (_) {} }
  slowPaste.active = false;
  slowPaste.timerId = null;
  updateSlowPasteUI();
  slowPasteStart.disabled = false;
  slowPasteStop.style.display = 'none';
  slowPasteStart.style.display = '';
  slowPasteText.disabled = false;
  slowPasteRate.disabled = false;
  // Defer completion message until RX has been idle for waitMs
  slowPaste.waitingIdle = true;
  slowPaste.lastRxTs = performance.now();
  scheduleSlowPasteCompletionAnnounce();
}
function sendChunk(chars) {
  if (!chars || chars.length === 0) return;
  try { if (isConnected()) { ws.send(chars.join('')); } } catch (_) {}
}
function startSlowPaste() {
  if (!isConnected()) { alert('Connect to a console before starting slow paste.'); return; }
  // Cancel any pending completion from a previous session
  slowPaste.waitingIdle = false; clearSlowPasteCompletionTimer();
  const raw = slowPasteText.value || '';
  if (!raw) { alert('Paste some text first.'); return; }
  const rateVal = Math.max(1, Math.min(2000, Number(slowPasteRate.value || 30)));
  slowPaste.rate = rateVal;
  // Convert to array of Unicode code points to avoid splitting surrogate pairs
  // Spread operator creates an array of code points for BMP and astral symbols
  slowPaste.codepoints = Array.from(raw);
  slowPaste.idx = 0;
  slowPaste.tokens = 0;
  slowPaste.lastTs = performance.now();
  slowPaste.active = true;
  updateSlowPasteUI();
  slowPasteStart.disabled = true;
  slowPasteStart.style.display = 'none';
  slowPasteStop.style.display = '';
  slowPasteText.disabled = true;
  slowPasteRate.disabled = false; // allow adjusting during paste
  try { term.write(`\r\n[Starting slow paste @ ${rateVal} chars/sec, ${slowPaste.codepoints.length} chars]` + '\r\n'); } catch (_) {}
  // Pace using a 20ms interval and a token bucket to smooth out scheduling jitter
  const TICK_MS = 20;
  slowPaste.timerId = setInterval(() => {
    if (!slowPaste.active) return;
    if (!isConnected()) { abortSlowPaste('Disconnected'); return; }
    // If user changed rate, pick it up
    const currentRate = Math.max(1, Math.min(2000, Number(slowPasteRate.value || slowPaste.rate)));
    slowPaste.rate = currentRate;
    const now = performance.now();
    const dt = Math.max(0, (now - slowPaste.lastTs) / 1000);
    slowPaste.lastTs = now;
    slowPaste.tokens += currentRate * dt;
    // limit burst per tick to avoid huge sends
    let budget = Math.min(512, Math.floor(slowPaste.tokens));
    const remaining = slowPaste.codepoints.length - slowPaste.idx;
    if (remaining <= 0) { finishSlowPaste(); return; }
    if (budget <= 0) { return; }
    const toSend = Math.min(budget, remaining);
    const chunk = slowPaste.codepoints.slice(slowPaste.idx, slowPaste.idx + toSend);
    sendChunk(chunk);
    slowPaste.idx += toSend;
    slowPaste.tokens -= toSend;
    updateSlowPasteUI();
    if (slowPaste.idx >= slowPaste.codepoints.length) { finishSlowPaste(); }
  }, TICK_MS);
}
slowPasteStart.addEventListener('click', startSlowPaste);
slowPasteStop.addEventListener('click', () => abortSlowPaste('Stopped by user'));

function formatSerial(sc) {
  if (!sc) return '';
  const dev = sc.device ?? 'n/a';
  const baud = sc.baudrate ?? 'n/a';
  const bs = sc.bytesize ?? 'n/a';
  const par = sc.parity ?? 'n/a';
  const sb = sc.stopbits ?? 'n/a';
  const fc = sc.flow_control ?? 'n/a';
  return `device=${dev} baud=${baud} ${bs}${par}${sb} flow=${fc}`;
}
function formatLine(ls) {
  if (!ls) return '';
  function v(x) { return (x === true || x === false) ? (x ? '1' : '0') : (x ?? 'n/a'); }
  const dcd = v(ls.DCD ?? ls.dcd);
  const dsr = v(ls.DSR ?? ls.dsr);
  const cts = v(ls.CTS ?? ls.cts);
  const rts = v(ls.RTS ?? ls.rts);
  const dtr = v(ls.DTR ?? ls.dtr);
  return `DCD=${dcd} DSR=${dsr} CTS=${cts} RTS=${rts} DTR=${dtr}`;
}
function renderInfo(p) {
  if (!p) { infoBody.innerHTML = '<div class="muted">No port selected</div>'; return; }
  const adapter = (p.adapter || p.adapter_type || '');
  const sc = p.serial_config || {};
  const ls = p.line_status || {};
  const connected = (('connected' in p) ? !!p.connected : !!p.is_running);
  const yesNoTag = (v) => `<span class="tag ${v ? 'ok' : 'bad'}">${v ? '1' : '0'}</span>`;
  const valueOrDash = (v) => (v === undefined || v === null || v === '' ? '<span class="muted">—</span>' : String(v));

  const rows = [];
  rows.push(`<tr><th>Name</th><td><strong>${p.name || ''}</strong></td></tr>`);
  rows.push(`<tr><th>Adapter</th><td>${adapter ? `<span class=\"tag\">${adapter}</span>` : '<span class=\"muted\">—</span>'}</td></tr>`);
  rows.push(`<tr><th>Description</th><td>${valueOrDash(p.description)}</td></tr>`);
  if (p.status_message) {
    rows.push(`<tr><th>Status</th><td><span class=\"tag bad\">offline</span> <span class=\"muted\">${escapeHtml(String(p.status_message))}</span></td></tr>`);
  } else if (p.readiness === 'idle') {
    // Readiness (issue #68): healthy but intentionally not running (resumes on
    // next client or Enter). No reason text — the idle state itself is the info.
    rows.push(`<tr><th>Status</th><td><span class=\"tag warn\">idle</span> <span class=\"muted\">resumes on next client or Enter</span></td></tr>`);
  }
  rows.push(`<tr><th>Connected</th><td>${connected ? '<span class=\"tag ok\">yes</span>' : '<span class=\"tag bad\">no</span>'} &nbsp; <span class=\"tag\">${clientMode}</span></td></tr>`);
  // Serial configuration rows
  rows.push(`<tr><th>Device</th><td>${valueOrDash(sc.device)}</td></tr>`);
  rows.push(`<tr><th>Baudrate</th><td>${valueOrDash(sc.baudrate)}</td></tr>`);
  rows.push(`<tr><th>Bytesize</th><td>${valueOrDash(sc.bytesize)}</td></tr>`);
  rows.push(`<tr><th>Parity</th><td>${valueOrDash(sc.parity)}</td></tr>`);
  rows.push(`<tr><th>Stopbits</th><td>${valueOrDash(sc.stopbits)}</td></tr>`);
  rows.push(`<tr><th>Flow control</th><td>${valueOrDash(sc.flow_control)}</td></tr>`);
  // Line status rows
  const dcd = (ls.DCD !== undefined) ? !!ls.DCD : (ls.dcd !== undefined ? !!ls.dcd : undefined);
  const dsr = (ls.DSR !== undefined) ? !!ls.DSR : (ls.dsr !== undefined ? !!ls.dsr : undefined);
  const cts = (ls.CTS !== undefined) ? !!ls.CTS : (ls.cts !== undefined ? !!ls.cts : undefined);
  const rts = (ls.RTS !== undefined) ? !!ls.RTS : (ls.rts !== undefined ? !!ls.rts : undefined);
  const dtr = (ls.DTR !== undefined) ? !!ls.DTR : (ls.dtr !== undefined ? !!ls.dtr : undefined);
  rows.push(`<tr><th>DCD</th><td>${dcd === undefined ? '<span class=\"muted\">—</span>' : yesNoTag(dcd)}</td></tr>`);
  rows.push(`<tr><th>DSR</th><td>${dsr === undefined ? '<span class=\"muted\">—</span>' : yesNoTag(dsr)}</td></tr>`);
  rows.push(`<tr><th>CTS</th><td>${cts === undefined ? '<span class=\"muted\">—</span>' : yesNoTag(cts)}</td></tr>`);
  rows.push(`<tr><th>RTS</th><td>${rts === undefined ? '<span class=\"muted\">—</span>' : yesNoTag(rts)}</td></tr>`);
  rows.push(`<tr><th>DTR</th><td>${dtr === undefined ? '<span class=\"muted\">—</span>' : yesNoTag(dtr)}</td></tr>`);
  if (Array.isArray(p.server_chain) && p.server_chain.length > 0) {
    rows.push(`<tr><th>Chain</th><td class=\"muted\">${p.server_chain.join(' \u2192 ')}</td></tr>`);
  }
  if (!connected && p.last_seen) {
    try {
      const dt = new Date(Number(p.last_seen) * 1000);
      const ts = isNaN(dt.getTime()) ? String(p.last_seen) : dt.toLocaleString();
      rows.push(`<tr><th>Last seen</th><td>${ts}</td></tr>`);
    } catch (_) {
      rows.push(`<tr><th>Last seen</th><td>${String(p.last_seen)}</td></tr>`);
    }
  }
  infoBody.innerHTML = `<table class=\"mini-table\"><tbody>${rows.join('')}</tbody></table>`;
}
function openInfo() {
  infoOverlay.style.display = 'block';
  renderInfo(ports.find(x => x.name === currentPort()) || null);
  if (infoTimer) { clearInterval(infoTimer); infoTimer = null; }
  // No polling fallback; metadata is pushed over WS (meta=1)
}
function closeInfo() { infoOverlay.style.display = 'none'; if (infoTimer) { clearInterval(infoTimer); infoTimer = null; } }
infoToggle.addEventListener('click', () => { const visible = infoOverlay.style.display !== 'none'; if (visible) closeInfo(); else openInfo(); });
infoClose.addEventListener('click', () => closeInfo());

// --- Port Actions overlay: catalog, run form, live event log, run history ---
const actionsOverlay = document.getElementById('actionsOverlay');
const actionsToggle = document.getElementById('actionsToggle');
const actionsClose = document.getElementById('actionsClose');
const actionsListRefresh = document.getElementById('actionsListRefresh');
const actionsListEl = document.getElementById('actionsList');
const actionsRunPanel = document.getElementById('actionsRunPanel');
const actionsRunTitle = document.getElementById('actionsRunTitle');
const actionsRunDesc = document.getElementById('actionsRunDesc');
const actionsRunForm = document.getElementById('actionsRunForm');
const actionsRunSubmit = document.getElementById('actionsRunSubmit');
const actionsRunStatus = document.getElementById('actionsRunStatus');
const actionsRunBack = document.getElementById('actionsRunBack');
const actionsHistoryRefresh = document.getElementById('actionsHistoryRefresh');
const actionsHistoryEl = document.getElementById('actionsHistory');
const actionsOperatorPrompt = document.getElementById('actionsOperatorPrompt');
const actionsOperatorPromptText = document.getElementById('actionsOperatorPromptText');
const actionsOperatorReadonlyNote = document.getElementById('actionsOperatorReadonlyNote');
const actionsOperatorText = document.getElementById('actionsOperatorText');
const actionsOperatorInput = document.getElementById('actionsOperatorInput');
const actionsOperatorButtons = document.getElementById('actionsOperatorButtons');
const actionsOperatorSelect = document.getElementById('actionsOperatorSelect');
const actionsOperatorSelectEl = document.getElementById('actionsOperatorSelectEl');
const actionsOperatorRadio = document.getElementById('actionsOperatorRadio');
const actionsOperatorRadioEl = document.getElementById('actionsOperatorRadioEl');
// Shared Send control for the text/select/radio prompt kinds - kept in one fixed spot
// (see actionsOperatorSendRow in the template) so pressing it is muscle-memory across kinds.
const actionsOperatorSendRow = document.getElementById('actionsOperatorSendRow');
const actionsOperatorSend = document.getElementById('actionsOperatorSend');

let actionsCsrf = null;
let actionsCatalog = [];
let currentAction = null;
let currentActionsWs = null;
// Id of the run this tab is streaming, if any - reported in the Run button's
// "cannot start" notice and used to clear the port-busy state on action_finished.
let currentRunId = null;
// Who may currently answer this run's operator-input prompts (see "Taking over as
// operator" in the design doc) - kept in sync via the `operator_changed` event.
let currentRunOperatorClientId = null;
// Whether the current run has already sent action_finished - the run WS can stay open
// (e.g. for history) after that, so this can't be inferred from currentActionsWs alone.
let currentRunFinished = false;
// Another client's already-running action on this port, discovered via loadActionsCatalog();
// cleared once the user clicks the strip to join it (see joinActiveRun()).
let pendingJoinRun = null;
// The port's in-flight run from the latest catalog fetch (the `active_run` field).
// Gates the Run button: while the port has a run, starting another one must be
// blocked client-side (the server still enforces it with a 400 for other tabs).
let portActiveRun = null;

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function fetchActionsCSRF() {
  try {
    const res = await fetch(getBasePath() + '/api/csrf', { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    actionsCsrf = data.csrf || null;
  } catch (_) { /* no session cookie (e.g. Basic Auth client) - CSRF not required then */ }
}

function actionsHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  if (actionsCsrf) headers['X-OMX-CSRF'] = actionsCsrf;
  return headers;
}

async function loadActionsCatalog() {
  const port = currentPort();
  if (!port) { actionsToggle.style.display = 'none'; portActiveRun = null; return; }
  try {
    const res = await fetch(`${getBasePath()}/api/ports/${encodeURIComponent(port)}/actions`, { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) { actionsCatalog = []; actionsToggle.style.display = 'none'; portActiveRun = null; return; }
    const data = await res.json();
    actionsCatalog = Array.isArray(data.actions) ? data.actions : [];
    actionsToggle.style.display = actionsCatalog.length ? '' : 'none';
    portActiveRun = data.active_run || null;
    if (portActiveRun && !currentActionsWs) {
      pendingJoinRun = data.active_run;
      const label = (actionsCatalog.find((a) => a.id === pendingJoinRun.action_id) || {}).name || pendingJoinRun.action_id;
      showActionStrip(`Script running: ${label} \u2014 click to open`);
    }
  } catch (_) {
    actionsCatalog = [];
    actionsToggle.style.display = 'none';
    portActiveRun = null;
  }
  updateActionsRunButton();
}

function showActionsListView() {
  actionsListEl.style.display = '';
  actionsRunPanel.style.display = 'none';
  actionsRunBack.style.display = 'none';
  actionsClose.style.display = '';
}

// Shows the single-action run panel instead of the catalog list, and swaps the overlay's
// top-right button from close (✕) to Back (←) - pressing it returns to the list, where
// ✕ closes the whole overlay (see docs/design/port_actions.md "UI surface").
function showActionsRunView() {
  actionsListEl.style.display = 'none';
  actionsRunPanel.style.display = '';
  actionsRunBack.style.display = '';
  actionsClose.style.display = 'none';
}

function renderActionsList() {
  showActionsListView();
  if (!actionsCatalog.length) {
    actionsListEl.innerHTML = '<div class="muted">No actions available for this port</div>';
    return;
  }
  const busyId = portActiveRun ? portActiveRun.action_id : null;
  actionsListEl.innerHTML = actionsCatalog.map((a) =>
    `<div class="actions-item" data-action-id="${escapeHtml(a.id)}" style="padding:6px 0; border-bottom:1px solid var(--border-color); cursor:pointer;">` +
    `<b>${escapeHtml(a.name || a.id)}</b>${a.id === busyId ? '<span class="muted mini"> (running)</span>' : ''}` +
    `<div class="muted mini">${escapeHtml(a.description || '')}</div></div>`
  ).join('');
  actionsListEl.querySelectorAll('.actions-item').forEach((el) => {
    el.addEventListener('click', () => {
      const action = actionsCatalog.find((a) => a.id === el.getAttribute('data-action-id'));
      if (action) openActionRunPanel(action);
    });
  });
}

function closeActionRunStream() {
  if (currentActionsWs) { try { currentActionsWs.close(); } catch (_) {} currentActionsWs = null; }
  currentRunId = null;
  currentRunOperatorClientId = null;
  updateOperatorTakeOverUI();
  hideOperatorPrompt();
}

// Action-run terminal: a vertical split of #term-container (beside the main port
// console) showing the run's transcript/operator prompt - see docs/design/port_actions.md
// "Live view"/"UI surface". Lazily created since xterm.js needs a visible container to fit.
const actionTermPane = document.getElementById('actionTermPane');
const actionTermSplitter = document.getElementById('actionTermSplitter');
const actionTermEl = document.getElementById('actionTerm');
const actionTermTitle = document.getElementById('actionTermTitle');
const actionTermStatus = document.getElementById('actionTermStatus');
// The tag shows run state with a status color: yellow while in flight (Running /
// Waiting for input…), green on Finished: success, red on Failed/timeout, muted on
// cancelled, plain neutral with no run (or no colorClass). One helper owns the class
// juggling so a state's color can never leak into the next.
function setActionTag(text, colorClass) {
  actionTermStatus.textContent = text;
  actionTermStatus.classList.remove('warn', 'ok', 'bad', 'muted');
  if (colorClass) actionTermStatus.classList.add(colorClass);
}
const actionTermTakeOver = document.getElementById('actionTermTakeOver');
const actionTermStop = document.getElementById('actionTermStop');
const actionTermClose = document.getElementById('actionTermClose');
// Prominent run-outcome banner (GitHub issue feedback: the small actionTermStatus tag
// alone wasn't obvious enough) - populated/shown only once action_finished arrives.
const actionResultBanner = document.getElementById('actionResultBanner');
function hideActionResultBanner() {
  if (!actionResultBanner) return;
  actionResultBanner.style.display = 'none';
  actionResultBanner.classList.remove('ok', 'bad', 'muted');
}
// Optional step/percent progress bar, driven by the script's own session.progress() calls
// (see docs/design/action_session.md) - shares the outcome banner's slot, shown only for
// scripts that actually call progress(), hidden again once action_finished arrives.
const actionProgressBar = document.getElementById('actionProgressBar');
const actionProgressFill = document.getElementById('actionProgressFill');
const actionProgressLabel = document.getElementById('actionProgressLabel');
const actionProgressWaiting = document.getElementById('actionProgressWaiting');
function hideActionProgressBar() {
  if (!actionProgressBar) return;
  actionProgressBar.style.display = 'none';
  actionProgressBar.classList.remove('indeterminate');
  if (actionProgressFill) actionProgressFill.style.width = '0%';
  if (actionProgressLabel) actionProgressLabel.textContent = '';
  setActionWaitingBadge(false);
}
function showActionProgress(step, percent) {
  if (!actionProgressBar) return;
  actionProgressBar.style.display = '';
  const indeterminate = percent === null || percent === undefined;
  actionProgressBar.classList.toggle('indeterminate', indeterminate);
  if (actionProgressFill) actionProgressFill.style.width = indeterminate ? '' : `${Math.max(0, Math.min(100, percent))}%`;
  if (actionProgressLabel) actionProgressLabel.textContent = indeterminate ? (step || '') : `${step || ''} (${percent}%)`;
}
// Waiting-for-operator is a paused overlay, not a step of its own - it never touches the
// last-reported step/percent underneath (see docs/design/action_session.md).
function setActionWaitingBadge(waiting) {
  if (!actionProgressWaiting) return;
  actionProgressWaiting.style.display = waiting ? '' : 'none';
}
let actionTerm = null;
let actionTermFit = null;
const ACTION_TERM_WIDTH_KEY = 'omx_action_term_width';

// Taking over as operator (docs/design/port_actions.md "Operator input"): mirrors the
// port's own "Force take read-write" - any connected viewer can become the client whose
// operator_input frames the running script accepts, notified to everyone via the
// `operator_changed` event so a previous operator learns they lost that role live.
function updateOperatorTakeOverUI() {
  if (actionTermTakeOver) {
    const show = !!currentActionsWs && !!currentRunOperatorClientId && currentRunOperatorClientId !== myClientId;
    actionTermTakeOver.style.display = show ? '' : 'none';
  }
  // Stopping a run is operator-only (docs/design/port_actions.md "Stopping a run") - a
  // non-operator viewer must take over first, same as answering an operator-input prompt.
  // Also hidden once the run has finished (see GitHub issue #46) - stopping a completed/
  // failed run makes no sense.
  if (actionTermStop) actionTermStop.style.display = (!!currentActionsWs && !currentRunFinished && isCurrentOperator()) ? '' : 'none';
  applyOperatorInputDisabledState();
}

// Whether this client may answer the current run's operator-input prompt: true once no
// operator is assigned yet (e.g. before the first `operator_changed`/launch assignment
// arrives) or once this client is the assigned operator.
function isCurrentOperator() {
  return !currentRunOperatorClientId || currentRunOperatorClientId === myClientId;
}

// Non-operator viewers can see a running action's operator-input prompt (it's part of
// the shared live view) but must not be able to answer it - disable every control here
// so only the assigned operator's clicks/keystrokes actually do anything.
function applyOperatorInputDisabledState() {
  const enabled = isCurrentOperator();
  if (actionsOperatorInput) actionsOperatorInput.disabled = !enabled;
  if (actionsOperatorSend) actionsOperatorSend.disabled = !enabled;
  if (actionsOperatorButtons) actionsOperatorButtons.querySelectorAll('button').forEach((b) => { b.disabled = !enabled; });
  if (actionsOperatorSelectEl) actionsOperatorSelectEl.disabled = !enabled;
  if (actionsOperatorRadioEl) actionsOperatorRadioEl.querySelectorAll('input').forEach((r) => { r.disabled = !enabled; });
  if (actionsOperatorReadonlyNote) actionsOperatorReadonlyNote.style.display = enabled ? 'none' : '';
}
if (actionTermTakeOver) actionTermTakeOver.addEventListener('click', () => {
  if (!currentActionsWs || currentActionsWs.readyState !== WebSocket.OPEN) return;
  try { currentActionsWs.send(JSON.stringify({ type: 'operator_take_over' })); } catch (_) {}
});
if (actionTermStop) actionTermStop.addEventListener('click', () => {
  if (!currentActionsWs || currentActionsWs.readyState !== WebSocket.OPEN) return;
  if (!confirm('Stop this running action script? This cannot be undone.')) return;
  try { currentActionsWs.send(JSON.stringify({ type: 'cancel_run' })); } catch (_) {}
});

function ensureActionTerm() {
  if (actionTerm) return;
  const colors = getThemeColors();
  actionTerm = new Terminal({ convertEol: true, theme: { background: colors.background, foreground: colors.foreground } });
  actionTermFit = (window.FitAddon) ? new window.FitAddon.FitAddon() : null;
  if (actionTermFit) actionTerm.loadAddon(actionTermFit);
  actionTerm.open(actionTermEl);
}
function fitActionTerminal() { try { actionTermFit && actionTermFit.fit(); } catch (_) {} }
window.addEventListener('theme-changed', () => {
  if (!actionTerm) return;
  const colors = getThemeColors();
  actionTerm.options.theme = { background: colors.background, foreground: colors.foreground };
});

function openActionTermPane() {
  // Show the pane BEFORE creating the terminal: xterm only measures its cell
  // metrics once its container is visible, and the fit addon no-ops while the
  // cell size is still 0. A terminal opened while the pane was hidden (or a
  // fit() in this same tick) would keep the terminal at its initial small
  // size until a later resize - so the fit runs again on the next frame(s)
  // and after a short delay, by which time the metrics are real.
  actionTermSplitter.style.display = 'block';
  actionTermPane.style.display = 'flex';
  const savedWidth = parseInt(localStorage.getItem(ACTION_TERM_WIDTH_KEY), 10);
  if (savedWidth) actionTermPane.style.width = savedWidth + 'px';
  ensureActionTerm();
  fitTerminal();
  fitActionTerminal();
  requestAnimationFrame(() => {
    fitTerminal();
    fitActionTerminal();
    requestAnimationFrame(() => { fitTerminal(); fitActionTerminal(); });
  });
  setTimeout(() => { fitTerminal(); fitActionTerminal(); }, 200);
  // The pane's own header already shows status - the fixed bottom-right strip would
  // otherwise sit on top of the pane's operator-input box.
  hideActionStrip();
}
// Only hides the pane - the run's WS stream keeps updating in the background (same
// "closing only hides" pattern as the Actions overlay/persistent strip). Re-shows the
// strip if a run is still active, since it's now the only visible "still running" cue.
function closeActionTermPane() {
  actionTermSplitter.style.display = 'none';
  actionTermPane.style.display = 'none';
  setTimeout(fitTerminal, 0);
  // The strip is the pane's reopen affordance - show it whenever there is
  // something to reopen: a run this tab streams, or the transcript of a run
  // this tab has watched (finish included - the strip click re-opens the
  // pane via a history replay, see the strip handler below).
  if (currentActionsWs) showActionStrip(lastActionStripText || 'Action running\u2026');
  else if (currentRunId) showActionStrip(lastActionStripText || 'Action finished \u2014 click to open');
}
if (actionTermClose) actionTermClose.addEventListener('click', () => closeActionTermPane());

// Draggable vertical divider between #term and #actionTermPane; width persists across reloads.
if (actionTermSplitter) {
  let dragging = false;
  actionTermSplitter.addEventListener('mousedown', (e) => {
    dragging = true;
    actionTermSplitter.classList.add('dragging');
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const containerRect = document.getElementById('term-container').getBoundingClientRect();
    let width = containerRect.right - e.clientX;
    width = Math.max(220, Math.min(width, containerRect.width - 220));
    actionTermPane.style.width = width + 'px';
    fitTerminal();
    fitActionTerminal();
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    actionTermSplitter.classList.remove('dragging');
    try { localStorage.setItem(ACTION_TERM_WIDTH_KEY, parseInt(actionTermPane.style.width, 10)); } catch (_) {}
  });
}

// Renders one start-run param as a form field. `widget` selects the control:
//   "text" (default) - a text/number/password <input> (per `p.type`/`p.sensitive`).
//   "select"          - a <select> built from `p.choices` ({label, value} dicts).
//   "radio"           - one radio button per `p.choices` entry.
function renderActionParamField(p) {
  const def = (p.default !== null && p.default !== undefined) ? String(p.default) : '';
  const labelHtml = `${escapeHtml(p.name)}${p.required ? ' *' : ''}` +
    (p.description ? `<span class="muted"> - ${escapeHtml(p.description)}</span>` : '');
  if (p.widget === 'select') {
    const options = (p.choices || []).map((c) =>
      `<option value="${escapeHtml(c.value)}"${c.value === def ? ' selected' : ''}>${escapeHtml(c.label)}</option>`
    ).join('');
    return `<label class="mini" style="display:block; margin-top:6px;">${labelHtml}` +
      `<select name="${escapeHtml(p.name)}" ${p.required ? 'required' : ''} ` +
      `style="display:block; width:100%; box-sizing:border-box; margin-top:2px;">${options}</select></label>`;
  }
  if (p.widget === 'radio') {
    const options = (p.choices || []).map((c) =>
      `<label class="mini" style="display:flex; align-items:center; gap:4px; font-weight:normal; margin-top:2px;">` +
      `<input type="radio" name="${escapeHtml(p.name)}" value="${escapeHtml(c.value)}"${c.value === def ? ' checked' : ''} ${p.required ? 'required' : ''} /> ${escapeHtml(c.label)}</label>`
    ).join('');
    return `<div class="mini" style="margin-top:6px;">${labelHtml}<div style="margin-top:2px;">${options}</div></div>`;
  }
  const inputType = (p.type === 'int' || p.type === 'float') ? 'number' : (p.sensitive ? 'password' : 'text');
  return `<label class="mini" style="display:block; margin-top:6px;">${labelHtml}` +
    `<input type="${inputType}" name="${escapeHtml(p.name)}" value="${escapeHtml(def)}" ${p.required ? 'required' : ''} ` +
    `style="display:block; width:100%; box-sizing:border-box; margin-top:2px;" /></label>`;
}

function openActionRunPanel(action) {
  // Only swaps the overlay's content to this action's run form. The action
  // pane, its terminal, and any running run's stream are left untouched - the
  // pane's visibility is the user's own (its ✕ button) decision, so selecting
  // a list entry (or the deep link / strip joining path) must not hide an
  // open pane or drop a live stream. The pane's content changes only when a
  // new stream starts (streamActionRun, on Run/join) or the pane is closed.
  currentAction = action;
  actionsRunTitle.textContent = action.name || action.id;
  actionsRunDesc.textContent = action.description || '';
  showActionsRunView();
  setActionsRunStatus('', false);
  actionsRunForm.innerHTML = (action.params || []).map(renderActionParamField).join('') || '<div class="muted">No parameters</div>';
  loadRunHistory();
  updateActionsRunButton();
  // Focus the first input-like field so Enter submits straight away (the form
  // submit listener above routes that to the launch). Runs after the deep link
  // pre-fill (see applyActionDeepLink) - focusing never changes a pre-filled
  // value. Skipped for forms whose fields are only selects/radios.
  requestAnimationFrame(() => {
    const field = actionsRunForm.querySelector('input:not([type=radio]):not([type=checkbox]), select');
    if (!field) return;
    try { field.focus(); } catch (_) {}
  });
}

actionsRunBack.addEventListener('click', () => { showActionsListView(); });

function collectActionParams() {
  const params = {};
  const fieldByName = {};
  (currentAction.params || []).forEach((p) => { fieldByName[p.name] = p; });
  new FormData(actionsRunForm).forEach((value, key) => {
    const spec = fieldByName[key] || {};
    if (spec.type === 'int') params[key] = value === '' ? null : parseInt(value, 10);
    else if (spec.type === 'float') params[key] = value === '' ? null : parseFloat(value);
    else params[key] = value;
  });
  return params;
}

function streamActionRun(runId) {
  closeActionRunStream();
  currentRunId = runId; // closeActionRunStream() above resets it for the old stream
  currentRunFinished = false;
  hideActionResultBanner();
  hideActionProgressBar();
  actionTermTitle.textContent = (currentAction && (currentAction.name || currentAction.id)) || 'Action';
  // Reset the tag per stream start: it only shows run state (Running / Waiting for
  // input… / Finished) in its status color, and a previous run's finish color (green/red/
  // muted) must not leak into this stream - the launch and deep-link paths skip
  // openActionRunPanel's reset.
  setActionTag('Running', 'warn');
  openActionTermPane();
  actionTerm.clear(); // fresh transcript for this run; openActionTermPane() created it if needed
  // The run dialog sits over the action terminal pane; get it out of the way once
  // streaming starts (the run panel reopens list-first next time the Actions button
  // is pressed - docs/design/port_actions.md "UI surface"). The bottom-right strip
  // and the terminal pane keep the run reachable until it finishes.
  closeActionsOverlay();
  const proto = (location.protocol === 'https:') ? 'wss' : 'ws';
  const qs = myClientId ? `?client_id=${encodeURIComponent(myClientId)}` : '';
  const sock = new WebSocket(`${proto}://${location.host}${getBasePath()}/ws/actions/${encodeURIComponent(runId)}${qs}`);
  currentActionsWs = sock;
  sock.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    const ts = msg.ts ? new Date(msg.ts * 1000).toLocaleTimeString() : '';
    // Structured events (unlike log() messages) carry their detail as separate top-level
    // fields, not folded into the event string - spell it out here too, or the terminal
    // log line would just read the bare event name.
    let detail = '';
    if (msg.event === 'progress') {
      detail = ` ${msg.step || ''}${msg.percent != null ? ` (${msg.percent}%)` : ''}`;
    } else if (msg.event === 'waiting_for_operator') {
      detail = ` ${msg.step ? `${msg.step}: ` : ''}${msg.prompt || ''}`;
    } else if (msg.event === 'action_finished') {
      detail = ` ${msg.status || ''}${msg.error ? `: ${msg.error}` : ''}`;
    } else if (msg.event === 'operator_changed') {
      detail = ` operator=${msg.operator_client_id || ''}`;
    }
    actionTerm.write(`[${ts}] ${msg.event || ''}${detail}\n`);
    const label = (currentAction && (currentAction.name || currentAction.id)) || 'action';
    if (msg.event === 'action_finished') {
      currentRunFinished = true;
      hideOperatorPrompt();
      const failed = msg.status === 'failed' || msg.status === 'timeout';
      // Matches the outcome banner's own ok/bad/muted mapping below; an unknown status
      // stays neutral.
      setActionTag(`Finished: ${msg.status || 'unknown'}`, { success: 'ok', failed: 'bad', timeout: 'bad', cancelled: 'muted' }[msg.status]);
      if (actionResultBanner) {
        // One glance, no reading the small header tag required: an icon, the outcome, and
        // (when relevant) the short error - color-coded ok/bad/muted to match the tag classes.
        const icons = { success: '\u2713', failed: '\u2717', timeout: '\u23f1', cancelled: '\u25a0' };
        const icon = icons[msg.status] || '\u2139';
        const bannerClass = msg.status === 'success' ? 'ok' : msg.status === 'cancelled' ? 'muted' : 'bad';
        let text = `${icon} ${label} ${msg.status === 'success' ? 'finished successfully' : msg.status || 'finished'}`;
        if (msg.status !== 'success' && msg.error) text += `: ${msg.error}`;
        actionResultBanner.textContent = text;
        actionResultBanner.classList.remove('ok', 'bad', 'muted');
        actionResultBanner.classList.add(bannerClass);
        actionResultBanner.style.display = '';
      }
      if (failed && msg.error) {
        // Full traceback for backend bugs goes to the server log (see runner.py); this
        // is the short message so an operator doesn't need log access to see WHY it failed.
        actionTerm.write(`Error: ${msg.error}\n`);
        setActionsRunStatus(`Finished: ${msg.status} — ${msg.error}`, true);
        showActionToast(`${label} failed: ${msg.error}`);
      } else {
        setActionsRunStatus(`Finished: ${msg.status || 'unknown'}`, false);
      }
      currentRunOperatorClientId = null;
      updateOperatorTakeOverUI();
      loadRunHistory();
      if (portActiveRun && portActiveRun.run_id === currentRunId) portActiveRun = null;
      updateActionsRunButton();
      showActionStrip(`Action finished: ${msg.status || 'unknown'} \u2014 click to open`);
      hideActionProgressBar(); // the outcome banner above takes over this slot now
    } else if (msg.event === 'progress') {
      // Script-reported step/percent (session.progress(), see docs/design/action_session.md) -
      // the step/percent detail lives only in the progress bar now; the tag stays generic
      // so it isn't just repeating the same text right below it.
      showActionProgress(msg.step, msg.percent);
      setActionTag('Running', 'warn');
      showActionStrip(`Action running: ${label} — ${msg.step || ''}`);
    } else if (msg.event === 'waiting_for_operator') {
      setActionTag('Waiting for input…', 'warn');
      showOperatorPrompt(msg.prompt, msg.kind, msg.choices, msg.color);
      setActionWaitingBadge(true); // overlay only - never touches the step/percent already shown
      showActionStrip(`${label}: waiting for input — click to answer`);
    } else if (msg.event === 'operator_changed') {
      const wasOperator = currentRunOperatorClientId === myClientId;
      currentRunOperatorClientId = msg.operator_client_id;
      updateOperatorTakeOverUI();
      if (wasOperator && currentRunOperatorClientId !== myClientId) {
        showActionToast('Another user took over as operator for this run');
      }
    } else {
      // Freetext/debug log() events (see docs/design/action_session.md) - the current step
      // is reported separately via progress() above, not inferred from these. The line
      // goes to the terminal (above) and the bottom strip only; the status tag is
      // reserved for run state (Running / Waiting for input… / Finished).
      showActionStrip(`Action running: ${label} — ${msg.event || ''}`);
    }
  };
  sock.onclose = () => { if (currentActionsWs === sock) { currentActionsWs = null; updateActionsRunButton(); } };
  updateOperatorTakeOverUI();
}

// Operator input (docs/design/port_actions.md "Operator input"): answers a script's
// session.prompt()/wait_for_input()/confirm()/choose()/select()/radio() call, routed
// upstream over the same run WS. `kind` selects which control row is shown; text/select/
// radio all share one Send button fixed at the bottom-left (actionsOperatorSendRow) so
// its position doesn't shift between prompt kinds:
//   "text" (default)  - a text input.
//   "buttons"         - one button per choice; clicking answers immediately, no Send row.
//   "select"          - a <select> (options supplied by the script).
//   "radio"           - one radio button per choice.
function showOperatorPrompt(prompt, kind, choices, color) {
  if (!actionsOperatorPrompt) return;
  actionsOperatorPromptText.textContent = prompt || 'Script is waiting for input';
  actionsOperatorPrompt.style.display = '';
  actionsOperatorPrompt.classList.add('action-needs-attention');
  // Script-chosen accent color (session.prompt(..., color=...), see docs/design/
  // action_session.md) - CSS keys off this attribute to override the default
  // --attention-* colors; "none"/absent leaves the default styling.
  actionsOperatorPrompt.dataset.color = color || 'none';
  const isButtons = kind === 'buttons';
  const isSelect = kind === 'select';
  const isRadio = kind === 'radio';
  if (actionsOperatorText) actionsOperatorText.style.display = (isButtons || isSelect || isRadio) ? 'none' : 'flex';
  if (actionsOperatorButtons) actionsOperatorButtons.style.display = isButtons ? 'flex' : 'none';
  if (actionsOperatorSelect) actionsOperatorSelect.style.display = isSelect ? 'block' : 'none';
  if (actionsOperatorRadio) actionsOperatorRadio.style.display = isRadio ? 'flex' : 'none';
  // Buttons kind has no separate Send - clicking a choice submits it directly.
  if (actionsOperatorSendRow) actionsOperatorSendRow.style.display = isButtons ? 'none' : 'flex';
  if (isButtons && actionsOperatorButtons) {
    actionsOperatorButtons.innerHTML = '';
    (choices || []).forEach((choice) => {
      const btn = document.createElement('button');
      btn.className = 'btn';
      btn.type = 'button';
      btn.textContent = choice.label;
      btn.addEventListener('click', () => sendOperatorInput(choice.value));
      actionsOperatorButtons.appendChild(btn);
    });
  } else if (isSelect && actionsOperatorSelectEl) {
    actionsOperatorSelectEl.innerHTML = '';
    (choices || []).forEach((choice) => {
      const opt = document.createElement('option');
      opt.value = choice.value;
      opt.textContent = choice.label;
      actionsOperatorSelectEl.appendChild(opt);
    });
  } else if (isRadio && actionsOperatorRadioEl) {
    actionsOperatorRadioEl.innerHTML = '';
    (choices || []).forEach((choice, i) => {
      const label = document.createElement('label');
      label.className = 'mini';
      label.style.cssText = 'display:flex; align-items:center; gap:4px; font-weight:normal;';
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'actionsOperatorRadioGroup';
      input.value = choice.value;
      if (i === 0) input.checked = true;
      label.appendChild(input);
      label.appendChild(document.createTextNode(choice.label));
      actionsOperatorRadioEl.appendChild(label);
    });
  } else {
    actionsOperatorInput.value = '';
    // .focus() fires 'focusin' synchronously, which would otherwise trip the
    // "operator noticed it" listener below and cancel the flash before it's seen.
    suppressAttentionClearOnFocus = true;
    actionsOperatorInput.focus();
    suppressAttentionClearOnFocus = false;
  }
  applyOperatorInputDisabledState();
}
function hideOperatorPrompt() {
  if (actionsOperatorPrompt) {
    actionsOperatorPrompt.style.display = 'none';
    actionsOperatorPrompt.classList.remove('action-needs-attention');
    delete actionsOperatorPrompt.dataset.color;
  }
  setActionWaitingBadge(false);
}
// Any interaction inside the prompt counts as "the operator noticed it" - stop flashing
// right away rather than waiting for the answer to actually be sent.
let suppressAttentionClearOnFocus = false;
if (actionsOperatorPrompt) {
  actionsOperatorPrompt.addEventListener('click', () => actionsOperatorPrompt.classList.remove('action-needs-attention'));
  actionsOperatorPrompt.addEventListener('focusin', () => {
    if (suppressAttentionClearOnFocus) return;
    actionsOperatorPrompt.classList.remove('action-needs-attention');
  });
}
// `value` is passed explicitly by button clicks; text/select/radio controls are read here.
function sendOperatorInput(value) {
  if (!currentActionsWs || currentActionsWs.readyState !== WebSocket.OPEN) return;
  if (!isCurrentOperator()) return; // defense-in-depth: controls are already disabled for non-operators
  let text = value;
  if (text === undefined) {
    if (actionsOperatorSelect && actionsOperatorSelect.style.display !== 'none') {
      text = actionsOperatorSelectEl.value;
    } else if (actionsOperatorRadio && actionsOperatorRadio.style.display !== 'none') {
      const checked = actionsOperatorRadioEl.querySelector('input[type="radio"]:checked');
      text = checked ? checked.value : '';
    } else {
      text = actionsOperatorInput.value;
    }
  }
  try { currentActionsWs.send(JSON.stringify({ type: 'operator_input', text })); } catch (_) {}
  hideOperatorPrompt();
}
if (actionsOperatorSend) actionsOperatorSend.addEventListener('click', () => sendOperatorInput());
if (actionsOperatorInput) actionsOperatorInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendOperatorInput(); });

// Extracted so both the Run button and a deep-link's &autorun=1 can trigger the same
// run path (docs/design/port_actions.md "Deep-linking an action") - autorun goes through
// this exact same auth/permission/validation-checked API call, no shortcut taken.
function setActionsRunStatus(text, isError) {
  // isError: true -> red (a failed start), 'busy' -> the status warning color
  // (the run button's "cannot start" notice), false -> default text color.
  actionsRunStatus.textContent = text;
  actionsRunStatus.style.color = isError === true ? 'var(--status-err-text)' : (isError === 'busy' ? 'var(--status-warn-text)' : '');
}

// Run button state (docs/design/port_actions.md "UI surface"): disabled while
// the port has a run - either one this tab streams (WS open and not yet
// finished) or one reported by the catalog for another client. Nothing happens
// until the user presses Run; while busy, the disabled button (kept in the
// dimmed disabled style) carries the reason in its title, and the
// #actionsRunStatus line carries the same notice in the warning color, so the
// state is visible without hovering. A forced click still gets the server's
// 400 message ("Failed to start: An action is already running on port ...")
// rendered in red below.
function updateActionsRunButton() {
  const busy = !!(portActiveRun || (currentActionsWs && !currentRunFinished));
  actionsRunSubmit.disabled = busy || !currentAction;
  actionsRunSubmit.title = busy ? 'Another action is running on this port' : '';
  if (busy) {
    const runId = (portActiveRun && portActiveRun.run_id) || (currentRunId || '');
    setActionsRunStatus(`Cannot start: another action is running on this port (run ${runId})`, 'busy');
  } else {
    // Keep the error/red text from a failed start and the green-ish text from a
    // finished run (both written by the WS path) untouched; only a stale notice
    // or an empty line is cleared.
    const t = actionsRunStatus.textContent;
    if (!t.startsWith('Failed to start') && !t.startsWith('Finished') && !t.startsWith('Running')) {
      setActionsRunStatus('', false);
    }
  }
}

async function launchCurrentAction() {
  if (!currentAction) return;
  const port = currentPort();
  if (!port) return;
  const params = collectActionParams();
  actionsRunSubmit.disabled = true;
  setActionsRunStatus('Starting\u2026', false);
  try {
    const res = await fetch(`${getBasePath()}/api/ports/${encodeURIComponent(port)}/actions/${encodeURIComponent(currentAction.id)}/run`, {
      method: 'POST', credentials: 'same-origin', headers: actionsHeaders(),
      body: JSON.stringify({ params, client_id: myClientId }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      // Rejected before a run was even created (bad/missing params, or another action
      // already running on this port) - also logged server-side, see runner.py.
      const message = data.message || res.status;
      setActionsRunStatus(`Failed to start: ${message}`, true);
      actionTerm.write(`Failed to start: ${message}\n`);
      return;
    }
    setActionsRunStatus(`Running (run ${data.run_id})`, false);
    showActionStrip(`Action running: ${currentAction.name || currentAction.id}`);
    streamActionRun(data.run_id);
    currentRunOperatorClientId = myClientId; // the launcher starts out as the operator
    updateOperatorTakeOverUI();
  } catch (e) {
    setActionsRunStatus(`Failed to start: ${e}`, true);
  } finally {
    updateActionsRunButton();
  }
}
// Enter inside a field submits the form. Route that to the exact same launch
// path as the Run button - without this, the plain <form> does a default
// implicit submission, which reloads the page to the console URL and loses the
// panel and the run's port context. The browser's native validation still
// applies: submit does not fire while a required field is empty.
actionsRunForm.addEventListener('submit', (e) => { e.preventDefault(); launchCurrentAction(); });
actionsRunSubmit.addEventListener('click', () => launchCurrentAction());

async function loadRunHistory() {
  if (!currentAction) return;
  const port = currentPort();
  try {
    const res = await fetch(
      `${getBasePath()}/api/ports/${encodeURIComponent(port)}/actions/${encodeURIComponent(currentAction.id)}/runs`,
      { credentials: 'same-origin', cache: 'no-store' }
    );
    if (!res.ok) { actionsHistoryEl.innerHTML = '<div class="muted">Unavailable</div>'; return; }
    const data = await res.json();
    const runs = Array.isArray(data.runs) ? data.runs.slice(0, 10) : [];
    if (!runs.length) { actionsHistoryEl.innerHTML = '<div class="muted">No runs yet</div>'; return; }
    actionsHistoryEl.innerHTML = `<table class="mini-table"><tbody>${runs.map((r) =>
      `<tr><th>${new Date(r.started_at * 1000).toLocaleTimeString()}</th><td>${escapeHtml(r.username)}</td><td>${escapeHtml(r.status)}</td></tr>`
    ).join('')}</tbody></table>`;
  } catch (_) {
    actionsHistoryEl.innerHTML = '<div class="muted">Unavailable</div>';
  }
}
actionsHistoryRefresh.addEventListener('click', () => loadRunHistory());

// Persistent strip: stays visible while a run is active even if the overlay is
// closed, so it's clear something is still active and holding the read-write lock.
const actionRunStrip = document.getElementById('actionRunStrip');
const actionRunStripText = document.getElementById('actionRunStripText');
let lastActionStripText = '';
function showActionStrip(text) {
  lastActionStripText = text;
  if (!actionRunStrip) return;
  // Suppress the fixed bottom-right strip while the action term pane is open - it
  // docks to the right too and would otherwise overlap the pane's operator-input box.
  if (actionTermPane && actionTermPane.style.display !== 'none') { actionRunStrip.style.display = 'none'; return; }
  actionRunStripText.textContent = text;
  actionRunStrip.style.display = 'inline-block';
  actionRunStrip.classList.toggle('action-needs-attention', text.indexOf('waiting for input') !== -1);
}
function hideActionStrip() {
  if (!actionRunStrip) return;
  actionRunStrip.style.display = 'none';
  actionRunStrip.classList.remove('action-needs-attention');
}
// Late join (docs/design/port_actions.md "Late join"): stream a run this tab does
// not have open yet, opening the action pane with the run's full event history
// plus live updates. Deliberately pane-only - it does not open the overlay's
// run panel ("properties" window), which the Actions button owns, so the strip
// click has exactly one behavior regardless of state.
function joinActiveRun(activeRun) {
  currentAction = actionsCatalog.find((a) => a.id === activeRun.action_id) || { id: activeRun.action_id, name: activeRun.action_id, params: [] };
  streamActionRun(activeRun.run_id);
  currentRunOperatorClientId = activeRun.operator_client_id || null;
  updateOperatorTakeOverUI();
}
if (actionRunStrip) actionRunStrip.addEventListener('click', () => {
  actionRunStrip.classList.remove('action-needs-attention');
  // The strip is ONE affordance with ONE behavior - open the action pane - in
  // every wording it carries ("Script running ...", "Action running ...", a
  // finished run's outcome). It never opens the run panel / properties overlay
  // (that is the Actions button's job):
  //   - a run this tab does not stream yet -> join it (pane + full WS stream);
  //   - a finished run this tab watched -> replay its transcript (pane + history);
  //   - a run this tab already streams -> just re-show the pane.
  if (pendingJoinRun && !currentActionsWs) { joinActiveRun(pendingJoinRun); pendingJoinRun = null; return; }
  if (!currentActionsWs && currentRunId) { streamActionRun(currentRunId); return; }
  openActionTermPane();
});

// Closing the overlay only hides it - the run keeps executing server-side and its
// WS stream (and the strip above) keep updating in the background regardless.
//
// Refetches the catalog every time (the server auto-reloads any action script whose
// file changed since it was last loaded - see _refresh_catalog() in port_actions.py),
// so an edited script's params/description show up here without a page reload. If the
// run panel is currently open, its form is rebuilt from the refreshed definition too.
async function refreshActionsCatalog() {
  await loadActionsCatalog();
  if (currentAction) {
    const updated = actionsCatalog.find((a) => a.id === currentAction.id);
    if (updated) {
      currentAction = updated;
      actionsRunTitle.textContent = updated.name || updated.id;
      actionsRunDesc.textContent = updated.description || '';
      actionsRunForm.innerHTML = (updated.params || []).map(renderActionParamField).join('') || '<div class="muted">No parameters</div>';
    }
  } else {
    renderActionsList();
  }
  updateActionsRunButton();
}
function openActionsOverlay() {
  // Always reopen the choose-script list first, even when a run panel is open:
  // the last action's form must not silently come back (docs/design/port_actions.md
  // "UI surface"). The run panel of a running action is reachable from the
  // bottom-right strip (the "view the run" affordance) and from any list entry.
  actionsOverlay.style.display = 'block';
  renderActionsList();
  refreshActionsCatalog();
}
function closeActionsOverlay() { actionsOverlay.style.display = 'none'; }
actionsToggle.addEventListener('click', () => {
  const visible = actionsOverlay.style.display !== 'none';
  if (visible) closeActionsOverlay(); else openActionsOverlay();
});
actionsClose.addEventListener('click', () => closeActionsOverlay());
actionsListRefresh.addEventListener('click', () => refreshActionsCatalog());

// Deep-linking (docs/design/port_actions.md "Deep-linking an action"): `?action=<id>`
// opens that action pre-selected, `&<param_name>=<value>` (bare declared param names)
// pre-fills its run form, `&autorun=1` launches it immediately - through the exact same
// `launchCurrentAction()`/API call a manual "Run" click uses, so auth/permission/param
// validation are never bypassed. Sensitive params are never read from the URL.
const actionToast = document.getElementById('actionToast');
function showActionToast(text) {
  if (!actionToast) return;
  actionToast.textContent = text;
  actionToast.style.display = 'inline-block';
  setTimeout(() => { actionToast.style.display = 'none'; }, 5000);
}
function applyActionDeepLink() {
  const actionId = (qs.get('action') || '').trim();
  if (!actionId) return;
  const action = actionsCatalog.find((a) => a.id === actionId);
  if (!action) return;
  openActionsOverlay();
  openActionRunPanel(action);
  (action.params || []).forEach((p) => {
    if (p.sensitive) return; // never pre-fill sensitive params from the URL
    const raw = qs.get(p.name);
    if (raw === null) return;
    const field = actionsRunForm.elements.namedItem(p.name);
    if (!field) return;
    if (typeof RadioNodeList !== 'undefined' && field instanceof RadioNodeList) {
      for (const el of field) { if (el.value === raw) { el.checked = true; break; } }
    } else {
      field.value = raw;
    }
  });
  if (qs.get('autorun') === '1' && actionsRunForm.reportValidity()) {
    showActionToast(`Auto-starting action: ${action.name || action.id}\u2026`);
    launchCurrentAction();
  }
}


term.onData((data) => {
  if (clientMode !== 'read-write') {
    if (data.includes('\r')) {
      try { term.write('\r\n[WARNING: console is in read-only mode]\r\n'); } catch (_) {}
    }
    return;
  }
  try { ws && ws.send(data); } catch (e) {}
});
function isConnected() { return ws && ws.readyState === WebSocket.OPEN; }
function currentPort() { return selectedPortName; }
function openLogsWindow() {
  const port = currentPort();
  if (!port) { alert('Select a port first.'); return; }
  const url = new URL(getBasePath() + '/logs', window.location.origin);
  url.searchParams.set('port', port);
  window.open(url.toString(), '_blank', 'noopener');
}
if (logsBtn) logsBtn.addEventListener('click', openLogsWindow);
function showBanner(msg, kind='error', detail=null) {
  if (!bannerEl) return;
  // Always clear any previous content (including a prior muted detail line)
  // before rebuilding, so re-showing does not stack leftover lines (issue #62).
  bannerEl.innerHTML = '';
  // Optional second line (detail, e.g. the live offline reason) renders on its
  // own muted line below the main message instead of inline (issue #62).
  const main = document.createElement('div');
  main.textContent = msg;
  bannerEl.appendChild(main);
  if (detail) {
    const sub = document.createElement('div');
    sub.className = 'muted';
    sub.textContent = detail;
    bannerEl.appendChild(sub);
  }
  bannerEl.classList.remove('error','warn');
  bannerEl.classList.add(kind === 'warn' ? 'warn' : 'error');
  bannerEl.style.display = 'block';
  if (kind === 'warn') { document.body.classList.add('port-down'); document.body.classList.remove('ws-down'); }
  else { document.body.classList.add('ws-down'); document.body.classList.remove('port-down'); }
}
function hideBanner() {
  if (!bannerEl) return;
  if (wsDownTimer) { clearTimeout(wsDownTimer); wsDownTimer = null; }
  if (portDownTimer) { clearTimeout(portDownTimer); portDownTimer = null; }
  bannerEl.style.display = 'none';
  document.body.classList.remove('ws-down');
  document.body.classList.remove('port-down');
}
function updateButton() { if (isConnected()) connectBtn.textContent = 'Disconnect'; else if (currentConnectedPort && currentPort() === currentConnectedPort) connectBtn.textContent = 'Reconnect'; else connectBtn.textContent = 'Connect'; }
let splashShown = false; function showSplash() { if (splashShown) return; splashShown = true; const art = [
    '\x1b[36m',
    '    ___                   __  __               ____                      _      ',
    '   / _ \\ _ __   ___ _ __ |  \\/  |_   ___  __  / ___|___  _ __  ___  ___ | | ___ ',
    '  | | | | \'_ \\ / _ \\ \'_ \\| |\\/| | | | \\ \\/ / | |   / _ \\| \'_ \\/ __|/ _ \\| |/ _ \\',
    '  | |_| | |_) |  __/ | | | |  | | |_| |>  <  | |__| (_) | | | \\__ \\ (_) | |  __/',
    '   \\___/| .__/ \\___|_| |_|_|  |_|\\__,_/_/\\_\\  \\____\\___/|_| |_|___/\\___/|_|\\___|',
    '        |_|                   ',
    '  \x1b[0m',
    '',
    ' Welcome to the OpenMux Web Console',
    ' - Select a Console from the left menu and click Connect',
    ' - URL params: ?port=<name>',
    '   - Add &scrollback=0 to skip scrollback replay',
    '   - Add &embed=1 to start with the sidebar collapsed',
    '     use the sidebar toggle button in the top bar to bring it back',
    '   - Add &action=<id> to open a Port Action run form pre-selected',
    '     &<param_name>=<value> pre-fills a param, &autorun=1 launches it',
    '     (never for params marked sensitive - those are never read from the URL)',
    ' - Disconnect/Reconnect using the button on the right',
    '',]; for (const line of art) term.write(line + '\r\n'); fitTerminal(); }

function updateSidebarHighlight(portName) {
  const items = document.querySelectorAll('.nav-sub-item');
  items.forEach(el => {
    try {
        const url = new URL(el.href, window.location.origin);
        const p = url.searchParams.get('port');
        if (p === portName) {
            el.classList.add('active');
        } else {
            el.classList.remove('active');
        }
    } catch (e) { }
  });
}

function connectSelected() {
  const port = currentPort(); if (!port) { alert('Select a console'); return; }
  if (isConnected()) {
    // Close the existing connection first, then reconnect after a short delay
    // so the server has time to process the close before the new connection arrives.
    try { abortSlowPaste('Reconnecting'); ws.close(1000, 'Client requested reconnect'); } catch (_) {}
    setTimeout(() => connectSelected(), 50);
    return;
  }
  const proto = (location.protocol === 'https:') ? 'wss' : 'ws';
  const basePath = getBasePath();
  const meta = (ports || []).find(p => p.name === port) || {};
  const url = meta.origin_server_id
    ? `${proto}://${location.host}${basePath}/ws/${encodeURIComponent(String(meta.origin_server_id))}/${encodeURIComponent(port)}?meta=1`
    : `${proto}://${location.host}${basePath}/ws/${encodeURIComponent(port)}?meta=1`;
  ws = new WebSocket(url); ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    const meta = (ports || []).find(p => p.name === port) || {};
    const desc = meta.description ? ` - ${meta.description}` : '';
    const origin = meta.origin_server_id ? String(meta.origin_server_id) : 'local';
    const isFirstConnect = (currentConnectedPort === null);
    // Do not clear the terminal when switching; continue on a new line instead
    if (!isFirstConnect) {
      try { term.write('\r\n'); } catch (_) {}
    }
    term.write(`[Connected to console: ${origin}::${port}${desc}]\r\n`);
    try { term.write('Use Ctrl+E to access control menu\r\n'); } catch (_) {}
    // Request scrollback replay unless explicitly disabled with ?scrollback=0
    if (qs.get('scrollback') !== '0') {
      try { ws.send('OMXCTRL ' + JSON.stringify({ type: 'request_scrollback' })); } catch (_) {}
    }
    currentConnectedPort = port;
    // Clear any pending banners when WS opens
    if (wsDownTimer) { clearTimeout(wsDownTimer); wsDownTimer = null; }
    if (portDownTimer) { clearTimeout(portDownTimer); portDownTimer = null; }
    hideBanner();
    updateButton();
    fitTerminal();
    term.focus();
    if (infoOverlay.style.display !== 'none') renderInfo(meta);
  };
  ws.onclose = (ev) => {
    // Ensure any ongoing slow paste is stopped
    try { abortSlowPaste('Disconnected'); } catch (_) {}
    // Clear read-only indicator when disconnected
    clientMode = 'read-only'; document.body.classList.remove('mode-readonly'); if (roIndicatorWrap) roIndicatorWrap.style.display = 'none'; closeRoMenu();
    updateViewersBadge([]); closeViewersMenu();
    const meta = (ports || []).find(p => p.name === currentConnectedPort) || {};
    const desc = meta.description ? ` - ${meta.description}` : '';
    const origin = meta.origin_server_id ? String(meta.origin_server_id) : 'local';
    const reason = ev && ev.reason ? ` (reason: ${ev.reason})` : (ev && ev.code ? ` (code: ${ev.code})` : '');
    const nameForMsg = currentConnectedPort || port;
    term.write(`[Disconnected from ${origin}::${nameForMsg}${desc}]${reason}\r\n`);
    // Delay showing WS-down banner to avoid flicker during quick reconnects
    if (wsDownTimer) { clearTimeout(wsDownTimer); wsDownTimer = null; }
    if (portDownTimer) { clearTimeout(portDownTimer); portDownTimer = null; }
    wsDownTimer = setTimeout(() => {
      showBanner('WebSocket disconnected. Click Reconnect to retry.');
      wsDownTimer = null;
    }, 1000);
    fitTerminal();
    updateButton();
  };
  ws.onerror = (e) => {
    // Also debounce error banner to avoid transient flicker on quick reconnect
    if (wsDownTimer) { clearTimeout(wsDownTimer); wsDownTimer = null; }
    if (portDownTimer) { clearTimeout(portDownTimer); portDownTimer = null; }
    wsDownTimer = setTimeout(() => {
      showBanner('WebSocket error. See console and retry.');
      wsDownTimer = null;
    }, 1000);
    console.error(e);
  };
  ws.onmessage = (ev) => {
    // Control path: metadata frames prefixed with 'OMXCTRL '
    if (typeof ev.data === 'string' && ev.data.startsWith('OMXCTRL ')) {
      try {
        const payload = ev.data.slice('OMXCTRL '.length);
        const msg = JSON.parse(payload);
        if (msg && msg.type === 'rw_holders') {
          updateRoMenuInfo(msg.holders, msg.max_rw_users);
          return;
        }
        if (msg && msg.type === 'presence') {
          // Ambient viewer badge (issue #48) - no toast/popup, just a live count/list update.
          updateViewersBadge(msg.viewers);
          return;
        }
        if (msg && msg.type === 'client_mode') {
          clientMode = (msg.mode === 'read-write') ? 'read-write' : 'read-only';
          if (msg.client_id) myClientId = msg.client_id;
          updateCtrlMenuButtons();
          if (Array.isArray(msg.rw_holders) || msg.max_rw_users !== undefined) updateRoMenuInfo(msg.rw_holders || [], msg.max_rw_users);
          if (msg.takeover) {
            // Targeted takeover success (issue #61): the `takeover` field is
            // the demoted holder's label `[id] username@ip (rw)`.
            try { term.write('\r\n[Taken from: ' + msg.takeover + ']\r\n'); } catch (_) {}
          }
          const silentReasons = ['demoted', 'action_self_demoted', 'action_restored', 'action_restore_denied'];
          if (msg.reason === 'invalid_target') {
            // Targeted takeover where the named client_id is not (or no
            // longer) a read-write holder: no slot moved, say so.
            try { term.write('\r\n[Take refused: that user does not hold read-write access (check the id in the holders list)]\r\n'); } catch (_) {}
          } else if (msg.reason === 'federation_denied') {
            try { term.write('\r\n[Take refused: the origin server did not grant the takeover]\r\n'); } catch (_) {}
          } else if (msg.ok === false && !silentReasons.includes(msg.reason)) {
            if (msg.max_rw_users === 0) {
              // 0 = the port's write-slot capacity is 'none' (issue #59): it has no driver at all.
              try { term.write('\r\n[read-write is not available on this port – it has no write slots (capacity: none)]\r\n'); } catch (_) {}
            } else {
              const who = (lastRwHolders.length ? ' (held by: ' + lastRwHolders.join(', ') + ')' : '');
              try { term.write('\r\n[read-write request denied' + who + ' – use Take control if needed]\r\n'); } catch (_) {}
            }
          }
          if (msg.reason === 'demoted') {
            // "taken_by" names the taker on a local write-slot takeover
            // (issue #59 Part 2); federation relays of the same takeover omit
            // it, so fall back to the generic "another user".
            const by = msg.taken_by ? msg.taken_by : 'another user';
            try { term.write('\r\n[Your read-write access was taken by ' + by + ']\r\n'); } catch (_) {}
          } else if (msg.reason === 'action_self_demoted') {
            try { term.write('\r\n[Your read-write access was set aside to run a Port Action]\r\n'); } catch (_) {}
          } else if (msg.reason === 'action_restored') {
            try { term.write('\r\n[Read-write access restored after the Port Action finished]\r\n'); } catch (_) {}
          } else if (msg.reason === 'action_restore_denied') {
            try { term.write('\r\n[Read-write not restored after the Port Action – the slot is held by another client]\r\n'); } catch (_) {}
          }
          if (clientMode === 'read-write' && msg.ok !== false && msg.reason !== 'demoted') {
            hideCtrlMenu();
            try { term.focus(); } catch (_) {}
          }
          if (infoOverlay.style.display !== 'none') {
            renderInfo(ports.find(x => x.name === currentPort()) || null);
          }
          return;
        }
        if (msg && msg.type === 'action_run') {
          // Live "script started/finished" notice for consoles that haven't joined this
          // run themselves (see docs/design/port_actions.md "Live view") - a client that
          // already has the run's own WS stream open handles it via streamActionRun()
          // instead, so skip here to avoid clobbering that richer live state.
          if (currentActionsWs) return;
          if (msg.event === 'action_started') {
            pendingJoinRun = { run_id: msg.run_id, action_id: msg.action_id, operator_client_id: msg.operator_client_id };
            const label = (actionsCatalog.find((a) => a.id === msg.action_id) || {}).name || msg.action_name || msg.action_id;
            showActionStrip(`Script running: ${label} \u2014 click to open`);
          } else if (msg.event === 'action_finished' && pendingJoinRun && pendingJoinRun.run_id === msg.run_id) {
            pendingJoinRun = null;
            hideActionStrip();
          }
          return;
        }
        if (msg && msg.type === 'meta' && msg.name) {
          const idx = ports.findIndex(p => p.name === msg.name);
          const prev = (idx >= 0 ? ports[idx] : {});
          // Start from previous snapshot and apply only defined fields from msg
          const merged = Object.assign({}, prev, { name: msg.name });
          const applyIf = (key) => { if (Object.prototype.hasOwnProperty.call(msg, key) && msg[key] !== undefined && msg[key] !== null) { merged[key] = msg[key]; } };
          applyIf('description');
          applyIf('adapter');
          applyIf('connected');
          applyIf('serial_config');
          applyIf('line_status');
          applyIf('server_chain');
          applyIf('last_seen');
          applyIf('readiness');
          // status_message (issue #57): the server omits the key when the port
          // is healthy, so drop it explicitly when a snapshot clears it.
          if (Object.prototype.hasOwnProperty.call(msg, 'status_message')) {
            if (msg.status_message) merged.status_message = msg.status_message;
            else delete merged.status_message;
          }
          if (idx >= 0) {
            ports[idx] = merged;
          } else {
            ports.push(merged);
          }
          if (currentPort() === msg.name) updatePortDisplay();
          // Track port-up/down for selected port
          if (currentPort() === msg.name) {
            portIsUp = !!merged.connected;
            // Debounce yellow port-down banner to avoid flicker on quick state transitions
            if (isConnected()) {
              if (!portIsUp) {
                if (portDownTimer) { clearTimeout(portDownTimer); }
                portDownTimer = setTimeout(() => {
                  // Re-check conditions before showing
                  if (isConnected() && currentPort() === msg.name && !portIsUp) {
                    // Issue #62: show the live reason (e.g. "Connection refused by
                    // host:port") as a second muted line when we have one, so the
                    // banner explains *why* the port is down.
                    // Issue #68: a healthy-but-not-running (idle) port is not a
                    // failure — use distinct wording and no reason line.
                    const p = ports.find(x => x.name === currentPort()) || null;
                    if (p && p.readiness === 'idle') {
                      showBanner('Port is healthy but not running. Data will resume when it starts.', 'warn');
                    } else {
                      const reason = (p && p.status_message ? p.status_message : null);
                      showBanner('Port is disconnected on server. Data will resume when it becomes available.', 'warn', reason);
                    }
                  }
                  portDownTimer = null;
                }, 1000);
              } else {
                if (portDownTimer) { clearTimeout(portDownTimer); portDownTimer = null; }
                hideBanner();
              }
            }
          }
          if (infoOverlay.style.display !== 'none' && currentPort() === msg.name) {
            renderInfo(merged);
          }
        }
      } catch (_) {
        // ignore malformed control frames
      }
      return; // don't send control frames to terminal
    }
    // Track last RX for slow paste completion debounce
    slowPaste.lastRxTs = performance.now();
    if (slowPaste.waitingIdle) { scheduleSlowPasteCompletionAnnounce(); }
    if (ev.data instanceof ArrayBuffer) {
      const dec = new TextDecoder('utf-8');
      term.write(dec.decode(new Uint8Array(ev.data)));
    } else {
      term.write(String(ev.data));
    }
  };
}
connectBtn.addEventListener('click', () => { if (isConnected()) { try { abortSlowPaste('Disconnecting'); ws.close(1000, 'Client requested disconnect'); } catch (_) {} } else { connectSelected(); } updateButton(); });
// Explicitly close the WebSocket when navigating away so the server can
// clean up the session immediately rather than waiting for a TCP timeout.
// beforeunload fires while the page is still fully active, giving Chrome the
// best chance to actually transmit the CLOSE frame before tearing down the
// connection.  pagehide is kept as a secondary safety net (fires on bfcache
// entry and back/forward navigations where beforeunload may not fire).
function _closeWsOnExit() {
  try { if (ws && ws.readyState === WebSocket.OPEN) ws.close(1000, 'Page navigated away'); } catch (_) {}
  closeActionRunStream();
}
window.addEventListener('beforeunload', _closeWsOnExit);
window.addEventListener('pagehide', _closeWsOnExit);
loadPorts().then(() => { const qpName = selectedPortName; if (qpName) { connectSelected(); } updateButton(); if (qpName) updateSidebarHighlight(qpName); if (!qpName) showSplash(); });
Promise.all([fetchActionsCSRF(), loadActionsCatalog()]).then(() => applyActionDeepLink());

// Keyboard shortcut: Ctrl+] then 'r' to request read-write directly from Web UI
window.addEventListener('keydown', (e) => {
  // Toggle control menu: Ctrl+E
  if (e.key.toLowerCase() === 'e' && e.ctrlKey) { if (ctrlMenu.style.display === 'none') { closeRoMenu(); closeViewersMenu(); showCtrlMenu(); } else hideCtrlMenu(); e.preventDefault(); return false; }
  // If ']' is pressed with Control
  if (e.key === ']' && e.ctrlKey) {
    const onKey = (ev) => {
      if (ev.key.toLowerCase() === 'r') {
        try { ws && ws.send('OMXCTRL ' + JSON.stringify({ type: 'request_rw' })); } catch (_) {}
        window.removeEventListener('keydown', onKey, true);
        ev.preventDefault();
        return false;
      }
      // any other key dismisses
      window.removeEventListener('keydown', onKey, true);
    };
    window.addEventListener('keydown', onKey, true);
    e.preventDefault();
    return false;
  }
}, true);
