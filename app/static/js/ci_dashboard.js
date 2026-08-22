/* Competitive Intelligence dashboard — client-side brand filter for the
 * Search Ranking table. Progressive enhancement: the full table renders
 * server-side; this only hides rows whose brand doesn't match the selection.
 * External file (CSP forbids inline scripts).
 */
(function () {
  "use strict";

  var select = document.getElementById("rank-brand-filter");
  var table = document.getElementById("rank-table");
  if (!select || !table) return;

  select.addEventListener("change", function () {
    var want = select.value;
    table.querySelectorAll("tbody tr").forEach(function (tr) {
      var brand = tr.getAttribute("data-brand");
      tr.style.display = !want || brand === want ? "" : "none";
    });
  });
})();
