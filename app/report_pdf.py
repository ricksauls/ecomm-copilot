"""Render a Business Performance Report to a 16:9 PDF deck (reportlab).

Takes the dict from :func:`app.report.assemble_business_report` and draws a
fixed-layout slide deck — one page per slide at 13.333in × 7.5in (960×540 pt),
the standard widescreen slide size, so the PDF and the PPTX
(:mod:`app.report_pptx`) share proportions. Drawn on a low-level canvas (absolute
positioning) rather than a flowing document, because slides are fixed layouts
with full-bleed title/section backgrounds a flowing frame can't express.

Pure-Python and synchronous (reportlab only, no browser, no system libraries),
so it runs inside a web request or the background worker on the droplet exactly
like the existing exports in :mod:`app.pdf_export`, whose self-contained chart
drawings (share-of-shelf stacked bar, rank placement grid) this module reuses.

Theme: the DISCOtech palette (Midnight + Electric Blue), kept here as constants
rather than imported from CSS — reportlab needs RGB values, and the palette is
stable (see app/static/css/tokens.css for the source of truth).
"""

import io
import logging

from reportlab.graphics import renderPDF
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

from app.pdf_export import _rank_placement_grid, _sos_chart

logger = logging.getLogger(__name__)

# ── Page geometry: 16:9 widescreen (matches the PPTX). ───────────────────────
PAGE_W, PAGE_H = 13.333 * inch, 7.5 * inch
MARGIN = 0.6 * inch

# ── DISCOtech palette (see app/static/css/tokens.css). ───────────────────────
MIDNIGHT = colors.HexColor("#0b1220")   # dark title/section/close backgrounds
NAVY = colors.HexColor("#132a52")
ACCENT = colors.HexColor("#2e6dff")     # Electric Blue — headings, accents
INK = colors.HexColor("#0b1220")        # primary text on light
SLATE = colors.HexColor("#64748b")      # secondary text
COOL = colors.HexColor("#94a3b8")       # labels, captions
BORDER = colors.HexColor("#d7deea")
DIVIDER = colors.HexColor("#eef2f8")
CARD_BG = colors.HexColor("#f3f6fb")
WHITE = colors.white
GREEN = colors.HexColor("#16a34a")
RED = colors.HexColor("#ff3b30")
AMBER = colors.HexColor("#d97706")
GREEN_BG = colors.HexColor("#e9f7ef")
AMBER_BG = colors.HexColor("#fdf4e3")

# Impact / priority → pill color.
_TONE_COLOR = {"good": GREEN, "warn": AMBER, "bad": RED, "muted": COOL,
               "info": ACCENT, "up": GREEN, "down": RED, "flat": COOL}
_IMPACT_COLOR = {"High": RED, "Medium": AMBER, "Low": SLATE}


def _style(size, color=INK, *, bold=False, leading=None, align=TA_LEFT, tracking=0) -> ParagraphStyle:
    """A ParagraphStyle for wrapped text drawn onto the canvas."""
    return ParagraphStyle(
        f"s{size}{'b' if bold else ''}", fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size, leading=leading or size * 1.25, textColor=color, alignment=align,
    )


def _para(c, text, x, y_top, width, style) -> float:
    """Draw a wrapped paragraph with its TOP-LEFT at (x, y_top); return its height.

    reportlab's origin is bottom-left, so we wrap to learn the height, then place
    the frame so its top sits at ``y_top`` (the layout math everywhere else here
    reasons top-down, like slides do).
    """
    p = Paragraph(text, style)
    w, h = p.wrap(width, PAGE_H)
    p.drawOn(c, x, y_top - h)
    return h


def _label(c, text, x, y_top, color=COOL, size=8.5):
    """The tracked-uppercase label used above most blocks (letter-spaced)."""
    _tracked(c, text.upper(), x, y_top - size, "Helvetica-Bold", size, color, 1.1)


def _tracked(c, text, x, y, font, size, color, spacing):
    """Draw letter-spaced text via reportlab's char spacing (no fake glyphs).

    Uses ``setCharSpace`` — the canvas's real tracking — rather than inserting
    spacer characters, which render as tofu boxes in the built-in fonts.
    """
    t = c.beginText(x, y)
    t.setFont(font, size)
    t.setFillColor(color)
    t.setCharSpace(spacing)  # char spacing is a text-object setting, not a canvas one
    t.textOut(text)
    c.drawText(t)


