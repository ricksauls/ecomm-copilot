// PDP Image Set — intake form enhancements.
//
// Progressive enhancement over the server-rendered form (works with JS off):
//   1. Repeatable feature rows (name="feature_title"/"feature_desc"), matching the
//      server markup so a no-JS submission and a JS-added row post identically.
//   2. Filename readout for the product-photo drop zone.
// CSP is script-src 'self', so this must stay an external file (no inline JS).

(function () {
  "use strict";

  // --- Feature rows: removal only ------------------------------------------
  // Rows come pre-filled (from Walmart prefill or saved features); the user can
  // delete the ones they don't want. There is no "add another" — features are
  // generated, not hand-authored — so this only wires the per-row remove button.
  var rows = document.getElementById("feature-rows");
  if (rows) {
    rows.addEventListener("click", function (e) {
      var btn = e.target.closest(".row-remove");
      if (!btn) {
        return;
      }
      if (rows.querySelectorAll(".feature-row").length > 1) {
        btn.closest(".feature-row").remove();
      } else {
        // Last row: clear it rather than remove it, so the section never empties.
        btn.closest(".feature-row").querySelectorAll("input").forEach(function (i) {
          i.value = "";
        });
      }
    });
  }

  // --- Brand-color swatches ⇄ hex fields -----------------------------------
  // Each hex text field has a sibling <input type="color"> swatch. Editing one
  // updates the other, so the user always sees the actual color next to its code.
  // A blank hex field is left blank (means "auto-detect") even though the swatch
  // shows the slot default; only an explicit pick/typed value fills the field.
  var HEX_RE = /^#[0-9a-fA-F]{6}$/;
  document.querySelectorAll(".color-swatch").forEach(function (swatch) {
    var code = document.getElementById(swatch.getAttribute("data-target"));
    if (!code) {
      return;
    }
    // Seed the swatch from an existing (valid) code so they start in sync.
    if (HEX_RE.test(code.value.trim())) {
      swatch.value = code.value.trim().toLowerCase();
    }
    // Picking a color fills and normalizes the hex field.
    swatch.addEventListener("input", function () {
      code.value = swatch.value.toLowerCase();
    });
    // Typing a valid hex moves the swatch; partial/invalid input leaves it as-is.
    code.addEventListener("input", function () {
      var v = code.value.trim();
      if (HEX_RE.test(v)) {
        swatch.value = v.toLowerCase();
      }
    });
  });

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
