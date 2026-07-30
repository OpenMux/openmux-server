// Apply theme immediately to avoid flash
(function() {
  try {
    var theme = localStorage.getItem('omx_theme');
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    }
  } catch(e) {}
})();
