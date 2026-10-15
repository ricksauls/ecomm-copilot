/* Competitive Intelligence — multi-brand trend charts + CSP-safe controls.
 *
 * The app's CSP is `script-src 'self'` (no inline handlers), so this external
 * file does two things:
 *   1. Draw each [data-ci-chart] as a dependency-free inline-SVG line chart from
 *      its data-ci-chart JSON ({labels, unit, series:[{name, mine, color, values}]}).
 *      unit "rank" inverts the y-axis (lower position is better); null values in a
 *      series are gaps (the line breaks rather than dropping to zero). The mine
 *      brand's line is drawn last (on top) in the app's near-black "mine" color.
 *   2. Auto-submit the Brand <select> on change (the "Go" button is the no-JS
 *      fallback, hidden once this runs).
 */
(function () {
  var SVG_NS = "http://www.w3.org/2000/svg";
  var W = 560, H = 210, PAD_L = 40, PAD_R = 14, PAD_T = 12, PAD_B = 26;

  function el(name, attrs) {
    var e = document.createElementNS(SVG_NS, name);
    for (var k in attrs) { e.setAttribute(k, attrs[k]); }
    return e;
  }

  // Every non-null value across all series, for the y-domain.
  function allValues(series) {
    var out = [];
    series.forEach(function (s) {
      (s.values || []).forEach(function (v) { if (v !== null && v !== undefined) { out.push(v); } });
    });
    return out;
  }

  function drawChart(container) {
    var data;
    try { data = JSON.parse(container.getAttribute("data-ci-chart") || "{}"); }
    catch (e) { return; }
    var series = data.series || [];
    var labels = data.labels || [];
    var vals = allValues(series);
    if (labels.length < 2 || vals.length === 0) {
      container.innerHTML =
        '<p class="ci-section-hint">Not enough completed periods yet for a trend line.</p>';
      return;
    }

    var isRank = data.unit === "rank";   // lower is better → invert the axis
    var isPct = data.unit === "share";
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var span = (hi - lo) || 1;
    lo -= span * 0.12; hi += span * 0.12;

    var n = labels.length;
    var plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;
    function x(i) { return PAD_L + (i / (n - 1)) * plotW; }
    function y(v) {
      var t = (v - lo) / (hi - lo);
      return isRank ? PAD_T + t * plotH : PAD_T + (1 - t) * plotH;
    }

    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, "class": "ci-trend-svg",
      role: "img", "aria-label": (isRank ? "Search rank" : "Share of shelf") + " trend" });

    // Gridlines + y labels (3 ticks).
    for (var g = 0; g <= 2; g++) {
      var val = lo + (g / 2) * (hi - lo);
      var gy = y(val);
      svg.appendChild(el("line", { x1: PAD_L, y1: gy, x2: W - PAD_R, y2: gy,
        stroke: "#eef2f8", "stroke-width": 1 }));
      var lab = isPct ? val.toFixed(0) + "%" : "#" + val.toFixed(1);
      var tx = el("text", { x: PAD_L - 6, y: gy + 3, "text-anchor": "end",
        "font-size": 9, fill: "#94a3b8" });
      tx.textContent = lab;
      svg.appendChild(tx);
    }

    // X labels — thin to ~6 so they don't collide on a narrow chart.
    var step = Math.ceil(n / 6);
    labels.forEach(function (d, i) {
      if (i % step !== 0 && i !== n - 1) { return; }
      var tx = el("text", { x: x(i), y: H - 8, "text-anchor": "middle",
        "font-size": 9, fill: "#94a3b8" });
      tx.textContent = d;
      svg.appendChild(tx);
    });

    // Series: one polyline per unbroken run of non-null points, plus an endpoint dot.
    series.forEach(function (s) {
      var w = s.mine ? 2.4 : 1.6;
      var run = [];
      var lastIdx = -1, lastVal = null;
      function flush() {
        if (run.length >= 2) {
          svg.appendChild(el("polyline", { points: run.join(" "), fill: "none",
            stroke: s.color, "stroke-width": w, "stroke-linejoin": "round",
            "stroke-linecap": "round" }));
        } else if (run.length === 1) {
          var p = run[0].split(",");
          svg.appendChild(el("circle", { cx: p[0], cy: p[1], r: w, fill: s.color }));
        }
        run = [];
      }
      (s.values || []).forEach(function (v, i) {
        if (v === null || v === undefined) { flush(); return; }
        run.push(x(i).toFixed(1) + "," + y(v).toFixed(1));
        lastIdx = i; lastVal = v;
      });
      flush();
      if (lastIdx >= 0) {
        svg.appendChild(el("circle", { cx: x(lastIdx), cy: y(lastVal), r: w + 1.2, fill: s.color }));
      }
    });

    container.innerHTML = "";
    container.appendChild(svg);
  }

  function wireControls() {
    var form = document.querySelector(".perf-controls-form");
    if (!form) { return; }
    var brand = form.querySelector(".ci-brand-select");
    if (brand) {
      brand.addEventListener("change", function () { form.submit(); });
    }
    var go = form.querySelector(".perf-go");
    if (go) { go.hidden = true; }  // JS handles switching; Go is the no-JS fallback
  }

  function init() {
    document.querySelectorAll("[data-ci-chart]").forEach(drawChart);
    wireControls();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
