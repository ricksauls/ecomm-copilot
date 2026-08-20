# Deploying ecomm-copilot to a DigitalOcean droplet

This is the one-time server setup plus the ongoing auto-deploy flow. The app is
a Flask/gunicorn service behind nginx (TLS via Let's Encrypt), running as the
unprivileged `www-data` user. After setup, every push to `main` that passes CI
deploys automatically via `.github/workflows/deploy.yml`.

Config files referenced here live in this `deploy/` directory:
- `ecomm-copilot.service` — the systemd unit
- `nginx.conf` — the reverse-proxy server block

Replace `your-domain.example` and `<droplet-ip>` throughout with real values.

---

## One-time droplet setup

### 1. System packages and app directory

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip nginx git
sudo mkdir -p /opt/ecomm-copilot && sudo chown $USER:$USER /opt/ecomm-copilot
```

### 2. Read-only git deploy key

The repo is private, so give the droplet a dedicated **read-only** deploy key
rather than personal credentials.

```bash
ssh-keygen -t ed25519 -C "ecomm-copilot-droplet" -f ~/.ssh/ecomm_deploy -N ""
cat ~/.ssh/ecomm_deploy.pub
```

Add that public key to the repo under **Settings → Deploy keys** (leave "Allow
write access" unchecked), then clone:

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/ecomm_deploy" \
  git clone git@github.com:ricksauls/ecomm-copilot.git /opt/ecomm-copilot
```

### 3. Virtualenv and dependencies

```bash
cd /opt/ecomm-copilot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

### 4. Secrets in .env (never committed)

Generate a signing key and write the file, then lock its permissions:

```bash
cd /opt/ecomm-copilot
printf 'SECRET_KEY=%s\nDATABASE_URL=/opt/ecomm-copilot/app.db\nAPP_URL=https://your-domain.example\n' \
  "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > .env
chmod 600 .env
```

Add any API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) by editing `.env`. See
`.env.example` for the full list of variables.

### 5. systemd service

```bash
sudo cp /opt/ecomm-copilot/deploy/ecomm-copilot.service /etc/systemd/system/
sudo chown -R www-data:www-data /opt/ecomm-copilot
sudo systemctl daemon-reload
sudo systemctl enable --now ecomm-copilot
sudo systemctl status ecomm-copilot   # should be active, listening on 127.0.0.1:8000
```

### 6. nginx reverse proxy

Edit `deploy/nginx.conf` to set the real `server_name`, then:

```bash
sudo cp /opt/ecomm-copilot/deploy/nginx.conf /etc/nginx/sites-available/ecomm-copilot
sudo ln -s /etc/nginx/sites-available/ecomm-copilot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

The app is now live on `http://your-domain.example`.

### 7. HTTPS

Point an `A` record for the domain at `<droplet-ip>` first, then:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example
```

certbot rewrites the nginx block to serve 443, redirects HTTP→HTTPS, and sets up
auto-renewal.

### 8. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
```

---

## Enable auto-deploy from GitHub Actions

The deploy user needs to (a) authenticate the CI SSH connection and (b) restart
the service without a password prompt.

### 1. CI SSH key

Create a second key pair for the GitHub runner (separate from the git deploy
key), and authorize its public half for the deploy user on the droplet:

```bash
# On your workstation:
ssh-keygen -t ed25519 -C "ecomm-copilot-ci" -f ecomm_ci -N ""
# Append ecomm_ci.pub to the droplet deploy user's ~/.ssh/authorized_keys.
```

### 2. Passwordless restart only

Give the deploy user permission to restart **just this service** — nothing more.
Create `/etc/sudoers.d/ecomm-copilot` (edit with `sudo visudo -f`):

```
<deploy-user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart ecomm-copilot
```

### 3. Repository Actions secrets

Under **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | `<droplet-ip>` or hostname |
| `DEPLOY_USER` | the deploy user |
| `DEPLOY_SSH_KEY` | contents of the private `ecomm_ci` file |
| `DEPLOY_FINGERPRINT` | output of `ssh-keyscan <droplet-ip> \| ssh-keygen -lf -` |

Once the secrets exist, every push to `main` that passes CI triggers
`.github/workflows/deploy.yml`, which SSHes in and runs the pull-and-restart.

---

## Manual deploy / rollback

To deploy by hand (or if you need to roll back to a known commit):

```bash
cd /opt/ecomm-copilot
GIT_SSH_COMMAND="ssh -i ~/.ssh/ecomm_deploy" git pull --ff-only   # or: git checkout <sha>
./.venv/bin/pip install -r requirements.txt
sudo systemctl restart ecomm-copilot
```

## Troubleshooting

- **App won't start:** `sudo journalctl -u ecomm-copilot -n 50 --no-pager`
- **502 from nginx:** the service is down or not on `127.0.0.1:8000` — check the
  status and journal above.
- **Static files 404:** confirm the `alias` in the nginx block matches
  `/opt/ecomm-copilot/app/static/`.
- **Never** run gunicorn/Flask with `debug=True` on the droplet — the debugger
  allows remote code execution.
