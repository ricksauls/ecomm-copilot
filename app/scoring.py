"""Rule-based PDP content scorer.

Takes a normalized :class:`PdpRecord` (however it was sourced — see the fetch
layer) and produces a 0-100 :class:`ScoreResult` decomposed across five weighted
dimensions, each with plain-language findings and recommendations that map to a
sellable fix.

This is the deterministic half of the model on purpose: every point is traceable
to a rule, so the score is explainable and stable. The qualitative half —
infographic/lifestyle image detection (vision), keyword/SEO coverage, and
category-specific attribute schemas — is layered on later by the AI pass; where a
signal isn't available to the rules yet, that is called out in the findings
rather than silently guessed.
"""

import logging
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

# Dimension weights (sum to 100). Starting point — meant to be recalibrated by
# regressing dimensions against real search-rank / conversion outcomes.
WEIGHTS = {
    "imagery": 25,
    "attributes": 20,
    "title": 18,
    "key_features": 18,
    "description": 19,
}


@dataclass
class PdpRecord:
    """Normalized PDP content, independent of how it was fetched.

    Fields default to "absent" so the scorer degrades gracefully on partial
    data. ``None`` on a boolean means "unknown to the rules" (e.g. white-bg
    detection needs vision), which the scorer treats as neutral rather than a
    failure.
    """

    url: str
    item_id: str | None = None
    title: str = ""
    # Product brand as read from the PDP (``product.brand`` in __NEXT_DATA__).
    # Empty when the page didn't expose one; the scorer doesn't use it, but the
    # job store persists it for the dashboard's distinct-brand count.
    brand: str = ""
    image_count: int = 0
    max_image_px: int = 0  # largest edge across all images, in pixels
    # Per-image resolution, one dict per gallery image in gallery order:
    # ``{"url": str, "px": int}`` where px is that image's largest edge (0 = it
    # couldn't be measured). Lets imagery grade *every* image and name the ones
    # below spec; empty when per-image measurement didn't run (older results,
    # unit tests), in which case imagery falls back to ``max_image_px``.
    image_dims: list[dict] = field(default_factory=list)
    has_video: bool = False
    bullets: list[str] = field(default_factory=list)
    description: str = ""
    attributes_present: int = 0
    # Whether attribute completeness could actually be measured. Walmart lazy-
    # loads the spec table, so until DOM extraction is built this is False and
    # the attributes dimension is excluded from the overall rather than scored 0
    # (which would unfairly depress every real score).
    attributes_measured: bool = False
    # The ranked target keyword set for this item (from app.keywords discovery).
    # ``None`` means keyword coverage wasn't evaluated (e.g. discovery didn't run
    # or is untested) and the title/description dimensions score on their other
    # signals alone; a list (possibly empty) means it was measured.
    target_keywords: list[str] | None = None
    # Whether the main image is a product on a pure white background (Walmart's
    # main-image requirement), measured by border-pixel analysis in the fetch
    # layer. ``None`` means it wasn't measured, so imagery scores without it.
    main_image_white_bg: bool | None = None
    # URL of the PDP's main image, carried through so the worker can cache a local
    # thumbnail (same-origin, CSP-safe) for the dashboard's activity tables. The
    # scorer doesn't use it; ``None`` when the page exposed no images.
    main_image_url: str | None = None


@dataclass
class DimensionScore:
    """One dimension's 0-100 score plus its weight and human-readable notes."""

    key: str
    label: str
    score: int
    weight: int
    findings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    # False when the signal can't be measured yet; excluded from the overall.
    available: bool = True
    # Imagery only: specific images flagged for an issue (resolution below spec),
    # each ``{"index": int, "url": str, "px": int, "severity": "low"|"mid"}`` so
    # the results UI can point the user at exactly which image to re-export. Empty
    # for every other dimension.
    image_issues: list[dict] = field(default_factory=list)
    # Imagery only: the main image URL when it fails the pure-white-background
    # check (a Walmart main-image requirement), so the results UI can offer a
    # one-click fix. ``None`` when the check passed or wasn't measured.
    white_bg_url: str | None = None


@dataclass
class ScoreResult:
    """Overall 0-100 score and the per-dimension breakdown."""

    overall: int
    dimensions: list[DimensionScore]


def _clamp(value: int) -> int:
    """Clamp a raw points total into the 0-100 range."""
    return max(0, min(100, value))


# Share of the Title and Description dimensions given to keyword coverage when a
# target keyword set is available; the rest stays with the format/depth signals.
# Blending (rather than adding raw points) keeps each dimension on a 0-100 range
# whether or not keywords were measured.
_KEYWORD_BLEND = 0.30

# Share of the Imagery dimension given to the white-background check on the main
# image (a Walmart main-image requirement) when it's been measured; blended so
# imagery stays 0-100 whether or not it was checked.
_WHITE_BG_BLEND = 0.20


