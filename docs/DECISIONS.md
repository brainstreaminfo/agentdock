# Decisions

Choices made while building, especially where the spec was ambiguous or had to
change. Newest phase at the bottom. Format: **decision**: why.

---

## Phase 1 — Foundation (2026-11-17)

### Changes to the spec forced by Shopify

1. **Expiring offline tokens + refresh tokens, not plain offline tokens.**
   Shopify requires *new* public apps to use expiring offline tokens since
   2026-04-01, and *all* public apps from 2027-01-01. A non-expiring token would
   stop working. So:
   - the OAuth code exchange sends `expiring=1`;
   - we store `access_token` (valid 1 hour) and `refresh_token` (90 days), both
     Fernet-encrypted, with their expiry times (`Shop.*_expires_at`);
   - `apps/shopify_app/tokens.get_access_token(shop)` refreshes automatically when
     less than 5 minutes are left. It locks the Shop row so two workers never
     refresh at once (Shopify warns this invalidates one of the results);
   - a daily beat task (`refresh_dormant_tokens`) refreshes shops whose refresh
     token would expire within 14 days, so inactive stores keep access;
   - if Shopify rejects a refresh (401), we clear the tokens. The shop must go
     through OAuth again (launching the app from Shopify Admin does that).

2. **`SHOPIFY_API_VERSION=2026-10`, not `2025-10`.** 2025-10 stopped being
   available on 2026-10-16. 2026-10 is the newest stable version (supported
   until 2027-10-16). Update this every year. `check_shop` shows if calls work.

3. **`use_legacy_install_flow = true` in `shopify.app.toml`.** We ask for extra
   scopes per agent by sending the merchant back through OAuth with a `scope`
   URL parameter (spec §5.6, §8.3). Shopify-managed installation only grants the
   scopes listed in the TOML, and adds optional scopes only through App Bridge
   (`scopes.request()`), which is embedded-only. Shopify says the legacy flow is
   "supported but not recommended" because installs can end up with different
   scopes. That is exactly what we want here.

### Shopify App Store requirements check (non-embedded)

Checked against shopify.dev/docs/apps/launch/app-requirements-checklist:

| Requirement | Our status |
|---|---|
| Authorize with OAuth immediately on install, before anything else (also on reinstall) | ✅ `/shopify/launch/` sends unknown/uninstalled shops straight to OAuth |
| No pop-ups for OAuth or charge approval | ✅ full-page redirects only |
| Request only the scopes you need | ✅ base scope is `read_products`; agents add more |
| Bill through Shopify | ⏳ Phase 6 (Billing API) |
| Merchants can upgrade/downgrade a plan without contacting support or reinstalling | ⏳ Phase 6 |
| Compatible with SameSite cookies | ✅ `SameSite=Lax` works because we're never in an iframe |
| Mandatory compliance webhooks | ✅ `customers/data_request`, `customers/redact`, `shop/redact` |
| Privacy policy in the listing | ⏳ before launch (`docs/LAUNCH.md`, Phase 6) |
| Detect store changes through webhooks | ✅ `shop/update`, `app/scopes_update` |
| Don't use APIs deprecated within 90 days | ✅ 2026-10; `ShopPlan.displayName` (deprecated) is not used, `publicDisplayName` is |
| "Built for Shopify" | ❌ requires embedded. Accepted, as the spec says |

For Phase 6: Shopify recommends "Shopify App Pricing" (managed pricing). It
only supports fixed plans, so it can't do per-agent extra prices (§11). We'll
probably need the Billing API. That gets decided in Phase 6.

### Architecture

4. **New app `apps/core`** (not in the spec layout) for shared code that belongs
   to no single feature: `ShopScopedModel`, `AuditLog`, encryption, logging,
   access decorators, rate limiter, Resend email backend, UI template tags.

5. **List fields are `JSONField(list)`, not Postgres `ArrayField`.** They're
   simpler to read and work the same on any database. Scope checks happen in
   Python (`apps/shopify_app/scopes.py`), not in SQL.

