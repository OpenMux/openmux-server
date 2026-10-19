// Sidebar status annotations (docs/design/port_actions.md).
//
// Each console list item gets a colored dot showing the port's current Port
// Actions annotation: the active run (pulse), else the finished run awaiting
// its operator's close. Data sources:
//   - Bootstrap: GET /api/port-actions/status. An empty map or a 404 (the
//     Port Actions plugin is not installed) simply means no dots.
//   - Live: "action_status" frames from the global /ws/events socket,
//     dispatched by layout.js as the "omx-site-event" CustomEvent.
// A "cancelled" or "closed" frame triggers a re-fetch instead of an update:
// neither is an outcome to show, and only the server knows what, if anything,
// annotates the port now (usually nothing: a close acks the annotated run, and
// a cancel's launch already settled the previous outcome).
// Entries carry `ts`; entries older than what is already applied are
// ignored, so a bootstrap response that was in flight while a live frame
// arrived can never revive a stale state. A fetched response is the full
// truth for ports it does not list, so those dots are dropped - unless a live
// frame landed after the fetch was issued, which the newer state wins.
(function () {
  const byPort = {};

  function getBasePath() {
    const meta = document.querySelector('meta[name="omx-base-path"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }

  function render() {
    if (window.omxSetPortStatuses) window.omxSetPortStatuses(byPort);
  }

  function applyEntry(port, entry) {
    if (!entry) return;
    const prev = byPort[port];
    if (prev && typeof prev.ts === 'number' && typeof entry.ts === 'number' && entry.ts < prev.ts) return;
    byPort[port] = Object.assign({}, entry, { appliedAt: Date.now() });
  }

  function fetchStatuses() {
    const issuedAt = Date.now();
    fetch(getBasePath() + '/api/port-actions/status', { credentials: 'same-origin' })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || !data.ports) return;
        const ports = data.ports;
        Object.keys(byPort).forEach(function (port) {
          if (ports[port]) return;
          // The server no longer annotates this port (its run was closed or
          // cancelled). Drop the dot - but never one fresher than this fetch.
          const local = byPort[port];
          if (!local.appliedAt || local.appliedAt <= issuedAt) delete byPort[port];
        });
        Object.keys(ports).forEach(function (port) {
          applyEntry(port, ports[port]);
        });
        render();
      })
      .catch(function () {});
  }

  window.addEventListener('omx-site-event', function (e) {
    const d = e && e.detail;
    if (!d || d.type !== 'action_status' || !d.port) return;
    if (d.state === 'cancelled' || d.state === 'closed') {
      fetchStatuses();
      return;
    }
    applyEntry(d.port, { action_id: d.action_id, state: d.state, run_id: d.run_id, ts: d.ts, error: d.error });
    render();
  });
  // (Re)connect fills the gap between the last delivered frame and the
  // disconnect, exactly like the status pages re-fetch /api/power.
  window.addEventListener('omx-site-event-open', fetchStatuses);

  fetchStatuses();
})();
