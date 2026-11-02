// Setup-screen enhancements for Competitive Tracking (tracking_config.html).
//
// Three independent behaviours, all CSP-safe (external file, no inline handlers)
// and progressive — with JS off the forms still submit and validate server-side:
//
//   1. Keyword-reuse panel — when the picked portfolio item is already tracked,
//      reveal its keywords and the "use the same keywords" checkbox.
//   2. Keyword staging — ticking that checkbox copies the item's keywords into the
//      Keywords section's input (comma-separated) and shows them as removable
//      chips above it; the user edits, then commits with "Use These Keywords".
//   3. Submit-button gating — "Use The Selected Item" stays disabled until an item
//      is picked, "Use These Competing Items" until at least one URL is entered.
(function () {
  "use strict";

  var picker = document.querySelector("[data-item-picker]");
  var panel = document.querySelector("[data-reuse-panel]");
  var copyBox = panel ? panel.querySelector("[data-reuse-copy]") : null;
  var kwInput = document.querySelector("[data-kw-input]");
  var staged = document.querySelector("[data-kw-staged]");
  var itemBtn = document.querySelector("[data-item-submit]");

  // The Keywords input is a comma-separated list; treat it as the source of truth
  // and keep the staged chips in sync with it.
  function parseTerms(value) {
    return (value || "").split(",").map(function (t) { return t.trim(); }).filter(Boolean);
  }
  function setInputTerms(terms) {
    if (kwInput) { kwInput.value = terms.join(", "); }
  }

  // Terms added by the reuse checkbox, so uncheck / × can remove exactly those.
  var stagedTerms = [];

  function renderStaged() {
    if (!staged) { return; }
    staged.textContent = "";
    stagedTerms.forEach(function (term) {
      var chip = document.createElement("span");
      chip.className = "ci-chip";
      chip.appendChild(document.createTextNode(term + " "));
      var x = document.createElement("button");
      x.type = "button";
      x.setAttribute("aria-label", "Remove keyword " + term);
      x.setAttribute("data-kw-remove", term);
      x.textContent = "×";
      chip.appendChild(x);
      staged.appendChild(chip);
    });
    staged.hidden = stagedTerms.length === 0;
  }

  function lowers(list) {
    return list.map(function (t) { return t.toLowerCase(); });
  }

  function addStaged(terms) {
    var current = parseTerms(kwInput ? kwInput.value : "");
    var seen = lowers(current);
    var stagedLower = lowers(stagedTerms);
    terms.forEach(function (term) {
      if (seen.indexOf(term.toLowerCase()) === -1) {
        current.push(term);
        seen.push(term.toLowerCase());
      }
      if (stagedLower.indexOf(term.toLowerCase()) === -1) {
        stagedTerms.push(term);
        stagedLower.push(term.toLowerCase());
      }
    });
    setInputTerms(current);
    renderStaged();
  }

  function removeTerm(term) {
    var keep = function (t) { return t.toLowerCase() !== term.toLowerCase(); };
    setInputTerms(parseTerms(kwInput ? kwInput.value : "").filter(keep));
    stagedTerms = stagedTerms.filter(keep);
    renderStaged();
  }

  function clearStaged() {
    stagedTerms.slice().forEach(removeTerm);
  }

  function selectedKeywords() {
    if (!picker) { return []; }
    var opt = picker.options[picker.selectedIndex];
    var raw = opt ? opt.getAttribute("data-keywords") || "" : "";
    return raw.split(",").map(function (t) { return t.trim(); }).filter(Boolean);
  }

  // 1 + item-button gating: react to the picker selection.
  function syncPicker() {
    var opt = picker ? picker.options[picker.selectedIndex] : null;
    var hasValue = !!(opt && opt.value);
    if (itemBtn) { itemBtn.disabled = !hasValue; }

    if (panel) {
      var tracked = opt && opt.getAttribute("data-tracked") === "1";
      var keywords = opt ? opt.getAttribute("data-keywords") || "" : "";
      var kwEl = panel.querySelector("[data-reuse-keywords]");
      if (tracked && keywords) {
        if (kwEl) { kwEl.textContent = keywords; }
        panel.hidden = false;
      } else {
        panel.hidden = true;
      }
    }
    // Changing the item resets any staging from a previous pick.
    if (copyBox) { copyBox.checked = false; }
    clearStaged();
  }

  if (picker) {
    picker.addEventListener("change", syncPicker);
    syncPicker();
  }

  // 2: checkbox stages / unstages the selected item's keywords.
  if (copyBox) {
    copyBox.addEventListener("change", function () {
      if (copyBox.checked) {
        addStaged(selectedKeywords());
      } else {
        clearStaged();
      }
    });
  }

  // × on a staged chip removes that term (event-delegated).
  if (staged) {
    staged.addEventListener("click", function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest("[data-kw-remove]") : null;
      if (!btn) { return; }
      removeTerm(btn.getAttribute("data-kw-remove"));
      // If every staged term is gone, drop the "use the same keywords" tick too.
      if (stagedTerms.length === 0 && copyBox) { copyBox.checked = false; }
    });
  }

  // 3: gate the competitors button on at least one non-empty URL.
  var compForm = document.querySelector("[data-comp-form]");
  var compBtn = document.querySelector("[data-comp-submit]");
  if (compForm && compBtn) {
    var urlInputs = Array.prototype.slice.call(
      compForm.querySelectorAll('input[name="urls"]')
    );
    var syncComp = function () {
      compBtn.disabled = !urlInputs.some(function (i) { return i.value.trim(); });
    };
    urlInputs.forEach(function (i) { i.addEventListener("input", syncComp); });
    syncComp();
  }
})();