6. **Scopes: `write_X` counts as `read_X`.** Shopify may return only
   `write_products` when we asked for both. `scopes.satisfies()` / `missing()`
   account for this, and `scopes.union()` drops redundant `read_X` before
   requesting.

7. **One webhook URL for every topic** (`/shopify/webhooks/`). The topic comes
   from the `X-Shopify-Topic` header. App lifecycle and compliance topics are
   declared in `shopify.app.toml`. Agent event topics (`orders/create`, …) will be
   registered per shop through the Admin API in Phase 5, because they depend on
   which agents a shop installs.

8. **Webhooks are saved first, then processed in Celery.** The view only checks
   the HMAC, stores a `WebhookEvent` and returns 200 (Shopify times out after
   5 s). `shopify_event_id` is unique (from `X-Shopify-Event-Id`, falling back to
   `X-Shopify-Webhook-Id`), so Shopify's retries are processed once. Events are
   deleted after 30 days because payloads can contain customer data. Failed
   events can be replayed from Django Admin.

9. **Signals decouple uninstall/compliance from later phases.**
   `shop_uninstalled`, `shop_installed`, `shop_scopes_updated`,
   `customers_redact_requested`, `customers_data_requested` and
   `shop_redact_requested` live in `apps/shopify_app/signals.py`. In Phase 5 the
   agents, triggers and runs apps connect to them (pause agents, revoke Celery
   tasks, delete customer data in run logs) without changing `shopify_app`.

10. **Resuming after re-OAuth uses a generic `next` URL.** `/shopify/auth/?next=…`
    stores a same-site URL in the session, and the callback redirects there. The
    Phase 5 install flow stores `pending_agent_install` in the session and passes
    the resume URL as `next`. Only same-host URLs are accepted (no open redirect).

11. **"Enqueue catalog sync" (§5.3) = `sync_shop` task** (shop details +
    granted scopes). We don't cache products: tools query Shopify live. If an agent
    later needs a local product cache, add it as its own task.

### Auth & sessions

12. **Opening the app from Shopify Admin logs in the shop's OWNER user.** An
    offline token doesn't tell us which staff member clicked. So a valid launch
    link logs in the earliest OWNER membership of that shop, unless the browser
    already has a session for a member of that shop. To stop a leaked launch URL
    being reused, the link must be at most 5 minutes old
    (`SHOPIFY_LAUNCH_MAX_AGE_SECONDS`) and each signature works once (stored in
    the cache). Otherwise we send the browser through OAuth, which is a strong
    proof. *Future option:* online (per-user) tokens would identify each staff
    member, at the cost of one extra redirect per login.

13. **Users log in by email; there are no usernames.** Merchants get an unusable
    password and use the Shopify launch or a magic link. Staff (Agent Studio and
    Django Admin) are created with `make superuser` and may use passwords at
    `/admin/`.

14. **Magic links:** only for existing users (no sign-up by email, because
    accounts come from installs). Responses are identical whether or not the
    email exists. Only the SHA-256 of the token is stored. Links are single-use
    (atomic update) and expire after 15 minutes. Limit: 5 emails per address per
    15 minutes.

15. **Connecting another store while logged in** makes the logged-in user OWNER
    of that store (they just approved it in Shopify), instead of creating a new
    user from the shop's email.

16. **Shop ids in URLs are the integer primary key**, and every merchant view
    checks membership. Other shops' ids, and uninstalled shops, return **404,
    not 403**, so they don't reveal which ids exist.

17. **On an OAuth state mismatch the callback shows a "Try again" button
    instead of redirecting automatically.** That way a browser that blocks
    cookies can't end up in a redirect loop.

### Tooling

18. **Django 5.2 LTS** (the spec says 5.x). **Python ≥ 3.12** (Docker uses 3.12;
    local dev also works on 3.13).

