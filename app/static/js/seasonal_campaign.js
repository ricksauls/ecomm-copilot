// Seasonal campaign detail — live updates.
//
// While any product is still acquiring its main image or any image is still
// generating, poll the status endpoint and reload once something changes, so the
// new seasonal results render from the server. The <noscript> meta-refresh is the
// no-JS fallback. CSP is script-src 'self' — external file only.

(function () {
  "use strict";

  var grid = document.querySelector(".seasonal-grid");
  if (!grid) {
    return;
  }

  // --- Polling (only while work is pending) --------------------------------

  if (grid.getAttribute("data-pending") !== "1") {
    return;
  }
  var url = grid.getAttribute("data-status-url");
  if (!url) {
    return;
  }

  var POLL_MS = 3000;

  // A compact fingerprint of what's on screen: per-product status + each image's
  // generation status. A reload happens as soon as the server's fingerprint differs
  // (a product resolved, an image became ready/failed, etc.) or all work is done.
  function fingerprint(products) {
    return (products || []).map(function (p) {
      var imgs = (p.images || []).map(function (im) { return im.id + ":" + im.gen_status; });
      return p.id + "=" + p.status + "[" + imgs.join(",") + "]";
    }).join("|");
  }

  function domFingerprint() {
    var parts = [];
    grid.querySelectorAll(".import-item").forEach(function (el) {
      var imgs = [];
      el.querySelectorAll(".seasonal-thumb").forEach(function (t) {
        imgs.push(t.getAttribute("data-image-id") + ":" + t.getAttribute("data-gen"));
      });
      parts.push(el.getAttribute("data-product-id") + "=" + el.getAttribute("data-status") + "[" + imgs.join(",") + "]");
    });
    return parts.join("|");
  }

  var shown = domFingerprint();

  function poll() {
    fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) {
          window.setTimeout(poll, POLL_MS);
          return;
        }
        if (fingerprint(data.products) !== shown || !data.pending) {
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