def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
    """Return the target keywords that appear (case-insensitive) in ``text``."""
    lowered = (text or "").lower()
    return [kw for kw in keywords if kw.lower() in lowered]


def _title_keyword_subscore(hit_count: int) -> int:
    """0-100 keyword score for a title (few, high-value terms expected).

    Tiers mirror the WM tool's SEO scorer: a title realistically holds 2-3 target
    terms, so 3+ is full marks.
    """
    return {0: 0, 1: 45, 2: 75}.get(hit_count, 100)


def _description_keyword_subscore(hit_count: int) -> int:
    """0-100 keyword score for a description (room for broader coverage)."""
    if hit_count >= 6:
        return 100
    if hit_count >= 3:
        return 70
    if hit_count >= 1:
        return 35
    return 0


# Resolution tiers, largest edge in px. Walmart's image zoom engages at ~2000px;
# below 1000px is too small to be useful. _RES_POINTS is the imagery points the
# resolution component can earn (the rest come from image count).
_RES_ZOOM_PX = 2000
_RES_MIN_PX = 1000
_RES_POINTS = 40


def _resolution_points(
    pdp: PdpRecord,
    findings: list[str],
    recs: list[str],
    image_issues: list[dict],
) -> int:
    """Resolution component of the imagery score; mutates the note lists.

    When per-image measurements exist (``pdp.image_dims``), grade *every* image:
    each zoom-ready image (>=2000px) earns full credit, each 1000-1999px earns
    half, each below 1000px none — so a single hero image can't mask a gallery of
    small ones. Every sub-2000px image is recorded in ``image_issues`` (with its
    gallery position) so the results page can point the user at exactly which one
    to re-export. Unmeasurable images (px 0 — the bytes couldn't be fetched) are
    neither graded nor flagged.

    Falls back to the single largest-edge tier when no per-image data is present
    (results scored before this change, or a run where measurement didn't happen).
    """
    measured = [d for d in pdp.image_dims if d.get("px")]
    if not measured:
        if pdp.max_image_px >= _RES_ZOOM_PX:
            findings.append("Zoom-ready resolution (2000px+)")
            return _RES_POINTS
        if pdp.max_image_px >= _RES_MIN_PX:
            findings.append("Images below the 2000px zoom recommendation")
            recs.append("Re-export images at 2000x2000 so Walmart zoom engages")
            return _RES_POINTS // 2
        recs.append("Provide 2000x2000 images for zoom")
        return 0

    total = len(measured)
    zoom_ready = sum(1 for d in measured if d["px"] >= _RES_ZOOM_PX)
    partial = sum(1 for d in measured if _RES_MIN_PX <= d["px"] < _RES_ZOOM_PX)
    credit = (zoom_ready + 0.5 * partial) / total

    if zoom_ready == total:
        findings.append(f"All {total} images are zoom-ready (2000px+)")
    else:
        findings.append(f"{zoom_ready} of {total} images are zoom-ready (2000px+)")

    # Flag each sub-spec image by its gallery position (1-based) so the user can
    # tell which to address; unmeasured images (px 0) are skipped.
    for i, d in enumerate(pdp.image_dims, start=1):
        px = d.get("px") or 0
        if 0 < px < _RES_ZOOM_PX:
            image_issues.append({
                "index": i,
                "url": d.get("url", ""),
                "px": px,
                "severity": "low" if px < _RES_MIN_PX else "mid",
            })
    if image_issues:
        recs.append("Re-export the flagged image(s) at 2000x2000 so Walmart zoom engages")

    return round(_RES_POINTS * credit)


