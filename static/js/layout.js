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
