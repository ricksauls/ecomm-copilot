/* Nav-rail group persistence (progressive enhancement).
 *
 * The rail's collapsible groups are native <details> elements, so they expand
 * and collapse with no JavaScript. This script only adds *memory*: as the user
 * navigates between server-rendered pages within a tab, it restores which
 * groups they had open. The group holding the current page is always kept open
 * (the server marks it `open`) so the active item is never hidden.
 *
 * State lives in sessionStorage (per-tab, per-viewer). Every storage access is
 * guarded — private windows and blocked site-data make it throw or return null,
 * and the rail must still work when it does.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "rail:groups";
  var groups = document.querySelectorAll("details.rail-group[data-group]");
  if (!groups.length) {
    return;
  }

  function loadState() {
    try {
      return JSON.parse(sessionStorage.getItem(STORAGE_KEY)) || {};
    } catch (e) {
      return {};
    }
  }

  function saveState(state) {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {
      /* Storage unavailable (private mode, cleared site data) — remembering
         open groups is a convenience, so fail silently. */
    }
  }

  var state = loadState();

  groups.forEach(function (group) {
    var key = group.getAttribute("data-group");
    var holdsActive = !!group.querySelector(".nav-sub-item.active");

    // Keep the current section open; otherwise restore the remembered state.
    if (holdsActive) {
      group.open = true;
    } else if (Object.prototype.hasOwnProperty.call(state, key)) {
      group.open = !!state[key];
    }

    // Persist the user's manual expand/collapse for the rest of the tab session.
    group.addEventListener("toggle", function () {
      state[key] = group.open;
      saveState(state);
    });
  });
})();
