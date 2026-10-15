"""Assemble the data for a Business Performance Report.

One report covers a single monitoring brand (a CI group) over one completed
period, built entirely from data the dashboard and Insights already compute:
search ranking + share of digital shelf (``app.ci_analysis``) and content
readiness (``app.jobs``). :func:`assemble_business_report` returns one plain
dict that both renderers — :mod:`app.report_pdf` and :mod:`app.report_pptx` —
consume unchanged, so the two outputs can never drift in substance.

Deliberately excluded (the app captures neither, and the agency example we
mirror showed them as N/A): ratings & reviews, and all secondary / below-the-
fold content (brand story, comparison charts, product video). The scorecard is
primary content only (see :data:`app.jobs.PRIMARY_DIMENSION_ORDER`).

This module is request- and worker-safe: it takes an open connection, a user id,
an already-ownership-validated group row, and a validated period key, and never
touches Flask ``g`` / ``request`` / ``url_for``. The scheduling worker (PR B)
calls it the same way the on-demand route does.
"""

import logging
from datetime import datetime, timezone

from app import ci_analysis, ci_config, ci_images, jobs

logger = logging.getLogger(__name__)

# Content-readiness grade bands. "Good" is pinned to the same strong-content
# target the Opportunities list and Performance tab use (jobs.READINESS_STRONG_MIN,
# currently 80), so a product the app flags green in one place reads "Good" here.
_GRADE_BANDS = (
    (90, "Excellent", "good"),
    (jobs.READINESS_STRONG_MIN, "Good", "good"),
    (60, "Needs Attention", "warn"),
    (0, "Critical", "bad"),
)


def grade(score) -> dict:
    """Map a 0-100 content score to ``{label, tone}`` (tone: good|warn|bad).

    ``None`` (an unscored dimension) yields a muted n/a so a scorecard row still
    renders. The bands are checked high-to-low; the first threshold met wins.
    """
    if score is None:
        return {"label": "n/a", "tone": "muted"}
    for threshold, label, tone in _GRADE_BANDS:
        if score >= threshold:
            return {"label": label, "tone": tone}
    return {"label": "Critical", "tone": "bad"}


def _delta(value, *, lower_is_better: bool = False, unit: str = "pts") -> dict:
    """Shape a period-over-period delta for display: ``{display, dir}``.

    ``dir`` is up|down|flat for coloring (up = improved). ``lower_is_better``
    flips the sense for rank, where a negative numeric change (moved toward #1)
    is an improvement. A ``None`` delta (no prior period) yields ``display=None``.
    """
    if value is None:
        return {"display": None, "dir": "flat"}
    improved = (value < 0) if lower_is_better else (value > 0)
    worsened = (value > 0) if lower_is_better else (value < 0)
    direction = "up" if improved else ("down" if worsened else "flat")
    mag = abs(value)
    # Drop a trailing .0 so whole numbers read cleanly ("2 pts", not "2.0 pts").
    mag_str = f"{mag:g}"
    sign = "↑" if improved else ("↓" if worsened else "→")
    return {"display": f"{sign} {mag_str} {unit}".strip(), "dir": direction}


def _mine_rows(rows: list[dict]) -> list[dict]:
    """The caller's own-brand rows from a mixed mine/competitor result set."""
    return [r for r in rows if r.get("type") == "mine"]


def _product_views(products) -> list[dict]:
    """Tracked-product rows for the report, mine-first, with the cached image path.

    Unlike the route helper of the same name, this carries only ``image_path``
    (the cache file the PDF embeds) — no ``url_for`` — so it works in the worker
    without an app/request context. A product with no cached image gets ``None``.
    """
    products = sorted(products, key=lambda p: 0 if p["brand_type"] == "mine" else 1)
    views = []
    for p in products:
        item_id = p["walmart_item_id"]
        cached = ci_images.has_product_image(item_id)
        views.append({
            "name": p["name"],
            "walmart_item_id": item_id,
            "brand_name": p["brand_name"],
            "brand_type": p["brand_type"],
            "image_path": ci_images.product_image_path(item_id) if cached else None,
        })
    return views


