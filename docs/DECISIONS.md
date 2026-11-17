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
