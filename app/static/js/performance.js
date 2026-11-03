/* Performance Insights — trend charts + CSP-safe controls.
 *
 * Three jobs, all without inline scripts (the app's CSP is `script-src 'self'`):
 *   1. Draw each [data-perf-trend] as a dependency-free inline-SVG line chart from
 *      its data-series JSON ({values, dates, unit}). unit "rank" inverts the y-axis
 *      (a lower position is a better rank, so it sits higher); "share" is a percent.
 *      The line is the app's near-black "mine" color (matching ci_charts.js).
 *   2. Auto-submit the Brand/Item form on change (changing brand resets Item to
 *      "all", since an item belongs to one brand); the Go button is the no-JS
 *      fallback and is hidden once this runs.
 *   3. Make a Tracked Products row (tr.row-drill[data-href]) navigate on click, so
 *      the whole row drills in — the product name is also a real link for no-JS.
 */
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  // Premium chart palette (matches the dashboard's in-card trend charts): a blue
  // line over a soft dashed grid, filling to a vertical gradient that fades to
  // transparent (see makeAreaGradient). Kept in sync with dashboard.css's
  // --ds-blue / --ds-border so Insights reads the same.
  var LINE = "#356dff";      // --ds-blue ("mine" series)
  var GRID = "#e2e8f0";      // --ds-border (drawn dashed below)
  var LABEL = "#94a3b8";     // --ds-faint

  // Per-chart counter so each SVG's area-gradient gets a unique id (multiple
  // trend charts can share one page via the tabbed views).
  var gradSeq = 0;

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function el(name, attrs) {
    var n = document.createElementNS(SVGNS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  // Short x-axis label. Trend dates are period labels already (e.g. "Aug 17–23",
  // "Q2 2026"); pass them through. An ISO date would be shortened to "Aug 24".
  function xLabel(d) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d || "");
    if (!m) return d || "";
    return MONTHS[parseInt(m[2], 10) - 1] + " " + parseInt(m[3], 10);
  }

  // Four evenly spaced ticks across [lo, hi], rounded for display.
  function ticks(lo, hi, isPercent) {
    var out = [], steps = 4;
    for (var i = 0; i <= steps; i++) {
      var v = lo + (hi - lo) * (i / steps);
      out.push(isPercent ? Math.round(v * 10) / 10 : Math.round(v));
    }
    // De-dup consecutive equal rounded ticks (tiny domains).
    return out.filter(function (v, i) { return i === 0 || v !== out[i - 1]; });
  }

  // Build a <defs> holding a vertical gradient that fades the area fill from a
  // soft blue at the line down to transparent at the axis — the same treatment
  // as the dashboard's .ds-area (dashboard.css ds-grad-a/ds-grad-b: 0.18 -> 0).
  // Returns the defs node to append and the paint url to reference it.
  function makeAreaGradient(color) {
    var id = "perf-grad-" + (++gradSeq);
    var defs = el("defs", {});
    var grad = el("linearGradient", { id: id, x1: "0", y1: "0", x2: "0", y2: "1" });
    grad.appendChild(el("stop", { offset: "0%", "stop-color": color, "stop-opacity": "0.18" }));
    grad.appendChild(el("stop", { offset: "100%", "stop-color": color, "stop-opacity": "0" }));
    defs.appendChild(grad);
    return { defs: defs, fill: "url(#" + id + ")" };
  }

  function drawTrend(container) {
    var data;
    try { data = JSON.parse(container.getAttribute("data-series") || "{}"); }
    catch (e) { return; }
    var vals = data.values || [], dates = data.dates || [];
    var isRank = data.unit === "rank", isPct = data.unit === "share";
    var isScore = data.unit === "score";  // content readiness, 0–100, higher better

    if (vals.length < 2) {
      container.innerHTML =
        '<p class="ci-section-hint">Not enough completed periods yet for a trend line.</p>';
      return;
    }

    var W = 760, H = 280, padL = 48, padR = 18, padT = 14, padB = 32;
    var plotW = W - padL - padR, plotH = H - padT - padB;

    // Domain: rank auto-scales around its min/max (padded); score is a fixed 0–100
    // scale; share runs 0..max+headroom.
    var lo, hi;
    var dmin = Math.min.apply(null, vals), dmax = Math.max.apply(null, vals);
    if (isRank) {
      lo = Math.max(1, Math.floor(dmin - 1));
      hi = Math.ceil(dmax + 1);
      if (hi - lo < 2) { hi = lo + 2; }
    } else if (isScore) {
      lo = 0;
      hi = 100;
    } else {
      lo = 0;
      hi = Math.max(1, Math.ceil(dmax * 1.25));
    }

    function x(i) { return padL + (i / (vals.length - 1)) * plotW; }
    function y(v) {
      var f = (v - lo) / (hi - lo);
      return isRank ? padT + f * plotH : padT + (1 - f) * plotH; // rank: low value -> top
    }

    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, "class": "perf-trend-svg",
      role: "img",
      "aria-label": (isRank ? "Search rank" : isScore ? "Content readiness" : "Share") + " trend" });

    // Gradient paint for the area fill (fades to transparent, like the dashboard).
    var area = makeAreaGradient(LINE);
    svg.appendChild(area.defs);

    // Gridlines + y labels.
    ticks(lo, hi, isPct).forEach(function (tv) {
      var yy = y(tv);
      svg.appendChild(el("line", { x1: padL, y1: yy, x2: W - padR, y2: yy,
        stroke: GRID, "stroke-width": 1, "stroke-dasharray": "2 4" }));
      var lab = el("text", { x: padL - 8, y: yy + 3.5, "text-anchor": "end",
        "font-size": 11, fill: LABEL });
      lab.textContent = isPct ? tv + "%" : "" + tv;
      svg.appendChild(lab);
    });

    // x labels — the first/last labels anchor inward (start/end) so a wide period
    // label (e.g. "Oct 26–Nov 1") stays inside the plot instead of clipping at the
    // SVG edge; interior labels stay centered on their point. Mirrors the dashboard
    // card charts (_card_chart in pages.py).
    dates.forEach(function (d, i) {
      var anchor = i === 0 ? "start" : (i === dates.length - 1 ? "end" : "middle");
      var t = el("text", { x: x(i), y: H - 11, "text-anchor": anchor,
        "font-size": 11, fill: LABEL });
      t.textContent = xLabel(d);
      svg.appendChild(t);
    });

    // Area fill + line.
    var pts = vals.map(function (v, i) { return x(i).toFixed(1) + "," + y(v).toFixed(1); });
    svg.appendChild(el("path", {
      d: "M" + x(0).toFixed(1) + "," + (padT + plotH) + " L" + pts.join(" L ") +
         " L" + x(vals.length - 1).toFixed(1) + "," + (padT + plotH) + " Z",
      fill: area.fill, stroke: "none" }));
    svg.appendChild(el("path", { d: "M" + pts.join(" L "), fill: "none", stroke: LINE,
      "stroke-width": 2.25, "stroke-linejoin": "round", "stroke-linecap": "round" }));

    // Emphasized endpoint (blue fill, white ring — matches the dashboard dot).
    svg.appendChild(el("circle", { cx: x(vals.length - 1).toFixed(1),
      cy: y(vals[vals.length - 1]).toFixed(1), r: 5, fill: LINE,
      stroke: "#ffffff", "stroke-width": 2 }));

    // y-axis title.
    var title = isRank ? "Avg rank (lower = better)"
      : isScore ? "Content readiness /100" : "Share of page 1";
    var at = el("text", { x: 13, y: padT + plotH / 2, "text-anchor": "middle",
      "font-size": 10.5, fill: LABEL,
      transform: "rotate(-90 13 " + (padT + plotH / 2) + ")" });
    at.textContent = title;
    svg.appendChild(at);

    container.innerHTML = "";
    container.appendChild(svg);
  }

  function wireControls() {
    var form = document.querySelector(".perf-controls-form");
    if (!form) { return; }
    var brand = form.querySelector(".perf-brand");
    var item = form.querySelector(".perf-item");
    if (brand) {
      brand.addEventListener("change", function () {
        // An item belongs to one brand, so switching brand returns to all-items.
        if (item) { item.value = "all"; }
        form.submit();
      });
    }
    if (item) {
      item.addEventListener("change", function () { form.submit(); });
    }
    var go = form.querySelector(".perf-go");
    if (go) { go.hidden = true; } // JS handles switching; Go is the no-JS fallback
  }

  function wireRowDrill() {
    document.querySelectorAll("tr.row-drill[data-href]").forEach(function (row) {
      row.addEventListener("click", function (e) {
        // Let a real click on the inner link behave normally (no double nav).
        if (e.target.closest("a")) { return; }
        window.location.href = row.getAttribute("data-href");
      });
    });
  }

  function init() {
    document.querySelectorAll("[data-perf-trend]").forEach(drawTrend);
    wireControls();
    wireRowDrill();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
