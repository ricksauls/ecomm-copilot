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
| `DEPLOY_FINGERPRINT` | `ssh-keyscan 142.93.244.23 \| ssh-keygen -lf -` |

After the secrets exist, every push to `main` that passes CI runs the deploy.

---

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
