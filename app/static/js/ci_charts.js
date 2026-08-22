/* Competitive Intelligence charts — dependency-free inline SVG.
 *
 * Two renderers, both reading JSON from data-attributes (strict CSP forbids
 * inline scripts, so no data can be embedded in a <script> tag):
 *   - [data-ci-chart]  a multi-line "share % over time" chart from
 *     {dates, brands:[{brand_name, type, share:[]}]}, with a legend into
 *     [data-ci-legend].
 *   - .ci-spark[data-points]  a tiny rank sparkline (y inverted — a lower
 *     position number is a better rank, so it sits higher).
 *
 * Palette stays monochrome: "mine" is near-black and solid; competitors are grey
 * and dashed so lines stay distinguishable without colour.
 */
(function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";
  var MINE = "#050505";
  var GREYS = ["#6b6b6b", "#9a9a9a", "#8c8c8c", "#b5b5b5"]; // cycled for non-mine
  var DASHES = ["", "4 3", "1 3", "6 3"];

  function el(name, attrs) {
    var node = document.createElementNS(SVG, name);
    for (var k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  function seriesColor(brand, i) {
    if (brand.type === "mine") return { stroke: MINE, dash: "" };
    return { stroke: GREYS[i % GREYS.length], dash: DASHES[i % DASHES.length] };
  }

  // ── Trend line chart ──────────────────────────────────────────────────────
  function drawChart(container) {
    var data;
    try { data = JSON.parse(container.getAttribute("data-series") || "{}"); }
    catch (e) { return; }
    var dates = data.dates || [];
    var brands = data.brands || [];
    if (dates.length < 2 || !brands.length) {
      container.innerHTML =
        '<p class="ci-section-hint">Not enough data points yet for a trend line.</p>';
      return;
    }

    var W = 720, H = 240, padL = 34, padR = 12, padT = 12, padB = 26;
    var plotW = W - padL - padR, plotH = H - padT - padB;

    // Y axis is 0..max share (%), with a little headroom.
    var maxY = 0;
    brands.forEach(function (b) {
      (b.share || []).forEach(function (v) { if (v > maxY) maxY = v; });
    });
    maxY = Math.max(10, Math.ceil(maxY / 10) * 10);

    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, role: "img",
      "aria-label": "Share of shelf trend" });

    // Horizontal gridlines + y labels at 0, 50%, 100% of maxY.
    [0, 0.5, 1].forEach(function (f) {
      var y = padT + plotH - f * plotH;
      svg.appendChild(el("line", { x1: padL, y1: y, x2: W - padR, y2: y,
        stroke: "#e1e1e1", "stroke-width": 1 }));
      var lbl = el("text", { x: 4, y: y + 3, "font-size": 9, fill: "#8c8c8c" });
      lbl.textContent = Math.round(f * maxY) + "%";
      svg.appendChild(lbl);
    });

    var stepX = dates.length > 1 ? plotW / (dates.length - 1) : plotW;
    function px(i) { return padL + i * stepX; }
    function py(v) { return padT + plotH - (v / maxY) * plotH; }

    brands.forEach(function (b, bi) {
      var c = seriesColor(b, bi);
      var d = "";
      (b.share || []).forEach(function (v, i) {
        d += (i === 0 ? "M" : "L") + px(i).toFixed(1) + " " + py(v).toFixed(1) + " ";
      });
      svg.appendChild(el("path", { d: d.trim(), fill: "none", stroke: c.stroke,
        "stroke-width": 2, "stroke-dasharray": c.dash,
        "stroke-linejoin": "round", "stroke-linecap": "round" }));
    });

    container.innerHTML = "";
    container.appendChild(svg);

    // Legend.
    var legend = document.querySelector("[data-ci-legend]");
    if (legend) {
      legend.innerHTML = "";
      brands.forEach(function (b, bi) {
        var c = seriesColor(b, bi);
        var item = document.createElement("span");
        item.className = "item";
        var sw = document.createElement("span");
        sw.className = "swatch";
        sw.style.background = c.stroke;
        item.appendChild(sw);
        item.appendChild(document.createTextNode(b.brand_name));
        legend.appendChild(item);
      });
    }
  }

  // ── Rank sparkline ────────────────────────────────────────────────────────
  function drawSparkline(container) {
    var pts;
    try { pts = JSON.parse(container.getAttribute("data-points") || "[]"); }
    catch (e) { return; }
    if (!pts.length) return;

    var W = 68, H = 20, pad = 2;
    var min = Math.min.apply(null, pts), max = Math.max.apply(null, pts);
    var range = max - min || 1;
    var stepX = pts.length > 1 ? (W - 2 * pad) / (pts.length - 1) : 0;

    // Invert: a smaller position (better rank) should render higher.
    function y(v) { return pad + ((v - min) / range) * (H - 2 * pad); }

    var svg = el("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H });
    var d = "";
    pts.forEach(function (v, i) {
      d += (i === 0 ? "M" : "L") + (pad + i * stepX).toFixed(1) + " " + y(v).toFixed(1) + " ";
    });
    svg.appendChild(el("path", { d: d.trim(), fill: "none", stroke: "#050505",
      "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    // Mark the latest point.
    var lastX = pad + (pts.length - 1) * stepX;
    svg.appendChild(el("circle", { cx: lastX.toFixed(1), cy: y(pts[pts.length - 1]).toFixed(1),
      r: 2, fill: "#050505" }));
    container.appendChild(svg);
  }

  document.querySelectorAll("[data-ci-chart]").forEach(drawChart);
  document.querySelectorAll(".ci-spark[data-points]").forEach(drawSparkline);
})();
