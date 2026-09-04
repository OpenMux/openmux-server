(function() {
  const themeToggle = document.getElementById('themeToggle');
  const THEME_KEY = 'omx_theme';

  function setTheme(theme) {
      if (theme === 'light') {
          document.documentElement.setAttribute('data-theme', 'light');
          if(themeToggle) themeToggle.textContent = '🌙';
      } else {
          document.documentElement.removeAttribute('data-theme');
          if(themeToggle) themeToggle.textContent = '☀️';
      }
      localStorage.setItem(THEME_KEY, theme);
  }

  // Init theme state for button icon
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