19. **Tailwind v4 standalone CLI**, downloaded by `scripts/vendor.sh` along with
    pinned HTMX, the HTMX SSE extension, Alpine, Chart.js and Lucide icons. All
    are self-hosted in `static/vendor/`, with no CDNs at runtime. The built
    `static/css/app.css` is committed, so the app runs without building CSS. If
    the binary can't run on a machine, use
    `make css TAILWIND="npx @tailwindcss/cli@4.1.11"` (needs a `tailwindcss`
    package resolvable from `assets`).
    Tailwind is told to scan only `templates/` and `apps/` (`source(none)`);
    otherwise it scans `.venv` and hangs.

20. **UI kit = Tailwind component classes** (`btn`, `card`, `badge`, `input`,
    `table`, `alert`, …) in `assets/tailwind.css`, plus a few `{% include %}`
    components in `templates/components/`. Icons:
    `{% load ui %}{% icon "name" %}` inlines a Lucide SVG.

21. **Non-embedded is enforced by CI.** `scripts/check_no_embedded.sh` fails on
    any Polaris or App Bridge reference (acceptance criterion §15.1).

22. **Default models** in `ALLOWED_MODELS`: `claude-sonnet-5-5`,
    `claude-opus-5-5`, `claude-haiku-5-5`. Change them in `.env` without code
    changes.

---

## Phase 2 — Tool Catalog (2026-11-17)

### Shopify API findings (checked against the real 2026-10 schema)

23. **Shopify's Admin schema is stored in the repo** (`apps/shopify_app/schema/admin-2026-10.graphql.gz`,
    124 KB). It's downloaded with `make schema` from the public proxy behind
    shopify.dev's GraphiQL. Three checks use it:
    - a test validates every catalog `.graphql` file;
    - the test fake (`tests/tools/conftest.py`) checks every variable a tool
      sends against the real input types: fields, enums and required values;
    - the Studio validates custom GraphQL tools before saving them.

    So queries and mutation inputs are verified without a real store. **When you
    bump `SHOPIFY_API_VERSION`, run `make schema` and `make test`**; deprecated or
    renamed fields then show up as test failures.

24. **Collections: products are added through "sources", not
    `collectionAddProducts`.** `collectionAddProducts` is deprecated in 2026-10.
    `collections.add_products` adds products to a source called "Added by
    AgentHub" on the collection, creating it the first time. Undo removes only
    the products we added. Products that were already in the collection are
    skipped and never removed.

25. **Inventory changes send an idempotency key and use compare-and-swap.** Since
    2026-04, `inventoryAdjustQuantities` requires `@idempotent(key:)`. We use the
    approval's UUID (and `<uuid>-undo` for undo), so a retried request never
    adjusts stock twice. `changeFromQuantity` makes the change fail if stock moved
    after we read it.

26. **Analytics are computed from orders, not ShopifyQL.** `shopifyqlQuery` exists
    in 2026-10, but it needs `read_reports` and **Level 2 protected customer data**
    approval, which a new app won't have. Our approach needs only `read_orders`.
    Limits:
    - at most 2,500 orders per call (`truncated: true` is returned beyond that);
    - only the last 60 days of orders without `read_all_orders` (which needs
      approval from Shopify);
    - line items are capped at 30 per order for `analytics.top_products`.

    Totals use each order's *current* total in the shop's currency: after refunds
    and edits, including tax and shipping. Cancelled and test orders are excluded.
    Days are counted in the shop's timezone.

27. **Protected customer data.** Orders and customers are "protected customer
    data". Before launch, apply in the Partner Dashboard for Level 1 (orders,
    customers) and Level 2 (names and emails, used by `customers.*`,
    `orders.get` and `analytics.top_customers`). Development stores work
    without approval. Added to the launch checklist for Phase 6.

28. **Pages and articles use `read_content` / `write_content`.** Those scopes
    cover both pages and blogs; using `*_online_store_pages` too would add a
    second scope for nothing. **`read_price_rules` was removed in 2026-10**;
    discounts use `read_discounts` / `write_discounts`.

### Design

29. **One `.graphql` file per domain** (`catalog/products.graphql` next to
    `catalog/products.py`), holding several named operations. The client sends
    `operationName`. Everything a domain sends to Shopify is in one readable file.

