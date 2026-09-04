(function () {
  const REFRESH_KEY = 'omx_status_autorefresh';
  const INTERVAL_MS = 5000;
  const btn = document.getElementById('autoRefreshToggle');
  let timerId = null;

  function updateButton(enabled) {
    if (!btn) return;
    btn.textContent = 'Auto Refresh: ' + (enabled ? 'On' : 'Off');
  }

  function start() {
    if (timerId) return;
    timerId = setInterval(() => { try { window.location.reload(); } catch (e) {} }, INTERVAL_MS);
    updateButton(true);
  }

  function stop() {
    if (timerId) { clearInterval(timerId); timerId = null; }
    updateButton(false);
  }

  function toggleRefresh() {
    const enabled = !!localStorage.getItem(REFRESH_KEY);
    if (enabled) {
      localStorage.removeItem(REFRESH_KEY);
      stop();
    } else {
      localStorage.setItem(REFRESH_KEY, '1');
      start();
    }
  }

  if (btn) btn.addEventListener('click', toggleRefresh);
  // Initialize from saved state
  if (localStorage.getItem(REFRESH_KEY)) {
    start();
  } else {
    updateButton(false);
  }
  // Wire Details toggles
  try {
    const toggles = document.querySelectorAll('.toggleInfo');
    toggles.forEach(btn => {
      // initialize all to '+' on load
      btn.textContent = '+';
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-target');
        const row = document.getElementById(id);
        if (!row) return;
        const show = row.style.display === 'none' || row.style.display === '';
        row.style.display = show ? 'table-row' : 'none';
        btn.textContent = show ? '-' : '+';
      });
    });
  } catch (e) {}
})();
