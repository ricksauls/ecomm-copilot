# Deploying ecomm-copilot

The app runs on the shared **wm-content-tools** droplet (`142.93.244.23`,
Ubuntu 24.04), following the same convention as the other apps there:

- App directory: **`/home/deploy/apps/ecomm-copilot`**, owned by `deploy`
- systemd service runs as **`User=deploy`**, gunicorn bound to **`127.0.0.1:8001`**
  (8000/8002 are used by other apps)
- nginx reverse proxy for **ecomm-copilot.com / www.ecomm-copilot.com**, TLS via
  Let's Encrypt (certbot)

Because `deploy` owns its home directory, the clone / venv / `.env` / git-pull
steps need **no sudo**. Only four steps need root, collected in
`deploy/setup-droplet.sh`.

Config files in this `deploy/` directory:
- `ecomm-copilot.service` — the systemd unit
- `nginx.conf` — the reverse-proxy server block
- `setup-droplet.sh` — the four root-only steps, run once with sudo

---

## One-time setup

### Unprivileged steps (as the `deploy` user)

```bash
# Read-only git deploy key, added to the repo under Settings -> Deploy keys
ssh-keygen -t ed25519 -C "ecomm-copilot-droplet" -f ~/.ssh/ecomm_deploy -N ""

# Clone, venv, deps
mkdir -p ~/apps
GIT_SSH_COMMAND="ssh -i ~/.ssh/ecomm_deploy" \
  git clone git@github.com:ricksauls/ecomm-copilot.git ~/apps/ecomm-copilot
cd ~/apps/ecomm-copilot
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Secrets — generated locally, never committed. chmod 600.
printf 'SECRET_KEY=%s\nDATABASE_URL=%s/app.db\nAPP_URL=https://ecomm-copilot.com\n' \
  "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" "$PWD" > .env
chmod 600 .env
```

### Privileged steps (once, with sudo)

`deploy/setup-droplet.sh` installs the systemd unit, the nginx site, a scoped
sudoers line (passwordless restart of *only* this service, for CI deploys), and
starts the service:

```bash
sudo bash ~/apps/ecomm-copilot/deploy/setup-droplet.sh
```

### DNS + HTTPS

Point the domain at the droplet, then issue the certificate:

1. At your DNS provider, create **A** records:
   - `ecomm-copilot.com` -> `142.93.244.23`
   - `www.ecomm-copilot.com` -> `142.93.244.23`
2. Wait for them to resolve (`dig +short ecomm-copilot.com` returns the IP).
3. Issue the cert (rewrites nginx to add 443 + HTTP->HTTPS redirect, auto-renews):

```bash
sudo certbot --nginx -d ecomm-copilot.com -d www.ecomm-copilot.com
```

---

## Auto-deploy (GitHub Actions)

`.github/workflows/deploy.yml` deploys over SSH **only after the CI workflow
passes on `main`**. It reuses the droplet's `deploy` user and restarts the
service via the scoped sudoers line above.

Required repository **Actions secrets** (Settings -> Secrets and variables ->
Actions):

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | `142.93.244.23` |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_SSH_KEY` | private half of a CI-only keypair whose public half is in `deploy`'s `~/.ssh/authorized_keys` |
| `DEPLOY_FINGERPRINT` | `ssh-keyscan -t ecdsa 142.93.244.23 \| ssh-keygen -lf - \| awk '{print $2}'` (the SSH client in the deploy action negotiates the ECDSA host key, so pin that one, not ed25519) |

After the secrets exist, every push to `main` that passes CI runs the deploy.

---

## PDP scoring worker

PDP scoring runs in a **separate background service** (`ecomm-copilot-worker`),
not in the web workers, because it drives a real browser (~seconds per item).
`setup-droplet.sh` installs and starts it alongside the web service.

Requirements on the droplet:
- **Playwright** (installed into the venv by `pip install -r requirements.txt`
  on deploy) plus a real Chrome — it uses `channel="chrome"`, the same system
  Chrome the WM scraper uses.
- The **Xvfb `:99` virtual display** (from the WM scraper setup): the worker
  runs headed Chrome via `Environment=DISPLAY=:99` and `Wants=xvfb.service`.

Manage / inspect it:

```bash
sudo systemctl status ecomm-copilot-worker
sudo journalctl -u ecomm-copilot-worker -n 50 --no-pager
```

The GitHub Actions deploy restarts the worker too (guarded, so it's a no-op
until `setup-droplet.sh` has installed the unit + sudoers rule).

## Competitive Intelligence monitoring timers

CI "monitoring" runs are enqueued **3×/day at 7:00 AM / 3:00 PM / 11:00 PM CST**
by three systemd timers driving a templated oneshot
(`ecomm-copilot-ci-monitor@<slot>.service`). The timers only enqueue `queued`
runs; the existing `ecomm-copilot-worker` does the scraping, so there's no extra
long-running process. `OnCalendar` carries an explicit `America/Chicago`
timezone, so systemd tracks CST/CDT automatically (the droplet clock is UTC).

`setup-droplet.sh` installs and enables all of this. If you're adding it to an
already-provisioned droplet **without** re-running the full script, do the
privileged install once (root via the DO web Console — the `deploy` sudo password
is lost; see the handoff):

```bash
cd /home/deploy/apps/ecomm-copilot
install -m 644 deploy/ecomm-copilot-ci-monitor@.service \
  /etc/systemd/system/ecomm-copilot-ci-monitor@.service
for slot in morning afternoon night; do
  install -m 644 "deploy/ecomm-copilot-ci-$slot.timer" \
    "/etc/systemd/system/ecomm-copilot-ci-$slot.timer"
done
systemctl daemon-reload
for slot in morning afternoon night; do
  systemctl enable --now "ecomm-copilot-ci-$slot.timer"
done
```

Inspect / verify:

```bash
systemctl list-timers 'ecomm-copilot-ci-*' --all       # next fire times
journalctl -u 'ecomm-copilot-ci-monitor@*' -n 50 --no-pager
# Trigger one immediately for a smoke test (enqueues a run for opted-in groups):
sudo systemctl start ecomm-copilot-ci-monitor@morning.service
```

A group is only swept when its owner has toggled **monitoring on** for it in the
app (`ci_groups.monitoring_enabled`).

## Manual deploy / rollback

```bash
cd ~/apps/ecomm-copilot
GIT_SSH_COMMAND="ssh -i ~/.ssh/ecomm_deploy" git pull --ff-only   # or: git checkout <sha>
venv/bin/pip install -r requirements.txt
sudo systemctl restart ecomm-copilot
```

## Troubleshooting

- **App won't start:** `sudo journalctl -u ecomm-copilot -n 50 --no-pager`
- **502 from nginx:** the service is down or not on `127.0.0.1:8001`.
- **Static files 404:** confirm the nginx `alias` matches
  `/home/deploy/apps/ecomm-copilot/app/static/`.
- **Never** run gunicorn/Flask with `debug=True` on the droplet — the debugger
  allows remote code execution.