def _slide_number(c, n, total):
    """Small page marker, bottom-right, matching the deck's footer."""
    c.setFont("Helvetica", 7.5)
    c.setFillColor(COOL)
    c.drawRightString(PAGE_W - MARGIN, MARGIN * 0.6, f"{n:02d} / {total:02d}")


def _bg(c, color):
    """Fill the whole page with one color (full-bleed slide background)."""
    c.setFillColor(color)
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)


def _heading(c, text, y_top=PAGE_H - MARGIN, sub=None):
    """Standard content-slide heading: accent rule + title (+ optional subtitle).

    Returns the y (top) where body content can begin below the heading.
    """
    c.setFillColor(ACCENT)
    c.rect(MARGIN, y_top - 3, 34, 3, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", 26)
    c.setFillColor(INK)
    c.drawString(MARGIN, y_top - 34, text)
    y = y_top - 48
    if sub:
        c.setFont("Helvetica", 11)
        c.setFillColor(SLATE)
        c.drawString(MARGIN, y - 10, sub)
        y -= 24
    return y - 10


# ── Slides ───────────────────────────────────────────────────────────────────

def _title_slide(c, meta):
    """Cover: Midnight field, product name, report title, brand + period."""
    _bg(c, MIDNIGHT)
    c.setFillColor(ACCENT)
    c.rect(MARGIN, PAGE_H - MARGIN - 3, 54, 4, stroke=0, fill=1)
    _tracked(c, "DISCOTECH", MARGIN, PAGE_H - MARGIN - 34, "Helvetica-Bold", 15,
             colors.HexColor("#eaeff7"), 2.0)

    c.setFont("Helvetica-Bold", 46)
    c.setFillColor(WHITE)
    c.drawString(MARGIN, PAGE_H / 2 + 6, "Performance Report")
    c.setFont("Helvetica", 20)
    c.setFillColor(colors.HexColor("#94a3b8"))
    c.drawString(MARGIN, PAGE_H / 2 - 28, "Search & Content Readiness")

    # Brand + period footer block.
    c.setFillColor(ACCENT)
    c.rect(MARGIN, MARGIN + 34, 28, 2, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", 16)
    c.setFillColor(WHITE)
    line = meta["brand_label"]
    if meta.get("period_label"):
        line += f"  ·  {meta['period_label']}"
    c.drawString(MARGIN, MARGIN + 10, line)
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.HexColor("#64748b"))
    c.drawString(MARGIN, MARGIN - 8, f"Generated {meta['generated_at']}")


def _agenda_slide(c, has_data):
    """What the report covers — numbered sections (Discovery omitted w/o CI data)."""
    _bg(c, WHITE)
    y = _heading(c, "What's Inside")
    rows = [("01", "Summary", "Headline KPIs and this period's highlights")]
    if has_data:
        rows.append(("02", "Product Discovery", "Search ranking and share of digital shelf"))
    rows.append((f"{len(rows) + 1:02d}", "Content Readiness",
                 "Primary content quality, scored per product"))
    rows.append((f"{len(rows) + 1:02d}", "Action Plan",
                 "Prioritized recommendations and next steps"))
    ry = y - 10
    for num, title, desc in rows:
        c.setFont("Helvetica-Bold", 22)
        c.setFillColor(ACCENT)
        c.drawString(MARGIN, ry - 20, num)
        c.setFont("Helvetica-Bold", 17)
        c.setFillColor(INK)
        c.drawString(MARGIN + 54, ry - 14, title)
        c.setFont("Helvetica", 11)
        c.setFillColor(SLATE)
        c.drawString(MARGIN + 54, ry - 32, desc)
        ry -= 66


def _kpi_card(c, x, y_top, w, h, label, card):
    """One KPI tile: label, big value, delta pill, caption. ``card`` may be empty."""
    c.setFillColor(CARD_BG)
    c.setStrokeColor(BORDER)
    c.roundRect(x, y_top - h, w, h, 6, stroke=1, fill=1)
    pad = 16
    _label(c, label, x + pad, y_top - pad + 2)
    value = card.get("value") if card.get("available") else "—"
    c.setFont("Helvetica-Bold", 40)
    c.setFillColor(INK if card.get("available") else COOL)
    c.drawString(x + pad, y_top - h + 56, value or "—")
    # Delta pill.
    delta = card.get("delta") or {}
    if delta.get("display"):
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(_TONE_COLOR.get(delta.get("dir"), COOL))
        c.drawString(x + pad, y_top - h + 36, delta["display"])
    c.setFont("Helvetica", 9)
    c.setFillColor(SLATE)
    c.drawString(x + pad, y_top - h + 18, card.get("caption", ""))


def _summary_slide(c, data):
    """Summary: Discovery Drivers (rank, shelf) + Conversion Drivers (readiness)."""
    _bg(c, WHITE)
    y = _heading(c, "Summary", sub="Overall KPI health")
    cards = data["cards"]
    usable = PAGE_W - 2 * MARGIN
    card_h = 150
    top = y - 14
    # Two groups, matching the example: Discovery (2 cards) | Conversion (1 card).
    gap = 18
    group_gap = 34
    disc_w = (usable - group_gap) * 0.62
    conv_w = (usable - group_gap) * 0.38
    _label(c, "Discovery Drivers", MARGIN, top, color=ACCENT, size=9)
    _label(c, "Conversion Drivers", MARGIN + disc_w + group_gap, top, color=ACCENT, size=9)
    cy = top - 16
    cw = (disc_w - gap) / 2
    _kpi_card(c, MARGIN, cy, cw, card_h, "Avg Search Rank", cards["avg_rank"])
    _kpi_card(c, MARGIN + cw + gap, cy, cw, card_h, "Share of Page 1", cards["shelf_share"])
    _kpi_card(c, MARGIN + disc_w + group_gap, cy, conv_w, card_h,
              "Content Readiness", cards["content_readiness"])


def _bullet_box(c, x, y_top, w, h, title, title_color, bg, bullets):
    """A titled box of bullet lines (the Wins / Attention blocks)."""
    c.setFillColor(bg)
    c.setStrokeColor(bg)
    c.roundRect(x, y_top - h, w, h, 5, stroke=0, fill=1)
    pad = 16
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(title_color)
    c.drawString(x + pad, y_top - pad - 4, title)
    by = y_top - pad - 26
    style = _style(10, INK, leading=13)
    for b in bullets:
        c.setFillColor(title_color)
        c.circle(x + pad + 3, by - 4, 1.8, stroke=0, fill=1)
        h_used = _para(c, b, x + pad + 14, by + 1, w - pad * 2 - 14, style)
        by -= max(h_used, 14) + 6
        if by < y_top - h + 10:
            break


def _highlights_slide(c, data):
    """Wins (green) and Attention Needed (amber), stacked."""
    _bg(c, WHITE)
    y = _heading(c, "Highlights")
    hl = data["highlights"]
    usable = PAGE_W - 2 * MARGIN
    total_h = y - MARGIN - 10
    box_h = (total_h - 20) / 2
    _bullet_box(c, MARGIN, y - 10, usable, box_h, "WINS", GREEN, GREEN_BG, hl["wins"])
    _bullet_box(c, MARGIN, y - 10 - box_h - 20, usable, box_h,
                "ATTENTION NEEDED", AMBER, AMBER_BG, hl["attention"])


def _section_slide(c, num, title, desc):
    """Full-bleed Midnight section divider."""
    _bg(c, MIDNIGHT)
    _tracked(c, f"SECTION {num}", MARGIN, PAGE_H / 2 + 54, "Helvetica-Bold", 12, ACCENT, 2.0)
    c.setFont("Helvetica-Bold", 40)
    c.setFillColor(WHITE)
    c.drawString(MARGIN, PAGE_H / 2 - 4, title)
    if desc:
        c.setFont("Helvetica", 13)
        c.setFillColor(colors.HexColor("#94a3b8"))
        c.drawString(MARGIN, PAGE_H / 2 - 34, desc)


def _table(c, header, rows, col_widths, x, y_top, *, max_rows=None):
    """Build + draw a compact data table top-anchored at (x, y_top)."""
    if max_rows:
        rows = rows[:max_rows]
    cell = _style(9, INK, leading=11)
    muted = _style(9, SLATE, leading=11)
    data = [[Paragraph(f"<b>{h}</b>", _style(8, SLATE)) for h in header]]
    for r in rows:
        data.append([Paragraph(str(v), muted if i and i == len(r) - 0 else cell)
                     for i, v in enumerate(r)])
    if len(data) == 1:
        data.append([Paragraph("No data.", muted)] + [""] * (len(header) - 1))
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), CARD_BG),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, DIVIDER),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    w, h = t.wrap(sum(col_widths), PAGE_H)
    t.drawOn(c, x, y_top - h)
    return h


