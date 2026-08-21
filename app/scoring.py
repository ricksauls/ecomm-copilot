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
    image_count: int = 0
    max_image_px: int = 0  # largest edge across all images, in pixels
    has_video: bool = False
    bullets: list[str] = field(default_factory=list)
    description: str = ""
    attributes_present: int = 0


@dataclass
class DimensionScore:
    """One dimension's 0-100 score plus its weight and human-readable notes."""

    key: str
    label: str
    score: int
    weight: int
    findings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


@dataclass
class ScoreResult:
    """Overall 0-100 score and the per-dimension breakdown."""

    overall: int
    dimensions: list[DimensionScore]


def _clamp(value: int) -> int:
    """Clamp a raw points total into the 0-100 range."""
    return max(0, min(100, value))


def _score_imagery(pdp: PdpRecord) -> DimensionScore:
    """Image count, zoom-ready resolution, and video presence.

    Infographic / lifestyle detection needs vision, so it's flagged as a
    follow-up rather than scored here.
    """
    findings: list[str] = []
    recs: list[str] = []
    points = 0

    n = pdp.image_count
    if n >= 6:
        points += 50
        findings.append(f"{n} images (rich gallery)")
    elif n >= 4:
        points += 35
        findings.append(f"{n} images")
        recs.append("Add images to reach 6+ (infographics, lifestyle, dimensions)")
    elif n >= 2:
        points += 18
        findings.append(f"Only {n} images")
        recs.append("Build out the gallery to 6+ images")
    else:
        findings.append("1 or no images")
        recs.append("Add a full image set (6+): main, infographics, lifestyle, dimensions")

    if pdp.max_image_px >= 2000:
        points += 30
        findings.append("Zoom-ready resolution (2000px+)")
    elif pdp.max_image_px >= 1000:
        points += 15
        findings.append("Images below the 2000px zoom recommendation")
        recs.append("Re-export images at 2000x2000 so Walmart zoom engages")
    else:
        recs.append("Provide 2000x2000 images for zoom")

    if pdp.has_video:
        points += 20
        findings.append("Has video")
    else:
        recs.append("Add a short product video")

    findings.append("Infographic/lifestyle mix scored in the AI vision pass")
    return DimensionScore("imagery", "Imagery", _clamp(points), WEIGHTS["imagery"], findings, recs)


def _score_attributes(pdp: PdpRecord) -> DimensionScore:
    """Attribute/spec completeness (a discoverability lever via filters).

    Scored on a count proxy until the per-category expected-attribute schema is
    wired in, which is when this becomes a true percentage.
    """
    findings: list[str] = []
    recs: list[str] = []
    n = pdp.attributes_present

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

    findings.append("Keyword coverage scored in the AI/SEO pass")
    return DimensionScore("title", "Title", _clamp(points), WEIGHTS["title"], findings, recs)


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

    findings.append("Structure and keyword/SEO depth scored in the AI pass")
    return DimensionScore(
        "description", "Description", _clamp(points), WEIGHTS["description"], findings, recs
    )


def score_pdp(pdp: PdpRecord) -> ScoreResult:
    """Score a PDP across all five dimensions and compute the weighted overall.

    Pure and deterministic: same record in, same score out.
    """
    dimensions = [
        _score_imagery(pdp),
        _score_attributes(pdp),
        _score_title(pdp),
        _score_key_features(pdp),
        _score_description(pdp),
    ]
    total_weight = sum(d.weight for d in dimensions)
    overall = round(sum(d.score * d.weight for d in dimensions) / total_weight)
    logger.info("Scored PDP item_id=%s overall=%d", pdp.item_id, overall)
    return ScoreResult(overall=overall, dimensions=dimensions)


def result_to_dict(result: ScoreResult) -> dict:
    """Serialize a ScoreResult to a plain dict for JSON storage/rendering."""
    return asdict(result)
