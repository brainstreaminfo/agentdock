# AgentHub

An AI agent platform for Shopify. Staff build agents in the **Agent Studio**;
merchants install them from the **Agent Marketplace**. It's a non-embedded
Shopify app: merchants use our own UI on our domain.

- Full brief: [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md)
- Why things are the way they are: [`docs/DECISIONS.md`](docs/DECISIONS.md)
- When something breaks: [`docs/DEBUGGING.md`](docs/DEBUGGING.md)

**Status:** Phase 1 (Foundation) done: OAuth install/launch/uninstall with
expiring tokens, scope upgrades, magic-link login, store switcher, compliance
webhooks, UI kit, admin, logging. Next: Phase 2, the Tool Catalog.

## Run it locally

Needs: Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL, Redis
(or Docker for everything: `make up`).

```bash
make install          # .venv + dependencies, creates .env from .env.example
# edit .env: DJANGO_SECRET_KEY, ENCRYPTION_KEY, SHOPIFY_API_KEY/SECRET
createdb agenthub
make migrate
make superuser        # staff account for /admin/ (and the Studio later)
make run              # http://localhost:8000
make worker           # second terminal: background jobs
make beat             # third terminal: scheduled jobs
```

Login emails are printed in the `make run` terminal until `RESEND_API_KEY` is set.

**Front-end:** no Node build. Templates use Tailwind classes; after changing them
run `make css` (or keep `make css-watch` running). `make vendor` downloads the
pinned JS libraries and the Tailwind CLI.

### Connect a real development store

1. Create the app in the Shopify Dev Dashboard and put its key/secret in `.env`.
2. Start a tunnel (`cloudflared tunnel --url http://localhost:8000`), set
   `APP_URL` to the tunnel URL, and replace `https://app.<domain>.com` in
   `shopify.app.toml` with it. Then `shopify app deploy`.
3. Install the app on your dev store. You'll land on `APP_URL`, not inside
   Shopify Admin.

## Everyday commands

```bash
make help        # list all commands
make check       # ruff + mypy + pytest + "no Polaris/App Bridge" check
make test-fast   # stop at the first failing test
make fmt         # auto-fix formatting
```

## Code map

```
config/                 settings (base/dev/prod/test), urls, celery, asgi
apps/core/              shared: base models, AuditLog, encryption, logging, decorators, UI tags
apps/accounts/          User (email login), Membership (OWNER/ADMIN/MEMBER), magic links
apps/shops/             Shop model (encrypted expiring tokens, scopes), store list/switcher
apps/shopify_app/       OAuth, launch, HMAC, webhooks, GraphQL client, token refresh
  queries/*.graphql     GraphQL documents
apps/dashboard/         merchant dashboard, onboarding, settings
templates/              Django templates (layouts/, components/, per-app folders)
static/src/app.css      Tailwind source + UI kit classes  →  static/css/app.css
tests/                  pytest (Shopify mocked with respx)
```
