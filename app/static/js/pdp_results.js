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

  // ── Auto-refresh (JS-controlled, pausable) ───────────────────────────────────
  //
  // The page auto-refreshes every few seconds while scoring/fixes run. With JS we
  // drive that reload ourselves (from the marker meta the template emits) instead
  // of a hard <meta http-equiv="refresh">, so the cost modal can PAUSE it — a
  // metered-spend confirmation must not vanish from under the user mid-decision.
  // Without JS, the template's <noscript> meta-refresh is the fallback.
  var autoRefresh = null;

  function createAutoRefresh() {
    var meta = document.querySelector('meta[name="pdp-refresh-seconds"]');
    if (!meta) {
      return { pause: function () {}, resume: function () {} };
    }
    var seconds = parseInt(meta.getAttribute("content"), 10) || 5;
    var timer = null;
    var paused = false;
    function arm() {
      if (paused || timer !== null) {
        return;
      }
      timer = window.setTimeout(function () {
        window.location.reload();
      }, seconds * 1000);
    }
    function disarm() {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    }
    arm();
    return {
      pause: function () { paused = true; disarm(); },
      resume: function () { paused = false; arm(); },
    };
  }

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

  // Which rows are open is remembered in sessionStorage (per tab) so it survives a
  // reload — the page auto-refreshes every 5s while a fix is running, and a fix
  // action POSTs then redirects back, so without this the open row would snap shut
  // under the user mid-task. Self-pruning: we only ever store ids present in the DOM.
  var OPEN_KEY = "pdp-open-rows";

  function persistOpen(body) {
    try {
      var ids = Array.prototype.slice
        .call(body.querySelectorAll(".pdp-item.pdp-open[data-id]"))
        .map(function (it) { return it.getAttribute("data-id"); });
      window.sessionStorage.setItem(OPEN_KEY, JSON.stringify(ids));
    } catch (e) {
      // Storage unavailable (e.g. private mode) — expand still works, just not sticky.
    }
  }

  function restoreOpen(body) {
    var ids;
    try {
      ids = JSON.parse(window.sessionStorage.getItem(OPEN_KEY) || "[]");
    } catch (e) {
      return;
    }
    if (!Array.isArray(ids) || ids.length === 0) {
      return;
    }
    var open = {};
    ids.forEach(function (id) { open[id] = true; });
    Array.prototype.slice
      .call(body.querySelectorAll(".pdp-item.has-detail[data-id]"))
      .forEach(function (it) {
        if (open[it.getAttribute("data-id")]) {
          it.classList.add("pdp-open");
        }
      });
  }

  // Toggle a row's detail panel. Ignore clicks that land on the row's own
  // interactive controls (the select checkbox/label and the product link) so
  // those keep their normal behaviour instead of also expanding the row.
  function initExpand(body) {
    body.addEventListener("click", function (event) {
      var item = event.target.closest(".pdp-item.has-detail");
      if (!item) {
        return;
      }
      if (event.target.closest("a, input, label, button")) {
        return;
      }
      item.classList.toggle("pdp-open");
      persistOpen(body);
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
        persistOpen(body);
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
      refreshNeedsSelection();
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
        refreshNeedsSelection();
      });
      // A per-row box changing re-derives the header's checked/indeterminate state.
      body.addEventListener("change", function (event) {
        if (event.target.matches('input[name="item_ids"]')) {
          refreshSelectAll(body);
          refreshNeedsSelection();
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
        persistOpen(body);
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

  // ── Cost-preflight modal (reusable) ──────────────────────────────────────────
  //
  // A generic confirm-with-estimate dialog for metered actions. A trigger button
  // carries data-scope / data-estimate-url / data-action-url / data-title. On
  // click we POST the current scope to the estimate URL, show the returned counts
  // and dollar figure, and on confirm build+submit a POST to the action URL. Kept
  // action-agnostic so other metered features can reuse the same modal later.

  // The ticked item ids (used by the "selected" scope and to enable its button).
  function selectedItemIds() {
    return Array.prototype.slice
      .call(document.querySelectorAll('input[name="item_ids"]:checked'))
      .map(function (b) { return b.value; });
  }

  // "Selected"-scope buttons (image fixes + copy rewrites) are inert until at
  // least one row is ticked.
  function refreshNeedsSelection() {
    var disabled = selectedItemIds().length === 0;
    Array.prototype.slice
      .call(document.querySelectorAll(".js-needs-selection"))
      .forEach(function (btn) { btn.disabled = disabled; });
  }

  // "View Copy Results": scope the view to the ticked rows when any are selected
  // (the route then shows the latest copy per selected product); with nothing
  // ticked, the plain href views the whole batch.
  function initViewCopy() {
    var link = document.querySelector(".js-view-copy");
    if (!link) {
      return;
    }
    link.addEventListener("click", function (event) {
      var ids = selectedItemIds();
      if (ids.length === 0) {
        return; // no selection → follow the default href (whole batch)
      }
      event.preventDefault();
      var base = link.getAttribute("href");
      var sep = base.indexOf("?") === -1 ? "?" : "&";
      var qs = ids
        .map(function (id) { return "item_ids=" + encodeURIComponent(id); })
        .join("&");
      window.location.href = base + sep + qs;
    });
  }

  function csrfToken() {
    var el = document.getElementById("cost-modal-csrf");
    return el ? el.value : "";
  }

  function initCostModal() {
    var modal = document.getElementById("cost-modal");
    if (!modal) {
      return; // feature off — no modal rendered
    }
    var loading = modal.querySelector(".cost-modal-loading");
    var detail = modal.querySelector(".cost-modal-detail");
    var summaryEl = modal.querySelector(".cost-modal-summary");
    var skippedEl = modal.querySelector(".cost-modal-skipped");
    var costEl = modal.querySelector(".cost-modal-cost");
    var noteEl = modal.querySelector(".cost-modal-note");
    var emptyEl = modal.querySelector(".cost-modal-empty");
    var errorEl = modal.querySelector(".cost-modal-error");
    var titleEl = modal.querySelector(".cost-modal-title");
    var confirmBtn = modal.querySelector(".cost-modal-confirm");
    var confirmDefault = confirmBtn.textContent;

    // The action the modal will run if confirmed, captured when it opens.
    var pending = null;

    function setState(which) {
      loading.hidden = which !== "loading";
      detail.hidden = which !== "detail";
      emptyEl.hidden = which !== "empty";
      errorEl.hidden = which !== "error";
      confirmBtn.disabled = which !== "detail";
    }

    function open() {
      modal.hidden = false;
      document.body.classList.add("cost-modal-open");
      // Freeze the page's auto-refresh so the estimate doesn't vanish mid-read.
      if (autoRefresh) {
        autoRefresh.pause();
      }
    }
    function close() {
      modal.hidden = true;
      document.body.classList.remove("cost-modal-open");
      pending = null;
      if (autoRefresh) {
        autoRefresh.resume();
      }
    }

    // Append scope fields (all=1 or one item_ids input per id) to a form/params.
    function applyScope(add, scope, ids) {
      if (scope === "all") {
        add("all", "1");
      } else {
        ids.forEach(function (id) { add("item_ids", id); });
      }
    }

    function fetchEstimate(trigger) {
      var scope = trigger.getAttribute("data-scope");
      var ids = selectedItemIds();
      if (scope === "selected" && ids.length === 0) {
        return; // nothing ticked — button should be disabled anyway
      }
      pending = { actionUrl: trigger.getAttribute("data-action-url"), scope: scope, ids: ids };
      titleEl.textContent = trigger.getAttribute("data-title") || "Confirm";
      confirmBtn.textContent = trigger.getAttribute("data-confirm-label") || confirmDefault;
      setState("loading");
      open();

      var params = new URLSearchParams();
      params.append("csrf_token", csrfToken());
      applyScope(function (k, v) { params.append(k, v); }, scope, ids);

      fetch(trigger.getAttribute("data-estimate-url"), {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
        credentials: "same-origin",
      })
        .then(function (r) {
          if (!r.ok) { throw new Error("estimate failed"); }
          return r.json();
        })
        .then(function (est) {
          if (!est.total) {
            setState("empty");
            return;
          }
          // The server formats the wording for each action; the modal just renders
          // the strings, so it stays action-agnostic.
          summaryEl.textContent = est.summary || "";
          skippedEl.textContent = est.skipped || "";
          skippedEl.hidden = !est.skipped;
          costEl.textContent = est.cost || "";
          costEl.hidden = !est.cost;
          noteEl.textContent = est.note || "";
          setState("detail");
        })
        .catch(function () {
          setState("error");
        });
    }

    // Open on any cost-action trigger (buttons live in the batch bar).
    document.addEventListener("click", function (event) {
      var trigger = event.target.closest(".js-cost-action");
      if (!trigger || trigger.disabled) {
        return;
      }
      event.preventDefault();
      fetchEstimate(trigger);
    });

    // Confirm: build a real POST form for the captured action + scope and submit.
    confirmBtn.addEventListener("click", function () {
      if (!pending) {
        return;
      }
      var form = document.createElement("form");
      form.method = "post";
      form.action = pending.actionUrl;
      function add(name, value) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = name;
        input.value = value;
        form.appendChild(input);
      }
      add("csrf_token", csrfToken());
      applyScope(add, pending.scope, pending.ids);
      document.body.appendChild(form);
      form.submit();
    });

    // Cancel via either Cancel button or the backdrop.
    modal.addEventListener("click", function (event) {
      if (event.target.closest("[data-cost-cancel]")) {
        close();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !modal.hidden) {
        close();
      }
    });
  }

  // A plain confirm before cancelling queued fixes (no cost, but destructive).
  function initCancelConfirm() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form.classList || !form.classList.contains("js-cancel-queued")) {
        return;
      }
      if (!window.confirm("Cancel all queued image fixes? In-progress fixes will still finish.")) {
        event.preventDefault();
      }
    });
  }

  // ── Wiring ──────────────────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", function () {
    // Start the pausable auto-refresh first, so the cost modal can freeze it.
    autoRefresh = createAutoRefresh();
    // The fix-all confirm is wired even when the results table is absent (e.g. a
    // single-item view), since it listens on the document.
    initFixAllConfirm();
    initCostModal();
    initCancelConfirm();
    var body = document.querySelector(".pdp-body");
    if (!body) {
      return;
    }
    // Reopen rows the user had expanded before the last (auto or action) reload,
    // before wiring handlers, so the restored state is what everything reflects.
    restoreOpen(body);
    initSorting(body);
    initExpand(body);
    initFilter(body);
    initBulk(body);
    refreshSelectAll(body);
    refreshExpandAll(body);
    refreshNeedsSelection();
    initViewCopy();
  });
})();
