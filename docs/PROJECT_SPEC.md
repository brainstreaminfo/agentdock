# Project Spec: AI Agent Platform for Shopify — Agent Studio + Agent Marketplace (Python)

> This document is the build brief for Claude Code. Read it fully before writing code. Build in the phase order in §14. When something is ambiguous, choose the simplest option that satisfies the spec and record the decision in `docs/DECISIONS.md`.
>
> **The project owner is a Python developer and does not know TypeScript.** Keep the codebase Python-first: server-rendered Django templates, minimal JavaScript (HTMX + Alpine.js only, no React/TypeScript build step). Write clear, commented code and make every failure easy to trace (§13).
>
> _Note: some details below were superseded during the build. See `docs/DECISIONS.md` (notably: expiring offline tokens, API version 2026-10)._

---

## 1. Product summary

**Working name:** `AgentHub` (rename via `APP_NAME`)

We are building the **platform first, not the agents.** No agent is hard-coded. The owner decides later which agents to offer.

The product has two sides inside one Django project:

1. **Agent Studio** (owner / staff only, `/studio/`): build complete AI agents without writing code — name, description, instructions (prompt), which Shopify API tools it can use, settings form for merchants, triggers (chat, schedule, Shopify events), pricing/plan, then test and **publish**.
2. **Merchant App** (`/s/<shop_id>/…`): merchants browse the **Agent Marketplace**, **install** published agents into their store, configure them, chat with them, approve their actions and see results.

Agents work on the merchant's store through a **Shopify Tool Catalog**: a library of safe, typed functions that call the Shopify Admin GraphQL API.

It is distributed as a **non-embedded Shopify app**: after install, the merchant lands on **our own UI** at `https://app.<domain>.com`. **No Shopify Polaris, no App Bridge.**

```
            ┌────────────── Agent Studio (staff) ──────────────┐
            │ create agent → pick tools → prompt → test → publish │
            └───────────────────────┬──────────────────────────┘
                                    │ AgentVersion (published)
                                    ▼
Merchant ── Marketplace ── Install (scopes check / re-OAuth) ── Configure ── Run
                                                                     │
                                         Agent Runtime (Claude tool-use loop)
                                                                     │
                                       Shopify Tool Catalog → Admin GraphQL API
```

---

## 2. Non-embedded Shopify app

| Topic | How we handle it |
|---|---|
| `shopify.app.toml` | `embedded = false`, `application_url = "https://app.<domain>.com/shopify/launch/"` |
| Install auth | **Authorization code grant OAuth** (token exchange is embedded-only; do not use it) |
| Access token | **Offline** token per shop, encrypted at rest (Fernet, `ENCRYPTION_KEY`) |
| Launch from Shopify Admin | Verify HMAC on `/shopify/launch/`, then OAuth (new / scopes missing) or log in and redirect to dashboard |
| Sessions | Django auth. Ways in: Shopify launch, or email magic link on our domain |
| Extra scopes per agent | When a merchant installs an agent needing scopes the shop hasn't granted, re-run OAuth with the **union** of granted + required scopes, then finish the install |
| Billing | Shopify Billing API (required for App Store apps) |

> **Claude Code:** before finishing Phase 1, check current Shopify App Store requirements for non-embedded apps (shopify.dev/docs/apps/launch/app-requirements-checklist) and note anything affecting this design in `docs/DECISIONS.md`. "Built for Shopify" requires embedded; we accept not having it.

---

## 3. Tech stack

- Python 3.12, type hints, `mypy` (django-stubs), `ruff`, `pre-commit`, `uv`
- **Django 5.x** on ASGI (`uvicorn`) for streaming
- **UI:** Django templates, Tailwind CSS (standalone CLI), HTMX (+ SSE extension), Alpine.js, Chart.js, Lucide SVG icons, light/dark theme
- **PostgreSQL** (Django ORM)
- **Celery + Redis**, `django-celery-beat` for schedules, **Flower** for monitoring
- **LLM:** official `anthropic` Python SDK with tool use, behind an `LLMProvider` interface. Models selectable per agent from a list in settings
- **Shopify:** our own OAuth/HMAC/webhook code in `apps/shopify_app/`; Admin **GraphQL** via `httpx`; version pinned in `SHOPIFY_API_VERSION`
- **Schemas:** Pydantic v2 for tool inputs; JSON Schema for agent settings forms (rendered to HTML forms server-side)
- **Testing:** `pytest`, `pytest-django`, `factory_boy`, `respx`, Playwright for Python
- **Infra:** Docker Compose (postgres, redis, web, worker, beat, flower); deploy on Railway / Render / Fly.io

---

