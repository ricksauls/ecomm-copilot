"""Seasonal Content Campaigns.

Bulk seasonal creative: a campaign pairs a season/theme with a set of products
(added by Walmart/Amazon URL). The user picks images from each product's acquired
gallery, and the pipeline composites every chosen image onto a fresh AI-generated
seasonal backdrop — the same compositor the PDP Image Set tool uses. Create-and-
download only; nothing is published to a marketplace.

Module map:
- :mod:`app.seasonal.themes`    — the season presets + the backdrop prompt builder.
- :mod:`app.seasonal.storage`   — on-disk artifact paths under MEDIA_DIR/seasonal/.
- :mod:`app.seasonal.store`     — the data layer (campaigns, products, images) + the
  two worker queues' status helpers, all IDOR-scoped by ``user_id``.
- :mod:`app.seasonal.jobs`      — the GENERATE queue (one job per selected image).
- :mod:`app.seasonal.generate`  — the acquire + generation pipelines the worker runs.
"""
