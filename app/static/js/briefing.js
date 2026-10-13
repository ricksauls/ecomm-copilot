// Dashboard brand selector — CSP-safe auto-submit.
//
// The app's CSP is `script-src 'self'` (no inline scripts), so the selector cannot
// use an inline `onchange` handler. This external file attaches the listener
// instead. Progressive enhancement: the form ships a visible submit button that
// works with JS disabled; when this script runs it auto-submits on change and
// hides that button, so switching brands is one click either way.
(function () {
  "use strict";

  function wire() {
    var form = document.querySelector(".briefing-brand-form");
    if (!form) {
      return;
    }
    var select = form.querySelector(".briefing-select");
    if (select) {
      select.addEventListener("change", function () {
        form.submit();
      });
    }
    var go = form.querySelector(".briefing-go");
    if (go) {
      go.hidden = true; // JS now handles switching; the button is the no-JS fallback
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
