// Client-side filtering for managed-portfolio lists.
//
// Powers two surfaces with identical markup contracts:
//   • the Portfolio Management table (rows are <tr data-portfolio-row>)
//   • the "Choose from your managed items" pick-list on the Content Scoring and
//     Product Copy intakes (rows are <label data-portfolio-row>)
//
// Loaded as an external file because the app's CSP is script-src 'self' (inline
// scripts are blocked). Pure progressive enhancement: with JS off, every row is
// visible and selectable — this only narrows what's shown.
//
// Contract within each [data-portfolio-filter] container:
//   [data-filter-brand]  — a <select>; its value is a lowercased brand ("" = all)
//   [data-filter-search] — a text/search input matched against item # and URL
//   [data-portfolio-row] — a row carrying data-brand / data-item / data-url
//                          (all lowercased server-side)
//   [data-filter-count]  — optional; its text is set to "(<visible>)"
//   .pf-empty-filtered   — optional; shown when no row matches
(function () {
  "use strict";

  function wire(container) {
    var brandSel = container.querySelector("[data-filter-brand]");
    var searchInput = container.querySelector("[data-filter-search]");
    var rows = Array.prototype.slice.call(
      container.querySelectorAll("[data-portfolio-row]")
    );
    var countEl = container.querySelector("[data-filter-count]");
    var emptyEl = container.querySelector(".pf-empty-filtered");

    // Nothing to filter (empty portfolio) — leave the static markup as-is.
    if (!rows.length) {
      return;
    }

    function apply() {
      var brand = brandSel ? brandSel.value.trim().toLowerCase() : "";
      var term = searchInput ? searchInput.value.trim().toLowerCase() : "";
      var visible = 0;

      rows.forEach(function (row) {
        var rowBrand = row.getAttribute("data-brand") || "";
        var item = row.getAttribute("data-item") || "";
        var url = row.getAttribute("data-url") || "";
        // Brand is an exact match against the dropdown value; the free-text term
        // matches either the item number or the URL (substring).
        var brandOk = !brand || rowBrand === brand;
        var termOk = !term || item.indexOf(term) !== -1 || url.indexOf(term) !== -1;
        var show = brandOk && termOk;
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

    if (brandSel) {
      brandSel.addEventListener("change", apply);
    }
    if (searchInput) {
      searchInput.addEventListener("input", apply);
    }
    apply();
  }

  var containers = document.querySelectorAll("[data-portfolio-filter]");
  Array.prototype.forEach.call(containers, wire);
})();
