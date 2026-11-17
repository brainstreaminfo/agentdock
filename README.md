# AgentHub

An AI agent platform for Shopify. Staff build agents in the **Agent Studio**;
merchants install them from the **Agent Marketplace**. It's a non-embedded
Shopify app: merchants use our own UI on our domain.

- Full brief: [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md)
- Why things are the way they are: [`docs/DECISIONS.md`](docs/DECISIONS.md)
- When something breaks: [`docs/DEBUGGING.md`](docs/DEBUGGING.md)
- Putting it on a server: [`docs/DEPLOY.md`](docs/DEPLOY.md)
- How tools work / add a tool: [`docs/TOOL_CATALOG.md`](docs/TOOL_CATALOG.md)
- Build and publish an agent (no code): [`docs/AGENT_STUDIO_GUIDE.md`](docs/AGENT_STUDIO_GUIDE.md)

**Status:**
- Phase 1 (Foundation) done: OAuth with expiring tokens, scope upgrades,
  magic-link login, store switcher, compliance webhooks, UI kit, logging.
- Phase 2 (Tool Catalog) done: 50 built-in Shopify tools, an executor with
  approvals and undo, custom GraphQL tools, and Studio tool pages with a Test
  button.
- Phase 3 (Agent runtime) done: agents and versions, a Claude agent loop with
  limits and cost tracking, streaming chat with charts and approval cards, run
  traces, the `run_agent` command, and 3 example agents.

- Phase 4 (Agent Studio) done: a no-code builder (listing, brain, tools,
  settings-form builder, triggers, behaviour, availability), a playground,
  test cases, publishing with automatic upgrades, versions and rollback, and
  analytics.
- Phase 5 (Marketplace & installs) done: Marketplace with search and
  categories, install flow with permission upgrade through Shopify, My agents
  (settings with product/collection pickers, Run now, pause, update, uninstall),
  "My Team" chat with all agents, approvals queue with bulk approve and undo,
  schedules in the store's timezone, Shopify-event triggers, alerts, brand
  memory, and a real dashboard.

Next: Phase 6, billing and launch.

## Run it locally

Needs: Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL, Redis
(or Docker for everything: `make up`).

```bash
make install          # .venv + dependencies, creates .env from .env.example
# edit .env: DJANGO_SECRET_KEY, ENCRYPTION_KEY, SHOPIFY_API_KEY/SECRET
createdb agenthub
make migrate
make sync-tools       # register the built-in tools
python manage.py seed_agent_templates   # 3 example agents
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
apps/dashboard/         merchant dashboard, alerts, brand memory, settings, Alert model
apps/tools/             Tool Catalog: registry, executor (rules + approvals), custom tools
  catalog/              the 50 built-in tools (<domain>.py + <domain>.graphql)
apps/runs/              Run, RunStep, ActionProposal, ActionSnapshot; runs list, approvals queue
apps/agents/            agents, versions, installations, runtime (Claude loop), LLM provider,
                        orchestrator ("My Team")
apps/marketplace/       Marketplace, install flow (scope upgrade), My agents, pickers
apps/triggers/          schedules (celery-beat per installation), Shopify event routing,
                        per-shop webhook subscriptions
apps/chat/             conversations, chat UI, live streaming (SSE), approvals in chat
apps/studio/            staff-only Studio (/studio/): agent builder, playground, tests,
                        publish, versions, analytics, tool catalog
apps/billing/           plans and entitlements (placeholders until Phase 6)
templates/              Django templates (layouts/, components/, per-app folders)
assets/tailwind.css     Tailwind source + UI kit classes  →  static/css/app.css
tests/                  pytest (Shopify mocked with respx)
```
