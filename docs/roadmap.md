# DISCOtech (ecomm-copilot) — Roadmap

## Pre-Production Hardening

Items that are acceptable in the current solo / light-testing stage but should be
revisited **before real users run a production version of the app**. Each notes
the trigger that should make us act.

### Move the Acquire-Images ZIP download to a background worker job
- **Status:** deferred (noted session 22, 2026-10-11). Intentionally not changing the
  code now — current synchronous path is fine for small selections.
- **Where:** `import_download_zip()` in `app/routes/pages.py` (and the single-file
  `import_asset_download()` which shares `_maybe_upscale`).
- **What's there today:** the Download-ZIP web request fetches each selected full-res
  image and (when under the 2000px target) upscales it via Claid **synchronously, inside
  the gunicorn request**, then streams the finished ZIP. For a handful of images this is
  fine.
- **The risk at scale:** a large multi-image selection means many sequential network
  fetches + paid Claid calls in one request. That can (a) exceed the gunicorn/nginx
  request timeout → the user gets a failed/truncated download, (b) tie up a gunicorn
  worker for the whole duration → on the small (~2GB) droplet, that can stall the site
  for other requests, and (c) run up unbounded Claid spend with no preflight.
- **The fix when we act:** reuse the app's existing background-worker + queue pattern
  (the same "enqueue + poll, no web-request timeout" model already used for image-set
  creation — see the comment near `pages.py` ~line 2312). The Download click enqueues a
  ZIP job and returns immediately to a polling page; the `ecomm-copilot-worker` service
  builds the ZIP to `$MEDIA_DIR` at its own pace and marks it done; the poll page then
  offers the finished file. This also enables a cost-preflight ("N images, ~$X to
  upscale — proceed?") before the job runs.
- **Trigger to revisit:** approaching a production launch, OR the first report of a slow
  / failed large ZIP download, whichever comes first.
