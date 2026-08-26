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

  // ── Sparkline hover tooltip ───────────────────────────────────────────────
  // One shared element reused by every sparkline; positioned over the hovered
  // point (a transparent, larger hit-circle makes the tiny points easy to hit).
  var tip;
  function getTip() {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "ci-spark-tip";
      tip.hidden = true;
      document.body.appendChild(tip);
    }
    return tip;
  }
  function showTip(target) {
    var t = getTip();
    t.textContent = target.getAttribute("data-value");
    t.hidden = false;
    var r = target.getBoundingClientRect();
    // Center above the point, in document coordinates (the tip lives on <body>).
    t.style.left = (r.left + r.width / 2 + window.scrollX) + "px";
    t.style.top = (r.top + window.scrollY) + "px";
  }
  function hideTip() {
    if (tip) tip.hidden = true;
  }

  function formatValue(v, unit) {
    if (unit === "rank") return "#" + v;    // search ranking position
    if (unit === "share") return v + "%";   // share of shelf
    return "" + v;
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // "2026-08-24" -> "Aug 24". Parsed by hand (not new Date) so a UTC date string
  // never shifts a day in the viewer's local timezone.
  function formatDate(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    if (!m) return iso || "";
    return MONTHS[parseInt(m[2], 10) - 1] + " " + parseInt(m[3], 10);
  }

  // ── Rank / share sparkline ────────────────────────────────────────────────
  function drawSparkline(container) {
    var pts, dates;
    try { pts = JSON.parse(container.getAttribute("data-points") || "[]"); }
    catch (e) { return; }
    if (!pts.length) return;
    try { dates = JSON.parse(container.getAttribute("data-dates") || "[]"); }
    catch (e) { dates = []; }

    var W = 68, H = 20, pad = 2;
    var min = Math.min.apply(null, pts), max = Math.max.apply(null, pts);
    var range = max - min || 1;
    var stepX = pts.length > 1 ? (W - 2 * pad) / (pts.length - 1) : 0;

    // Orientation: "better" values render toward the top. For rank (default),
    // smaller is better; for share (data-better="high"), larger is better.
    var betterHigh = container.getAttribute("data-better") === "high";
    var unit = container.getAttribute("data-unit") || "";
    function y(v) {
      var t = betterHigh ? (max - v) : (v - min);
      return pad + (t / range) * (H - 2 * pad);
    }

    var svg = el("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H });
    var d = "";
    pts.forEach(function (v, i) {
      d += (i === 0 ? "M" : "L") + (pad + i * stepX).toFixed(1) + " " + y(v).toFixed(1) + " ";
    });
    svg.appendChild(el("path", { d: d.trim(), fill: "none", stroke: "#050505",
      "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" }));

    // A visible dot at each point (the latest is larger), plus a transparent,
    // larger hit-circle that shows the value on hover.
    pts.forEach(function (v, i) {
      var cx = (pad + i * stepX).toFixed(1);
      var cy = y(v).toFixed(1);
      var isLast = i === pts.length - 1;
      svg.appendChild(el("circle", { cx: cx, cy: cy, r: isLast ? 2 : 1.4, fill: "#050505" }));

      // Tooltip text: "date · value" when a date is known, else just the value.
      var value = formatValue(v, unit);
      var label = dates[i] ? formatDate(dates[i]) + " · " + value : value;
      var hit = el("circle", { cx: cx, cy: cy, r: 6, fill: "#050505",
        "fill-opacity": "0", "class": "ci-spark-hit" });
      hit.setAttribute("data-value", label);
      hit.addEventListener("mouseenter", function () { showTip(hit); });
      hit.addEventListener("mouseleave", hideTip);
      svg.appendChild(hit);
    });
    container.appendChild(svg);
  }

  document.querySelectorAll("[data-ci-chart]").forEach(drawChart);
  document.querySelectorAll(".ci-spark[data-points]").forEach(drawSparkline);
})();