def _rank_slide(c, data):
    """Search rank: avg-ranking table (left) + brand placement grid (right)."""
    _bg(c, WHITE)
    y = _heading(c, "Search Rank", sub="Average page-1 position across tracked keywords")
    disc = data["discovery"]
    usable = PAGE_W - 2 * MARGIN
    left_w = usable * 0.42
    # Avg ranking table (mine + competitors).
    rows = [[r["brand_name"], r.get("type", ""),
             f"#{r['avg_position']}" if r.get("avg_position") is not None else "—"]
            for r in disc["avg_ranks"]]
    _table(c, ["Brand", "Type", "Avg rank"], rows,
           [left_w * 0.5, left_w * 0.24, left_w * 0.26], MARGIN, y - 6, max_rows=10)
    # Placement grid (reused from pdf_export; self-contained Drawing).
    grid = _rank_placement_grid(disc["rank_map"])
    if grid and grid.width:
        gx = MARGIN + left_w + 26
        avail_w = usable - left_w - 26
        scale = min(1.0, avail_w / grid.width)
        grid.scale(scale, scale)
        renderPDF.draw(grid, c, gx, y - 10 - grid.height * scale)
        c.setFont("Helvetica", 8)
        c.setFillColor(SLATE)
        c.drawString(gx, y - 4, "Page-1 placement — red = your brand, dark = competitors")


