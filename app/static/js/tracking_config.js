// Keyword-reuse panel on the Competitive Tracking setup screen.
//
// When the user picks a product from the [data-item-picker] dropdown, an item
// that is already tracked in their Portfolio can lend its keywords to this
// category. On each change we read the selected <option>'s data-tracked flag and
// data-keywords; for a tracked item with keywords we reveal [data-reuse-panel]
// and show the list, otherwise we hide it and clear the "use the same keywords"
// checkbox so a later untracked pick can't submit a stale opt-in.
//
// Loaded as an external file because the app's CSP is script-src 'self'. Pure
// progressive enhancement: with JS off the dropdown and submit still work; only
// the keyword preview is unavailable (the checkbox then just defaults to off).
(function () {
  "use strict";

  var picker = document.querySelector("[data-item-picker]");
  var panel = document.querySelector("[data-reuse-panel]");
  if (!picker || !panel) {
    return;
  }
  var kwEl = panel.querySelector("[data-reuse-keywords]");
  var copyBox = panel.querySelector("[data-reuse-copy]");

  function sync() {
    var opt = picker.options[picker.selectedIndex];
    var tracked = opt && opt.getAttribute("data-tracked") === "1";
    var keywords = opt ? opt.getAttribute("data-keywords") || "" : "";
    if (tracked && keywords) {
      if (kwEl) {
        kwEl.textContent = keywords;
      }
      panel.hidden = false;
    } else {
      panel.hidden = true;
      if (copyBox) {
        copyBox.checked = false;
      }
    }
  }

  picker.addEventListener("change", sync);
  sync();
})();
