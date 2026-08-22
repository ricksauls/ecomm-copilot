#!/usr/bin/env bash
#
# One-time privileged setup for ecomm-copilot on the wm-content-tools droplet.
# Run once, as root:
#
#   sudo bash ~/apps/ecomm-copilot/deploy/setup-droplet.sh
#
# Does the four things that need root: install the systemd unit, install the
# nginx site, add a tightly-scoped sudoers line (passwordless restart of ONLY
# this service, so CI deploys can restart it), and start the service. It is
# idempotent — safe to re-run. TLS (certbot) is a separate step, done after DNS
# points at this droplet (see deploy/DEPLOY.md).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "This script must run as root (use: sudo bash $0)" >&2
  exit 1
fi

# Resolve the repo directory from this script's location, so paths are correct
# regardless of where it's invoked from.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="deploy"

echo "==> App directory: $APP_DIR"

# Guard: the unprivileged prep (clone, venv, .env) must already be done.
if [[ ! -x "$APP_DIR/venv/bin/gunicorn" ]]; then
  echo "ERROR: $APP_DIR/venv/bin/gunicorn not found. Run the unprivileged" >&2
  echo "       setup (venv + pip install) as the deploy user first." >&2
  exit 1
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "ERROR: $APP_DIR/.env not found. Create it (with SECRET_KEY) first." >&2
  exit 1
fi

echo "==> Installing systemd units (web + scoring worker + CI monitoring timers)"
install -m 644 "$APP_DIR/deploy/ecomm-copilot.service" \
  /etc/systemd/system/ecomm-copilot.service
install -m 644 "$APP_DIR/deploy/ecomm-copilot-worker.service" \
  /etc/systemd/system/ecomm-copilot-worker.service
# Competitive Intelligence monitoring: a templated oneshot + three timers that
# enqueue a monitoring run at 7AM/3PM/11PM CST. The worker (above) does the
# actual scraping, so no extra always-on process or display is needed.
install -m 644 "$APP_DIR/deploy/ecomm-copilot-ci-monitor@.service" \
  /etc/systemd/system/ecomm-copilot-ci-monitor@.service
for slot in morning afternoon night; do
  install -m 644 "$APP_DIR/deploy/ecomm-copilot-ci-$slot.timer" \
    "/etc/systemd/system/ecomm-copilot-ci-$slot.timer"
done
systemctl daemon-reload

# The worker drives headed Chrome and needs the Xvfb :99 display. Warn (don't
# fail) if xvfb.service isn't present — it ships with the WM scraper setup.
if ! systemctl list-unit-files | grep -q '^xvfb.service'; then
  echo "WARNING: xvfb.service not found. The scoring worker needs a virtual" >&2
  echo "         display on :99 (see the WM scraper's xvfb.service)." >&2
fi

echo "==> Installing nginx site"
NGINX_SITE=/etc/nginx/sites-available/ecomm-copilot
# CRITICAL: certbot --nginx rewrites this same file in place to add the TLS (443)
# server block. Overwriting it with the plain-HTTP template on a re-run would
# silently drop HTTPS — nginx then serves the default server's cert for
# ecomm-copilot.com and browsers show ERR_CERT_COMMON_NAME_INVALID. So only lay
# down the template when no TLS block exists yet (first run); otherwise leave the
# certbot-managed config untouched. This is what makes the script truly
# idempotent post-certbot.
if [[ -f "$NGINX_SITE" ]] && grep -qE 'listen[^;]*443' "$NGINX_SITE"; then
  echo "    TLS (443) block already present — leaving nginx site untouched."
else
  install -m 644 "$APP_DIR/deploy/nginx.conf" "$NGINX_SITE"
fi
ln -sf /etc/nginx/sites-available/ecomm-copilot \
  /etc/nginx/sites-enabled/ecomm-copilot

# nginx (www-data) serves /static/ directly from the app dir under
# /home/deploy. Home dirs are drwxr-x--- (750) by default, so www-data can't
# traverse in and static assets 403. Add ONLY world-execute (search) on the
# home dir — not read — so nginx can reach the static tree while .env (600) and
# the DB stay unreadable to others.
chmod o+x /home/deploy

echo "==> Installing scoped sudoers line for CI restart"
# Validate into a temp file with visudo before moving into place — a malformed
# sudoers file can lock out sudo entirely, so never write it directly.
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
# Allow the deploy user (used by the GitHub Actions deploy) to restart ONLY the
# ecomm-copilot services without a password. Both binary paths are listed
# because sudo matches the resolved path, which differs across setups.
$DEPLOY_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart ecomm-copilot, /bin/systemctl restart ecomm-copilot, /usr/bin/systemctl restart ecomm-copilot-worker, /bin/systemctl restart ecomm-copilot-worker
EOF
visudo -cf "$SUDOERS_TMP"
install -m 440 "$SUDOERS_TMP" /etc/sudoers.d/ecomm-copilot
rm -f "$SUDOERS_TMP"

echo "==> Testing nginx config"
nginx -t

echo "==> Starting services"
systemctl enable --now ecomm-copilot
systemctl enable --now ecomm-copilot-worker
# CI monitoring timers (the templated service is started by the timers, not enabled itself).
for slot in morning afternoon night; do
  systemctl enable --now "ecomm-copilot-ci-$slot.timer"
done
systemctl reload nginx

echo
echo "Done. Service status:"
systemctl --no-pager --lines=0 status ecomm-copilot || true
systemctl --no-pager --lines=0 status ecomm-copilot-worker || true
systemctl --no-pager list-timers 'ecomm-copilot-ci-*' || true
echo
# Only prompt for certbot when TLS isn't wired up yet. If the site already has a
# 443 block (guarded above), the cert is in place and re-issuing is unnecessary;
# certbot's own timer handles renewal.
if ! grep -qE 'listen[^;]*443' "$NGINX_SITE"; then
  echo "Next: once DNS A records for ecomm-copilot.com and www point at this"
  echo "droplet, issue the certificate (this rewrites the nginx site to add TLS):"
  echo "  sudo certbot --nginx -d ecomm-copilot.com -d www.ecomm-copilot.com"
else
  echo "TLS is already configured for ecomm-copilot.com (443 block present)."
fi