## 4. Repository layout

```
/config                  settings (base/dev/prod), urls, asgi, celery
/apps
  /accounts              users, magic-link login, team memberships
  /shops                 Shop, store switcher, shop settings
  /shopify_app           OAuth, launch, HMAC, webhooks, GraphQL client, billing API
  /tools                 Shopify Tool Catalog (code tools + custom GraphQL tools)
    /catalog/<domain>.py   products.py, orders.py, customers.py, inventory.py, ...
  /agents                AgentDefinition, AgentVersion, runtime (tool-use loop), orchestrator, memory
  /studio                Agent Studio UI (staff only): builder, playground, tests, publish
  /marketplace           Merchant-facing catalog, install / uninstall / upgrade
  /runs                  Run, RunStep, ActionProposal, approvals, undo
  /chat                  conversations, SSE streaming
  /triggers              schedules + Shopify-event routing to installed agents
  /billing               plans, entitlements, usage
  /dashboard             merchant dashboard, alerts
/templates  /static  /docs  /tests
shopify.app.toml  docker-compose.yml  pyproject.toml  Makefile  .env.example
```

---

## 5. Install, launch, auth (platform)

1. `GET /shopify/launch/` → verify HMAC + `shop` regex (`^[a-z0-9][a-z0-9-]*\.myshopify\.com$`). Not installed → `/shopify/auth/`.
2. `/shopify/auth/?shop=…&scopes=…` → random `state` in session → redirect to Shopify OAuth.
3. `/shopify/callback/` → verify state + HMAC → exchange for **offline** token → fetch shop info → upsert `Shop` (save `granted_scopes`) → create `User` + `Membership(OWNER)` → enqueue catalog sync → log in → `/onboarding/` or `/s/<shop_id>/`. If a `pending_agent_install` is in the session, resume it (§8.3).
4. Re-launch from Shopify Admin with a valid HMAC → log in → dashboard.
5. `/login/` magic link → `/stores/` → "Connect another store".
6. **Base scopes** at first install are minimal: `read_products`. Everything else is requested when an agent that needs it is installed.
7. `app/uninstalled` → delete token, pause all installed agents and schedules, revoke queued tasks, purge data after 30 days (or on `shop/redact`).

---

## 6. Shopify Tool Catalog (how agents get Shopify API access)

Agents never call Shopify directly. They call **tools**. Each tool declares the Shopify scopes it needs, whether it writes data, and an input schema.

### 6.1 Code tools (built by Claude Code, shipped with the platform)

```python
@tool(
    key="products.search",
    name="Search products",
    description="Search products by text, status, collection, vendor or tag.",
    scopes=["read_products"],
    mutating=False,
    category="Products",
)
def search_products(ctx: ToolContext, args: SearchProductsInput) -> SearchProductsOutput: ...
```

- `ToolContext` gives the tool the shop, an authenticated GraphQL client, the run id and a logger. Tools **cannot** access other shops.
- Mutating tools also implement `snapshot_before()` and (where possible) `undo()`.
- Every tool registers itself in a `ToolRegistry`; the Studio reads the registry to show the catalog.
- GraphQL queries live in `.graphql` files next to the tool.

**v1 catalog (build all of these; read + write where noted):**

| Category | Tools |
|---|---|
| Shop | `shop.info`, `shop.policies` |
| Products | `products.search`, `products.get`, `products.update` (title, description HTML, tags, vendor, type, status), `products.update_seo`, `products.update_images_alt`, `variants.update_price` |
| Collections | `collections.list`, `collections.get`, `collections.add_products`, `collections.create` |
| Inventory | `inventory.levels`, `inventory.adjust`, `inventory.low_stock` |
| Orders | `orders.search`, `orders.get`, `orders.add_tags`, `orders.add_note`, `orders.timeseries`, `orders.sales_summary` |
| Customers | `customers.search`, `customers.get`, `customers.add_tags`, `customers.segments_list` |
| Discounts | `discounts.list`, `discounts.create_code`, `discounts.deactivate` |
| Content | `pages.list/get/create/update`, `blogs.list`, `articles.list/get/create_draft/update` |
| Metafields | `metafields.get`, `metafields.set` |
| Files | `files.list` |
| Analytics | `analytics.top_products`, `analytics.top_customers`, `analytics.aov` (computed from orders; check whether ShopifyQL is available on the pinned API version and use it if so) |
| Platform (non-Shopify) | `ui.render_chart`, `ui.render_table`, `alerts.create`, `memory.read`, `memory.propose_update`, `reply.draft`, `web.fetch_url` (optional, off by default) |

### 6.2 Custom GraphQL tools (created in the Studio, no code)

