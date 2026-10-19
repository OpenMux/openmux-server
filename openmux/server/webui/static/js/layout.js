(function() {
  const sidebar = document.getElementById('sidebar');
  const toggle = document.getElementById('sidebarToggle');
  const KEY = 'omx_sidebar_collapsed';

  // Sidebar width: user-resizable via the right-edge handle, persisted per
  // browser (omx_sidebar_width), same drag pattern as actionTermSplitter in
  // console.js. The min/max clamp keeps the list usable and the main pane alive.
  const SIDEBAR_WIDTH_KEY = 'omx_sidebar_width';
  const SIDEBAR_MIN_W = 160;
  const SIDEBAR_MAX_W = 600;
  // Remembered custom width in px; 0 means "no custom width, use the CSS
  // default". The value lives in this variable, never read back from the
  // element: the element's rendered width is not a source of truth (#91).
  let sidebarWidth = 0;
  function clampSidebarWidth(w) {
      const max = Math.min(SIDEBAR_MAX_W, Math.floor(window.innerWidth * 0.6));
      return Math.max(SIDEBAR_MIN_W, Math.min(w, max));
  }
  // JS never sets the element width directly: it publishes the user width
  // as the --sidebar-width custom property and CSS does all the sizing
  // (`.sidebar` uses the var when set; the more specific `.sidebar.collapsed`
  // always wins with `width: 0`). A direct `style.width` write is exactly
  // what broke the collapse in issue #91 - an inline style outranks every
  // stylesheet rule, so a stored width made the class `width: 0` inert.
  function applySidebarWidth() {
      if (sidebarWidth > 0) sidebar.style.setProperty('--sidebar-width', sidebarWidth + 'px');
      else sidebar.style.removeProperty('--sidebar-width');
  }

  function setCollapsed(collapsed) {
    if (collapsed) {
      sidebar.classList.add('collapsed');
      localStorage.setItem(KEY, '1');
    } else {
      sidebar.classList.remove('collapsed');
      localStorage.removeItem(KEY);
    }
    applySidebarWidth();
    // Trigger resize for terminal if present
    if (window.fitTerminal) setTimeout(window.fitTerminal, 250);
  }

  // Init
  if (localStorage.getItem(KEY)) {
    sidebar.classList.add('collapsed');
  }

  if (toggle) {
    toggle.addEventListener('click', () => {
      setCollapsed(!sidebar.classList.contains('collapsed'));
    });
  }

  try {
      const savedW = parseInt(localStorage.getItem(SIDEBAR_WIDTH_KEY), 10);
      if (savedW >= SIDEBAR_MIN_W && savedW <= SIDEBAR_MAX_W) sidebarWidth = clampSidebarWidth(savedW);
  } catch (_) {}
  applySidebarWidth();

  const resizeHandle = document.getElementById('sidebar-resize-handle');
  if (resizeHandle) {
      let resizing = false;
      resizeHandle.addEventListener('mousedown', (e) => {
          e.preventDefault();
          resizing = true;
          sidebar.classList.add('resizing');
      });
      document.addEventListener('mousemove', (e) => {
          if (!resizing) return;
          sidebarWidth = clampSidebarWidth(e.clientX);
          applySidebarWidth();
          try { if (window.fitTerminal) window.fitTerminal(); } catch (_) {}
      });
      document.addEventListener('mouseup', () => {
          if (!resizing) return;
          resizing = false;
          sidebar.classList.remove('resizing');
          if (sidebarWidth > 0) {
              try { localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth)); } catch (_) {}
          }
      });
  }

  window.toggleConsolePorts = function(e) {
      e.preventDefault();
      e.stopPropagation();
      const el = document.getElementById('console-ports');
      const btn = document.getElementById('console-expand-btn');
      if (el.style.display === 'none') {
          el.style.display = 'block';
          btn.textContent = '-';
          localStorage.setItem('omx_console_expanded', '1');
      } else {
          el.style.display = 'none';
          btn.textContent = '+';
          localStorage.removeItem('omx_console_expanded');
      }
  };

  // Clicking the "Console" label expands the port list when it's collapsed,
  // instead of always navigating away to the console page.
  window.onConsoleNavClick = function(e) {
      const el = document.getElementById('console-ports');
      if (el && el.style.display === 'none') {
          window.toggleConsolePorts(e);
      }
  };

  window.togglePowerMenu = function(e) {
      e.preventDefault();
      e.stopPropagation();
      const el = document.getElementById('power-pdus');
      const btn = document.getElementById('power-expand-btn');
      if (el.style.display === 'none') {
          el.style.display = 'block';
          btn.textContent = '-';
          localStorage.setItem('omx_power_expanded', '1');
      } else {
          el.style.display = 'none';
          btn.textContent = '+';
          localStorage.removeItem('omx_power_expanded');
      }
  };

  // Clicking the "Power" label expands the PDU list when it's collapsed,
  // instead of always navigating away to the PDU list page.
  window.onPowerNavClick = function(e) {
      const el = document.getElementById('power-pdus');
      if (el && el.style.display === 'none') {
          window.togglePowerMenu(e);
      }
  };

  window.toggleConfigMenu = function(e) {
      e.preventDefault();
      e.stopPropagation();
      const el = document.getElementById('config-menu');
      const btn = document.getElementById('config-expand-btn');
      if (el.style.display === 'none') {
          el.style.display = 'block';
          btn.textContent = '-';
          localStorage.setItem('omx_config_expanded', '1');
      } else {
          el.style.display = 'none';
          btn.textContent = '+';
          localStorage.removeItem('omx_config_expanded');
      }
  };

  // Restore console menu state
  if (localStorage.getItem('omx_console_expanded')) {
       const el = document.getElementById('console-ports');
       const btn = document.getElementById('console-expand-btn');
       if (el && btn) {
           el.style.display = 'block';
           btn.textContent = '-';
       }
  }
  // Restore config menu state
  if (localStorage.getItem('omx_config_expanded')) {
       const el = document.getElementById('config-menu');
       const btn = document.getElementById('config-expand-btn');
       if (el && btn) {
           el.style.display = 'block';
           btn.textContent = '-';
       }
  }
  // Restore power menu state; auto-expanded on the power pages
  const powerParent = document.getElementById('nav-power-parent');
  if (localStorage.getItem('omx_power_expanded') || (powerParent && powerParent.classList.contains('active'))) {
       const el = document.getElementById('power-pdus');
       const btn = document.getElementById('power-expand-btn');
       if (el && btn) {
           el.style.display = 'block';
           btn.textContent = '-';
       }
  }

  // Auto-expand config menu if active
  const configParent = document.getElementById('nav-config-parent');
  if (configParent && configParent.classList.contains('active')) {
       const el = document.getElementById('config-menu');
       const btn = document.getElementById('config-expand-btn');
       if (el && btn) {
           el.style.display = 'block';
           btn.textContent = '-';
       }
  }

  // Center the current port in the port list after page load.
  // Port links are real page loads, so the scrollable list resets to the top on
  // every port switch. When the URL selects a port (?port=...), scroll the list
  // so that port is centered in the visible list area.
  (function () {
      const list = document.getElementById('console-ports');
      if (!list) return;

      // Center an item inside the list only - never scrollIntoView, which can
      // also scroll the page. Only #console-ports is scrollable in this layout.
      function centerItem(item) {
          const lr = list.getBoundingClientRect();
          const ir = item.getBoundingClientRect();
          list.scrollTop += (ir.top + ir.height / 2) - (lr.top + lr.height / 2);
      }

      // Match by decoded URL port param, not link text, so any port name works.
      function findItem(port) {
          const links = list.querySelectorAll('.nav-sub-item');
          for (const link of links) {
              const href = link.getAttribute('href');
              if (!href) continue;
              if (new URL(href, window.location.origin).searchParams.get('port') === port) {
                  return link;
              }
          }
          return null;
      }

      function apply() {
          if (list.style.display === 'none') return;
          const port = new URLSearchParams(window.location.search).get('port');
          const item = findItem(port) || list.querySelector('.nav-sub-item.active');
          if (item) centerItem(item);
      }

      requestAnimationFrame(apply);
  })();

  // Theme toggle logic
  const themeToggle = document.getElementById('themeToggle');
  const themeIcon = document.getElementById('themeIcon');
  const themeText = document.getElementById('themeText');
  const THEME_KEY = 'omx_theme';

  function setTheme(theme) {
      if (theme === 'light') {
          document.documentElement.setAttribute('data-theme', 'light');
          if(themeIcon) themeIcon.textContent = '🌙';
          if(themeText) themeText.textContent = 'Dark Mode';
      } else {
          document.documentElement.removeAttribute('data-theme');
          if(themeIcon) themeIcon.textContent = '☀️';
          if(themeText) themeText.textContent = 'Light Mode';
      }
      localStorage.setItem(THEME_KEY, theme);
      window.dispatchEvent(new CustomEvent('theme-changed', { detail: { theme } }));
  }

  // Init theme
  const savedTheme = localStorage.getItem(THEME_KEY);
  if (savedTheme) {
      setTheme(savedTheme);
  }

  if (themeToggle) {
      themeToggle.addEventListener('click', (e) => {
          e.preventDefault();
          const current = document.documentElement.getAttribute('data-theme');
          setTheme(current === 'light' ? 'dark' : 'light');
      });
  }

  // Port list label options: independent "show server" and "show description"
  // toggles (omx_port_show_server / omx_port_show_desc, same localStorage
  // pattern as the other omx_* prefs). They combine into four label forms:
  // port | port (description) | server::port | server::port (description).
  // Port names are re-read from each link's href port= param (never from the
  // mutated text), so click routing, active highlighting, and centering - all
  // href-based elsewhere - are unaffected.
  // The server renders the list sorted by port name; capture that order once so
  // toggling "show server" off can restore it exactly (a live DOM read would
  // return the already-sorted order after an earlier "show server" pass). The
  // capture is refreshed via setPortOrder() when the list is rebuilt
  // (refreshSidebarPorts after a Config Editor save).
  let NATURAL_PORT_ORDER = (() => {
      const pl = document.getElementById('console-ports');
      return pl ? Array.from(pl.querySelectorAll('a.nav-sub-item[data-origin]')) : [];
  })();
  // Exposed for the Config Editor: after a save, refreshSidebarPorts in
  // config_editor.js rebuilds the sidebar links, so the natural order must
  // be re-captured from the new link elements (the original ones are detached).
  window.setPortOrder = function(){
      const pl = document.getElementById('console-ports');
      NATURAL_PORT_ORDER = pl ? Array.from(pl.querySelectorAll('a.nav-sub-item[data-origin]')) : [];
      return NATURAL_PORT_ORDER;
  };
  const PORT_LABELS_SERVER_KEY = 'omx_port_show_server';
  const PORT_LABELS_DESC_KEY = 'omx_port_show_desc';
  // Per-port action outcome for the sidebar dots (docs/design/port_actions.md,
  // "Sidebar status annotations"). Fed by port_status.js through
  // window.omxSetPortStatuses(); rendered by applyPortLabels() below, which
  // owns the item text and must therefore (re)create the dot on every pass.
  let PORT_STATUS_BY_NAME = {};
  const PORT_STATUS_LABELS = { success: 'success', failed: 'failed', timeout: 'timed out' };

  function portStatusDotClass(state) {
      if (state === 'success') return 'ok';
      if (state === 'failed') return 'bad';
      if (state === 'timeout') return 'warn';
      if (state === 'running') return 'running';
      return '';
  }

  function portStatusTooltip(st) {
      const when = st.ts ? new Date(st.ts * 1000).toLocaleString() : '';
      if (st.state === 'running') return 'action ' + st.action_id + ': running since ' + when;
      const label = PORT_STATUS_LABELS[st.state] || st.state;
      let text = 'last action ' + st.action_id + ': ' + label + (when ? ' at ' + when : '');
      if (st.error) text += ' - ' + String(st.error).slice(0, 120);
      return text;
  }

  function portLabelPrefs() {
      return {
          server: !!localStorage.getItem(PORT_LABELS_SERVER_KEY),
          desc: !!localStorage.getItem(PORT_LABELS_DESC_KEY),
      };
  }

  function portLabelDesc(desc) {
      return String(desc.replace(/\s+/g, ' ')).trim();
  }

  function applyPortLabels() {
      const prefs = portLabelPrefs();
      const setMenuState = (btnId, on) => {
          const btn = document.getElementById(btnId);
          if (!btn) return;
          if (on) btn.classList.remove('port-labels-menu-item-off');
          else btn.classList.add('port-labels-menu-item-off');
      };
      setMenuState('plShowServer', prefs.server);
      setMenuState('plShowDesc', prefs.desc);
      const list = document.getElementById('console-ports');
      if (!list) return;
      const portItems = Array.from(list.querySelectorAll('a.nav-sub-item[data-origin]'));
      for (const item of portItems) {
          const href = item.getAttribute('href') || '';
          let name = '';
          try { name = new URL(href, window.location.origin).searchParams.get('port') || ''; } catch (e) {}
          if (!name) continue;
          const origin = item.getAttribute('data-origin') || 'local';
          const desc = portLabelDesc(item.getAttribute('data-desc') || '');
          const main = prefs.server ? origin + '::' + name : name;
          const fullText = prefs.desc && desc ? main + ' (' + desc + ')' : main;
          item.textContent = main;
          const status = PORT_STATUS_BY_NAME[name];
          item.title = fullText + (status ? '\n' + portStatusTooltip(status) : '');
          const oldDesc = item.querySelector('.port-desc');
          if (oldDesc) oldDesc.remove();
          if (prefs.desc && desc) {
              const span = document.createElement('span');
              span.className = 'port-desc';
              span.textContent = ' (' + desc + ')';
              item.appendChild(span);
          }
          // Status dot (port_status.js data): a colored dot after the label
          // showing the last action outcome on this port. The textContent
          // assignment above dropped any previous dot, so it is (re)appended
          // here on every pass; no entry means no dot (never ran / feature off).
          if (status) {
              const dot = document.createElement('span');
              dot.className = 'port-status-dot ' + portStatusDotClass(status.state);
              dot.setAttribute('aria-hidden', 'true');
              item.appendChild(dot);
          }
          item.setAttribute('data-sort-name', name);
          item.setAttribute('data-sort-origin', origin);
      }
      // "Show server" also reorders the list: by server id, then port name
      // (numeric-aware, matching the server's by-port sort). Toggling it off
      // restores the server-rendered order.
      if (prefs.server) {
          for (const item of [...portItems].sort((a, b) =>
              a.dataset.sortOrigin.localeCompare(b.dataset.sortOrigin, undefined, {sensitivity: 'base', numeric: true})
              || a.dataset.sortName.localeCompare(b.dataset.sortName, undefined, {sensitivity: 'base', numeric: true}))) {
              list.appendChild(item);
          }
      } else {
          for (const item of NATURAL_PORT_ORDER) {
              if (item.isConnected) list.appendChild(item);
          }
      }
  }

  window.togglePortLabelPref = function(e, which) {
      e.preventDefault();
      e.stopPropagation();
      const key = which === 'server' ? PORT_LABELS_SERVER_KEY : PORT_LABELS_DESC_KEY;
      if (localStorage.getItem(key)) localStorage.removeItem(key);
      else localStorage.setItem(key, '1');
      applyPortLabels();
  };

  window.togglePortLabelsMenu = function(e) {
      e.preventDefault();
      e.stopPropagation();
      const menu = document.getElementById('port-labels-menu');
      if (!menu) return;
      menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
  };
  window.closePortLabelsMenu = function() {
      const menu = document.getElementById('port-labels-menu');
      if (menu) menu.style.display = 'none';
  };
  document.addEventListener('click', () => {
      const menu = document.getElementById('port-labels-menu');
      if (menu) menu.style.display = 'none';
  });
  window.applyPortLabels = applyPortLabels;
  // Sidebar dots entry point (port_status.js): replace the per-port status map
  // and repaint. The map is {port_name: {action_id, state, run_id, ts, error}}.
  window.omxSetPortStatuses = function (map) {
      PORT_STATUS_BY_NAME = map && typeof map === 'object' ? map : {};
      applyPortLabels();
  };
  applyPortLabels();
})();

