// Product Detail Page Content Scores — dense results table.
//
// Progressive enhancement over a server-rendered table: the page works with JS
// disabled (every row and its recommendations render, nothing is JS-gated), and
// this script layers on three things for the common hundreds-of-items case:
//   1. Row expand/collapse — recommendations are hidden by default and revealed
//      per row, so the list stays scannable.
//   2. Column-header sorting — click a header (or Enter/Space) to sort by that
//      column; click again to flip direction.
//   3. A live text filter over the product/item-number text.
//
// No network, nothing persisted across loads. Mirrors the sort/compare idioms in
// dashboard.js; thumbnail hover-enlarge is provided by dashboard.js itself.

(function () {
  "use strict";

  // Mark the page as JS-capable *first*, before paint settles, so the CSS rule
  // that collapses detail panels (.pdp-js .pdp-item:not(.pdp-open) .pdp-detail)
  // takes effect immediately rather than flashing every panel open.
  document.documentElement.classList.add("pdp-js");

  // ── Sorting ────────────────────────────────────────────────────────────────

  // A cell's sort key: an explicit data-sort (score, or -1 for unmeasured) wins,
  // else the visible text (product name, status label).
  function cellValue(cell) {
    if (!cell) {
      return "";
    }
    var explicit = cell.getAttribute("data-sort");
    return explicit !== null ? explicit : cell.textContent.trim();
  }

  // Numeric compare when both values are wholly numeric (scores), else a
  // case-insensitive string compare. Number() — not parseFloat() — so a value
  // like "#4471 Foo" isn't coerced to a leading-digit number.
  function compare(a, b) {
    var na = Number(a);
    var nb = Number(b);
    if (a !== "" && b !== "" && !isNaN(na) && !isNaN(nb)) {
      return na - nb;
    }
    return a.toLowerCase().localeCompare(b.toLowerCase());
  }

  // Reorder .pdp-item nodes (each wraps a .pdp-summary grid + optional detail) by
  // the given summary-column index. appendChild moves the existing node, so the
  // summary grid and its detail panel travel together.
  function sortItems(body, colIndex, ascending) {
    var items = Array.prototype.slice.call(body.querySelectorAll(".pdp-item"));
    items.sort(function (i1, i2) {
      var c1 = i1.querySelector(".pdp-summary").children[colIndex];
      var c2 = i2.querySelector(".pdp-summary").children[colIndex];
      var result = compare(cellValue(c1), cellValue(c2));
      return ascending ? result : -result;
    });
    items.forEach(function (item) {
      body.appendChild(item);
    });
  }

  function initSorting(body) {
    var headRow = body.querySelector(".pdp-head-row");
    if (!headRow) {
      return;
    }
    var headers = Array.prototype.slice.call(headRow.children);
    headers.forEach(function (header, index) {
      if (header.hasAttribute("data-nosort")) {
        return; // select / image / caret columns have nothing to sort on
      }
      header.classList.add("sortable");
      header.setAttribute("role", "button");
      header.setAttribute("tabindex", "0");
      var ascending = true;

      function run() {
        headers.forEach(function (h) {
          h.removeAttribute("data-dir");
        });
        header.setAttribute("data-dir", ascending ? "asc" : "desc");
        sortItems(body, index, ascending);
        ascending = !ascending;
      }

      header.addEventListener("click", run);
      header.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          run();
        }
      });
    });
  }

  // ── Row expand / collapse ────────────────────────────────────────────────────

  // Toggle a row's detail panel. Ignore clicks that land on the row's own
  // interactive controls (the select checkbox/label and the product link) so
  // those keep their normal behaviour instead of also expanding the row.
  function initExpand(body) {
    body.addEventListener("click", function (event) {
      var item = event.target.closest(".pdp-item.has-detail");
      if (!item) {
        return;
      }
      if (event.target.closest("a, input, label")) {
        return;
      }
      item.classList.toggle("pdp-open");
      refreshExpandAll(body);
    });
    // Keyboard: Enter/Space on a focused caret toggles its row.
    body.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" && event.key !== " ") {
        return;
      }
      var caret = event.target.closest(".pdp-caret");
      if (!caret) {
        return;
      }
      event.preventDefault();
      var item = caret.closest(".pdp-item.has-detail");
      if (item) {
        item.classList.toggle("pdp-open");
        refreshExpandAll(body);
      }
    });
  }

  // ── Filter ───────────────────────────────────────────────────────────────────

  // Hide rows whose data-search text (lowercased product name + item number +
  // URL) doesn't contain the query. Empty query shows everything.
  function initFilter(body) {
    var input = document.querySelector(".pdp-filter");
    if (!input) {
      return;
    }
    var items = Array.prototype.slice.call(body.querySelectorAll(".pdp-item"));
    input.addEventListener("input", function () {
      var query = input.value.trim().toLowerCase();
      items.forEach(function (item) {
        var hay = item.getAttribute("data-search") || "";
        item.classList.toggle("pdp-hidden", query !== "" && hay.indexOf(query) === -1);
      });
      // Bulk controls act on visible rows, so re-sync them to the filtered set.
      refreshSelectAll(body);
      refreshExpandAll(body);
    });
  }

  // ── Bulk actions: select-all + expand/collapse-all ───────────────────────────

  // The rows a bulk action touches: everything the filter hasn't hidden.
  function visibleItems(body) {
    return Array.prototype.slice.call(body.querySelectorAll(".pdp-item:not(.pdp-hidden)"));
  }

  // Reflect the select-all box against the visible selectable rows: checked when
  // all are ticked, indeterminate when only some are, clear when none.
  function refreshSelectAll(body) {
    var master = document.querySelector(".pdp-select-all");
    if (!master) {
      return;
    }
    var boxes = visibleItems(body)
      .map(function (it) { return it.querySelector('input[name="item_ids"]'); })
      .filter(Boolean);
    var checked = boxes.filter(function (b) { return b.checked; }).length;
    master.checked = boxes.length > 0 && checked === boxes.length;
    master.indeterminate = checked > 0 && checked < boxes.length;
  }

  // Reflect the expand-all button's label/state against the visible expandable
  // rows (all open → "Collapse all", otherwise "Expand all").
  function refreshExpandAll(body) {
    var btn = document.querySelector(".pdp-expand-all");
    if (!btn) {
      return;
    }
    var rows = visibleItems(body).filter(function (it) {
      return it.classList.contains("has-detail");
    });
    var open = rows.filter(function (it) { return it.classList.contains("pdp-open"); }).length;
    var allOpen = rows.length > 0 && open === rows.length;
    btn.textContent = allOpen ? "Collapse All" : "Expand All";
    btn.setAttribute("aria-expanded", allOpen ? "true" : "false");
  }

  function initBulk(body) {
    var master = document.querySelector(".pdp-select-all");
    if (master) {
      // Header checkbox ticks/unticks every visible row's select box.
      master.addEventListener("change", function () {
        visibleItems(body).forEach(function (it) {
          var box = it.querySelector('input[name="item_ids"]');
          if (box) {
            box.checked = master.checked;
          }
        });
        master.indeterminate = false;
      });
      // A per-row box changing re-derives the header's checked/indeterminate state.
      body.addEventListener("change", function (event) {
        if (event.target.matches('input[name="item_ids"]')) {
          refreshSelectAll(body);
        }
      });
    }

    var btn = document.querySelector(".pdp-expand-all");
    if (btn) {
      // One click opens every visible expandable row; if all are already open it
      // collapses them instead.
      btn.addEventListener("click", function () {
        var rows = visibleItems(body).filter(function (it) {
          return it.classList.contains("has-detail");
        });
        var anyClosed = rows.some(function (it) { return !it.classList.contains("pdp-open"); });
        rows.forEach(function (it) { it.classList.toggle("pdp-open", anyClosed); });
        refreshExpandAll(body);
      });
    }
  }

  // Confirm before a bulk "Fix all images" submit. Each fix is a metered provider
  // call, so an accidental click shouldn't spend several at once. Progressive
  // enhancement only: without JS the form still submits and the server still
  // enforces auth + CSRF; this just guards the click.
  function initFixAllConfirm() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form.classList || !form.classList.contains("js-fixall")) {
        return;
      }
      var ok = window.confirm(
        "Fix every flagged image for this item? Each fix is a metered AI call."
      );
      if (!ok) {
        event.preventDefault();
      }
    });
  }

  // ── Wiring ──────────────────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", function () {
    // The fix-all confirm is wired even when the results table is absent (e.g. a
    // single-item view), since it listens on the document.
    initFixAllConfirm();
    var body = document.querySelector(".pdp-body");
    if (!body) {
      return;
    }
    initSorting(body);
    initExpand(body);
    initFilter(body);
    initBulk(body);
    refreshSelectAll(body);
    refreshExpandAll(body);
  });
})();
