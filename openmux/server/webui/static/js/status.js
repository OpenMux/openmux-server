(function () {
  // Status page live updates (issue #85; always-on per issue #86 follow-up):
  // the page opens /ws/status on load - like the Power pages, no toggle -
  // and swaps the OMX-STATUS-FRAGMENT regions in place on each server poke
  // (re-fetching /api/status/fragments, like the Power pages re-fetch
  // /api/power). Scroll position and expanded per-port Details survive an
  // update because nothing reloads. The old Auto Refresh button predates the
  // WebSocket (it triggered a window.location.reload() every 5 s); with
  // in-place updates its cost/benefit disappeared, so it is gone. Pre-#86
  // browsers may still hold its per-browser pref key; delete the stale key.
  try {
    localStorage.removeItem('omx_status_autorefresh');
  } catch (e) {}
  // Reconnect timing lives in the shared layout.js policy (omxReconnect):
  // capped backoff, and a stop with a "Signed out" badge once the server no
  // longer accepts our session (e.g. after a server restart). An earlier
  // fixed 3 s retry spammed the server forever in that case.
  // Regions the fragment endpoint returns, mapped to their container ids.
  const REGION_IDS = { summary: 'statusSummary', ports: 'portsTbody', federation: 'federationArea', clients: 'clientsTbody' };
  let ws = null;
  let fetching = false;
  let refetchQueued = false;

  function getBasePath() {
    const meta = document.querySelector('meta[name="omx-base-path"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }

  function setLive(open) {
    // Shared header badge (issue #86): green while the socket is open, red
    // (after the helper's debounce) while it is closed.
    if (window.omxSetLive) window.omxSetLive(open);
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
    if (ws) return;
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(scheme + '://' + location.host + getBasePath() + '/ws/status');
    ws.onopen = () => {
      setLive(true);
      if (window.omxReconnectReset) window.omxReconnectReset();
    };
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
      // Shared policy: backoff + stop when the login session died (layout.js).
      if (window.omxReconnect) window.omxReconnect(connect);
    };
    ws.onerror = () => {
      try {
        if (ws) ws.close();
      } catch (e) {}
    };
  }

  // Opt in before the first connect: a socket that never opens turns the
  // badge red after the helper's debounce (issue #86).
  setLive(false);
  connect();

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
