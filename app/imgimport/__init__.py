"""Import Product Images — acquire product imagery that already exists on other
sites (Walmart PDPs, direct/Amazon image URLs) by URL or CSV, resolve each to its
highest native resolution, upscale only when it's below spec, and bundle the result.

A Creative Content Studio feature. Phase 1 handles Walmart product pages (reusing
the existing scraper), direct image URLs, and Amazon *image* URLs (CDN); Amazon
*product-page* extraction is Phase 2 and needs a residential proxy, so it's rejected
here with a clear message rather than failing opaquely.
"""
