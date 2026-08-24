"""Render PDP scoring results to a PDF.

Uses reportlab (pure-Python, no browser or system libraries) so the export runs
synchronously inside a web request and needs nothing extra on the droplet. Takes
the same per-item view dicts the results page uses (see ``pages._row_view``): each
carries ``item_id``, ``url``, ``title``, ``status``, ``overall`` and a parsed
``result`` blob (overall + dimensions).
"""

import io
import logging
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    HRFlowable,
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

_INK = colors.HexColor("#050505")
_MUTED = colors.HexColor("#6b6b6b")
_RULE = colors.HexColor("#d9d9d9")
_HEADER_BG = colors.HexColor("#f3f3f3")
# Share-of-shelf chart segment fills, matching workspace.css: organic reuses the
# ink (--black), sponsored uses the cool-gray (--cool-gray).
_ORGANIC = _INK
_SPONSORED = colors.HexColor("#8c8c8c")


def _styles() -> dict:
    """Build the paragraph styles used in the report."""
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle(
            "h1", parent=base["Title"], fontSize=20, leading=24, textColor=_INK,
            spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "meta", parent=base["Normal"], fontSize=9, textColor=_MUTED, spaceAfter=2,
        ),
        "item": ParagraphStyle(
            "item", parent=base["Heading2"], fontSize=13, leading=16, textColor=_INK,
            spaceBefore=14, spaceAfter=1,
        ),
        "overall": ParagraphStyle(
            "overall", parent=base["Normal"], fontSize=11, textColor=_INK,
            spaceBefore=4, spaceAfter=6,
        ),
        "cell": ParagraphStyle(
            "cell", parent=base["Normal"], fontSize=8.5, leading=11, alignment=TA_LEFT,
        ),
        "cellmuted": ParagraphStyle(
            "cellmuted", parent=base["Normal"], fontSize=8.5, leading=11,
            textColor=_MUTED,
        ),
    }


def _notes_paragraph(dimension: dict, style: ParagraphStyle) -> Paragraph:
    """Combine a dimension's findings and recommendations into one cell.

    Findings render plain; recommendations are prefixed with an arrow so the
    action items stand out. Text is XML-escaped because reportlab parses a small
    markup language in Paragraphs.
    """
    lines = [escape(f) for f in dimension.get("findings", [])]
    lines += [f"&#8594; {escape(r)}" for r in dimension.get("recommendations", [])]
    return Paragraph("<br/>".join(lines) or "—", style)


