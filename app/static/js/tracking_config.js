// Setup-screen enhancements for Competitive Tracking (tracking_config.html).
//
// All CSP-safe (external file, no inline handlers) and progressive — with JS off
// the forms still submit and validate server-side. Four behaviours:
//
//   1. Keyword-reuse panel — when the picked portfolio item is already tracked,
//      reveal its keywords and the "use the same keywords" checkbox.
//   2. Keyword chips — the Keywords input is the source of truth; its comma-
//      separated terms are mirrored as removable chips above it (× drops a term).
//      Ticking the reuse checkbox appends the picked item's keywords; unticking
//      removes them. The user edits, then commits with "Use These Keywords".
//   3. Draft persistence — every other action on this screen POSTs and reloads,
//      which would wipe an uncommitted keyword draft. We stash the input in
//      sessionStorage before such a reload and restore it after (same idea as
//      portfolio.js's scroll-restore), clearing it once the keywords are
//      committed. Keyed per group so drafts don't bleed across categories.
//   4. Submit-button gating — "Use The Selected Item" stays disabled until an item
//      is picked, "Use These Competing Items" until at least one URL is entered.
(function () {
  "use strict";

  var picker = document.querySelector("[data-item-picker]");
  var panel = document.querySelector("[data-reuse-panel]");
  var copyBox = panel ? panel.querySelector("[data-reuse-copy]") : null;
  var kwInput = document.querySelector("[data-kw-input]");
  var chips = document.querySelector("[data-kw-staged]");
  var itemBtn = document.querySelector("[data-item-submit]");

  // ── keyword helpers: the input is a comma-separated list, the source of truth.
  function parseTerms(value) {
    return (value || "").split(",").map(function (t) { return t.trim(); }).filter(Boolean);
  }
  function inputTerms() {
    return parseTerms(kwInput ? kwInput.value : "");
  }
  function setTerms(terms) {
    if (kwInput) { kwInput.value = terms.join(", "); }
  }

  function renderChips() {
    if (!chips) { return; }
    var terms = inputTerms();
    chips.textContent = "";
    terms.forEach(function (term) {
      var chip = document.createElement("span");
      chip.className = "ci-chip";
      chip.appendChild(document.createTextNode(term + " "));
      var x = document.createElement("button");
      x.type = "button";
      x.setAttribute("aria-label", "Remove keyword " + term);
      x.setAttribute("data-kw-remove", term);
      x.textContent = "×";
      chip.appendChild(x);
      chips.appendChild(chip);
    });
    chips.hidden = terms.length === 0;
  }

  function addTerms(terms) {
    var current = inputTerms();
    var seen = current.map(function (t) { return t.toLowerCase(); });
    terms.forEach(function (term) {
      if (seen.indexOf(term.toLowerCase()) === -1) {
        current.push(term);
        seen.push(term.toLowerCase());
      }
    });
    setTerms(current);
    renderChips();
  }

  function removeTerms(terms) {
    var drop = terms.map(function (t) { return t.toLowerCase(); });
    setTerms(inputTerms().filter(function (t) { return drop.indexOf(t.toLowerCase()) === -1; }));
    renderChips();
  }

  function selectedKeywords() {
    if (!picker) { return []; }
    var opt = picker.options[picker.selectedIndex];
    return parseTerms(opt ? opt.getAttribute("data-keywords") || "" : "");
  }

  // ── 3: draft persistence across this screen's reloads. ──────────────────────
  var groupMatch = window.location.pathname.match(/\/groups\/(\d+)/);
  var draftKey = "ci-kw-draft:" + (groupMatch ? groupMatch[1] : "x");

  function saveDraft() {
    try { sessionStorage.setItem(draftKey, kwInput ? kwInput.value : ""); } catch (e) { /* no-op */ }
  }
  function clearDraft() {
    try { sessionStorage.removeItem(draftKey); } catch (e) { /* no-op */ }
  }
  function restoreDraft() {
    try {
      var saved = sessionStorage.getItem(draftKey);
      if (saved !== null && kwInput) {
        kwInput.value = saved;
        sessionStorage.removeItem(draftKey);
      }
    } catch (e) { /* private mode / storage disabled — just skip restore */ }
  }

  // Before any of this screen's POSTs reload the page, stash the draft — except
  // the keyword form itself, whose submit commits the draft (so we clear it).
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form || form.tagName !== "FORM") { return; }
    if (form.hasAttribute("data-kw-form")) {
      clearDraft();
    } else {
      saveDraft();
    }
  }, true);

  // ── 1 + item-button gating: react to the picker selection. ──────────────────
  function syncPicker() {
    var opt = picker ? picker.options[picker.selectedIndex] : null;
    if (itemBtn) { itemBtn.disabled = !(opt && opt.value); }

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
    // Reset the checkbox for the newly-selected item, but leave any staged
    // keywords in the input (switching items should not discard them).
    if (copyBox) { copyBox.checked = false; }
  }

  if (picker) {
    picker.addEventListener("change", syncPicker);
  }

  // ── 2: checkbox stages / unstages the selected item's keywords. ─────────────
  if (copyBox) {
    copyBox.addEventListener("change", function () {
      if (copyBox.checked) {
        addTerms(selectedKeywords());
      } else {
        removeTerms(selectedKeywords());
      }
    });
  }
  // Chips mirror the input; keep them in sync as the user types, and let × drop a term.
  if (kwInput) { kwInput.addEventListener("input", renderChips); }
  if (chips) {
    chips.addEventListener("click", function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest("[data-kw-remove]") : null;
      if (btn) { removeTerms([btn.getAttribute("data-kw-remove")]); }
    });
  }

  // Initial paint: restore any draft first, then derive chips + picker state.
  restoreDraft();
  renderChips();
  if (picker) { syncPicker(); }

  // ── 4: gate the competitors button on at least one non-empty URL. ───────────
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
