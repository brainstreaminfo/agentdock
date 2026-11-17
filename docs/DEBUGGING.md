# Debugging guide

A plain-language guide to finding out what went wrong. Updated each phase.

## 1. Where to look first

| Symptom | Look here |
|---|---|
| A page shows an error | The terminal running `make run` (dev) or Sentry (production). Every log line has a `request_id`; the same id is in the `X-Request-ID` response header. |
| Something "didn't happen" in the background | The terminal running `make worker`, or Flower at http://localhost:5555 (`make flower`). |
| A store isn't working | `python manage.py check_shop <store>.myshopify.com` |
| A webhook didn't do anything | Django Admin → **Webhook events**. The status shows PENDING / PROCESSED / FAILED / IGNORED, and FAILED events include the full Python traceback. |
| "Who did that?" | Django Admin → **Audit logs** |

## 2. Reading logs

Logs are structured: each line is an event name plus key/value pairs, e.g.

```
2026-11-17T10:00:00Z [info] shopify.graphql  op=ShopInfo duration_ms=210 cost=2 shop_id=4 request_id=9f1c…
```

- In dev they are coloured and readable (`LOG_JSON=false`). In production they
  are JSON, so your host's log search can filter on `shop_id=4`.
- Follow one request: search for its `request_id`.
  Follow one background job: search for its `task_id` (shown in Flower).
- Secrets are never logged. Access tokens, refresh tokens, passwords and API
  keys are replaced by `[redacted]`, and emails / phone numbers by `[email]` /
  `[phone]`.

Add a log line in your own code:

```python
from apps.core.logging import get_logger

log = get_logger(__name__)
log.info("something.happened", product_id=123)
```

## 3. Common problems

**"This link was not signed by Shopify" (403) on launch or callback**
`SHOPIFY_API_SECRET` in `.env` doesn't match the app in the Shopify Dev Dashboard.

**OAuth error "redirect_uri is not whitelisted"**
`APP_URL` must match the `redirect_urls` in `shopify.app.toml` exactly, including
the trailing `/shopify/callback/`. For local testing use a tunnel (e.g.
`cloudflared tunnel --url http://localhost:8000`), set `APP_URL` to the tunnel
URL, and update the TOML.

**"Let's try that again / session expired" after approving on Shopify**
The session cookie was lost between starting OAuth and coming back, often
because `APP_URL` and the address in the browser are different hosts. Open the
app on the same host as `APP_URL`.

**A shop suddenly has no access (`ShopNotInstalled`, `ReauthRequired`)**
The refresh token was rejected (the merchant uninstalled the app, or no refresh
happened for 90 days). The Audit log shows `shop.token_revoked`. Opening the app
from Shopify Admin runs OAuth again and fixes it.

**Shopify GraphQL errors**
The error message includes Shopify's own text, e.g.
`GraphQL error in ShopInfo: Field 'x' doesn't exist`. A `THROTTLED` error is
retried automatically, so you'll only see it in logs as
`shopify.graphql.throttled`.

**`DecryptionError`**
`ENCRYPTION_KEY` was changed. Put the old key back after the new one
(`ENCRYPTION_KEY=new,old`), then run `python manage.py rotate_encryption`.

## 4. Tools