def build_results_pdf(items: list[dict]) -> bytes:
    """Return a PDF (bytes) of the scored items in a batch.

    Only ``scored`` items with a result are included; queued/blocked/errored rows
    are skipped (they have nothing to report yet).
    """
    styles = _styles()
    scored = [it for it in items if it.get("status") == "scored" and it.get("result")]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, title="PDP Content Scores",
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
    )

    flow = [
        Paragraph("PDP (Product Detail Page) Scores", styles["h1"]),
        Paragraph(
            "Generated " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            + f" · {len(scored)} item{'' if len(scored) == 1 else 's'}",
            styles["meta"],
        ),
        HRFlowable(width="100%", thickness=1, color=_RULE, spaceBefore=6, spaceAfter=2),
    ]

    if not scored:
        flow.append(Paragraph("No scored items in this batch.", styles["overall"]))

    for it in scored:
        title = it.get("title") or it["url"]
        flow.append(Paragraph(escape(title), styles["item"]))
        flow.append(Paragraph(
            "Item #" + escape(str(it.get("item_id") or "—")) + " &#183; "
            + escape(it["url"]),
            styles["meta"],
        ))
        flow.append(Paragraph(
            f"<b>Overall: {it.get('overall')}</b> / 100", styles["overall"]
        ))

        rows = [["Dimension", "Score", "Findings & recommendations"]]
        for d in it["result"]["dimensions"]:
            score = str(d["score"]) if d.get("available", True) else "n/a"
            rows.append([
                Paragraph(escape(d["label"]), styles["cell"]),
                Paragraph(score, styles["cell"]),
                _notes_paragraph(d, styles["cell"]),
            ])

        table = Table(rows, colWidths=[1.4 * inch, 0.7 * inch, 4.9 * inch], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
            ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, _RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        flow.append(table)
        flow.append(Spacer(1, 6))

    doc.build(flow)
    logger.info("Built results PDF: %d scored item(s)", len(scored))
    return buffer.getvalue()


def _copy_cell(copy: dict, key: str, style: ParagraphStyle) -> Paragraph:
    """Render one copy field (title/description as text, bullets as a list)."""
    if key == "bullets":
        bullets = copy.get("bullets") or []
        text = "<br/>".join("&#8226; " + escape(b) for b in bullets) or "—"
    else:
        text = escape(copy.get(key) or "—")
    return Paragraph(text, style)


def build_copy_pdf(items: list[dict]) -> bytes:
    """Return a PDF (bytes) of the generated copy in a batch.

    Only ``done`` items (current + new copy both present) are included; rows still
    fetching/generating or errored are skipped. Each item shows the current and
    new copy side by side with the current -> projected score, mirroring the
    on-screen results.
    """
    styles = _styles()
    done = [
        it for it in items
        if it.get("status") == "done" and it.get("current") and it.get("new")
    ]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, title="PDP Copy Content",
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
    )

    flow = [
        Paragraph("PDP Copy Content", styles["h1"]),
        Paragraph(
            "Generated " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            + f" · {len(done)} item{'' if len(done) == 1 else 's'}",
            styles["meta"],
        ),
        HRFlowable(width="100%", thickness=1, color=_RULE, spaceBefore=6, spaceAfter=2),
    ]

    if not done:
        flow.append(Paragraph("No generated copy in this batch yet.", styles["overall"]))

    for it in done:
        title = it.get("title") or it["url"]
        flow.append(Paragraph(escape(title), styles["item"]))
        flow.append(Paragraph(
            "Item #" + escape(str(it.get("item_id") or "—")) + " &#183; "
            + escape(it["url"]),
            styles["meta"],
        ))

        # Current -> projected score line (delta helps the reader see the lift).
        cur, proj = it.get("current_overall"), it.get("projected_overall")
        if cur is not None and proj is not None:
            flow.append(Paragraph(
                f"<b>Current: {cur}</b> / 100 &#8594; <b>Projected: {proj}</b> / 100 "
                f"({proj - cur:+d})",
                styles["overall"],
            ))

        rows = [["", "Current copy", "New copy"]]
        for label, field in (("Title", "title"), ("Key Features", "bullets"),
                             ("Description", "description")):
            rows.append([
                Paragraph(f"<b>{label}</b>", styles["cell"]),
                _copy_cell(it["current"], field, styles["cell"]),
                _copy_cell(it["new"], field, styles["cell"]),
            ])

        table = Table(rows, colWidths=[1.0 * inch, 3.0 * inch, 3.0 * inch], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
            ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, _RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        flow.append(table)
        flow.append(Spacer(1, 6))

    doc.build(flow)
    logger.info("Built copy PDF: %d generated item(s)", len(done))
    return buffer.getvalue()


# ── Competitive Intelligence exports ─────────────────────────────────────────────

def _ci_table(rows: list[list], col_widths: list[float], styles: dict) -> Table:
    """Build a standard CI report table (header row styled, hairline rows)."""
    table = Table(rows, colWidths=col_widths, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, _RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def _ci_header(group: dict, subtitle: str, styles: dict) -> list:
    """Common CI report header: title, generated timestamp + subtitle, rule."""
    return [
        Paragraph(escape(group.get("name") or "Competitive Intelligence"), styles["h1"]),
        Paragraph(
            "Generated " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            + " &#183; " + escape(subtitle),
            styles["meta"],
        ),
        HRFlowable(width="100%", thickness=1, color=_RULE, spaceBefore=6, spaceAfter=2),
    ]


def _sos_table(sos_rows: list[dict], styles: dict, *, with_delta: bool) -> Table:
    """Share-of-Digital-Shelf table. Includes a Δ column only for monitoring."""
    header = ["Brand", "Type", "Organic", "Sponsored", "Total share"]
    if with_delta:
        header.append("Δ vs prior")
    rows = [header]
    for r in sos_rows:
        row = [
            Paragraph(escape(r["brand_name"]), styles["cell"]),
            Paragraph(escape(r["type"]), styles["cellmuted"]),
            Paragraph(f"{r['organic_share']}%", styles["cell"]),
            Paragraph(f"{r['sponsored_share']}%", styles["cell"]),
            Paragraph(f"{r['total_share']}%", styles["cell"]),
        ]
        if with_delta:
            d = r.get("total_share_delta", 0)
            sign = "+" if d > 0 else ""
            row.append(Paragraph(f"{sign}{d}", styles["cellmuted"]))
        rows.append(row)
    if len(rows) == 1:
        rows.append([Paragraph("No data.", styles["cellmuted"])] + [""] * (len(header) - 1))
    widths = [1.9, 0.9, 0.9, 1.0, 1.0] + ([1.0] if with_delta else [])
    return _ci_table(rows, [w * inch for w in widths], styles)


def _pos(value) -> str:
    """Render a position cell ('#n', or '—' when the brand had no such slot)."""
    return f"#{value}" if value is not None else "—"


def _rank_table(rank_rows: list[dict], styles: dict, *, with_delta: bool) -> Table:
    """Brand-level search-ranking table (mine + competitors).

    Monitoring (``with_delta``) shows Best position + Δ vs the prior window;
    the snapshot form shows the organic/sponsored best-position split instead.
    """
    if with_delta:
        header = ["Brand", "Type", "Keyword", "Best", "Δ"]
        widths = [1.7, 0.9, 1.9, 0.8, 0.6]
    else:
        header = ["Brand", "Type", "Keyword", "Best", "Organic", "Sponsored"]
        widths = [1.6, 0.9, 1.7, 0.7, 0.8, 0.9]
    rows = [header]
    for r in rank_rows:
        row = [
            Paragraph(escape(r["brand_name"]), styles["cell"]),
            Paragraph(escape(r.get("type", "")), styles["cellmuted"]),
            Paragraph(escape(r["keyword"]), styles["cell"]),
            Paragraph(_pos(r["current_position"]), styles["cell"]),
        ]
        if with_delta:
            d = r.get("delta")
            row.append(Paragraph("new" if d is None else (f"+{d}" if d > 0 else str(d)),
                                 styles["cellmuted"]))
        else:
            row.append(Paragraph(_pos(r.get("organic_position")), styles["cellmuted"]))
            row.append(Paragraph(_pos(r.get("sponsored_position")), styles["cellmuted"]))
        rows.append(row)
    if len(rows) == 1:
        rows.append([Paragraph("No brands ranked.", styles["cellmuted"])] + [""] * (len(header) - 1))
    return _ci_table(rows, [w * inch for w in widths], styles)


def _product_thumbs(products: list[dict], styles: dict) -> Table:
    """A row per tracked product: cached main-image thumbnail + name/item/type.

    A product without a cached image gets a muted "(no image)" placeholder so the
    row still lines up. Embeds the JPEG bytes reportlab needs (a hotlinked URL
    wouldn't render), scaling the thumbnail to a fixed width by its own aspect.
    """
    rows = []
    for p in products:
        path = p.get("image_path")
        thumb = Paragraph("(no image)", styles["cellmuted"])
        if path:
            try:
                iw, ih = ImageReader(path).getSize()
                w = 46.0
                thumb = RLImage(path, width=w, height=(w * ih / iw) if iw else w)
            except Exception:  # noqa: BLE001 - a bad file just falls back to text
                logger.warning("Could not embed product thumb %s", path)
        label = Paragraph(escape(p["name"] or f"Item {p['walmart_item_id']}"), styles["cell"])
        # When the name already is the item number, don't repeat it in the meta line.
        if p["name"]:
            meta_text = f"Item {escape(str(p['walmart_item_id']))} &#183; {escape(p['brand_type'])}"
        else:
            meta_text = escape(p["brand_type"])
        meta = Paragraph(meta_text, styles["cellmuted"])
        rows.append([thumb, [label, meta]])
    table = Table(rows, colWidths=[0.75 * inch, 4.6 * inch])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, _RULE),
    ]))
    return table


