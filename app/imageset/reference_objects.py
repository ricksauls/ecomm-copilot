"""Everyday reference objects for the size-comparison asset.

A small library of common items with known real-world sizes, used to give
shoppers a familiar visual scale next to the product. Selection picks the objects
closest in size to the product so the comparison reads as "similar-sized items."
Python port of the source app's ``src/lib/reference-objects.ts`` (same library,
same closest-by-log-ratio selection, so DISCOtech matches its output).
"""

import logging
import math

logger = logging.getLogger(__name__)

# Common, universally recognizable objects spanning a broad size range. ``inches``
# is the object's characteristic long dimension (height as typically shown).
REFERENCE_OBJECTS: list[dict] = [
    {"name": "AA battery", "inches": 1.99},
    {"name": "golf ball", "inches": 1.68},
    {"name": "credit card", "inches": 3.37},
    {"name": "coffee mug", "inches": 3.8},
    {"name": "soda can", "inches": 4.83},
    {"name": "smartphone", "inches": 5.8},
    {"name": "TV remote", "inches": 6.5},
    {"name": "banana", "inches": 7.0},
    {"name": "paperback book", "inches": 7.8},
    {"name": "water bottle", "inches": 8.0},
    {"name": "wine bottle", "inches": 11.8},
    {"name": "12-inch ruler", "inches": 12.0},
]

# Conversion to inches for comparison against the reference library.
_INCHES_PER_UNIT = {"in": 1.0, "cm": 1 / 2.54, "mm": 1 / 25.4}

# An object within ~2% of the product's size reads as "the same thing," which
# defeats the reference, so it is skipped (log-ratio threshold, matches source).
_NEAR_IDENTICAL = math.log(1.02)


def to_inches(value: float, unit: str) -> float:
    """Convert a measurement to inches; unknown units are treated as inches."""
    return value * _INCHES_PER_UNIT.get((unit or "in").lower(), 1.0)


def select_reference_objects(product_value: float, unit: str, count: int = 2) -> list[dict]:
    """Pick up to ``count`` reference objects closest in size to the product.

    Ranked by absolute log scale-ratio (so a 2× difference counts the same whether
    the object is larger or smaller), skipping any within ~2% of the product's size.
    Returns an empty list for a non-positive size (no scale anchor).
    """
    try:
        product_inches = to_inches(float(product_value), unit)
    except (TypeError, ValueError):
        return []
    if not product_inches > 0:
        return []
    ranked = sorted(
        (
            {"name": o["name"], "ratio": abs(math.log(o["inches"] / product_inches))}
            for o in REFERENCE_OBJECTS
        ),
        key=lambda r: r["ratio"],
    )
    picked = [r["name"] for r in ranked if r["ratio"] > _NEAR_IDENTICAL][: max(0, count)]
    logger.debug("Reference objects for %.2f in: %s", product_inches, picked)
    return [{"name": n} for n in picked]


def reference_phrases(product_value: float, unit: str, count: int = 2) -> tuple[list[str], list[str]]:
    """Return (prompt_phrases, names) for the chosen reference objects.

    ``prompt_phrases`` read like "a paperback book (about 7.8 in tall)" for the AI
    prompt; ``names`` ("paperback book") are for the human-facing scale caption.
    """
    unit = unit or "in"
    phrases, names = [], []
    for obj in select_reference_objects(product_value, unit, count):
        name = obj["name"]
        spec = next((o for o in REFERENCE_OBJECTS if o["name"] == name), None)
        article = "an" if name[:1].lower() in "aeiou" else "a"
        if spec:
            phrases.append(f"{article} {name} (about {spec['inches']:g} in tall)")
        else:
            phrases.append(f"{article} {name}")
        names.append(name)
    return phrases, names