def _score_imagery(pdp: PdpRecord) -> DimensionScore:
    """Image count and zoom-ready resolution.

    Video is intentionally NOT scored right now (product decision, 2026-08-21) —
    see the commented block below for how to re-enable it. The count/resolution
    tiers were rescaled from 50/30 to 60/40 so imagery still spans a full 0-100
    without the video points; restore the old tiers if video comes back.
    Infographic / lifestyle detection needs vision (the future AI pass) and is
    not scored or surfaced here.
    """
    findings: list[str] = []
    recs: list[str] = []
    image_issues: list[dict] = []
    white_bg_url: str | None = None
    points = 0

    n = pdp.image_count
    if n >= 6:
        points += 60
        findings.append(f"{n} images (rich gallery)")
    elif n >= 4:
        points += 40
        findings.append(f"{n} images")
        recs.append("Add more product images to reach 6+")
    elif n >= 2:
        points += 20
        findings.append(f"Only {n} images")
        recs.append("Build out the gallery to 6+ images")
    else:
        findings.append("1 or no images")
        recs.append("Add a full image set (6+ product images)")

    points += _resolution_points(pdp, findings, recs, image_issues)

    # Video scoring is paused for now. `pdp.has_video` is still detected in the
    # fetch layer, so re-enabling is just uncommenting this and reverting the
    # image/resolution tiers above to 50/35/18 and 30/15 (video was worth 20).
    # if pdp.has_video:
    #     points += 20
    #     findings.append("Has video")
    # else:
    #     recs.append("Add a short product video")

    base_score = _clamp(points)

    # Main-image white background: blend it in when measured, so a non-compliant
    # main image is penalized against a Walmart requirement. Absent the check
    # (unit tests, or the image couldn't be fetched), imagery scores on
    # count/resolution alone and still reaches 100.
    if pdp.main_image_white_bg is None:
        return DimensionScore("imagery", "Imagery", base_score, WEIGHTS["imagery"],
                              findings, recs, image_issues=image_issues)

    white_score = 100 if pdp.main_image_white_bg else 0
    score = round(base_score * (1 - _WHITE_BG_BLEND) + white_score * _WHITE_BG_BLEND)
    if pdp.main_image_white_bg:
        findings.append("Main image on a clean white background")
    else:
        recs.append(
            "Set the main image to the product on a pure white background "
            "(a Walmart main-image requirement)"
        )
        # Surface the main image so the results UI can offer a one-click fix.
        white_bg_url = pdp.main_image_url
    return DimensionScore("imagery", "Imagery", score, WEIGHTS["imagery"],
                          findings, recs, image_issues=image_issues, white_bg_url=white_bg_url)


def _score_attributes(pdp: PdpRecord) -> DimensionScore:
    """Attribute/spec completeness (a discoverability lever via filters).

    Scored on a count proxy until the per-category expected-attribute schema is
    wired in, which is when this becomes a true percentage.
    """
    findings: list[str] = []
    recs: list[str] = []
    n = pdp.attributes_present

    # Walmart lazy-loads the spec table, so we can't measure this yet. Mark the
    # dimension unavailable so score_pdp leaves it out of the overall.
    if not pdp.attributes_measured:
        return DimensionScore(
            "attributes",
            "Attributes",
            0,
            WEIGHTS["attributes"],
            ["Attribute completeness not yet measured (spec extraction pending)"],
            [],
            available=False,
        )

    if n >= 15:
        score = 100
        findings.append(f"{n} attributes populated")
    elif n >= 10:
        score = 75
        findings.append(f"{n} attributes populated")
        recs.append("Fill remaining category attributes to power more search filters")
    elif n >= 5:
        score = 50
        findings.append(f"Only {n} attributes populated")
        recs.append("Populate the full category attribute set")
    elif n >= 1:
        score = 25
        findings.append(f"Only {n} attributes populated")
        recs.append("Populate the full category attribute set — a major discoverability gap")
    else:
        score = 0
        recs.append("No attributes populated — fill the category spec sheet")

    findings.append("Category-specific completeness % scored once the schema is wired in")
    return DimensionScore(
        "attributes", "Attributes", score, WEIGHTS["attributes"], findings, recs
    )


def _score_title(pdp: PdpRecord) -> DimensionScore:
    """Title format signals: length band, not ALL CAPS, enough descriptors.

    Keyword/SEO coverage is scored in the AI pass against a built keyword set.
    """
    findings: list[str] = []
    recs: list[str] = []
    points = 0
    title = pdp.title.strip()
    length = len(title)
    words = len(title.split())

    if 50 <= length <= 75:
        points += 50
        findings.append(f"Title length {length} chars (in range)")
    elif 40 <= length <= 100:
        points += 30
        findings.append(f"Title length {length} chars (outside the 50-75 sweet spot)")
        recs.append("Tighten the title toward 50-75 characters")
    else:
        points += 10
        findings.append(f"Title length {length} chars (well outside 50-75)")
        recs.append("Rewrite the title to 50-75 characters, Brand + Name + key attribute")

    # ALL CAPS check: only meaningful when there are letters to judge.
    letters = [c for c in title if c.isalpha()]
    if letters and title == title.upper():
        findings.append("Title is ALL CAPS")
        recs.append("Use title case, not ALL CAPS (Walmart discourages it)")
    else:
        points += 25

    if words >= 5:
        points += 25
        findings.append(f"{words} words (enough descriptors)")
    else:
        findings.append(f"Only {words} words")
        recs.append("Add brand, key attributes, size/pack to the title")

    format_score = _clamp(points)

    # Keyword coverage: blend in how many target search terms the title carries,
    # when a keyword set was discovered. Absent one, the title scores on format
    # alone (unit tests, or discovery skipped/failed).
    if pdp.target_keywords is None:
        return DimensionScore("title", "Title", format_score, WEIGHTS["title"], findings, recs)

    hits = _keyword_hits(title, pdp.target_keywords)
    score = round(format_score * (1 - _KEYWORD_BLEND)
                  + _title_keyword_subscore(len(hits)) * _KEYWORD_BLEND)
    if hits:
        findings.append(f"{len(hits)} target keyword(s) in title: {', '.join(hits[:5])}")
    else:
        findings.append("No target keywords in the title")
        recs.append("Work 2-3 high-value search terms into the title")
    return DimensionScore("title", "Title", score, WEIGHTS["title"], findings, recs)