Staff can create a tool from the Studio by entering:
- key, name, description (shown to the AI), category
- a GraphQL query or mutation
- input variables as JSON Schema
- required scopes (validated against Shopify's scope list)
- `mutating` flag (auto-detected: any `mutation` → mutating, cannot be unticked)
- optional output path to trim the response (e.g. `data.products.edges[*].node`)

Custom tools run through the same `ToolContext`, rate limiting, approval and logging as code tools. They have a **Test** button that runs them against the staff dev store.

### 6.3 Tool rules (enforced in one place: `apps/tools/executor.py`)
1. Check the shop has granted every scope the tool needs; otherwise return a clear error to the agent and create an alert asking the merchant to grant access.
2. Validate input with Pydantic / JSON Schema.
3. If `mutating` and the installed agent's autonomy isn't `AUTO` → create an `ActionProposal` and return "pending approval" to the agent.
4. Respect GraphQL cost (`extensions.cost`); back off on `THROTTLED`.
5. Wrap Shopify-sourced text as data (prompt-injection guard). Redact emails/phones in logs.
6. Log a `RunStep` with input, output, duration and errors.

---

## 7. Agent Studio (staff only, `/studio/`)

Access: `is_staff` users only. Full CRUD UI built with our templates (not just Django Admin).

### 7.1 Agent builder — fields of an agent

| Section | Fields |
|---|---|
| Listing | name, slug, icon (upload or Lucide name), category, tagline, long description (markdown), screenshots, "what it can do" bullets, example prompts |
| Brain | system instructions (markdown editor with variables: `{{shop.name}}`, `{{memory}}`, `{{settings.*}}`), model, temperature, max steps, max tokens per run |
| Tools | pick from the Tool Catalog (grouped by category, shows scopes + read/write badge). Required scopes are **calculated automatically** from the selected tools |
| Merchant settings | form builder that produces JSON Schema: text, textarea, number, select, multi-select, toggle, product picker, collection picker. Default values |
| Triggers | chat (on/off), schedule (cron presets + custom, runs in shop timezone), Shopify events (pick webhook topics, e.g. `orders/create`, `products/update`, `inventory_levels/update`), manual "Run now" button |
| Behaviour | default autonomy (`SUGGEST` / `APPROVE` / `AUTO`), which autonomy levels merchants may choose, output type (chat reply, report, alerts, actions) |
| Availability | plans that include it, optional extra price per month, visibility (`private` = only selected shops / `beta` / `public`), featured flag |

### 7.2 Versioning
- Editing creates a **draft**. Publishing creates an immutable `AgentVersion` (v1, v2…) with a changelog.
- Installed agents are pinned to a version. When a new version is published:
  - same scopes → auto-upgrade (setting per agent: auto or manual)
  - new scopes → merchant sees "Update available — needs access to X" and must approve (re-OAuth)
- Roll back = republish an older version.

### 7.3 Playground & tests
- **Playground:** pick a connected test store, fill settings, chat or trigger the draft agent, see the live trace (each LLM turn, tool call, Shopify response, cost). Mutating tools always go to approval in the playground.
- **Test cases:** save prompts with expected checks (must call tool X, must not call tool Y, reply contains / doesn't contain text, max cost). "Run all tests" before publish; publishing shows the results.

### 7.4 Studio dashboard
Agents list with status, installs, active shops, runs, error rate, average cost per run, revenue. Per-agent analytics page.

### 7.5 Templates
Ship 3 **example templates** (not published) the owner can clone to learn: "Product Description Writer", "Low Stock Alert", "Sales Q&A Analyst". Seeded via a management command `seed_agent_templates`.

---

## 8. Merchant side

### 8.1 Marketplace (`/s/<shop_id>/marketplace/`)
Grid of published agents with search, categories, featured row. Agent detail page: description, screenshots, example prompts, **permissions it needs** (plain-language list from scopes, e.g. "Read and edit your products"), plan/price, reviews (Phase 5), Install button.

### 8.2 My agents (`/s/<shop_id>/agents/`)
Installed agents with status, last run, next scheduled run, pending approvals. Per-agent page: settings form (rendered from JSON Schema), autonomy, schedule on/off, run history, "Run now", pause, uninstall, update to new version.

### 8.3 Install flow
1. Click Install → check plan entitlement (upsell if needed).
2. Compare agent's required scopes with `shop.granted_scopes`. Missing → save `pending_agent_install` in session → show "This agent needs access to: …" → Continue → re-OAuth with union of scopes → callback resumes the install.
3. Settings form (pre-filled defaults) → choose autonomy → Save.
4. Create `AgentInstallation`; register its schedule and event subscriptions.

### 8.4 Chat (`/s/<shop_id>/chat/`)
- Merchant can chat with **one agent** or with **"My Team"** (orchestrator).
- The orchestrator is generic: its tools are built at runtime from the shop's installed, chat-enabled agents (`delegate_to_<agent_slug>`), using each agent's tagline/description. No orchestrator code changes when agents are added.
- Streaming replies (SSE), inline charts/tables, action cards with Approve / Reject, link to full trace.

### 8.5 Other pages
Dashboard (KPIs, pending approvals, alerts, recent runs, installed agents), Approvals queue (before/after diff, bulk approve, undo), Runs list + trace, Alerts, Brand Memory (brand voice, audience, policies, banned words — shared by all agents), Settings (team, granted permissions, billing, usage, data export/delete).

---

## 9. Agent runtime

`apps/agents/runtime.py` — one function runs any agent version:

```
run_agent(installation, trigger, input) ->
  build system prompt (version instructions + shop info + memory + settings)
  tools = catalog tools selected in the version (+ delegate tools for orchestrator)
  loop (≤ max_steps):
    call Claude with messages + tools
    for each tool_use → executor (§6.3) → tool_result
    stop on end_turn, limit reached, or error
  save Run (status, output, tokens, cost) + RunSteps
```

- Runs execute in Celery; chat streams tokens to the browser via Redis pub/sub → SSE.
- Limits: per-run steps/tokens, per-shop daily spend (from plan), per-agent concurrency of 1 per shop for scheduled/event runs.
- **Triggers:** schedules = `django-celery-beat` task per installation; Shopify events = app subscribes to the **union of all topics used by any published agent** (plus mandatory ones); the webhook dispatcher saves the event and starts runs for every installation subscribed to that topic. Debounce noisy topics (e.g. `products/update`, 60s per product).

---

## 10. Data model (core)

```
User, Membership(user, shop, role)
Shop(domain, name, email, plan_name, currency, timezone, access_token_enc, granted_scopes[], installed_at, uninstalled_at)
Subscription(shop, shopify_charge_id, plan_key, status, trial_ends_at, current_period_end)

ToolDefinition(key, name, description, category, kind: CODE|GRAPHQL, scopes[], mutating,
               input_schema JSON, graphql_document, output_path, enabled, created_by)   # code tools synced from registry on deploy
AgentDefinition(slug, name, icon, category, tagline, description_md, screenshots, example_prompts[],
                visibility: PRIVATE|BETA|PUBLIC, allowed_shops M2M, featured, plans[], extra_price,
                auto_upgrade, status: DRAFT|PUBLISHED|ARCHIVED, created_by)
AgentVersion(agent, number, changelog, instructions_md, model, temperature, max_steps, max_tokens,
             tools M2M ToolDefinition, required_scopes[], settings_schema JSON, settings_defaults JSON,
             triggers JSON {chat, schedule_cron, events[]}, default_autonomy, allowed_autonomy[],
             published_at, published_by)
AgentTestCase(agent, prompt, settings JSON, checks JSON)   AgentTestResult(version, case, passed, run, details)
AgentInstallation(shop, agent, version, settings JSON, autonomy, enabled, schedule_enabled,
                  installed_by, installed_at, last_run_at)

ShopMemory(shop, key, value, source, updated_at)
Conversation(shop, user, installation|null for team, title)   Message(conversation, role, content JSON, agent_slug)
Run(shop, installation, version, parent_run, trigger: CHAT|SCHEDULE|EVENT|MANUAL|PLAYGROUND, status,
    input JSON, output JSON, tokens_in, tokens_out, cost_usd, celery_task_id, started_at, finished_at, error, traceback)
RunStep(run, idx, type: LLM|TOOL, name, input JSON, output JSON, duration_ms, error)
ActionProposal(shop, run, tool, payload JSON, preview JSON, status, decided_by, decided_at)  ActionSnapshot(proposal, before, after)
Alert(shop, installation, severity, title, body, read_at)
WebhookEvent(shop, topic, shopify_event_id UNIQUE, payload, received_at, processed_at, error)
UsageDaily(shop, agent, date, runs, tokens, cost_usd)
AuditLog(shop, user, action, meta JSON, created_at)
```

All shop-owned models inherit `ShopScopedModel`.

---

## 11. Billing

- Plans in `apps/billing/plans.py` (placeholders): Starter $19 (3 installed agents, 300 runs/mo), Growth $49 (10 agents, 1,500 runs, AUTO mode, schedules), Pro $149 (unlimited agents, 6,000 runs, team).
- Agents may carry an extra monthly price → added to the Shopify subscription as an additional line / recurring charge (decide the cleanest Billing API approach and document it).
- 7-day trial. `test: true` in dev / development stores. All gating through `apps/billing/entitlements.py`.

---

## 12. Security & compliance
- HMAC on launch, callback and webhooks; strict `shop` validation; encrypted tokens never rendered or logged.
- `@require_shop_access(role=…)` on every merchant view; `@staff_required` on every Studio view; tests prove no cross-shop access.
- Custom GraphQL tools can only be created by staff and must declare scopes; the executor refuses to run a mutation not marked mutating.
- Mandatory compliance webhooks: `customers/data_request`, `customers/redact`, `shop/redact`.
- CSRF on all forms/HTMX; webhooks exempt (HMAC instead). Secrets via env only.

---

## 13. Debugging & observability
- `structlog` JSON logs tagged with `shop_id`, `agent`, `run_id`, `task_id`; `make logs` tails web + worker.
- Sentry on web and worker. Flower for Celery. `django-debug-toolbar` in dev.
- Run trace page (merchant + Studio) with every step and Python traceback.
- Django Admin registered for all models with actions: retry run, replay webhook, re-sync shop.
- Management commands: `run_agent --shop <domain> --agent <slug> [--version N] --task "..."` (prints each step), `check_shop <domain>` (token, scopes, webhooks), `sync_tools` (registry → DB), `seed_agent_templates`.
- `docs/DEBUGGING.md` in plain language; `docs/AGENT_STUDIO_GUIDE.md` explaining how to build and publish an agent.

---

## 14. Build phases (each ends green: ruff, mypy, pytest)

1. **Foundation** — project, Docker, models, non-embedded OAuth (install, launch, uninstall, scope upgrade), magic link, store switcher, compliance webhooks, base UI kit, Django Admin, logging, Sentry.
2. **Tool Catalog** — `@tool` registry, executor with all rules (§6.3), all v1 code tools with tests (mocked GraphQL), custom GraphQL tools + test button.
3. **Agent runtime** — AgentDefinition/Version models, runtime loop, Run/RunStep, SSE chat, memory, `run_agent` command.
4. **Agent Studio** — builder (all sections in §7.1), settings form builder, playground, test cases, versioning + publish, Studio dashboard, seeded templates.
5. **Marketplace & installs** — marketplace pages, install flow with scope upgrade, settings rendering, my agents, orchestrator "My Team", approvals + undo, schedules + event triggers, alerts, dashboard.
6. **Billing & launch** — plans, per-agent pricing, entitlements, usage limits, onboarding, team settings, e2e tests, `docs/LAUNCH.md`.

---

## 15. Acceptance criteria
1. Fresh install on a dev store lands on **our** domain; no Polaris/App Bridge in the code (CI grep check).
2. With **zero code changes**, staff can create an agent in the Studio (e.g. "Rewrite product descriptions in brand voice" using `products.search`, `products.get`, `products.update`), test it in the playground, and publish it.
3. The published agent appears in the merchant Marketplace; installing it asks for `write_products` if not granted, runs re-OAuth, and completes the install.
4. Merchant asks the agent to rewrite 5 products → 5 approval cards → approve updates Shopify → undo restores the old text.
5. Staff creates a **custom GraphQL tool** in the Studio, adds it to an agent, publishes v2; installed shops get the update (or are asked for new scopes).
6. A scheduled agent runs at its time in the shop's timezone; an event-triggered agent runs on `orders/create`.
7. "My Team" chat automatically includes every installed chat-enabled agent.
8. Uninstall stops everything; compliance webhooks handled; plan limits enforced; every run has a full trace.

---

## 16. Environment variables (`.env.example`)

```
APP_NAME=AgentHub
APP_URL=https://app.example.com
DJANGO_SECRET_KEY=
DJANGO_DEBUG=false
ALLOWED_HOSTS=app.example.com
SHOPIFY_API_KEY=
SHOPIFY_API_SECRET=
SHOPIFY_API_VERSION=2025-10
SHOPIFY_BASE_SCOPES=read_products
DATABASE_URL=postgres://...
REDIS_URL=redis://...
ENCRYPTION_KEY=
ANTHROPIC_API_KEY=
ALLOWED_MODELS=            # comma-separated model IDs shown in the Studio
DEFAULT_MODEL=
RESEND_API_KEY=
SENTRY_DSN=
```

---

## 17. Out of scope for v1
Merchants building their own agents, third-party developers publishing agents, storefront chat widget, WhatsApp/Instagram channels, Shopify Flow actions, multi-language UI. Keep the registry/versioning design ready for these.
