// PDP Image Set — intake form enhancements.
//
// Progressive enhancement over the server-rendered form (works with JS off):
//   1. Repeatable feature rows (name="feature_title"/"feature_desc"), matching the
//      server markup so a no-JS submission and a JS-added row post identically.
//   2. Filename readout for the product-photo drop zone.
// CSP is script-src 'self', so this must stay an external file (no inline JS).

(function () {
  "use strict";

  var MAX_FEATURES = 10;

  // --- Repeatable feature rows ---------------------------------------------
  var rows = document.getElementById("feature-rows");
  var addButton = document.getElementById("add-feature");

  function makeFeatureRow() {
    var row = document.createElement("div");
    row.className = "feature-row";

    var title = document.createElement("input");
    title.type = "text";
    title.name = "feature_title";
    title.className = "text-input feature-title";
    title.maxLength = 120;
    title.placeholder = "Feature";

    var desc = document.createElement("input");
    desc.type = "text";
    desc.name = "feature_desc";
    desc.className = "text-input feature-desc";
    desc.maxLength = 300;
    desc.placeholder = "Short detail (optional)";

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-remove";
    remove.setAttribute("aria-label", "Remove this feature");
    remove.innerHTML = "&times;";

    row.appendChild(title);
    row.appendChild(desc);
    row.appendChild(remove);
    return row;
  }

  if (rows && addButton) {
    addButton.addEventListener("click", function () {
      if (rows.querySelectorAll(".feature-row").length >= MAX_FEATURES) {
        return;
      }
      var row = makeFeatureRow();
      rows.appendChild(row);
      var input = row.querySelector(".feature-title");
      if (input) {
        input.focus();
      }
    });

    // Remove a row (always keep at least one so the section never empties).
    rows.addEventListener("click", function (e) {
      var btn = e.target.closest(".row-remove");
      if (!btn) {
        return;
      }
      if (rows.querySelectorAll(".feature-row").length > 1) {
        btn.closest(".feature-row").remove();
      } else {
        // Last row: clear it rather than remove it.
        btn.closest(".feature-row").querySelectorAll("input").forEach(function (i) {
          i.value = "";
        });
      }
    });
  }

  // --- Photo filename readout ----------------------------------------------
  var photo = document.getElementById("photo");
  var filename = document.getElementById("photo-filename");
  if (photo && filename) {
    photo.addEventListener("change", function () {
      if (photo.files && photo.files.length) {
        filename.textContent = photo.files[0].name;
        filename.hidden = false;
      } else {
        filename.hidden = true;
      }
    });
  }
})();
