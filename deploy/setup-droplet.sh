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

echo "==> Installing systemd unit"
install -m 644 "$APP_DIR/deploy/ecomm-copilot.service" \
  /etc/systemd/system/ecomm-copilot.service
systemctl daemon-reload

echo "==> Installing nginx site"
install -m 644 "$APP_DIR/deploy/nginx.conf" \
  /etc/nginx/sites-available/ecomm-copilot
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
# ecomm-copilot service without a password. Both paths are listed because sudo
# matches the resolved binary path, which differs across setups.
$DEPLOY_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart ecomm-copilot, /bin/systemctl restart ecomm-copilot
EOF
visudo -cf "$SUDOERS_TMP"
install -m 440 "$SUDOERS_TMP" /etc/sudoers.d/ecomm-copilot
rm -f "$SUDOERS_TMP"

echo "==> Testing nginx config"
nginx -t

echo "==> Starting services"
systemctl enable --now ecomm-copilot
systemctl reload nginx

echo
echo "Done. Service status:"
systemctl --no-pager --lines=0 status ecomm-copilot || true
echo
echo "Next: once DNS A records for ecomm-copilot.com and www point at this"
echo "droplet, issue the certificate:"
echo "  sudo certbot --nginx -d ecomm-copilot.com -d www.ecomm-copilot.com"