def _summary_flow(config_summary: dict, styles: dict) -> list:
    """"What this group tracks" block: mine/competitor brands, items, terms.

    Tracked items render as a thumbnail table (main image + name/item/type) so the
    PDF mirrors the results page's product grid.
    """
    def _line(label: str, value: str) -> Paragraph:
        body = escape(value) if value else "&#8212;"
        return Paragraph(f"<b>{escape(label)}:</b> {body}", styles["cellmuted"])

    products = config_summary.get("products") or []
    flow = [
        Paragraph("What this group tracks", styles["item"]),
        Spacer(1, 4),
        _line("My brands", ", ".join(config_summary.get("my_brands") or [])),
        _line("Competitor brands", ", ".join(config_summary.get("competitor_brands") or [])),
        _line("Search terms", ", ".join(config_summary.get("keywords") or [])),
        _line("Items tracked", str(len(products))),
    ]
    if products:
        flow.append(Spacer(1, 4))
        flow.append(_product_thumbs(products, styles))
    return flow


def _avg_rank_table(avg_ranks: list[dict], styles: dict) -> Table:
    """Overall Search Ranking: one average figure per brand (mine + competitors)."""
    rows = [["Brand", "Type", "Avg ranking"]]
    for r in avg_ranks:
        rows.append([
            Paragraph(escape(r["brand_name"]), styles["cell"]),
            Paragraph(escape(r.get("type", "")), styles["cellmuted"]),
            Paragraph(_pos(r.get("avg_position")), styles["cell"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("No brands ranked.", styles["cellmuted"]), "", ""])
    return _ci_table(rows, [w * inch for w in (2.4, 1.1, 1.3)], styles)


def _rank_by_keyword_table(rank_rows: list[dict], styles: dict) -> Table:
    """Search Ranking: average ranking per brand per keyword."""
    rows = [["Keyword", "Brand", "Type", "Avg ranking"]]
    for r in rank_rows:
        rows.append([
            Paragraph(escape(r["keyword"]), styles["cell"]),
            Paragraph(escape(r["brand_name"]), styles["cell"]),
            Paragraph(escape(r.get("type", "")), styles["cellmuted"]),
            Paragraph(_pos(r.get("avg_ranking")), styles["cell"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("No data.", styles["cellmuted"]), "", "", ""])
    return _ci_table(rows, [w * inch for w in (2.0, 2.0, 1.0, 1.3)], styles)


def _share_by_keyword_table(share_rows: list[dict], styles: dict) -> Table:
    """Per-keyword Share of Digital Shelf: shares of each keyword's own slots."""
    rows = [["Keyword", "Brand", "Type", "Organic", "Sponsored", "Total share"]]
    for r in share_rows:
        rows.append([
            Paragraph(escape(r["keyword"]), styles["cell"]),
            Paragraph(escape(r["brand_name"]), styles["cell"]),
            Paragraph(escape(r.get("type", "")), styles["cellmuted"]),
            Paragraph(f"{r['organic_share']}%", styles["cell"]),
            Paragraph(f"{r['sponsored_share']}%", styles["cell"]),
            Paragraph(f"{r['total_share']}%", styles["cell"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("No data.", styles["cellmuted"])] + [""] * 5)
    return _ci_table(rows, [w * inch for w in (1.5, 1.5, 0.9, 0.9, 1.0, 1.0)], styles)


def _sos_chart(sos_rows: list[dict]) -> Drawing | Spacer:
    """Stacked-bar chart of each brand's organic/sponsored share of the shelf.

    Mirrors the on-screen chart (``ci_snapshot_results.html``): one vertical bar
    per brand, organic (ink) at the base with sponsored (gray) stacked on top,
    every bar scaled to the tallest organic+sponsored stack, labeled with the
    brand's total share above and its name below. Drawn with reportlab shapes so
    it renders synchronously in the request — no chart library, matching the
    strict-CSP constraint the HTML view works under. Returns an empty spacer when
    there is nothing to plot.
    """
    # Scale to the tallest stack, exactly as the page does (sos_scale =
    # max organic+sponsored) so the PDF and screen bars have identical proportions.
    scale = max((r["organic_share"] + r["sponsored_share"] for r in sos_rows), default=0)
    if scale <= 0:
        return Spacer(1, 0)

    plot_h = 150          # height of the tallest bar, in points
    top_pad = 14          # room above the bars for the total-share label
    bottom_pad = 12       # room below the bars for the brand-name label
    bar_w, gap = 46, 20
    n = len(sos_rows)
    width = n * bar_w + (n - 1) * gap
    # Keep the chart within the usable page width; shrink bars/gaps if a group has
    # many brands rather than letting the drawing overflow the margins.
    max_width = 500
    if width > max_width:
        shrink = max_width / width
        bar_w *= shrink
        gap *= shrink
        width = n * bar_w + (n - 1) * gap

    height = plot_h + top_pad + bottom_pad
    drawing = Drawing(width, height)
    drawing.hAlign = "CENTER"
    x = 0.0
    for r in sos_rows:
        org_h = r["organic_share"] / scale * plot_h
        spon_h = r["sponsored_share"] / scale * plot_h
        if org_h > 0:
            drawing.add(Rect(x, bottom_pad, bar_w, org_h, fillColor=_ORGANIC, strokeColor=None))
        if spon_h > 0:
            drawing.add(Rect(x, bottom_pad + org_h, bar_w, spon_h,
                             fillColor=_SPONSORED, strokeColor=None))
        cx = x + bar_w / 2
        drawing.add(String(cx, bottom_pad + org_h + spon_h + 3, f"{r['total_share']}%",
                           fontName="Helvetica-Bold", fontSize=8, fillColor=_INK,
                           textAnchor="middle"))
        # Truncate a long brand name so a centered label can't collide with its
        # neighbours (bars are narrow; the table carries the full names).
        label = r["brand_name"] if len(r["brand_name"]) <= 12 else r["brand_name"][:11] + "…"
        drawing.add(String(cx, 2, label, fontName="Helvetica", fontSize=7,
                           fillColor=_MUTED, textAnchor="middle"))
        x += bar_w + gap
    return drawing


def _truncate(text: str, limit: int) -> str:
    """Shorten a label to fit a narrow tile (reportlab Strings don't wrap)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rank_placement_grid(rank_map: dict | None) -> Drawing | Spacer:
    """Page-1 result grid marking each brand's *overall* average rank.

    Mirrors the on-screen placement map (``ci_snapshot_results.html``): a
    ``cols``-wide grid numbered left-to-right, top-to-bottom, every slot blank
    except the one a brand's average rounds onto — my brand in the signal-red
    accent, competitors in ink, tied brands sharing one outlined tile. Consumes
    :func:`app.ci_analysis.build_rank_placement_map`. Tile height auto-shrinks so
    even a deep page-1 grid stays on one page (a Drawing can't split).
    """
    if not rank_map:
        return Spacer(1, 0)
    cols, total, rows = rank_map["cols"], rank_map["total"], rank_map["rows"]
    marks = rank_map["marks"]

    width, gap = 504.0, 6.0
    tile_w = (width - (cols - 1) * gap) / cols
    # Fit the whole grid within a height budget; clamp so tiles stay legible.
    budget = 470.0
    tile_h = min(40.0, (budget - (rows - 1) * gap) / rows) if rows else 40.0
    tile_h = max(tile_h, 26.0)
    height = rows * tile_h + (rows - 1) * gap

    drawing = Drawing(width, height)
    drawing.hAlign = "CENTER"
    red = colors.HexColor("#ff3b30")
    light = colors.HexColor("#f3f3f3")
    faint_white = colors.Color(1, 1, 1, 0.7)

    for pos in range(1, total + 1):
        idx = pos - 1
        r, c = idx // cols, idx % cols
        x = c * (tile_w + gap)
        y = height - (r + 1) * tile_h - r * gap  # row 0 at the top
        cx = x + tile_w / 2
        cell = marks.get(pos)

        if not cell:  # empty placement — hairline outline + faint number
            drawing.add(Rect(x, y, tile_w, tile_h, rx=5, fillColor=None,
                             strokeColor=_RULE, strokeWidth=0.75))
            drawing.add(String(x + 6, y + tile_h - 11, str(pos),
                               fontName="Helvetica", fontSize=6.5, fillColor=_MUTED))
        elif len(cell) == 1:
            b = cell[0]
            mine = b["type"] == "mine"
            drawing.add(Rect(x, y, tile_w, tile_h, rx=5,
                             fillColor=red if mine else _INK, strokeColor=None))
            drawing.add(String(x + 6, y + tile_h - 11, str(pos),
                               fontName="Helvetica", fontSize=6.5, fillColor=faint_white))
            txt = colors.white if mine else light
            drawing.add(String(cx, y + tile_h / 2, _truncate(b["brand_name"], 20),
                               fontName="Helvetica-Bold", fontSize=8, fillColor=txt,
                               textAnchor="middle"))
            drawing.add(String(cx, y + tile_h / 2 - 10, f"avg #{b['avg_position']}",
                               fontName="Helvetica", fontSize=7, fillColor=txt,
                               textAnchor="middle"))
        else:  # tie: outlined tile, one colored line per brand sharing the slot
            drawing.add(Rect(x, y, tile_w, tile_h, rx=5, fillColor=None,
                             strokeColor=_RULE, strokeWidth=0.75))
            drawing.add(String(x + 6, y + tile_h - 11, str(pos),
                               fontName="Helvetica", fontSize=6.5, fillColor=_MUTED))
            line_h = 9.0
            start = y + tile_h / 2 + (len(cell) - 1) * line_h / 2 - 3
            for i, b in enumerate(cell):
                col = red if b["type"] == "mine" else _INK
                label = f"{_truncate(b['brand_name'], 14)} #{b['avg_position']}"
                drawing.add(String(cx, start - i * line_h, label,
                                   fontName="Helvetica-Bold", fontSize=7,
                                   fillColor=col, textAnchor="middle"))
    return drawing


def build_ci_snapshot_pdf(group: dict, *, config_summary: dict, avg_ranks: list[dict],
                          rank_rows: list[dict], sos_rows: list[dict],
                          share_rows: list[dict], rank_map: dict | None = None) -> bytes:
    """One-Time Snapshot PDF — mirrors the results page, current-state (no trends).

    Sections, in page order: the config summary, Overall Search Ranking (table +
    the placement-map grid), per-keyword Search Ranking, Overall Share of Digital
    Shelf (table + the stacked bar chart), and per-keyword Share of Digital Shelf.
    Page breaks fall after the config summary and after per-keyword Search Ranking,
    so the summary, the ranking sections, and the share sections each start fresh.
    """
    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, title="CI Snapshot",
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
    )
    flow = _ci_header(group, "One-Time Snapshot — current state", styles)
    flow += _summary_flow(config_summary, styles)
    # Start the ranking sections on a fresh page so the config summary (with its
    # thumbnail grid) stands on its own.
    flow.append(PageBreak())
    flow.append(Paragraph("Overall Search Ranking", styles["item"]))
    flow.append(_avg_rank_table(avg_ranks, styles))
    grid = _rank_placement_grid(rank_map)
    if not isinstance(grid, Spacer):
        flow.append(Spacer(1, 8))
        flow.append(Paragraph(
            '<font color="#ff3b30">■</font> My brand'
            ' &#160;&#160;&#160; '
            '<font color="#050505">■</font> Competitor'
            ' &#160;&#160;&#160; (outlined = other page-1 placement)',
            styles["cellmuted"],
        ))
        flow.append(Spacer(1, 4))
        flow.append(grid)
    flow.append(Spacer(1, 10))
    flow.append(Paragraph("Search Ranking", styles["item"]))
    flow.append(_rank_by_keyword_table(rank_rows, styles))
    # Start the share-of-shelf sections on a fresh page.
    flow.append(PageBreak())
    flow.append(Paragraph("Overall Share of Digital Shelf", styles["item"]))
    flow.append(_sos_table(sos_rows, styles, with_delta=False))
    chart = _sos_chart(sos_rows)
    if not isinstance(chart, Spacer):
        flow.append(Spacer(1, 10))
        flow.append(chart)
        # Legend mirrors the page's: colored square + label per segment type.
        flow.append(Spacer(1, 4))
        flow.append(Paragraph(
            '<font color="#050505">■</font> Organic share'
            ' &#160;&#160;&#160; '
            '<font color="#8c8c8c">■</font> Sponsored share',
            styles["cellmuted"],
        ))
    flow.append(Spacer(1, 10))
    flow.append(Paragraph("Share of Digital Shelf", styles["item"]))
    flow.append(_share_by_keyword_table(share_rows, styles))
    doc.build(flow)
    logger.info("Built CI snapshot PDF: group=%s", group.get("name"))
    return buffer.getvalue()


def build_ci_monitoring_pdf(group: dict, period: str, sos_rows: list[dict],
                            rank_rows: list[dict]) -> bytes:
    """Monitoring PDF: Share of Shelf + Search Ranking with deltas vs the prior window."""
    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, title="CI Monitoring",
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
    )
    flow = _ci_header(group, f"Monitoring — {period.upper()} window (Δ vs prior)", styles)
    flow.append(Paragraph("Share of Digital Shelf", styles["item"]))
    flow.append(_sos_table(sos_rows, styles, with_delta=True))
    flow.append(Spacer(1, 10))
    flow.append(Paragraph("Search Ranking", styles["item"]))
    flow.append(_rank_table(rank_rows, styles, with_delta=True))
    doc.build(flow)
    logger.info("Built CI monitoring PDF: group=%s period=%s", group.get("name"), period)
    return buffer.getvalue()