30. **Tools return plain dicts, not Pydantic output models.** Inputs are strict
    Pydantic models (unknown fields rejected, so AI typos are caught). Outputs are
    dicts, which keeps 50 tools readable. The executor makes them JSON-safe.

31. **Every mutating tool has preview, snapshot and undo:**
    - `preview` shows the approver a before/after diff;
    - `snapshot` saves the data just before applying;
    - `undo` restores it, and touches only the fields that were changed.

    Creations are undone by deleting what was created (collection, page, article,
    discount). A test makes undo mandatory for every built-in mutating tool.
    Custom GraphQL tools can't be undone; the Studio shows a "No undo" badge.

32. **AUTO mode still creates an `ActionProposal`** (status `APPLIED`,
    `auto_applied=True`) with a snapshot. Every change, approved or automatic,
    appears in the same history and can be undone.

33. **Approval lives in `apps/tools/executor.py`** (`apply_proposal`,
    `reject_proposal`, `undo_proposal`). It's next to the executor because it
    runs the same tool code. The proposal row is locked (`SELECT … FOR UPDATE`)
    and must still be PENDING, so a double click applies once. Django Admin has
    Approve and Undo actions for debugging, until the merchant approvals page
    exists in Phase 5.

34. **Models created early**, because the executor needs them now:
    - `Run`, `RunStep`, `ActionProposal`, `ActionSnapshot` (`apps/runs`);
    - `Alert` (`apps/dashboard`);
    - `ShopMemory` and `Autonomy` (`apps/agents`).

    Phase 3 adds agent/installation links with new migrations.

35. **The stored trace is redacted.** RunStep input and output have emails and
    phone numbers masked, the same as logs. This keeps customer data out of our
    database (helps `customers/redact`). The cost is that traces show `[email]`
    instead of the address.

36. **Prompt-injection guard:** every tool result is sent to the AI behind a fixed
    notice that store text is data, never instructions. Control characters are
    stripped, strings are capped at 20,000 characters and results at 100,000.
    Phase 3 repeats the rule in the system prompt.

37. **Metafield tools check scopes at run time.** The scope needed depends on the
    owner (product → `products`, customer → `customers`, …), so `metafields.*`
    declare no static scopes and call `ctx.require_scopes()`. A missing scope gives
    the same error and alert as rule 1. The Studio notes this on the tool page.

38. **`web.fetch_url` is off by default** (spec). When enabled, it only fetches
    public internet addresses: every redirect hop is resolved and private,
    loopback and link-local IPs are refused, which blocks SSRF to cloud metadata
    and internal services. Limits: 1 MB download, 20,000 characters of text,
    text content types only.

39. **The Studio Test button only targets stores the staff member belongs to.**
    Connect your own dev store first. Mutating tools need a "this changes data"
    checkbox and are then applied directly (no approval step). Each test creates
    a `Run` with trigger `TOOL_TEST`, so it has a full trace.

40. **`sync_tools` runs on every deploy** (pre-deploy command). It never
    overwrites the `enabled` flag, disables code tools that were removed, and
    refuses a custom tool that reuses a built-in key.

---

## Phase 3 — Agent runtime (2026-11-17)

### Claude API changes that affect the spec

41. **Per-agent "temperature" is replaced by "effort".** Current Claude models
    (Opus 5.5, Sonnet 5.5, Haiku 5.5…) reject `temperature` with an error. How hard
    the model thinks is set with `effort` instead: `low` / `medium` / `high` /
    `xhigh` / `max` (`AgentVersion.effort`, default `medium`). Thinking is always on
    (adaptive); we ask for summarized thinking so traces show the model's reasoning.

