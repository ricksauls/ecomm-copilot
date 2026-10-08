"""PDP Image Set Creation.

A Python port of the marketplace-creative-studio pipeline: from one approved
product (facts + a photo) it produces a set of marketplace creative assets —
AI-generated scenes composited with the real product cutout and programmatic
copy. Every external provider (image generation, background removal, planning)
is pluggable and defaults to an offline mock, so the feature is inert until an
operator supplies keys, and the whole pipeline is testable with none.
"""
