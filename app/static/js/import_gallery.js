// Import Product Images — gallery selection + live updates.
//
// Two independent enhancements over a page that works without JS:
//   1. Selection: keep the "Download Selected (N)" count in sync with the ticked
//      images, flip the select-all/clear toggle, and disable the download when
//      nothing is selected. (The ZIP form is a plain GET, so it already works with
//      JS off — the browser builds ?ids= from the checked boxes.)
//   2. Polling: while a run is still acquiring, poll the status endpoint and reload
//      once something changes, so new thumbnails render from the server. The
//      <noscript> meta-refresh is the no-JS fallback.
// CSP is script-src 'self' — external file only.

(function () {
  "use strict";

  var grid = document.querySelector(".import-grid");
  if (!grid) {
    return;
  }

  // --- Selection -----------------------------------------------------------

  var toggle = document.getElementById("import-select-toggle");
  var countEl = document.querySelector(".import-sel-count");
  var downloadBtn = document.querySelector('button[form="import-select"]');

  function checkboxes() {
    return Array.prototype.slice.call(grid.querySelectorAll('input[name="ids"]'));
  }

  function refreshSelection() {
    var boxes = checkboxes();
    var checked = boxes.filter(function (b) { return b.checked; });
    if (countEl) {
      countEl.textContent = String(checked.length);
    }
    if (downloadBtn) {
      downloadBtn.disabled = checked.length === 0;
    }
    if (toggle) {
      // When everything is selected the toggle clears; otherwise it selects all.
      var allChecked = boxes.length > 0 && checked.length === boxes.length;
      toggle.textContent = allChecked
        ? toggle.getAttribute("data-clear-label")
        : toggle.getAttribute("data-select-label");
      toggle.setAttribute("data-mode", allChecked ? "clear" : "select");
    }
  }

  grid.addEventListener("change", function (event) {
    if (event.target && event.target.name === "ids") {
      refreshSelection();
    }
  });

  if (toggle) {
    toggle.addEventListener("click", function () {
      var selectAll = toggle.getAttribute("data-mode") !== "clear";
      checkboxes().forEach(function (b) { b.checked = selectAll; });
      refreshSelection();
    });
  }

  refreshSelection();

  // --- Polling (only while acquiring) --------------------------------------

  if (grid.getAttribute("data-pending") !== "1") {
    return;
  }
  var url = grid.getAttribute("data-status-url");
  if (!url) {
    return;
  }

  var POLL_MS = 3000;

  function currentItemStatuses() {
    var map = {};
    grid.querySelectorAll(".import-item").forEach(function (el) {
      map[el.getAttribute("data-item-id")] = el.getAttribute("data-status");
    });
    return map;
  }

  var shownReady = parseInt(grid.getAttribute("data-ready-count"), 10) || 0;

  function readyCount(items) {
    var n = 0;
    items.forEach(function (it) {
      (it.assets || []).forEach(function (a) {
        if (a.ready) {
          n += 1;
        }
      });
    });
    return n;
  }

  function poll() {
    fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) {
          window.setTimeout(poll, POLL_MS);
          return;
        }
        var before = currentItemStatuses();
        var itemChanged = (data.items || []).some(function (it) {
          return String(before[it.id]) !== String(it.status);
        });
        // Reload when an item's status changed, when more images became ready, or
        // when the run just finished (so the header + selection controls appear).
        if (itemChanged || readyCount(data.items || []) !== shownReady || !data.pending) {
          window.location.reload();
          return;
        }
        window.setTimeout(poll, POLL_MS);
      })
      .catch(function () {
        window.setTimeout(poll, POLL_MS);
      });
  }

  window.setTimeout(poll, POLL_MS);
})();
