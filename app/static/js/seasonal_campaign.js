// Seasonal campaign detail — selection count + live updates.
//
// Two enhancements over a page that works without JS:
//   1. Selection: keep the "…selected (N)" count on the Generate button in sync
//      with the ticked source images. (The generate form is a plain POST, so it
//      already works with JS off — the browser submits the checked image_ids.)
//   2. Polling: while any product is still acquiring or any image is still
//      generating, poll the status endpoint and reload once something changes, so
//      new thumbnails and the seasonal results render from the server. The
//      <noscript> meta-refresh is the no-JS fallback.
// CSP is script-src 'self' — external file only.

(function () {
  "use strict";

  var grid = document.querySelector(".seasonal-grid");
  if (!grid) {
    return;
  }

  // --- Selection count -----------------------------------------------------

  var countEl = document.querySelector(".seasonal-sel-count");

  function checkboxes() {
    // Every image_ids checkbox on the page belongs to the one generate form
    // (associated via its form="seasonal-generate" attribute).
    return Array.prototype.slice.call(document.querySelectorAll('input[name="image_ids"]'));
  }

  function refreshCount() {
    if (!countEl) {
      return;
    }
    var checked = checkboxes().filter(function (b) { return b.checked; });
    countEl.textContent = String(checked.length);
  }

  document.addEventListener("change", function (event) {
    if (event.target && event.target.name === "image_ids") {
      refreshCount();
    }
  });
  refreshCount();

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
