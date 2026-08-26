// Dashboard / View All activity tables: click a header to sort, cap capped
// tables at 10 visible rows (rest scroll), and show an enlarged preview when a
// row thumbnail is hovered.
//
// Progressive enhancement only. The server renders every row, newest-first, so
// the tables work with JS disabled; this reorders rows already in the DOM,
// constrains scroll height, and adds the hover preview. No network, nothing
// persisted across loads.

(function () {
  "use strict";

  var MAX_VISIBLE_ROWS = 10;
  var PREVIEW_SIZE = 240; // px; the enlarged thumbnail box
  var GAP = 12;

  // ── Sorting ────────────────────────────────────────────────────────────────

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
  // else a case-insensitive string compare (brands, titles, ISO dates sort
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
    rows.forEach(function (row) {
      body.appendChild(row); // appendChild moves the existing node
    });
  }

  // ── Row cap ─────────────────────────────────────────────────────────────────

  // Cap a capped body's height to the header + first 10 rows so an 11th row onward
  // is reached by scrolling. Only bodies marked .dash-body-capped (the dashboard,
  // not the View All screen) are capped; either way a table of <= 10 rows shows
  // them all. Measured once at load; row heights are uniform within a table, so
  // it stays right after a sort reorders them.
  function capHeight(body, headRow) {
    if (!body.classList.contains("dash-body-capped")) {
      return;
    }
    var rows = body.querySelectorAll(".dash-row");
    if (rows.length <= MAX_VISIBLE_ROWS) {
      return;
    }
    var height = headRow ? headRow.offsetHeight : 0;
    for (var i = 0; i < MAX_VISIBLE_ROWS; i++) {
      height += rows[i].offsetHeight;
    }
    body.style.maxHeight = height + "px";
  }

  function initTable(body) {
    var headRow = body.querySelector(".dash-head-row");
    if (!headRow) {
      return;
    }
    var headers = Array.prototype.slice.call(headRow.children);
    headers.forEach(function (header, index) {
      if (header.hasAttribute("data-nosort")) {
        return; // e.g. the Image column has nothing meaningful to sort on
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

  // ── Image hover preview ─────────────────────────────────────────────────────

  var preview = null;

  function ensurePreview() {
    if (!preview) {
      preview = document.createElement("img");
      preview.className = "dash-thumb-preview";
      preview.setAttribute("alt", "");
      document.body.appendChild(preview);
    }
    return preview;
  }

  // Show the enlarged image floating next to its thumbnail. Fixed-positioned on
  // <body> so it escapes the tables' overflow clipping; placed to the right, or
  // flipped left when the right edge is short, and clamped into the viewport.
  function showPreview(img) {
    var p = ensurePreview();
    p.src = img.currentSrc || img.src;
    var rect = img.getBoundingClientRect();
    var left = rect.right + GAP;
    if (left + PREVIEW_SIZE > window.innerWidth) {
      left = rect.left - GAP - PREVIEW_SIZE;
    }
    if (left < GAP) {
      left = GAP;
    }
    var top = rect.top + rect.height / 2 - PREVIEW_SIZE / 2;
    top = Math.max(GAP, Math.min(top, window.innerHeight - PREVIEW_SIZE - GAP));
    p.style.left = left + "px";
    p.style.top = top + "px";
    p.classList.add("show");
  }

  function hidePreview() {
    if (preview) {
      preview.classList.remove("show");
    }
  }

  // ── Wiring ──────────────────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", function () {
    var bodies = document.querySelectorAll(".dash-body");
    Array.prototype.forEach.call(bodies, initTable);
  });

  // Only real thumbnails (an <img>) enlarge; placeholder tiles are <div>s.
  document.addEventListener("mouseover", function (event) {
    var img = event.target.closest && event.target.closest("img.dash-thumb");
    if (img) {
      showPreview(img);
    }
  });
  document.addEventListener("mouseout", function (event) {
    var img = event.target.closest && event.target.closest("img.dash-thumb");
    if (img) {
      hidePreview();
    }
  });
  window.addEventListener("scroll", hidePreview, true); // hide if the page scrolls under it

  // "View all" lives inside a <summary>, so a plain click would also toggle the
  // <details>. Intercept a plain left-click and navigate instead; modifier/middle
  // clicks fall through so "open in new tab" still works.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest(".dash-viewall");
    if (link && event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
      event.preventDefault();
      window.location.href = link.href;
    }
  });
})();