42. **Default model `claude-opus-5-5`** (Anthropic's recommended default).
    `ALLOWED_MODELS` also offers `claude-sonnet-5-5` (cheaper) and
    `claude-haiku-5-5` (cheapest, for simple agents). Prices are in
    `settings.MODEL_PRICING`; update them there when Anthropic changes prices.

43. **Refusal fallbacks are on.** If Claude's safety classifier declines a turn,
    Anthropic re-runs it on a fallback model in the same call (`fallbacks:
    "default"`, beta `server-side-fallback-2026-07-01`). This applies to the
    models that support it (`LLM_FALLBACK_MODELS`). After such a fallback,
    `apps/agents/llm.content_for_history` drops the blocks Anthropic says must not
    be sent back. If the whole chain refuses, the agent answers politely that it
    can't help.

44. **Forced tool choice isn't used.** Current models reject `tool_choice`
    any/tool. Agents use the default (`auto`) and are steered by their
    instructions.

### Runtime design

45. **Our own loop, not the SDK's beta tool runner.** Every tool call must go
    through `apps/tools/executor.py` (scopes, approvals, trace), and we need
    per-turn cost tracking, budgets and live streaming to the browser. The loop is
    about 100 readable lines in `apps/agents/runtime.py`.

46. **Always streaming, with eager tool-input streaming.** Turns are streamed:
    text goes live to the chat, and long turns can't hit HTTP timeouts. Tool
    inputs (e.g. a long new product description) stream as they're written
    (`eager_input_streaming`). With that on, Anthropic no longer validates tool
    inputs, which is fine: our executor validates every input with strict Pydantic
    models. A tool call cut off by the output limit is never executed.

47. **The conversation history is append-only.** `chat.Message` rows hold the
    exact content blocks sent to and received from Claude, including thinking
    blocks with their signatures, and they are never edited or deleted. Current
    models check that earlier turns are unchanged ("preserved thinking"); editing
    history would break conversations.

48. **Prompt caching:** the system prompt and tool list are built the same way on
    every turn, with no ids or timestamps; only the date changes, once a day. The
    request uses automatic caching (`cache_control` on the request), so later turns
    and conversations reuse the cached prefix. Cache reads cost about 10% of normal
    input.

49. **Tool names:** our keys use dots (`products.search`), but Claude tool names
    can't, so the API name is `products__search`. The runtime maps back.

50. **Limits per run:**
    - `max_steps` model turns;
    - `max_tokens` output tokens in total, with each turn capped at
      `LLM_MAX_TOKENS_PER_TURN` (the run stops when fewer than 1,024 remain);
    - a per-shop daily spend cap (`DEFAULT_DAILY_SPEND_LIMIT_USD`, $10), which
      Phase 6 replaces with plan limits. "Daily" is the UTC day;
    - one scheduled/event run at a time per installation (a cache lock);
      chat isn't limited.

    When a limit stops a run, the chat shows why.

51. **Live events use Redis Streams, not pub/sub** (the spec said pub/sub).
    Events are kept for an hour, so a browser that connects after the agent
    started, or reconnects, replays what it missed (`Last-Event-ID`). With pub/sub
    those events would be lost. The SSE view is async (ASGI), so waiting
    connections don't hold a worker thread. Tests use an in-memory backend.

52. **The live chat uses a small `EventSource` script** (`static/js/app.js`)
    instead of the HTMX SSE extension. It's easier to read and debug (events:
    `status`, `delta`, `tool`, `done`, `failed`). When a run finishes, the server
    sends the fully rendered answer (Markdown, charts, tables, approval cards),
    which replaces the live bubble. The failure event is named `failed`, not
    `error`, because browsers use `error` for connection problems.

53. **Model text is sanitized.** Markdown is rendered to HTML and cleaned with
    `nh3` (no scripts, no `javascript:` links), because answers can quote store
    data.

54. **Approving a change requires the ADMIN role** (or OWNER). Members can chat
    and see proposals but not approve them. Phase 6 team settings may refine this.

### Models added