def _summary_cards(conn, user_id, group_id, period, mine_brands, has_data) -> dict:
    """The three headline KPIs, mirroring the dashboard's performance briefing.

    Search Rank + Shelf Share come from the period's CI aggregation; Content
    Readiness from the brand's latest scores (with a period-over-period delta when
    CI data anchors a prior period). Each card is ``{value, delta, caption,
    available}``; ``available`` False means "gathering data" and the renderer
    shows a dash. Kept in lockstep with ``pages._performance_briefing`` — same
    source functions, same mine-average rule.
    """
    # ── Search Rank: average page-1 position of my tracked SKUs (lower is better).
    rank_card = {"value": None, "delta": _delta(None), "caption": "Across keywords",
                 "available": False}
    if has_data:
        ranks = _mine_rows(ci_analysis.monitoring_avg_rank(conn, group_id, period))
        scored = [r for r in ranks if r["avg_position"] is not None]
        if scored:
            avg_pos = round(sum(r["avg_position"] for r in scored) / len(scored), 1)
            deltas = [r["delta"] for r in scored if r["delta"] is not None]
            rank_delta = round(sum(deltas) / len(deltas), 1) if deltas else None
            rank_card = {
                "value": f"{avg_pos:g}",
                # monitoring_avg_rank frames delta as prior-minus-current, so a
                # positive delta already means "moved up" — not lower-is-better here.
                "delta": _delta(rank_delta, unit="spots"),
                "caption": "Avg page-1 rank across keywords", "available": True,
            }

    # ── Shelf Share: my tracked SKUs' page-1 slots ÷ the whole page-1 shelf.
    share_card = {"value": None, "delta": _delta(None), "caption": "Share of page 1",
                  "available": False}
    if has_data:
        shelf = ci_analysis.monitoring_tracked_shelf_share(conn, group_id, period)
        share_card = {
            "value": f"{shelf['share']}%", "delta": _delta(shelf["delta"]),
            "caption": "Total share of page 1", "available": True,
        }

    # ── Content Readiness: average latest overall score of the brand's products.
    read_card = {"value": None, "delta": _delta(None), "caption": "Across portfolio",
                 "available": False}
    cur = jobs.readiness_score(conn, user_id, mine_brands)
    if cur["avg"] is not None:
        r_delta = None
        if has_data:
            prior_end = ci_analysis.period_bounds(period, 1)[1]
            prior = jobs.readiness_score(conn, user_id, mine_brands,
                                         as_of=f"{prior_end} 23:59:59")
            if prior["avg"] is not None:
                r_delta = cur["avg"] - prior["avg"]
        read_card = {
            "value": f"{cur['avg']}", "delta": _delta(r_delta),
            "caption": f"{cur['scored_count']} product"
                       f"{'' if cur['scored_count'] == 1 else 's'} scored",
            "available": True,
        }

    return {"avg_rank": rank_card, "shelf_share": share_card,
            "content_readiness": read_card,
            "readiness_detail": cur}  # scored_count / needs_attention_count for highlights


def _mix(sos_rows: list[dict]) -> dict:
    """Organic-vs-paid split of the brand's own page-1 placements.

    Sums the mine rows' organic/sponsored shelf shares, then splits the two
    against each other (``organic_pct`` = organic ÷ (organic+sponsored)). Returns
    both the split and the raw shelf shares; all zero when the brand holds no shelf.
    """
    mine = _mine_rows(sos_rows)
    organic = round(sum(r["organic_share"] for r in mine), 2)
    sponsored = round(sum(r["sponsored_share"] for r in mine), 2)
    total = organic + sponsored
    return {
        "organic_share": organic, "sponsored_share": sponsored,
        "organic_pct": round(organic / total * 100) if total else 0,
        "sponsored_pct": round(sponsored / total * 100) if total else 0,
    }


def _no_rank_keywords(share_rows: list[dict]) -> list[str]:
    """Measured keywords where none of my tracked SKUs hold a page-1 slot."""
    measured = {r["keyword"] for r in share_rows}
    present = {r["keyword"] for r in share_rows
              if r.get("type") == "mine" and r.get("total_share", 0) > 0}
    return sorted(measured - present)


def _competitor_led_keywords(share_rows: list[dict]) -> list[dict]:
    """Keywords where a competitor holds more total shelf share than my brand.

    Returns ``[{keyword, leader, leader_share, mine_share}]`` worst-gap first —
    the raw material for both an Attention highlight and a recommendation.
    """
    by_kw: dict[str, dict] = {}
    for r in share_rows:
        kw = r["keyword"]
        slot = by_kw.setdefault(kw, {"mine": 0.0, "leader": None, "leader_share": 0.0})
        if r.get("type") == "mine":
            slot["mine"] = max(slot["mine"], r.get("total_share", 0))
        else:
            if r.get("total_share", 0) > slot["leader_share"]:
                slot["leader_share"] = r.get("total_share", 0)
                slot["leader"] = r["brand_name"]
    led = [
        {"keyword": kw, "leader": v["leader"], "leader_share": v["leader_share"],
         "mine_share": v["mine"]}
        for kw, v in by_kw.items()
        if v["leader"] and v["leader_share"] > v["mine"]
    ]
    led.sort(key=lambda x: x["leader_share"] - x["mine_share"], reverse=True)
    return led


