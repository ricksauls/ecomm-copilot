// Client-side search for managed-portfolio lists.
//
// Powers two surfaces with identical markup contracts:
//   • the Portfolio Management table (rows are <tr data-portfolio-row>)
//   • the "Choose … managed items" pick-lists on the Content Scoring, Product
//     Copy, and Image Set intakes (rows are <label data-portfolio-row>, with
//     checkboxes for multi-select or radios for single-select)
//
// A single unified search box matches across brand, item #, product title, and
// URL at once (space-separated terms must ALL match somewhere). Loaded as an
// external file because the app's CSP is script-src 'self'. Pure progressive
// enhancement: with JS off, every row is visible and selectable.
//
// Contract within each [data-portfolio-filter] container:
//   [data-filter-search] — the search input
//   [data-portfolio-row] — a row carrying data-brand / data-item / data-title /
//                          data-url (all lowercased server-side)
//   [data-filter-count]  — optional; its text is set to "(<visible>)"
//   .pf-empty-filtered   — optional; shown when no row matches
(function () {
  "use strict";

  function wire(container) {
    var searchInput = container.querySelector("[data-filter-search]");
    var rows = Array.prototype.slice.call(
      container.querySelectorAll("[data-portfolio-row]")
    );
    var countEl = container.querySelector("[data-filter-count]");
    var emptyEl = container.querySelector(".pf-empty-filtered");

    // Nothing to search (empty portfolio) — leave the static markup as-is.
    if (!rows.length || !searchInput) {
      return;
    }

    // Precompute each row's searchable haystack once (brand + item # + title +
    // URL + tracking keywords). data-keywords is present only on tracked rows (on
    // the Portfolio list); the pick-lists have no such attribute, so it no-ops there.
    var haystacks = rows.map(function (row) {
      return [
        row.getAttribute("data-brand") || "",
        row.getAttribute("data-item") || "",
        row.getAttribute("data-title") || "",
        row.getAttribute("data-url") || "",
        row.getAttribute("data-keywords") || "",
      ].join(" ");
    });

    function apply() {
      var terms = searchInput.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      var visible = 0;

      rows.forEach(function (row, i) {
        // Every term must appear somewhere in the row's fields (AND match).
        var hay = haystacks[i];
        var show = terms.every(function (t) { return hay.indexOf(t) !== -1; });
        row.hidden = !show;
        if (show) {
          visible += 1;
        }
      });

      if (countEl) {
        countEl.textContent = "(" + visible + ")";
      }
      if (emptyEl) {
        emptyEl.hidden = visible !== 0;
      }
    }

    searchInput.addEventListener("input", apply);
    apply();
  }

  var containers = document.querySelectorAll("[data-portfolio-filter]");
  Array.prototype.forEach.call(containers, wire);
})();

// Preserve scroll position across an in-list action (track toggle / Remove).
//
// Those controls are plain POST forms that redirect back to this page, so the
// browser loads a fresh document scrolled to the top — losing the user's place
// in a long list. We stash the scroll offset just before the form navigates
// away and re-apply it on the next load, then clear it so an ordinary visit
// still starts at the top.
//
// The scrolling element is `.pane` (the shell hides body overflow), NOT the
// window — so window.scrollY is always 0 here and we must read/write
// pane.scrollTop. Loaded only on the list tab (where portfolio.js is included),
// so it never runs on the Upload tab. Pure enhancement: with JS off the page
// just behaves as before (jumps to top).
(function () {
  "use strict";

  var KEY = "pf-pane-scroll";
  var pane = document.querySelector(".pane");
  if (!pane) {
    return;
  }

  // Restore first (this script runs at end of body, so layout is ready). A
  // missing/blocked sessionStorage simply means no restore — never a hard fail.
  try {
    var saved = sessionStorage.getItem(KEY);
    if (saved !== null) {
      sessionStorage.removeItem(KEY);
      pane.scrollTop = parseInt(saved, 10) || 0;
    }
  } catch (e) {
    /* private mode / storage disabled — skip restore */
  }

  // Capture phase so we record the offset before the submit navigates away.
  // Scoped to forms inside the managed-items container (the track + delete
  // forms); the search box doesn't submit, so it never triggers this.
  document.addEventListener(
    "submit",
    function (ev) {
      var form = ev.target;
      if (form && form.closest && form.closest("[data-portfolio-filter]")) {
        try {
          sessionStorage.setItem(KEY, String(pane.scrollTop));
        } catch (e) {
          /* storage disabled — the reload will just land at the top */
        }
      }
    },
    true
  );
})();

