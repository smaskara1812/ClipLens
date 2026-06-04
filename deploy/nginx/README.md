# nginx configuration — local dev

This folder holds the reference nginx config used by the local dev environment.
It is the **source of truth** — `/opt/homebrew/etc/nginx/servers/` is just a
copy that nginx loads at runtime.

## Files

| File | Purpose |
|------|---------|
| `cliplens-local.conf` | Single-server block for `*.cliplens.local` and bare `cliplens.local`, proxies to Django on `127.0.0.1:8000` |

## What this config does

- Routes every `*.cliplens.local` request (and the bare root) to Django on port 8000
- Forwards the original `Host` header so `TenantMiddleware` can resolve the tenant subdomain
- Allows uploads up to 4 GB
- **Disables proxy buffering** — required for large MP4 downloads (redacted copies, originals). Without this, nginx holds the whole body before relaying and Safari aborts.
- 1-hour read/send timeouts for slow AI provisioning, big uploads, and ffmpeg renders

## Apply changes to the local dev nginx

```bash
# 1. Copy this file into the runtime config location
sudo cp deploy/nginx/cliplens-local.conf \
        /opt/homebrew/etc/nginx/servers/cliplens-local.conf

# 2. Test syntax — DO THIS FIRST, never reload without it
sudo nginx -t

# 3. Apply (zero-downtime — keeps existing connections alive)
sudo nginx -s reload

# 4. Confirm only one server block matches cliplens.local
sudo nginx -T 2>/dev/null | grep -c "server_name ~\^((?<tenant>"
# Should print: 1
```

## Pre-requisites for a fresh dev machine

```bash
# 1. Install nginx
brew install nginx

# 2. Create the writable upload temp dir
mkdir -p ~/.nginx_tmp/client_body

# 3. Add /etc/hosts entries for the dev subdomains
sudo tee -a /etc/hosts << 'EOF'

# ClipLens dev (multi-tenant)
127.0.0.1   cliplens.local
127.0.0.1   admin.cliplens.local
127.0.0.1   testorg1.cliplens.local
127.0.0.1   maskara.cliplens.local
# add any other tenant slugs you provision here
EOF

# 4. Copy the config and reload
sudo cp deploy/nginx/cliplens-local.conf \
        /opt/homebrew/etc/nginx/servers/cliplens-local.conf
sudo nginx -t
sudo nginx -s reload

# 5. Start nginx if it isn't already
sudo brew services start nginx
```

## Common nginx commands

```bash
sudo nginx                              # start
sudo nginx -s stop                      # stop
sudo nginx -s reload                    # zero-downtime config reload
sudo nginx -t                           # syntax check before reloading
sudo nginx -T                           # dump all loaded config (useful for debugging)
sudo brew services restart nginx        # full restart via launchd

tail -f /opt/homebrew/var/log/nginx/access.log
tail -f /opt/homebrew/var/log/nginx/error.log

ps aux | grep nginx                     # is nginx actually running?
lsof -iTCP:80 -sTCP:LISTEN              # what's listening on port 80?
```

## Troubleshooting

| Symptom | Cause / fix |
|---------|------|
| Safari aborts download mid-stream ("network connection was lost") | `proxy_buffering` is on. Confirm `proxy_buffering off;` is in the active config. |
| 502 Bad Gateway | Django on `127.0.0.1:8000` isn't running. Check `ps aux \| grep runserver`. |
| 413 Request Entity Too Large on upload | `client_max_body_size` too small. Default is 1 MB. We set 4096 MB. |
| Duplicate `server_name` warning at reload | Two server blocks with the same `server_name` — usually a stale `.bak` file in `/opt/homebrew/etc/nginx/servers/`. nginx's `include servers/*` (no extension filter) loads everything. Either delete the backup OR change main `nginx.conf` to `include servers/*.conf` so only real conf files are loaded. |
| Wrong tenant routes (request lands in wrong DB) | `proxy_set_header Host $host` missing or overridden. Confirm with `curl -H "Host: tenant.cliplens.local" http://127.0.0.1/...` |
| Upload fails with permission denied on `/tmp` | `client_body_temp_path` not set or points to non-writable dir. Confirm `~/.nginx_tmp/client_body` exists and is writable by the nginx user. |

## Production checklist (when you eventually deploy)

When deploying for real (replace this dev config), make sure to:

1. **Add TLS** — Let's Encrypt via `certbot --nginx`
2. **`server_name`** — match your real domain, not `cliplens.local`
3. **`proxy_pass`** — point to your gunicorn/uvicorn socket, not the Django dev server
4. **Static files** — uncomment the `/static/` and `/media/` location blocks so nginx serves them directly (faster + offloads Django)
5. **Rate limiting** — add `limit_req_zone` for `/login/`, `/register/`, `/onboard/` to mitigate brute force
6. **CSP / security headers** — add `add_header` lines for HSTS, X-Frame-Options, etc.
7. **Access log format** — switch to JSON for log aggregation tools