def _highlights(cards, discovery, readiness) -> dict:
    """Rule-based Wins / Attention bullets, derived from the assembled sections.

    Every bullet is backed by a real figure already in the report; nothing is
    invented. Both lists are capped so the slide stays scannable, and each falls
    back to a single neutral line when there's nothing notable to say.
    """
    wins: list[str] = []
    attention: list[str] = []

    rank, share, read = cards["avg_rank"], cards["shelf_share"], cards["content_readiness"]
    detail = cards["readiness_detail"]

    # Wins
    no_rank = discovery["no_rank_keywords"]
    measured = {r["keyword"] for r in discovery["share_rows"]}
    if measured and not no_rank:
        wins.append(f"Tracked products rank on page 1 for all {len(measured)} "
                    f"keyword{'' if len(measured) == 1 else 's'} — full coverage.")
    if rank["available"] and rank["delta"]["dir"] == "up":
        wins.append(f"Average search rank improved ({rank['delta']['display']}).")
    if share["available"] and share["delta"]["dir"] == "up":
        wins.append(f"Share of page 1 is up ({share['delta']['display']}).")
    for d in readiness["dimensions"]:
        if d["avg"] is not None and d["avg"] >= 90:
            wins.append(f"{d['label']} content is excellent ({d['avg']}/100) across the portfolio.")
    if read["available"] and detail["needs_attention_count"] == 0 and detail["scored_count"]:
        wins.append("All scored products are in good content shape.")

    # Attention
    if no_rank:
        shown = ", ".join(f'"{k}"' for k in no_rank[:4])
        more = f" (+{len(no_rank) - 4} more)" if len(no_rank) > 4 else ""
        attention.append(f"No page-1 ranking on {len(no_rank)} keyword"
                         f"{'' if len(no_rank) == 1 else 's'}: {shown}{more}.")
    led = discovery["competitor_led_keywords"]
    if led:
        kws = ", ".join(f'"{x["keyword"]}"' for x in led[:3])
        attention.append(f"A competitor leads the shelf on {kws}.")
    if share["available"] and share["delta"]["dir"] == "down":
        attention.append(f"Share of page 1 slipped ({share['delta']['display']}).")
    weak = [d for d in readiness["dimensions"]
            if d["avg"] is not None and d["avg"] < jobs.READINESS_STRONG_MIN]
    for d in weak:
        attention.append(f"{d['label']} averages {d['avg']}/100 — below the "
                         f"{jobs.READINESS_STRONG_MIN} target.")
    if read["available"] and detail["needs_attention_count"]:
        n = detail["needs_attention_count"]
        attention.append(f"{n} product{'' if n == 1 else 's'} ha"
                         f"{'s' if n == 1 else 've'} content scoring below target.")

    if not wins:
        wins.append("No standout wins this period — see the detail sections.")
    if not attention:
        attention.append("No pressing issues flagged this period.")
    return {"wins": wins[:6], "attention": attention[:6]}


def _insights(cards, discovery) -> list[dict]:
    """Two-to-four narrative callouts for the Discovery Key Insights slide.

    Each is ``{tone, headline, body}`` (tone: info|good|warn), mirroring the
    agency example's color-barred callouts. Built from the same figures as the
    highlights, phrased as a short analytical paragraph.
    """
    out: list[dict] = []
    mix = discovery["mix"]
    no_rank = discovery["no_rank_keywords"]
    measured = {r["keyword"] for r in discovery["share_rows"]}

    if measured and not no_rank:
        out.append({"tone": "good", "headline": "Full Keyword Coverage",
                    "body": f"Tracked products appear on page 1 for all "
                            f"{len(measured)} tracked keyword"
                            f"{'' if len(measured) == 1 else 's'} — complete visibility "
                            f"across the terms shoppers use."})
    elif no_rank:
        out.append({"tone": "warn", "headline": "Visibility Gaps",
                    "body": f"{len(no_rank)} tracked keyword"
                            f"{'' if len(no_rank) == 1 else 's'} return no page-1 "
                            f"placement for your products — the clearest near-term "
                            f"opportunity to win new shelf."})

    if mix["organic_share"] or mix["sponsored_share"]:
        ratio = (f"{mix['organic_pct']}% organic / {mix['sponsored_pct']}% paid")
        tone = "warn" if mix["sponsored_pct"] > mix["organic_pct"] else "info"
        out.append({"tone": tone, "headline": "Organic vs. Paid Mix",
                    "body": f"Your page-1 presence splits {ratio}. "
                            + ("Paid placements are carrying visibility — an opening to "
                               "build organic rank and reduce ad reliance."
                               if mix["sponsored_pct"] > mix["organic_pct"]
                               else "Organic placement is doing the heavy lifting, a "
                                    "durable position that doesn't depend on ad spend.")})

    led = discovery["competitor_led_keywords"]
    if led:
        top = led[0]
        out.append({"tone": "warn", "headline": "Competitive Pressure",
                    "body": f"{top['leader']} leads the shelf on \"{top['keyword']}\" "
                            f"({top['leader_share']}% vs your {top['mine_share']}%). "
                            f"Targeted placement here could close the gap."})

    if cards["avg_rank"]["available"]:
        d = cards["avg_rank"]["delta"]
        if d["dir"] == "up":
            out.append({"tone": "good", "headline": "Rankings Improving",
                        "body": f"Average search rank moved up this period "
                                f"({d['display']}), a sign content and placement work "
                                f"is compounding."})
    return out[:4]


