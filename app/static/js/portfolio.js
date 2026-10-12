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

    // Precompute each row's searchable haystack once (brand + item # + title + URL).
    var haystacks = rows.map(function (row) {
      return [
        row.getAttribute("data-brand") || "",
        row.getAttribute("data-item") || "",
        row.getAttribute("data-title") || "",
        row.getAttribute("data-url") || "",
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
