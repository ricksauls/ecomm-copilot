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
  // Organic/paid stacked-area colors, matching the KPI card's composition bar and the
  // CI Paid-vs-Organic screen: organic = the app's near-black "ink" (earned shelf),
  // paid = the electric-blue accent (bought shelf). Kept in sync with tokens.css
  // (--black / --accent) and workspace.css's .share-split-dot.organic/.paid.
  var ORGANIC = "#0b1220";   // --black (earned)
  var PAID = "#2e6dff";      // --accent (bought)

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

  // One legend chip: a colored swatch + its label (Organic / Paid).
  function legendChip(kind, label) {
    var chip = document.createElement("span");
    chip.className = "leg";
    var sw = document.createElement("span");
    sw.className = "sw " + kind;
    var tx = document.createElement("span");
    tx.textContent = label;
    chip.appendChild(sw);
    chip.appendChild(document.createTextNode(" "));
    chip.appendChild(tx);
    return chip;
  }

  // Rewrite a chart's legend to match what was drawn: two swatches (Organic/Paid) for a
  // stacked share area, or the single swatch + caption for a one-line trend. Text is set
  // via textContent (never innerHTML) so a per-keyword caption can't inject markup.
  function syncLegend(legend, stacked, text) {
    if (!legend) { return; }
    legend.textContent = "";
    if (stacked) {
      legend.appendChild(legendChip("organic", "Organic"));
      legend.appendChild(legendChip("paid", "Paid"));
    } else {
      var sw = document.createElement("span");
      sw.className = "sw";
      var tx = document.createElement("span");
      tx.setAttribute("data-kw-legend", "");
      tx.textContent = text || "";
      legend.appendChild(sw);
      legend.appendChild(document.createTextNode(" "));
      legend.appendChild(tx);
    }
  }

  function drawTrend(container) {
    var data;
    try { data = JSON.parse(container.getAttribute("data-series") || "{}"); }
    catch (e) { return; }
    var vals = data.values || [], dates = data.dates || [];
    var isRank = data.unit === "rank", isPct = data.unit === "share";
    var isScore = data.unit === "score";  // content readiness, 0–100, higher better
    // Stacked share: organic + paid areas whose top edge is the blended share (vals).
    // Guard on matching lengths so a malformed series falls back to the single line.
    var stacked = data.stacked === true && Array.isArray(data.organic) &&
      Array.isArray(data.paid) && data.organic.length === vals.length &&
      data.paid.length === vals.length;

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

    var baseY = padT + plotH;                    // y of the 0 axis (share/score share it)
    var pt = function (i, v) { return x(i).toFixed(1) + "," + y(v).toFixed(1); };
    var pts = vals.map(function (v, i) { return pt(i, v); });

    if (stacked) {
      // Two stacked areas: organic (ink) from the axis up to each period's organic
      // share, then paid (blue) from there up to the blended total. The total line and
      // its endpoint dot cap the stack; a faint ink line marks the organic/paid seam.
      var orgPts = data.organic.map(function (v, i) { return pt(i, v); });
      var last = vals.length - 1, endX = x(last).toFixed(1);
      // Organic band: baseline → organic tops → back to baseline.
      svg.appendChild(el("path", {
        d: "M" + x(0).toFixed(1) + "," + baseY + " L" + orgPts.join(" L ") +
           " L" + endX + "," + baseY + " Z",
        fill: ORGANIC, "fill-opacity": "0.16", stroke: "none" }));
      // Paid band: between the organic seam and the total line (organic tops forward,
      // total tops back).
      svg.appendChild(el("path", {
        d: "M" + orgPts.join(" L ") + " L" + pts.slice().reverse().join(" L ") + " Z",
        fill: PAID, "fill-opacity": "0.22", stroke: "none" }));
      svg.appendChild(el("path", { d: "M" + orgPts.join(" L "), fill: "none",
        stroke: ORGANIC, "stroke-width": 1.25, "stroke-opacity": "0.45",
        "stroke-linejoin": "round", "stroke-linecap": "round" }));
      svg.appendChild(el("path", { d: "M" + pts.join(" L "), fill: "none", stroke: PAID,
        "stroke-width": 2.25, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      svg.appendChild(el("circle", { cx: endX, cy: y(vals[last]).toFixed(1), r: 5,
        fill: PAID, stroke: "#ffffff", "stroke-width": 2 }));
    } else {
      // Single line + gradient area fill (rank / score / un-split share).
      svg.appendChild(el("path", {
        d: "M" + x(0).toFixed(1) + "," + baseY + " L" + pts.join(" L ") +
           " L" + x(vals.length - 1).toFixed(1) + "," + baseY + " Z",
        fill: area.fill, stroke: "none" }));
      svg.appendChild(el("path", { d: "M" + pts.join(" L "), fill: "none", stroke: LINE,
        "stroke-width": 2.25, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      // Emphasized endpoint (blue fill, white ring — matches the dashboard dot).
      svg.appendChild(el("circle", { cx: x(vals.length - 1).toFixed(1),
        cy: y(vals[vals.length - 1]).toFixed(1), r: 5, fill: LINE,
        stroke: "#ffffff", "stroke-width": 2 }));
    }

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

    // Brand-view charts (no keyword swap) own their legend here, converting it to the
    // two-swatch Organic/Paid form when a stacked area was drawn; the single-line legend
    // is left as the template rendered it. Drill charts are synced by the keyword
    // selector (wireKeywordSelect) so the two code paths never fight over the element.
    if (stacked && !container.closest("[data-kw-drill]")) {
      var card = container.closest(".chart-card");
      if (card) { syncLegend(card.querySelector(".chart-legend"), true); }
    }
  }

  // Re-point a trend container at a new series and redraw it (used by the Search
  // Rank keyword selector to swap the chart without a page reload).
  function renderSeries(container, series) {
    container.setAttribute("data-series", JSON.stringify(series || {}));
    drawTrend(container);
  }

  // Discovery drill (Search Rank / Share of Search): clicking a keyword in the KPI
  // card swaps the trend chart and the KPI headline (metric + delta + legend) to that
  // keyword's value; "All keywords" resets to the item average. Data comes from
  // data-kw-context (built server-side), so this is pure enhancement — the list
  // already shows keyword + value without JS.
  function wireKeywordSelect() {
    document.querySelectorAll("[data-kw-drill]").forEach(function (drill) {
      var ctx;
      try { ctx = JSON.parse(drill.getAttribute("data-kw-context") || "null"); }
      catch (e) { return; }
      if (!ctx || !ctx.keywords) { return; }

      var metricNode = drill.querySelector("[data-kw-metric]");
      var deltaNode = drill.querySelector("[data-kw-delta]");
      var legend = drill.querySelector(".chart-legend");
      var chart = drill.querySelector("[data-kw-chart]");
      var opts = Array.prototype.slice.call(drill.querySelectorAll("[data-kw-select] .kw-opt"));
      if (!chart || !opts.length) { return; }

      // id -> the view (metric/delta/legend/series) to show when it's selected.
      var byId = { all: ctx.all };
      ctx.keywords.forEach(function (k) { byId[String(k.id)] = k; });

      function apply(view) {
        if (metricNode) { metricNode.textContent = view.metric; }
        if (deltaNode) {
          var d = view.delta || {};
          deltaNode.className = "kpi-delta " + (d.dir || "");
          if (d.display) { deltaNode.textContent = d.display; deltaNode.hidden = false; }
          else { deltaNode.textContent = ""; deltaNode.hidden = true; }
        }
        // "All keywords" may carry a stacked organic/paid series (two swatches); a
        // single keyword is always a one-line share trend (its caption).
        syncLegend(legend, view.series && view.series.stacked === true, view.legend);
        renderSeries(chart, view.series);
      }

      function select(opt) {
        opts.forEach(function (o) {
          var on = o === opt;
          o.classList.toggle("is-active", on);
          o.setAttribute("aria-selected", on ? "true" : "false");
        });
        apply(byId[opt.getAttribute("data-kw-id")] || ctx.all);
      }

      opts.forEach(function (opt) {
        function choose() {
          // Clicking the already-active keyword (not "All") deselects back to average.
          var active = opt.classList.contains("is-active");
          var id = opt.getAttribute("data-kw-id");
          if (active && id !== "all") {
            select(opts[0]); // the "All keywords" option is always first
          } else {
            select(opt);
          }
        }
        opt.addEventListener("click", choose);
        opt.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); }
        });
      });

      // init() already drew the chart from the template's data-series, but drawTrend
      // skips drill-chart legends (this function owns them), so sync the legend once to
      // the initially-active option — a stacked "all" series needs the Organic/Paid
      // legend on load, not just after the first click.
      var active = opts.filter(function (o) { return o.classList.contains("is-active"); })[0] || opts[0];
      var view = byId[active.getAttribute("data-kw-id")] || ctx.all;
      syncLegend(legend, !!(view.series && view.series.stacked), view.legend);
    });
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
    wireKeywordSelect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