55. Added in this phase:
    - `AgentDefinition` / `AgentVersion` (spec §10). Each agent has **one
      editable draft** (`is_draft=True`, enforced by a database constraint).
      `draft.publish()` copies it into an immutable numbered version and computes
      `required_scopes` from the tools;
    - `AgentInstallation`, so the runtime and chat have something to run (the
      Marketplace install flow is Phase 5);
    - `Conversation` / `Message` (`apps/chat`);
    - `UsageDaily` (`apps/runs`, used by the spend limit).

56. **Example templates are seeded now** (`seed_agent_templates`, spec §7.5,
    planned for Phase 4), so there are real agents to run. They're marked
    `is_template` and never published by the platform. `manage.py install_agent
    --publish` is a development shortcut until the Marketplace exists.

57. **`run_agent --version`**: Django reserves `--version` for every command; this
    command overrides it so the spec's interface works
    (`--version 2` / `--version draft`).

---

## Phase 4 — Agent Studio (2026-11-17)

58. **One draft per agent, edited section by section.** The builder only changes
    the draft. Listing and availability fields (name, tagline, visibility,
    price…) live on `AgentDefinition` and apply immediately. Everything that
    changes behaviour lives on the version (`AgentVersion.CONFIG_FIELDS`) and only
    reaches merchants on **Publish**.

59. **Templates are read-only.** The 3 seeded templates can be viewed and
    **cloned**, but not edited or published (POSTs are refused). A clone copies the
    draft, tools and test cases.

60. **Merchant settings form = JSON Schema**, built from a simple field list
    (`apps/agents/settings_schema.py`). Field types: text, textarea, number,
    select, multi-select, toggle, product picker, collection picker.
    - The same module renders the schema as a Django form (playground now,
      merchant install page in Phase 5) and validates values with `jsonschema`.
    - Field order is stored in `x-order`, because Postgres `jsonb` doesn't keep
      key order.
    - Product and collection pickers store lists of Shopify ids. The Studio
      preview shows a text box; Phase 5 turns it into a search picker.

61. **Event triggers add their scopes.** `compute_required_scopes()` is the union
    of the tools' scopes and the read scope each selected Shopify event needs
    (`apps/shopify_app/topics.py`). Cron schedules are validated with Celery's own
    crontab parser.

62. **Autonomy is told to the model.** The system prompt gets one paragraph for
    the run's autonomy level. *Suggest*: change things only when asked.
    *Approve*: propose changes freely. *Auto*: changes apply immediately, be
    careful. Approval enforcement itself stays in the executor.

63. **Playground and tests never apply changes.** Runs with trigger
    `PLAYGROUND` or `TEST` always send mutations to approval, even for AUTO
    agents. After a test, its pending proposals are rejected automatically.
    Playground chats are `Conversation`s linked to a version (not an
    installation), are hidden from the merchant chat list, and run on stores the
    staff member belongs to.

64. **Test results are tied to the draft's configuration fingerprint** (a hash of
    the config fields and tool keys). The publish page shows whether tests passed
    *on this exact draft*. Tests run as Celery tasks (one per case), and the
    results table refreshes itself every 2 seconds while any are running.

65. **Publishing upgrades installs automatically when it's safe.** An
    installation moves to the new version immediately if the agent allows
    auto-upgrade and the shop already granted every required scope. Otherwise
    it stays on its version: Phase 5 shows the merchant "Update available"
    (`upgrades.pending_update`) and asks for the new permissions.

66. **Rollback = restore + publish.** "Restore as draft" copies an old version
    into the draft; publishing it creates a new version number. Published
    versions are never edited.

67. **Studio stats exclude playground, test and tool-test runs**, so analytics
    reflect merchant usage. "Revenue" is currently extra price × active shops;
    Phase 6 adds plan revenue from billing.

68. **Screenshots use Django's default file storage** (`MEDIA_ROOT` locally).
    Railway's disk is wiped on every deploy, so before launch set up persistent
    storage (an S3-compatible bucket via `django-storages`). Added to the Phase 6
    launch checklist.

69. **Plans are placeholders in `apps/billing/plans.py`** (Starter, Growth, Pro,
    spec §11), so agents can be assigned to plans now. Billing and enforcement
    arrive in Phase 6.