def _priority_fixes(breakdown) -> list[dict]:
    """Listing-readiness fix rows from the content-gap breakdown.

    One row per gapped product/dimension: ``{item, issue, impact, action}``.
    Impact is High for a copy gap (copy drives indexing + conversion directly),
    Medium for an imagery gap — matching how the Opportunities area prioritizes.
    Worst scores first so the highest-leverage fixes lead.
    """
    fixes: list[dict] = []
    for it in breakdown["copy_items"]:
        # weak is worst-first; name the weakest copy dimension in the issue.
        worst = it["weak"][0]
        others = len(it["weak"]) - 1
        issue = f"{worst['label']} scores {worst['score']}/100"
        if others:
            issue += f" (+{others} more copy field{'' if others == 1 else 's'} below target)"
        fixes.append({
            "item": it["title"] or it["item_id"], "issue": issue, "impact": "High",
            "action": "Rewrite copy to lift the weak fields above target.",
        })
    for it in breakdown["image_items"]:
        fixes.append({
            "item": it["title"] or it["item_id"],
            "issue": f"Imagery scores {it['score']}/100",
            "impact": "Medium",
            "action": "Add/upgrade images (resolution, count, lifestyle coverage).",
        })
    # High impact first, then by nothing in particular (stable within group).
    fixes.sort(key=lambda f: 0 if f["impact"] == "High" else 1)
    return fixes


def _recommendations(cards, discovery, readiness, breakdown) -> list[dict]:
    """Prioritized strategic recommendations: ``[{priority, title, body}]``.

    Rule-based from the same gaps that drive Priority Fixes and the highlights,
    but phrased as initiatives (the "Action Plan" section), High→Low. Each is
    grounded in a figure from the report; an all-healthy brand gets a single
    "maintain" recommendation rather than an empty slide.
    """
    recs: list[dict] = []

    copy_n = breakdown["copy_gap_count"]
    if copy_n:
        recs.append({"priority": "High", "title": "Strengthen Product Copy",
                     "body": f"{copy_n} product{'' if copy_n == 1 else 's'} ha"
                             f"{'s' if copy_n == 1 else 've'} copy fields scoring below "
                             f"the {jobs.READINESS_STRONG_MIN}-point target. Rewriting "
                             f"titles, key features and descriptions improves search "
                             f"indexing and conversion — the highest-leverage fix."})

    led = discovery["competitor_led_keywords"]
    if led:
        kws = ", ".join(f'"{x["keyword"]}"' for x in led[:3])
        recs.append({"priority": "High", "title": "Win Back Contested Shelf",
                     "body": f"Competitors lead the page-1 shelf on {kws}. Prioritize "
                             f"organic optimization and targeted sponsored placement on "
                             f"these terms to recover share."})

    no_rank = discovery["no_rank_keywords"]
    if no_rank:
        kws = ", ".join(f'"{k}"' for k in no_rank[:3])
        recs.append({"priority": "Medium", "title": "Build Page-1 Presence",
                     "body": f"Your products don't rank on page 1 for {len(no_rank)} "
                             f"tracked keyword{'' if len(no_rank) == 1 else 's'} "
                             f"({kws}…). Sponsored placement plus keyword-aligned copy "
                             f"can establish an initial foothold."})

    img_n = breakdown["image_gap_count"]
    if img_n:
        recs.append({"priority": "Medium", "title": "Upgrade Product Imagery",
                     "body": f"{img_n} product{'' if img_n == 1 else 's'} fall"
                             f"{'s' if img_n == 1 else ''} short on imagery (resolution, "
                             f"count, or lifestyle coverage). Richer image sets lift both "
                             f"conversion and the content-readiness score."})

    if not recs:
        recs.append({"priority": "Low", "title": "Maintain Momentum",
                     "body": "No critical gaps this period. Hold the current content and "
                             "placement cadence, and keep monitoring for competitor moves."})
    return recs


