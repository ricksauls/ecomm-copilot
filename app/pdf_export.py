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

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
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


def _summary_flow(config_summary: dict, styles: dict) -> list:
    """"What this group tracks" block: mine/competitor brands, items, terms."""
    def _line(label: str, value: str) -> Paragraph:
        body = escape(value) if value else "&#8212;"
        return Paragraph(f"<b>{escape(label)}:</b> {body}", styles["cellmuted"])

    products = config_summary.get("products") or []
    names = ", ".join((p["name"] or p["walmart_item_id"]) for p in products)
    items = f"{len(products)}" + (f" — {names}" if products else "")
    return [
        Paragraph("What this group tracks", styles["item"]),
        Spacer(1, 4),
        _line("My brands", ", ".join(config_summary.get("my_brands") or [])),
        _line("Competitor brands", ", ".join(config_summary.get("competitor_brands") or [])),
        _line("Items tracked", items),
        _line("Search terms", ", ".join(config_summary.get("keywords") or [])),
    ]


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


def build_ci_snapshot_pdf(group: dict, *, config_summary: dict, avg_ranks: list[dict],
                          rank_rows: list[dict], sos_rows: list[dict],
                          share_rows: list[dict]) -> bytes:
    """One-Time Snapshot PDF — mirrors the results page, current-state (no trends).

    Sections, in page order: the config summary, Overall Search Ranking, per-keyword
    Search Ranking, Overall Share of Digital Shelf, and per-keyword Share of Digital
    Shelf. (The on-screen stacked bar chart is omitted; the share tables carry the
    same numbers.)
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
    flow.append(Paragraph("Overall Search Ranking", styles["item"]))
    flow.append(_avg_rank_table(avg_ranks, styles))
    flow.append(Spacer(1, 10))
    flow.append(Paragraph("Search Ranking", styles["item"]))
    flow.append(_rank_by_keyword_table(rank_rows, styles))
    flow.append(Spacer(1, 10))
    flow.append(Paragraph("Overall Share of Digital Shelf", styles["item"]))
    flow.append(_sos_table(sos_rows, styles, with_delta=False))
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