// "Live data" badge (issue #86): the single place the header badge is shown.
// A page with a page-level WebSocket opts in by calling window.omxSetLive():
//   omxSetLive(true)  - socket open: green, "updates over WebSocket, no reload"
//   omxSetLive(false) - socket closed: red, "data may be stale", after a 1 s
//                       debounce so a fast reconnect never flickers red (same
//                       window as console.js' WS-down banner debounce)
// Once opted in the badge stays visible (green or red), so a dropped socket is
// noticeable. Terminal pages never call this, so the badge never shows there.
(function () {
  const badge = document.getElementById('liveDataBadge');
  if (!badge) return;
  const GREEN = { cls: 'ok', tip: 'Live data: updates over WebSocket, no reload' };
  const RED = { cls: 'bad', tip: 'Live data: disconnected, data may be stale' };
  const RED_DEBOUNCE_MS = 1000;
  let redTimer = null;
  function apply(state) {
    badge.classList.remove('ok', 'bad');
    badge.classList.add(state.cls);
    badge.title = state.tip;
    badge.textContent = '\u25CF Live data';
    badge.style.display = '';
  }
  window.omxSetLive = function (open) {
    if (redTimer) { clearTimeout(redTimer); redTimer = null; }
    if (open === true) { apply(GREEN); return; }
    // false: show red only if the socket stays closed past the debounce window.
    redTimer = setTimeout(() => { redTimer = null; apply(RED); }, RED_DEBOUNCE_MS);
  };
})();

