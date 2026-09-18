(function() {
  const sidebar = document.getElementById('sidebar');
  const toggle = document.getElementById('sidebarToggle');
  const KEY = 'omx_sidebar_collapsed';

  function setCollapsed(collapsed) {
    if (collapsed) {
      sidebar.classList.add('collapsed');
      localStorage.setItem(KEY, '1');
    } else {
      sidebar.classList.remove('collapsed');
      localStorage.removeItem(KEY);
    }
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

  // Sidebar width: user-resizable via the right-edge handle, persisted per
  // browser (omx_sidebar_width), same drag pattern as actionTermSplitter in
  // console.js. The min/max clamp keeps the list usable and the main pane alive.
  const SIDEBAR_WIDTH_KEY = 'omx_sidebar_width';
  const SIDEBAR_MIN_W = 160;
  const SIDEBAR_MAX_W = 600;
  function clampSidebarWidth(w) {
      const max = Math.min(SIDEBAR_MAX_W, Math.floor(window.innerWidth * 0.6));
      return Math.max(SIDEBAR_MIN_W, Math.min(w, max));
  }
  try {
      const savedW = parseInt(localStorage.getItem(SIDEBAR_WIDTH_KEY), 10);
      if (savedW >= SIDEBAR_MIN_W && savedW <= SIDEBAR_MAX_W) sidebar.style.width = clampSidebarWidth(savedW) + 'px';
  } catch (_) {}

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
          sidebar.style.width = clampSidebarWidth(e.clientX) + 'px';
          try { if (window.fitTerminal) window.fitTerminal(); } catch (_) {}
      });
      document.addEventListener('mouseup', () => {
          if (!resizing) return;
          resizing = false;
          sidebar.classList.remove('resizing');
          try { localStorage.setItem(SIDEBAR_WIDTH_KEY, parseInt(sidebar.style.width, 10)); } catch (_) {}
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
          item.title = fullText;
          const oldDesc = item.querySelector('.port-desc');
          if (oldDesc) oldDesc.remove();
          if (prefs.desc && desc) {
              const span = document.createElement('span');
              span.className = 'port-desc';
              span.textContent = ' (' + desc + ')';
              item.appendChild(span);
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
  applyPortLabels();
})();
