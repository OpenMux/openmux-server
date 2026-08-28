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
})();
