(function() {
  const viewer = document.getElementById('logViewer');
  if (!viewer) return;
  // Scroll to bottom after paint so newest entries are visible
  requestAnimationFrame(() => {
    viewer.scrollTop = viewer.scrollHeight;
  });
})();