def _score_key_features(pdp: PdpRecord) -> DimensionScore:
    """Bullet count and length; benefit-vs-feature quality is an AI-pass signal."""
    findings: list[str] = []
    recs: list[str] = []
    points = 0
    bullets = [b.strip() for b in pdp.bullets if b and b.strip()]
    n = len(bullets)

    if 3 <= n <= 10:
        points += 55
        findings.append(f"{n} key features")
    elif n in (1, 2):
        points += 25
        findings.append(f"Only {n} key features")
        recs.append("Expand to 3-10 benefit-led bullets")
    else:
        findings.append("No key features")
        recs.append("Add 3-10 benefit-led bullets")

    if bullets:
        avg = sum(len(b) for b in bullets) / n
        if 30 <= avg <= 180:
            points += 25
            findings.append("Bullet length is substantive")
        else:
            recs.append("Aim for ~1 line per bullet (roughly 30-180 characters)")
        # Reward having at least a few genuinely descriptive bullets.
        if sum(1 for b in bullets if len(b) >= 30) >= 3:
            points += 20

    findings.append("Benefit-vs-feature quality and keywords scored in the AI pass")
    return DimensionScore(
        "key_features", "Key features", _clamp(points), WEIGHTS["key_features"], findings, recs
    )


def _score_description(pdp: PdpRecord) -> DimensionScore:
    """Description depth by word count; SEO richness is an AI-pass signal."""
    findings: list[str] = []
    recs: list[str] = []
    points = 0
    words = len(pdp.description.split())

    if words >= 150:
        points += 70
        findings.append(f"{words}-word description")
    elif words >= 80:
        points += 45
        findings.append(f"{words}-word description (below the ~150 recommendation)")
        recs.append("Expand the description to 150+ words")
    elif words >= 1:
        points += 20
        findings.append(f"Only {words} words")
        recs.append("Write a full 150+ word description")
    else:
        recs.append("Add a description (150+ words, scannable, keyword-rich)")

    if words >= 300:
        points += 30
        findings.append("Rich, in-depth copy")
    elif words >= 150:
        points += 15

    depth_score = _clamp(points)

    # SEO/keyword depth: blend in how many target search terms the description
    # works in, when a keyword set was discovered. Absent one, score on depth.
    if pdp.target_keywords is None:
        return DimensionScore(
            "description", "Description", depth_score, WEIGHTS["description"], findings, recs
        )

    hits = _keyword_hits(pdp.description, pdp.target_keywords)
    score = round(depth_score * (1 - _KEYWORD_BLEND)
                  + _description_keyword_subscore(len(hits)) * _KEYWORD_BLEND)
    if hits:
        findings.append(f"{len(hits)} target keyword(s) in the description")
    else:
        findings.append("No target keywords in the description")
        recs.append("Weave the top search terms into the description naturally")
    return DimensionScore(
        "description", "Description", score, WEIGHTS["description"], findings, recs
    )


def score_pdp(pdp: PdpRecord) -> ScoreResult:
    """Score a PDP across all five dimensions and compute the weighted overall.

    Pure and deterministic: same record in, same score out.
    """
    dimensions = [
        _score_imagery(pdp),
        # Attributes scoring is paused (product decision, 2026-08-21). The spec
        # extraction + record fields (attributes_present/measured) still run in
        # the fetch layer, so re-enabling is just uncommenting this line — the
        # weighting below already normalizes over whatever dimensions are present.
        # _score_attributes(pdp),
        _score_title(pdp),
        _score_key_features(pdp),
        _score_description(pdp),
    ]
    # Only measurable dimensions count toward the overall, so an unmeasurable
    # one doesn't unfairly drag the score down. The overall is normalized over
    # the present dimensions' weights, so dropping one (e.g. paused attributes)
    # rescales the rest automatically.
    scored = [d for d in dimensions if d.available]
    total_weight = sum(d.weight for d in scored) or 1
    overall = round(sum(d.score * d.weight for d in scored) / total_weight)
    logger.info("Scored PDP item_id=%s overall=%d", pdp.item_id, overall)
    return ScoreResult(overall=overall, dimensions=dimensions)


def result_to_dict(result: ScoreResult) -> dict:
    """Serialize a ScoreResult to a plain dict for JSON storage/rendering."""
    return asdict(result)
