// PDP Image Set — gallery live updates.
//
// While any asset is still generating, poll the status endpoint and reload the
// page once a status actually changes (an asset becomes ready/failed), so the new
// thumbnail + download control render from the server. Polling stops when nothing
// is pending. Without JS the template's <noscript> meta-refresh is the fallback.
// CSP is script-src 'self' — external file only.

(function () {
  "use strict";

  var grid = document.querySelector(".imageset-grid");
  if (!grid || grid.getAttribute("data-pending") !== "1") {
    return;
  }
  var url = grid.getAttribute("data-status-url");
  if (!url) {
    return;
  }

  var POLL_MS = 3000;

  // Snapshot the current per-asset status so we can detect a real change.
  function currentStatuses() {
    var map = {};
    grid.querySelectorAll(".imageset-card").forEach(function (card) {
      map[card.getAttribute("data-asset-id")] = card.getAttribute("data-status");
    });
    return map;
  }

  function poll() {
    fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data) {
          window.setTimeout(poll, POLL_MS);
          return;
        }
        var before = currentStatuses();
        var changed = data.assets.some(function (a) {
          return String(before[a.id]) !== String(a.status);
        });
        // Reload when a status changed (new thumbnail/download to show) or when the
        // run just finished (so the "Download All" header appears).
        if (changed || !data.pending) {
          window.location.reload();
          return;
        }
        window.setTimeout(poll, POLL_MS);
      })
      .catch(function () {
        // Network hiccup — keep trying; the no-JS meta-refresh is also in play.
        window.setTimeout(poll, POLL_MS);
      });
  }

  window.setTimeout(poll, POLL_MS);
})();

// Batch actions (Regenerate / Discard / Keep selected): enable the buttons only
// when ≥1 image is ticked and show the count. Runs regardless of polling state.
// CSP-safe (external file).
(function () {
  "use strict";

  var buttons = document.querySelectorAll(".imgset-batch-btn");
  var boxes = document.querySelectorAll('input[name="asset_ids"][form="imgset-select-form"]');
  if (!buttons.length || !boxes.length) {
    return;
  }
  // Remember each button's base label so we can append/remove the "(N)".
  buttons.forEach(function (b) {
    b.setAttribute("data-label", b.textContent.trim());
  });

  function sync() {
    var n = 0;
    boxes.forEach(function (b) {
      if (b.checked) {
        n += 1;
      }
    });
    buttons.forEach(function (b) {
      b.disabled = n === 0;
      b.textContent = n ? b.getAttribute("data-label") + " (" + n + ")" : b.getAttribute("data-label");
    });
  }

  boxes.forEach(function (b) {
    b.addEventListener("change", sync);
  });
  sync();
})();