def _shelf_slide(c, data):
    """Share of digital shelf: table + stacked-bar chart."""
    _bg(c, WHITE)
    y = _heading(c, "Share of Digital Shelf",
                 sub="Your page-1 slots vs. the whole shelf")
    disc = data["discovery"]
    usable = PAGE_W - 2 * MARGIN
    left_w = usable * 0.5
    rows = [[r["brand_name"], f"{r['organic_share']}%", f"{r['sponsored_share']}%",
             f"{r['total_share']}%"] for r in disc["sos_rows"]]
    _table(c, ["Brand", "Organic", "Sponsored", "Total"], rows,
           [left_w * 0.4, left_w * 0.2, left_w * 0.2, left_w * 0.2], MARGIN, y - 6,
           max_rows=10)
    chart = _sos_chart(disc["sos_rows"])
    if getattr(chart, "width", 0):
        cx = MARGIN + left_w + 26
        avail_w = usable - left_w - 26
        scale = min(1.0, avail_w / chart.width)
        chart.scale(scale, scale)
        renderPDF.draw(chart, c, cx, y - 20 - chart.height * scale)
        c.setFont("Helvetica", 8)
        c.setFillColor(SLATE)
        c.drawString(cx, y - 10, "Organic (dark) + Sponsored (gray) share per brand")


def _mix_slide(c, data):
    """Organic vs. Paid placement mix — two big percentages + context."""
    _bg(c, WHITE)
    y = _heading(c, "Organic vs. Paid Placement Mix")
    mix = data["discovery"]["mix"]
    usable = PAGE_W - 2 * MARGIN
    card_h = 150
    cw = (usable - 24) / 2
    for i, (label, pct, share, note) in enumerate([
        ("Organic Placements", mix["organic_pct"], mix["organic_share"],
         "Primary discovery channel"),
        ("Sponsored Placements", mix["sponsored_pct"], mix["sponsored_share"],
         "Paid search results"),
    ]):
        x = MARGIN + i * (cw + 24)
        c.setFillColor(CARD_BG)
        c.setStrokeColor(BORDER)
        c.roundRect(x, y - card_h, cw, card_h, 6, stroke=1, fill=1)
        _label(c, label, x + 18, y - 16)
        c.setFont("Helvetica-Bold", 44)
        c.setFillColor(INK)
        c.drawString(x + 18, y - card_h + 64, f"{pct}%")
        c.setFont("Helvetica", 10)
        c.setFillColor(SLATE)
        c.drawString(x + 18, y - card_h + 42, note)
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(ACCENT)
        c.drawString(x + 18, y - card_h + 22, f"{share}% share of page 1")


