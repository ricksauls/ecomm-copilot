"""Pluggable external providers for the image-set pipeline.

Each provider has a real implementation and an offline mock selected by env
(see :mod:`app.imageset.config`); all deal in raw image bytes so the pipeline
stays decoupled from any web or storage layer.
"""
