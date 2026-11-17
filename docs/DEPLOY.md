# Deploying AgentHub

This guide uses **Railway**: the simplest option for this stack (web + Celery
worker + Celery beat + Postgres + Redis, all from one repo and one Dockerfile).
Render and Fly.io work the same way. See "Other hosts" at the end.

What runs in production:

| Service | Command | Public? | Instances |
|---|---|---|---|
| **web** | Dockerfile default (uvicorn) | yes, `app.yourdomain.com` | 1+ |
| **worker** | `celery -A config worker -l info --concurrency 2` | no | 1+ |
| **beat** | `celery -A config beat -l info` | no | **exactly 1** (runs agent schedules) |
| **Postgres** | Railway database | no | — |
| **Redis** | Railway database | no | — |

All three app services use the same Docker image and the same environment variables.

---

## 0. Testing without a Shopify app (standalone)

You can deploy and try everything before registering the Shopify app:

1. Follow sections 1–5 and 7, but skip 6. Set placeholders, because production
   refuses to start without them:
   `SHOPIFY_API_KEY=not-registered-yet`, `SHOPIFY_API_SECRET=not-registered-yet`.
   `RESEND_API_KEY` can stay empty if you only log in with your staff password.
2. After `createsuperuser`, in `railway ssh --service web`:
   ```bash
   python manage.py seed_agent_templates
   python manage.py create_demo_shop --email <your superuser email> --timezone Asia/Kolkata
   ```
3. Log in at `/admin/` with your password, then open `/stores/`: the demo store is
   there and you are its owner. The Studio is at `/studio/`.

The demo store has every permission, so installs never go to Shopify, and the
app never sends it anything. Tools that need store data (products, orders…)
answer "demo store isn't connected to Shopify"; the agent says so in chat.
Chat, My Team, brand memory, charts and tables, alerts, reply drafts,
schedules, Run now, run traces, the playground and Studio tests all work.
When you register the Shopify app later, do section 6 and replace the
placeholder key/secret; delete the demo store in Django Admin → Shops.

## 1. Before you start (one time)

1. **A domain** where you can add DNS records, e.g. `app.yourdomain.com`.
2. **Resend** (resend.com) for login emails: add and verify your domain, then create
   an API key. Without it, merchants can't log in by email.
3. **Sentry** (optional but recommended): create a Django project and copy the DSN.
4. **Generate two secrets** on your laptop and keep them somewhere safe (a password manager):
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(50))"          # DJANGO_SECRET_KEY
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # ENCRYPTION_KEY
   ```
   ⚠️ **Never lose `ENCRYPTION_KEY`**: without it, the stored Shopify tokens can't be
   decrypted and every store would have to reinstall.

## 2. Create the Railway project

1. Sign in at railway.com with GitHub and give Railway access to the
   `brainstreaminfo/agentdock` repository.
2. **New Project → Deploy from GitHub repo → `brainstreaminfo/agentdock`**, branch **`master`**.
   Railway finds the `Dockerfile` and builds it. Rename this service to **web**.
   The first deploy will fail until the variables below are set; that's expected.
3. In the project: **+ New → Database → PostgreSQL**, then **+ New → Database → Redis**.

## 3. Set environment variables

Project **Settings → Shared Variables**, so web, worker and beat all get them:

```
APP_NAME=AgentHub
APP_URL=https://app.yourdomain.com
ALLOWED_HOSTS=app.yourdomain.com,healthcheck.railway.app
DJANGO_SECRET_KEY=<generated above>
ENCRYPTION_KEY=<generated above>
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
SHOPIFY_API_KEY=<from the production Shopify app, step 6>
SHOPIFY_API_SECRET=<from the production Shopify app, step 6>
SHOPIFY_API_VERSION=2026-10
SHOPIFY_BASE_SCOPES=read_products
RESEND_API_KEY=<from Resend>
DEFAULT_FROM_EMAIL=AgentHub <login@yourdomain.com>
SENTRY_DSN=<optional>
SENTRY_ENVIRONMENT=production
LOG_LEVEL=INFO
LOG_JSON=true
ANTHROPIC_API_KEY=<from console.anthropic.com — agents can't run without it>
ALLOWED_MODELS=claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5
DEFAULT_MODEL=claude-opus-5-5
DEFAULT_DAILY_SPEND_LIMIT_USD=10
TRIGGERS_AUTO_SYNC_WEBHOOKS=true
```

- `${{Postgres.DATABASE_URL}}` is Railway's syntax for "use the database's URL".
  Type it exactly like that.
- `APP_URL` is also the address Shopify sends agent events to: each store is
  subscribed to `APP_URL/shopify/webhooks/` for the events its agents use. If you
  change the domain later, re-save each installation (or reinstall) so the
  subscriptions move to the new address.
- `DJANGO_SETTINGS_MODULE` is already `config.settings.prod` in the Docker image.
  Production refuses to start if `DJANGO_SECRET_KEY`, `ENCRYPTION_KEY` or the
  Shopify key/secret are missing. That's deliberate, so a misconfiguration can't
  run half-broken.

## 4. Configure the web service

**web → Settings:**
- **Pre-deploy command:** `python manage.py migrate --noinput && python manage.py sync_tools`
  (applies database changes and registers new built-in tools before each new
  version goes live).
- **Healthcheck path:** `/healthz/`
- **Networking → Custom domain:** `app.yourdomain.com`. Railway shows a CNAME
  record; add it at your DNS provider. HTTPS is automatic.

## 5. Add the worker and beat services

For each one: **+ New → GitHub Repo → `brainstreaminfo/agentdock`**, then in its
**Settings**:

| Service name | Custom start command | Notes |
|---|---|---|
| `worker` | `celery -A config worker -l info --concurrency 4` | no domain; runs every agent, so scale it up as usage grows |
| `beat` | `celery -A config beat -l info` | no domain, **never more than 1 replica** (2 would run every schedule twice). Without it, scheduled agents never run |

Leave the healthcheck path empty for these two, since they don't serve HTTP.

## 6. Create the production Shopify app

Keep your **development** app (tunnel URL) and a **production** app separate, so
you can test on the dev app without touching real merchants.

On your laptop, in the project folder:

```bash
shopify app config link        # choose "Create a new app", name it e.g. "AgentHub"
```

This creates a second config file such as `shopify.app.agenthub.toml`. In that file,
replace every `https://app.<domain>.com` with `https://app.yourdomain.com`, then:

```bash
shopify app config use agenthub   # select the production config
shopify app deploy
```

Copy that app's **Client ID** and **Client secret** (Dev Dashboard → app →
Settings) into Railway's `SHOPIFY_API_KEY` / `SHOPIFY_API_SECRET`.

## 7. Create your staff account

Install the Railway CLI (`brew install railway`), then:

```bash
railway login
railway link                   # pick the project
railway ssh --service web      # opens a shell inside the running web container
python manage.py createsuperuser
```

Optionally, load the 3 example agents to clone in the Studio (they are never
shown to merchants):

```bash
python manage.py seed_agent_templates
```

## 8. Check that it works

1. `https://app.yourdomain.com/healthz/` → `{"ok": true}`
2. `https://app.yourdomain.com/admin/` → log in with the staff account.
3. Install the **production** app on a development store (Dev Dashboard → app →
   Install). You should land on `https://app.yourdomain.com/s/<id>/welcome/`.
4. In `railway ssh --service web`:
   `python manage.py check_shop your-dev-store.myshopify.com` → green ticks.
5. **An agent end to end** (needs `ANTHROPIC_API_KEY`; costs a few cents):
   - Studio (`/studio/`) → clone a template, set **Availability → Public**, publish.
   - In the store's app: **Marketplace** → the agent → **Install**. If it needs
     more access, Shopify asks first and sends you back to the settings form.
   - **My agents** → the agent → **Run now** → the run page fills in live.
   - If it has a schedule: Django Admin → **Periodic tasks** shows
     `agent-installation-<id>` with the store's timezone.
   - If it uses store events (e.g. `orders/create`): `check_shop` lists the new
     webhook subscription. Create a test order and look in Django Admin →
     **Webhook events** and in **Runs**.
6. Uninstall the app from the dev store. Within seconds, Django Admin →
   **Webhook events** should show `app/uninstalled` as PROCESSED, and the
   store's periodic tasks are gone.

Development stores receive order and customer events without Shopify's
"protected customer data" approval. Real merchant stores need it: apply in the
Partner Dashboard before launch (see `docs/DECISIONS.md` #27).

## 9. Everyday: shipping a change

`git push` to `master` → Railway rebuilds and redeploys all three services. Migrations
and the tool sync run automatically (pre-deploy command). In each service's **Settings**, turn on
**"Wait for CI"**, so a push whose GitHub checks fail is never deployed.

Logs: Railway → service → **Logs** (JSON lines; search e.g. `shop_id=4` or a
`request_id`). See `docs/DEBUGGING.md`.

---

## Other hosts

The same image works anywhere that runs Docker:

- **Render:** a Web Service (Docker) + two Background Workers with the commands
  above + Render Postgres + Render Key Value (Redis). Set the pre-deploy command to
  `python manage.py migrate --noinput && python manage.py sync_tools`.
- **Fly.io:** one app with three `[processes]` (web/worker/beat) in `fly.toml`,
  plus Fly Postgres and Upstash Redis. Run `migrate` and `sync_tools` as `release_command`.
- **Own VPS:** `docker compose` with a production compose file, plus Caddy or
  nginx for HTTPS. More work to maintain; only worth it at scale.

Wherever you host: `ALLOWED_HOSTS` must contain your domain, `APP_URL` must
exactly match the Shopify app's URLs, and there must be **exactly one** beat
process.