def _callout(c, x, y_top, w, tone, headline, body) -> float:
    """An insight callout: accent left-bar + headline + body; returns height used."""
    color = _TONE_COLOR.get(tone, ACCENT)
    style = _style(10, INK, leading=13)
    hstyle = _style(11, color, bold=True)
    th = _para(c, headline, x + 14, y_top, w - 20, hstyle)
    bh = _para(c, body, x + 14, y_top - th - 4, w - 20, style)
    total = th + bh + 10
    c.setFillColor(color)
    c.rect(x, y_top - total + 6, 3, total - 6, stroke=0, fill=1)
    return total


def _insights_slide(c, data):
    """Discovery Key Insights — stacked color-barred callouts."""
    _bg(c, WHITE)
    y = _heading(c, "Discovery Key Insights")
    usable = PAGE_W - 2 * MARGIN
    iy = y - 8
    for ins in data["discovery"]["insights"]:
        used = _callout(c, MARGIN, iy, usable, ins["tone"], ins["headline"], ins["body"])
        iy -= used + 14
        if iy < MARGIN:
            break


def _bar(c, x, y, w, value, color):
    """A horizontal score bar (0-100) with a filled portion."""
    track_h = 9
    c.setFillColor(DIVIDER)
    c.roundRect(x, y, w, track_h, 2, stroke=0, fill=1)
    if value:
        c.setFillColor(color)
        c.roundRect(x, y, w * min(value, 100) / 100, track_h, 2, stroke=0, fill=1)


def _readiness_slide(c, data):
    """Content Readiness Scorecard — per-dimension bars + overall."""
    _bg(c, WHITE)
    r = data["readiness"]
    sub = (f"Overall {r['overall']}/100 across {r['scored_count']} product"
           f"{'' if r['scored_count'] == 1 else 's'}" if r["available"]
           else "No products scored yet")
    y = _heading(c, "Content Readiness Scorecard", sub=sub)
    _label(c, "Primary Content", MARGIN, y - 4, color=COOL)
    ry = y - 24
    usable = PAGE_W - 2 * MARGIN
    bar_x = MARGIN + 190
    bar_w = usable - 190 - 150
    for d in r["dimensions"]:
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(INK)
        c.drawString(MARGIN, ry - 10, d["label"])
        tone = d["grade"]["tone"]
        _bar(c, bar_x, ry - 13, bar_w, d["avg"] or 0, _TONE_COLOR.get(tone, COOL))
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(INK if d["avg"] is not None else COOL)
        c.drawString(bar_x + bar_w + 14, ry - 12, f"{d['avg']}" if d["avg"] is not None else "n/a")
        # Grade pill.
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(_TONE_COLOR.get(tone, COOL))
        c.drawString(bar_x + bar_w + 50, ry - 12, d["grade"]["label"])
        ry -= 40
    if not r["dimensions"]:
        c.setFont("Helvetica", 11)
        c.setFillColor(SLATE)
        c.drawString(MARGIN, ry - 10, "Score this brand's products to populate the scorecard.")


def _item_scores_slide(c, data):
    """Per-item primary content scores table."""
    _bg(c, WHITE)
    y = _heading(c, "Content Scores by Item")
    rows = [[it["title"] or it["item_id"], f"{it['score']}", it["grade"]["label"]]
            for it in data["readiness"]["items"]]
    usable = PAGE_W - 2 * MARGIN
    _table(c, ["Item", "Score", "Grade"], rows,
           [usable * 0.64, usable * 0.16, usable * 0.20], MARGIN, y - 6, max_rows=11)


