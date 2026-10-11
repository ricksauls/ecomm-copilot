"""Import Product Images — acquire product imagery that already exists on other
sites (Walmart PDPs, direct/Amazon image URLs) by URL or CSV, resolve each to its
highest native resolution, upscale only when it's below spec, and bundle the result.

A Creative Content Studio feature. Handles Walmart product pages (reusing the
existing scraper) and Amazon product pages — both rendered through the residential
proxy — plus direct/Amazon image URLs. The gallery hotlinks thumbnails; a full-
resolution file is downloaded only when the user selects it (no pre-download).
"""