def assemble_business_report(conn, user_id: int, group, period: str) -> dict:
    """Build the full report data dict for one brand (``group``) over ``period``.

    ``group`` is an already-ownership-validated CI group row/dict (the caller runs
    the IDOR check); ``period`` is a validated period key. Returns the single dict
    both renderers consume. Degrades gracefully: with no completed CI period,
    ``meta.has_data`` is False and the discovery sections are empty, but content
    readiness (which is independent of CI runs) still populates, so the report
    always builds.
    """
    group_id = group["id"]
    brands = ci_config.list_brands(conn, group_id, user_id)
    mine_brands = [b["name"] for b in brands if b["type"] == "mine"]
    competitor_brands = [b["name"] for b in brands if b["type"] != "mine"]
    keywords = [k["keyword"] for k in ci_config.list_keywords(conn, group_id, user_id)]
    products = _product_views(ci_config.list_products(conn, group_id, user_id))

    available = ci_analysis.available_periods(conn, group_id)
    has_data = period in available
    period_label = ci_analysis.period_label(period) if has_data else None
    prior_label = ci_analysis.period_label(period, 1) if has_data else None

    logger.info("Assembling business report user_id=%s group_id=%s period=%s has_data=%s",
                user_id, group_id, period, has_data)

    # ── Discovery (CI) sections — only when a completed period has data. ───────
    if has_data:
        avg_ranks = ci_analysis.monitoring_avg_rank(conn, group_id, period)
        rank_rows = ci_analysis.monitoring_rank_by_keyword(conn, group_id, period)
        sos_rows = ci_analysis.monitoring_share_of_shelf(conn, group_id, period)
        brand_sos_rows = ci_analysis.monitoring_brand_share_of_shelf(conn, group_id, period)
        share_rows = ci_analysis.monitoring_share_by_keyword(conn, group_id, period)
        rank_map = ci_analysis.monitoring_placement_map(conn, group_id, period, avg_ranks)
    else:
        avg_ranks = rank_rows = sos_rows = brand_sos_rows = share_rows = []
        rank_map = None

    discovery = {
        "avg_ranks": avg_ranks, "rank_rows": rank_rows, "rank_map": rank_map,
        "sos_rows": sos_rows, "brand_sos_rows": brand_sos_rows, "share_rows": share_rows,
        "mix": _mix(sos_rows),
        "no_rank_keywords": _no_rank_keywords(share_rows),
        "competitor_led_keywords": _competitor_led_keywords(share_rows),
    }

    # ── Content Readiness (independent of CI period data). ─────────────────────
    breakdown = jobs.readiness_breakdown(conn, user_id, mine_brands)
    dims = jobs.readiness_dimension_averages(conn, user_id, mine_brands)
    for d in dims:
        d["grade"] = grade(d["avg"])
    items = jobs.readiness_items(conn, user_id, mine_brands)
    for it in items:
        it["grade"] = grade(it["score"])
    overall = jobs.readiness_score(conn, user_id, mine_brands)
    readiness = {
        "overall": overall["avg"], "scored_count": overall["scored_count"],
        "needs_attention_count": overall["needs_attention_count"],
        "available": overall["avg"] is not None,
        "dimensions": dims, "items": items,
    }

    cards = _summary_cards(conn, user_id, group_id, period, mine_brands, has_data)
    discovery["insights"] = _insights(cards, discovery)
    highlights = _highlights(cards, discovery, readiness)
    priority_fixes = _priority_fixes(breakdown)
    recommendations = _recommendations(cards, discovery, readiness, breakdown)
    # Drop the internal detail blob before returning (highlights has consumed it).
    cards.pop("readiness_detail", None)

    return {
        "meta": {
            "brand_label": group["name"],
            "period": period, "period_label": period_label, "prior_label": prior_label,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "generated_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "has_data": has_data,
            "my_brands": mine_brands, "competitor_brands": competitor_brands,
            "keywords": keywords, "products": products, "product_count": len(products),
        },
        "cards": cards,
        "highlights": highlights,
        "discovery": discovery,
        "readiness": readiness,
        "priority_fixes": priority_fixes,
        "recommendations": recommendations,
    }
