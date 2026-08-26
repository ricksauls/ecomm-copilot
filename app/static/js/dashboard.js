// Dashboard activity tables: click a column header to sort, and cap each table
// at 10 visible rows with the rest reachable by scrolling.
//
// Progressive enhancement only. The server renders rows newest-first and shows
// every row, so the tables are fully usable with JS disabled; this just reorders
// the rows already in the DOM and constrains the scroll height. No network calls,
// and nothing is persisted across loads (a reload resets sort + scroll).

(function () {
  "use strict";

  var MAX_VISIBLE_ROWS = 10;

  // The value a cell sorts by: an explicit data-sort (e.g. the ISO date behind a
  // year-less "Aug 26" label) wins, else the visible text.
  function cellValue(cell) {
    if (!cell) {
      return "";
    }
    var explicit = cell.getAttribute("data-sort");
    return explicit !== null ? explicit : cell.textContent.trim();
  }

  // Numeric compare when both values are wholly numeric (scores, item counts),
  // else a case-insensitive string compare (brands, titles, ISO dates all sort
  // correctly as strings). Number() — not parseFloat() — is used for the test so
  // an ISO timestamp like "2026-08-12 10:00:00" is treated as text, not the
  // number 2026 (which parseFloat would return from the leading digits).
  function compare(a, b) {
    var na = Number(a);
    var nb = Number(b);
    if (a !== "" && b !== "" && !isNaN(na) && !isNaN(nb)) {
      return na - nb;
    }
    return a.toLowerCase().localeCompare(b.toLowerCase());
  }

  function sortBody(body, colIndex, ascending) {
    var rows = Array.prototype.slice.call(body.querySelectorAll(".dash-row"));
    rows.sort(function (r1, r2) {
      var result = compare(cellValue(r1.children[colIndex]), cellValue(r2.children[colIndex]));
      return ascending ? result : -result;
    });
    // Re-append in the new order; appendChild moves an existing node.
    rows.forEach(function (row) {
      body.appendChild(row);
    });
  }

  // Cap the body's height to the header + first 10 rows so an 11th row onward is
  // reached by scrolling. Skipped when a table has 10 or fewer rows (it just
  // shows them all). Measured once at load; row heights are uniform within a
  // table, so it stays right after a sort reorders them.
  function capHeight(body, headRow) {
    var rows = body.querySelectorAll(".dash-row");
    if (rows.length <= MAX_VISIBLE_ROWS) {
      return;
    }
    var height = headRow ? headRow.offsetHeight : 0;
    for (var i = 0; i < MAX_VISIBLE_ROWS; i++) {
      height += rows[i].offsetHeight;
    }
    body.style.maxHeight = height + "px";
    body.classList.add("dash-body-scroll");
  }

  function initTable(body) {
    var headRow = body.querySelector(".dash-head-row");
    if (!headRow) {
      return;
    }
    var headers = Array.prototype.slice.call(headRow.children);
    headers.forEach(function (header, index) {
      if (header.hasAttribute("data-nosort")) {
        return; // e.g. the Image column has nothing meaningful to sort on.
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
        sortBody(body, index, ascending);
        ascending = !ascending; // next click on the same header flips direction
      }

      header.addEventListener("click", run);
      header.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          run();
        }
      });
    });

    capHeight(body, headRow);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var bodies = document.querySelectorAll(".dash-body");
    Array.prototype.forEach.call(bodies, initTable);
  });
})();
