// PDP Image Set — Walmart prefill poller.
//
// On the "fetching product details…" state, poll the fetch-status endpoint and
// reload the page once the worker finishes the fetch, so the form comes back
// prefilled. Without JS the page still works — the user can refresh manually.
// CSP is script-src 'self' — external file only.

(function () {
  "use strict";

  var card = document.querySelector(".imageset-fetching");
  if (!card) {
    return;
  }
  var url = card.getAttribute("data-status-url");
  if (!url) {
    return;
  }

  var POLL_MS = 2500;

  function poll() {
    fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data && data.fetching === false) {
          window.location.reload();
          return;
        }
        window.setTimeout(poll, POLL_MS);
      })
      .catch(function () {
        window.setTimeout(poll, POLL_MS);
      });
  }

  window.setTimeout(poll, POLL_MS);
})();
