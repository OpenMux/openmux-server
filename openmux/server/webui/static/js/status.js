(function () {
  // Status page auto-refresh (issue #85): live updates instead of the old
  // window.location.reload() loop. With Auto Refresh ON the page opens
  // /ws/status and swaps the OMX-STATUS-FRAGMENT regions in place on each
  // server poke (re-fetching /api/status/fragments, like the Power pages
  // re-fetch /api/power). With the toggle OFF no WebSocket is opened at all
  // (deliberate: the page is then a plain static render). Scroll position and
  // expanded per-port Details survive an update because nothing reloads.
  const REFRESH_KEY = 'omx_status_autorefresh';
  const RETRY_MS = 3000;
  const btn = document.getElementById('autoRefreshToggle');
  const liveBadge = document.getElementById('statusLiveState');
  // Regions the fragment endpoint returns, mapped to their container ids.
  const REGION_IDS = { summary: 'statusSummary', ports: 'portsTbody', federation: 'federationArea', clients: 'clientsTbody' };
  let ws = null;
  let retryTimer = null;
  let wanted = false; // tracks the toggle; drives (re)connect decisions
  let fetching = false;
  let refetchQueued = false;

  function getBasePath() {
    const meta = document.querySelector('meta[name="omx-base-path"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }

  function updateButton(enabled) {
    if (btn) btn.textContent = 'Auto Refresh: ' + (enabled ? 'On' : 'Off');
  }

  function setLive(on) {
    if (liveBadge) liveBadge.style.display = on ? '' : 'none';
  }

  // Remember which Details (±) rows are open so a fragment swap can restore
  // them (the buttons/rows themselves are replaced by the new markup).
  function collectOpenDetails() {
    const open = {};
    document.querySelectorAll('.toggleInfo[data-target]').forEach((b) => {
      const row = document.getElementById(b.getAttribute('data-target'));
      if (row && row.style.display === 'table-row') open[b.getAttribute('data-target')] = true;
    });
    return open;
  }

  function applyOpenDetails(open) {
    document.querySelectorAll('.toggleInfo[data-target]').forEach((b) => {
      const id = b.getAttribute('data-target');
      const row = document.getElementById(id);
      if (!row) return;
      const isOpen = !!open[id];
      row.style.display = isOpen ? 'table-row' : 'none';
      b.textContent = isOpen ? '-' : '+';
    });
  }

  function swapRegions(fragments) {
    const open = collectOpenDetails();
    Object.keys(REGION_IDS).forEach((name) => {
      if (!(name in fragments)) return; // unknown/missing region: keep server render
      const el = document.getElementById(REGION_IDS[name]);
      if (el) el.innerHTML = fragments[name];
    });
    applyOpenDetails(open);
  }

  function refresh() {
    if (fetching) {
      refetchQueued = true; // coalesce pokes that arrive mid-fetch
      return;
    }
    fetching = true;
    // Echo the page's current query (?sort=...&dir=...) so swapped rows keep the
    // active sort order (the sort links themselves live in the static thead).
    const url = getBasePath() + '/api/status/fragments' + window.location.search;
    fetch(url, { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (j && j.fragments) swapRegions(j.fragments);
      })
      .catch(() => {
        /* transient; the next poke retries */
      })
      .finally(() => {
        fetching = false;
        if (refetchQueued) {
          refetchQueued = false;
          refresh();
        }
      });
  }

  function connect() {
    if (!wanted || ws || retryTimer) return;
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(scheme + '://' + location.host + getBasePath() + '/ws/status');
    ws.onopen = () => setLive(true);
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg && msg.event === 'status_change') refresh();
      } catch (e) {
        /* ignore malformed frame */
      }
    };
    ws.onclose = () => {
      ws = null;
      setLive(false);
      // Silent retry while the toggle stays on (same pattern as power.js).
      if (wanted && !retryTimer) {
        retryTimer = setTimeout(() => {
          retryTimer = null;
          connect();
        }, RETRY_MS);
      }
    };
    ws.onerror = () => {
      try {
        if (ws) ws.close();
      } catch (e) {}
    };
  }

  function disconnect() {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (ws) {
      const sock = ws;
      ws = null; // detach first so onclose does not schedule a retry
      try {
        sock.close();
      } catch (e) {}
    }
    setLive(false);
  }

  function start() {
    wanted = true;
    updateButton(true);
    connect();
  }

  function stop() {
    wanted = false;
    updateButton(false);
    disconnect();
  }

  function toggleRefresh() {
    if (localStorage.getItem(REFRESH_KEY)) {
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
  // Details (±) toggles. Delegated on the document so buttons arriving with a
  // fragment swap keep working without re-wiring.
  try {
    document.querySelectorAll('.toggleInfo').forEach((b) => {
      b.textContent = '+';
    });
    document.addEventListener('click', (e) => {
      const b = e.target && e.target.closest ? e.target.closest('.toggleInfo') : null;
      if (!b) return;
      const id = b.getAttribute('data-target');
      if (!id) return;
      const row = document.getElementById(id);
      if (!row) return;
      const show = row.style.display === 'none' || row.style.display === '';
      row.style.display = show ? 'table-row' : 'none';
      b.textContent = show ? '-' : '+';
    });
  } catch (e) {}
})();