// Background reconnect policy for the silent page-level WebSocket loops
// (Status, Power list, Power detail). The old loops retried a fixed 3 s
// forever, which caused two problems:
//   * a long server outage was hammered every 3 s with no backoff;
//   * after a server restart the login session (held in server memory) no
//     longer exists, so every reconnect is rejected: the loop never
//     recovered and kept spamming the server indefinitely.
// The policy adds capped backoff (1 s to 60 s) and, from the second failed
// attempt on, asks one cheap authenticated /api/* request what happened
// before the next try:
//   401    our login session is gone (restart or expiry): stop
//          reconnecting and turn the "Live data" badge into a
//          reload-to-sign-in link;
//   else   server down or answering strangely: keep reconnecting - the
//          page recovers on its own when the server returns.
// Loops call window.omxReconnect(connect) in ws.onclose and
// window.omxReconnectReset() in ws.onopen.
(function () {
  const DELAYS_MS = [1000, 2000, 5000, 10000, 30000, 60000];
  const PROBE_PATH = '/api/status/fragments'; // core route on every install
  function getBasePath() {
    const meta = document.querySelector('meta[name="omx-base-path"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }
  function probeAuth() {
    // /api/* answers 401 (never a login redirect) when the session is gone,
    // so one fetch distinguishes "logged out" from "server down" without
    // redirect handling. A rejected fetch means the server is unreachable,
    // which is not an auth failure: keep reconnecting.
    return fetch(getBasePath() + PROBE_PATH, { credentials: 'same-origin' }).then(function (r) {
      return r.status === 401 ? 'auth' : 'continue';
    });
  }
  function showSignedOut() {
    const badge = document.getElementById('liveDataBadge');
    if (!badge) return;
    badge.classList.remove('ok');
    badge.classList.add('bad');
    badge.textContent = '\u21bb Signed out \u2013 click to sign in again';
    badge.title =
      'The server no longer recognizes this login session (a server restart clears sessions). ' +
      'Background reconnect stopped so the page does not spam the server. Click to reload and sign in again.';
    badge.style.cursor = 'pointer';
    badge.style.display = '';
    badge.onclick = function () {
      window.location.reload();
    };
  }
  // One policy instance per WebSocket: a page can now hold two live sockets
  // (the status page socket and the global /ws/events site socket), and a
  // single shared attempt/timer state silently starves whichever socket calls
  // second, because the `timer` guard swallows its retry.
  function makeReconnectPolicy() {
    let attempt = 0; // consecutive failed reconnects since the last open
    let timer = null;
    let stopped = false;
    let generation = 0; // invalidates a scheduled attempt after a reset
    return {
      reconnect: function (connect) {
        if (stopped || timer) return;
        const delay = DELAYS_MS[Math.min(attempt, DELAYS_MS.length - 1)];
        attempt += 1;
        const gen = generation;
        timer = setTimeout(function () {
          timer = null;
          if (gen !== generation || stopped) return;
          if (attempt < 2) {
            connect();
            return;
          }
          probeAuth()
            .then(function (verdict) {
              if (gen !== generation) return;
              if (verdict === 'auth') {
                stopped = true;
                showSignedOut();
                return;
              }
              connect();
            })
            .catch(function () {
              if (gen !== generation || stopped) return;
              connect(); // probe itself failed (server unreachable): keep trying
            });
        }, delay);
      },
      reset: function () {
        attempt = 0;
        stopped = false;
        generation += 1;
        if (timer) {
          clearTimeout(timer);
          timer = null;
        }
      },
    };
  }
  window.omxMakeReconnectPolicy = makeReconnectPolicy;
  // Shared default instance backing the original global API: consumers that
  // predate per-socket policies (status.js) keep working unchanged.
  const DEFAULT_POLICY = makeReconnectPolicy();
  window.omxReconnect = function (connect) {
    DEFAULT_POLICY.reconnect(connect);
  };
  window.omxReconnectReset = function () {
    DEFAULT_POLICY.reset();
  };
})();

// Global site-event socket (/ws/events): one connection per page for
// cross-cutting updates, so features do not each open their own socket.
// Every frame arrives as an "omx-site-event" CustomEvent (detail = parsed
// JSON frame); each (re)connect also fires "omx-site-event-open" and every
// drop "omx-site-event-close", so consumers can re-fetch their bootstrap
// endpoint and show connection state without owning a socket. Server side:
// openmux/server/site_events.py; producers: Port Actions "action_status"
// frames (sidebar dots, port_status.js) and the status page's coalesced
// "status_change" nudges (status.js, migrated off the old /ws/status socket).
(function () {
  const policy = window.omxMakeReconnectPolicy();
  window.omxSiteLive = false; // current socket state, readable by late subscribers
  function getBasePath() {
    const meta = document.querySelector('meta[name="omx-base-path"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }
  function dispatch(name, detail) {
    try {
      window.dispatchEvent(new CustomEvent(name, { detail: detail }));
    } catch (e) {}
  }
  function connect() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    let ws;
    try {
      ws = new WebSocket(scheme + '://' + location.host + getBasePath() + '/ws/events');
    } catch (e) {
      policy.reconnect(connect);
      return;
    }
    ws.onopen = function () {
      policy.reset();
      window.omxSiteLive = true;
      dispatch('omx-site-event-open', null);
    };
    ws.onmessage = function (ev) {
      let data = null;
      try {
        data = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (data && typeof data === 'object') dispatch('omx-site-event', data);
    };
    ws.onclose = function () {
      window.omxSiteLive = false;
      dispatch('omx-site-event-close', null);
      policy.reconnect(connect);
    };
    ws.onerror = function () {
      try {
        ws.close();
      } catch (e) {}
    };
  }
  connect();
})();