def _fixes_slide(c, data):
    """Listing Readiness Priority Fixes table."""
    _bg(c, WHITE)
    y = _heading(c, "Listing Readiness Priority Fixes")
    fixes = data["priority_fixes"]
    usable = PAGE_W - 2 * MARGIN
    rows = [[f["item"], f["issue"], f["impact"], f["action"]] for f in fixes]
    if rows:
        _table(c, ["Item", "Issue", "Impact", "Action required"], rows,
               [usable * 0.24, usable * 0.28, usable * 0.12, usable * 0.36],
               MARGIN, y - 6, max_rows=11)
    else:
        c.setFont("Helvetica", 12)
        c.setFillColor(SLATE)
        c.drawString(MARGIN, y - 30, "No content gaps flagged — all scored products meet target.")


def _recs_slide(c, data):
    """Strategic Recommendations — priority-pilled cards."""
    _bg(c, WHITE)
    y = _heading(c, "Strategic Recommendations")
    usable = PAGE_W - 2 * MARGIN
    iy = y - 6
    for rec in data["recommendations"]:
        color = _IMPACT_COLOR.get(rec["priority"], SLATE)
        # Priority pill.
        c.setFillColor(color)
        c.roundRect(MARGIN, iy - 17, 54, 15, 3, stroke=0, fill=1)
        c.setFont("Helvetica-Bold", 8)
        c.setFillColor(WHITE)
        c.drawString(MARGIN + 8, iy - 13, rec["priority"].upper())
        c.setFont("Helvetica-Bold", 13)
        c.setFillColor(INK)
        c.drawString(MARGIN + 66, iy - 13, rec["title"])
        bh = _para(c, rec["body"], MARGIN + 66, iy - 22, usable - 66, _style(10, SLATE, leading=13))
        iy -= 22 + bh + 16
        if iy < MARGIN:
            break


def _close_slide(c, meta):
    """Closing slide."""
    _bg(c, MIDNIGHT)
    c.setFont("Helvetica-Bold", 44)
    c.setFillColor(WHITE)
    c.drawString(MARGIN, PAGE_H / 2, "Questions?")
    c.setFont("Helvetica", 13)
    c.setFillColor(colors.HexColor("#94a3b8"))
    c.drawString(MARGIN, PAGE_H / 2 - 30,
                 f"{meta['brand_label']} · {meta.get('period_label') or 'Current'} · "
                 f"DISCOtech")


def build_business_report_pdf(data: dict) -> bytes:
    """Render the assembled report dict to a PDF deck; return the bytes.

    Slide set adapts to the data: the Product Discovery section (and its slides)
    is included only when CI monitoring has a completed period; content readiness
    and the action plan always render. Every slide handles empty inputs, so a
    brand with thin data still produces a complete, sensible deck.
    """
    meta = data["meta"]
    has_data = meta["has_data"]
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"DISCOtech Performance Report — {meta['brand_label']}")

    # Assemble the slide call list so we can number footers consistently.
    slides = [lambda: _title_slide(c, meta),
              lambda: _agenda_slide(c, has_data),
              lambda: _summary_slide(c, data),
              lambda: _highlights_slide(c, data)]
    sec = 1
    if has_data:
        slides += [
            lambda s=sec: _section_slide(c, f"{s:02d}", "Product Discovery",
                                         "Search ranking and share of digital shelf"),
            lambda: _rank_slide(c, data),
            lambda: _shelf_slide(c, data),
            lambda: _mix_slide(c, data),
            lambda: _insights_slide(c, data),
        ]
        sec += 1
    slides += [
        lambda s=sec: _section_slide(c, f"{s:02d}", "Content Readiness",
                                     "Primary content quality, scored per product"),
        lambda: _readiness_slide(c, data),
        lambda: _item_scores_slide(c, data),
        lambda: _fixes_slide(c, data),
    ]
    sec += 1
    slides += [
        lambda s=sec: _section_slide(c, f"{s:02d}", "Action Plan",
                                     "Prioritized recommendations and next steps"),
        lambda: _recs_slide(c, data),
        lambda: _close_slide(c, meta),
    ]

    total = len(slides)
    for i, draw in enumerate(slides, start=1):
        draw()
        _slide_number(c, i, total)
        c.showPage()
    c.save()
    logger.info("Built business report PDF: brand=%s period=%s slides=%d",
                meta["brand_label"], meta["period"], total)
    return buffer.getvalue()
