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

## 4. Useful commands

```bash
make check                              # lint + types + tests (what CI runs)
python manage.py check_shop demo.myshopify.com            # token, scopes, webhooks
python manage.py check_shop demo.myshopify.com --offline  # without calling Shopify
python manage.py shell                  # poke at the database in Python
```

Django Admin (`/admin/`) actions:
- **Shops → "Re-sync shop details and scopes"**
- **Webhook events → "Replay selected webhooks"**
