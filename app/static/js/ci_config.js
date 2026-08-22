/* Competitive Intelligence config page interactions.
 *
 * Two jobs, both progressive enhancement over working server-rendered forms:
 *   1. Confirm destructive submits (buttons carrying data-confirm).
 *   2. While the group's latest run is queued/running, poll its status endpoint
 *      and reload once it finishes so fresh results show without a manual refresh.
 *
 * External file by necessity: the CSP is script-src 'self' (no inline scripts).
 */
(function () {
  "use strict";

  // 1. Confirm-on-delete for any form button with data-confirm.
  document.querySelectorAll("button[data-confirm]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      if (!window.confirm(btn.getAttribute("data-confirm"))) {
        e.preventDefault();
      }
    });
  });

  // 2. Run-status poller.
  var statusEl = document.getElementById("ci-run-status");
  if (!statusEl) return;
  var url = statusEl.getAttribute("data-status-url");
  if (!url) return;

  var badge = statusEl.querySelector(".status-badge");
  var current = badge ? badge.textContent.trim() : null;
  var ACTIVE = ["queued", "running"];
  if (ACTIVE.indexOf(current) === -1) return; // nothing in flight

  var poll = setInterval(function () {
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.status) return;
        if (badge) {
          badge.textContent = data.status;
          badge.className = "status-badge status-" + data.status;
        }
        if (ACTIVE.indexOf(data.status) === -1) {
          clearInterval(poll);
          // Reload so newly written results / counts appear.
          window.location.reload();
        }
      })
      .catch(function () { /* transient network error — keep polling */ });
  }, 4000);
})();