// Auto-refresh while items are still being set up.
//
// A just-uploaded item is prepared by the background worker (PDP scrape + keyword
// pick); the row's title_status moves queued → fetching → done/error. The template
// emits <meta name="portfolio-refresh-seconds"> ONLY while at least one item is
// still in flight, so this reload loop exists just long enough to show setup
// finishing, then the meta is gone on the next load and the loop stops. Mirrors
// the pdp_results.js pattern; the <noscript> meta-refresh is the JS-off fallback.
//
// Before reloading we stash the pane scroll under the SAME key the scroll-restore
// block above uses, so the refresh lands the user back in place rather than at the
// top. We hold off a tick (re-arming, not cancelling) when the user is mid-task —
// a keyword edit form is open (a reload would discard unsaved edits) or they've
// typed a filter (a reload would clear it) — so the refresh never interrupts them.
(function () {
  "use strict";

  var meta = document.querySelector('meta[name="portfolio-refresh-seconds"]');
  if (!meta) {
    return;
  }
  var seconds = parseInt(meta.getAttribute("content"), 10) || 5;
  var SCROLL_KEY = "pf-pane-scroll";
  var pane = document.querySelector(".pane");

  // True when reloading now would destroy in-progress work the user can see.
  function userBusy() {
    // An open inline keyword editor (its form is un-hidden).
    var openEditor = document.querySelector("[data-pf-kw-form]:not([hidden])");
    if (openEditor) {
      return true;
    }
    // A non-empty search filter (the reload would wipe the typed query).
    var search = document.querySelector("[data-filter-search]");
    return !!(search && search.value.trim());
  }

  function tick() {
    if (userBusy()) {
      // Try again later instead of reloading out from under the user.
      window.setTimeout(tick, seconds * 1000);
      return;
    }
    if (pane) {
      try {
        sessionStorage.setItem(SCROLL_KEY, String(pane.scrollTop));
      } catch (e) {
        /* storage disabled — the reload will just land at the top */
      }
    }
    window.location.reload();
  }

  window.setTimeout(tick, seconds * 1000);
})();

// Inline keyword editor on the Portfolio list. Each tracked row's Tracking cell
// has a read view (chips + "Edit keywords") and a hidden edit form. "Edit"
// reveals the form and hides the chips; "Cancel" reverts unsaved changes and
// collapses; submitting posts to the shared edit route (return_to=portfolio) and
// the server redirects back to the list. Event-delegated so it covers every row
// with one listener (CSP-safe, no inline handlers). Pure enhancement: with JS off
// the chips stay visible and the edit form stays in the DOM, so editing still
// works without the reveal/collapse.
(function () {
  "use strict";

  document.addEventListener("click", function (ev) {
    var target = ev.target;
    if (!target || !target.closest) {
      return;
    }
    var editBtn = target.closest("[data-pf-kw-edit]");
    var cancelBtn = target.closest("[data-pf-kw-cancel]");
    if (!editBtn && !cancelBtn) {
      return;
    }

    var container = (editBtn || cancelBtn).closest("[data-pf-kw]");
    if (!container) {
      return;
    }
    var view = container.querySelector("[data-pf-kw-view]");
    var form = container.querySelector("[data-pf-kw-form]");
    if (!view || !form) {
      return;
    }

    if (editBtn) {
      view.hidden = true;
      form.hidden = false;
      var first = form.querySelector("input[type=text]");
      if (first) {
        first.focus();
      }
    } else {
      // Cancel — discard any unsaved edits (reset to the server-rendered values)
      // and return to the chip view.
      form.reset();
      form.hidden = true;
      view.hidden = false;
    }
  });
})();
