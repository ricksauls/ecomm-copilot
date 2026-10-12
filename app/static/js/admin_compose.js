/* Admin "Compose message" recipient controls (progressive enhancement).
 *
 * The form submits and validates entirely server-side; this only adds the
 * convenience layer on top of plain checkboxes: a "Select all" master toggle
 * (kept in sync when individual boxes change) and a live count of how many
 * recipients are currently selected. With JS off, the checkboxes and Send
 * button still work — the admin just picks each recipient by hand.
 */
(function () {
  "use strict";

  var form = document.querySelector("[data-compose-form]");
  if (!form) {
    return;
  }

  var selectAll = form.querySelector("[data-select-all]");
  var boxes = Array.prototype.slice.call(
    form.querySelectorAll("[data-recipient]")
  );
  var countEl = form.querySelector("[data-recipient-count]");
  if (!boxes.length) {
    return;
  }

  function selectedCount() {
    return boxes.filter(function (b) {
      return b.checked;
    }).length;
  }

  // Reflect the current selection in the count label and the master checkbox
  // (checked when all are on, indeterminate on a partial selection).
  function sync() {
    var n = selectedCount();
    if (countEl) {
      countEl.textContent = n + (n === 1 ? " recipient" : " recipients") + " selected";
    }
    if (selectAll) {
      selectAll.checked = n === boxes.length;
      selectAll.indeterminate = n > 0 && n < boxes.length;
    }
  }

  if (selectAll) {
    selectAll.addEventListener("change", function () {
      boxes.forEach(function (b) {
        b.checked = selectAll.checked;
      });
      sync();
    });
  }

  boxes.forEach(function (b) {
    b.addEventListener("change", sync);
  });

  sync();
})();