**Try a tool by hand:** Studio → Tool catalog → pick a tool → **Test** (runs
against a store you're a member of). The result and a full trace step are shown.

**An agent says a tool failed:** open the run in Django Admin → **Runs**. Each
tool call is a step with its input, output, duration and error. A Python
traceback is included if the tool crashed.

**"This store hasn't given the app permission for …"** The tool needs a scope
the merchant hasn't granted. An alert with a "grant access" link was created
for them (Django Admin → **Alerts**). Granting goes through OAuth again.

**"Shopify rejected the change: field: message"** Shopify's own validation
message for a mutation (e.g. title too long). The proposal shows `FAILED` with
this text in Django Admin → **Action proposals**.

**Approve / undo while debugging:** Django Admin → **Action proposals** → select
→ "Approve and apply" or "Undo". The snapshot (before/after) is shown on each
proposal.

**A query broke after changing `SHOPIFY_API_VERSION`:** run `make schema` (fetch
the new schema), then `make test`. The test names the field or input that changed.

## 5. Agents and chat

**The chat says "Thinking…" forever.** Runs execute in the Celery worker. Is
`make worker` running? Is Redis up? In Django Admin → **Runs**, a run stuck in
`QUEUED` was never picked up by a worker.

**Run an agent from the terminal and watch every step:**

```bash
python manage.py run_agent --shop demo.myshopify.com --agent sales-qa-analyst \
    --task "How did sales go last week?"          # add --version draft to test the draft
```

It prints the streamed answer, each tool call, and a summary with tokens and
cost. It calls the real Claude API and costs money. It needs `ANTHROPIC_API_KEY`
in `.env` (or `ant auth login`).

**Why did the agent do that?** Open the run: click "Details" under the answer in
chat, or Django Admin → **Runs**. Each model turn shows its reasoning summary,
the tools it called with their inputs and outputs, the stop reason, tokens and
cost. Staff also see the Python traceback of a failed run.

**"Today's AI budget for this store is used up"** The shop reached
`DEFAULT_DAILY_SPEND_LIMIT_USD` (UTC day). See Django Admin → **Usage daily**.

**"AI service error 529" / "busy (rate limited)"** Anthropic is overloaded or
we hit a rate limit. The SDK already retried 3 times; ask again in a minute.

**Unknown variable {{x}} in the run output's `warnings`.** A typo in the agent's
instructions. Valid variables: `{{shop.name}}`, `{{shop.domain}}`,
`{{shop.currency}}`, `{{shop.timezone}}`, `{{memory}}`, `{{memory.KEY}}`,
`{{settings.KEY}}`, `{{today}}`.

**A Studio test stays "Running…".** Tests run in the Celery worker: is
`make worker` running? Each test has a **Trace** link once it starts.

**An agent works in the playground but merchants still get the old behaviour.**
The playground runs the *draft*. Merchants get published versions only: publish
it. Shops that must grant new permissions stay on the old version until they
approve.

## 6. Installs, schedules and events

**"Install" keeps asking for permissions.** The merchant declined some access
in Shopify, or Shopify returned fewer scopes. Compare the agent's required
scopes (Marketplace page) with **Settings → Permissions granted**.
`check_shop <domain>` shows what Shopify actually granted.

**A scheduled agent didn't run.** It needs all of: the installation is active
(not paused), "Run on its schedule" is ticked, its version has a cron, the shop
is installed. Then check:
- Django Admin → **Periodic tasks** → `agent-installation-<id>` exists, is
  enabled, and its crontab shows the shop's timezone;
- `make beat` is running (beat sends the task) and `make worker` too;
- worker logs: `trigger.schedule_run` (started) or `trigger.schedule_skipped`.
The agent page shows "next run" in the store's local time.

**An event-triggered agent didn't run on `orders/create`.**
1. Django Admin → **Webhook events**: did the event arrive? If not, check the
   subscription: `check_shop <domain>` lists the shop's webhook subscriptions.
   They're created per shop when an agent using that topic is installed (worker
   log `webhooks.synced`). A topic whose read scope isn't granted is skipped.
2. Event status `IGNORED` = no enabled installation listens to that topic.
3. `products/update`, `inventory_levels/update` and `customers/update` are
   debounced: the run starts about 60 s after the last update of that product.
4. If the agent was busy, the event run waits and retries (`run.waiting_for_lock`
   in the logs) for up to 10 minutes.

**My Team didn't use an agent.** Only installed, active, chat-enabled agents are
in the team. My Team picks agents from their tagline and description, so make
those clear in the Studio. The trace of a My Team run shows each `delegate:<slug>`
step and links to the agent's own run.

## 7. Useful commands

```bash
make check                              # lint + types + tests (what CI runs)
python manage.py check_shop demo.myshopify.com            # token, scopes, webhooks
python manage.py check_shop demo.myshopify.com --offline  # without calling Shopify
python manage.py shell                  # poke at the database in Python
python manage.py sync_tools             # register built-in tools in the database
make schema                             # download Shopify's schema for SHOPIFY_API_VERSION
python manage.py seed_agent_templates   # (re)create the 3 example agents
python manage.py install_agent --shop X --agent Y --publish   # dev: install an agent
```

Django Admin (`/admin/`) actions:
- **Shops → "Re-sync shop details and scopes"**
- **Webhook events → "Replay selected webhooks"**
