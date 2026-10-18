# Lakoniya Clean API

FastAPI backend for the Telegram customer Mini App and owner panel. It is still being prepared for production; do not accept real customer data until persistent storage and live end-to-end checks are complete.

## Access model

- `OWNER_TELEGRAM_IDS` is a comma-separated allowlist. Every listed ID has the same implemented owner API permissions: list requests and update request statuses.
- In Render, include both the primary owner and manager IDs in `OWNER_TELEGRAM_IDS`. Keep production IDs and all bot tokens out of this public repository.
- The owner panel validates Telegram `initData` with `OWNER_BOT_TOKEN`, falling back to `TELEGRAM_BOT_TOKEN` for backwards compatibility.
- Customer request creation validates `initData` using the separate `CUSTOMER_BOT_TOKEN`. Never reuse the owner bot token for the customer bot.
- CORS is restricted to configured HTTPS origins. CORS is not authentication; server-side signature validation and the owner allowlist provide authentication/authorization.
- SQLite requires persistent storage. Render's local filesystem may be ephemeral; do not store real customer data there without a persistent disk or managed database.

## Hosting environment variables

- `OWNER_BOT_TOKEN`: owner bot token (recommended).
- `TELEGRAM_BOT_TOKEN`: backwards-compatible fallback for the owner bot token.
- `CUSTOMER_BOT_TOKEN`: separate token for the customer bot; required for customer submissions.
- `OWNER_TELEGRAM_IDS`: comma-separated numeric Telegram IDs with equal access to implemented owner endpoints.
- `ALLOWED_ORIGINS`: comma-separated exact HTTPS origins allowed to call the API.
- `DATABASE_URL`: optional managed PostgreSQL connection URL; when set, PostgreSQL is used instead of SQLite. Keep the URL secret.
- `DATABASE_PATH`: local SQLite path used only when `DATABASE_URL` is unset.
- `ENABLE_SYNTHETIC_TEST_REQUESTS`: temporary test-only switch; defaults to disabled.

## Database notes

- PostgreSQL is selected when `DATABASE_URL` is set; SQLite remains the local/test default.
- Render Free Postgres is suitable only for temporary testing: it expires after 30 days and does not include backups. Back up/export data and upgrade or migrate before expiry; do not use it as the sole production store.

## Tests

From this directory run `pytest -q`. Tests use fake tokens and synthetic data only; never use production tokens in tests.

## Local run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export OWNER_BOT_TOKEN='owner-test-token'
export CUSTOMER_BOT_TOKEN='customer-test-token'
export OWNER_TELEGRAM_IDS='primary-owner-id,manager-id'
export ALLOWED_ORIGINS='https://lakoniya-miniapp-test.pages.dev'
export DATABASE_PATH='./data/lakoniya.sqlite3'
uvicorn app:app --host 127.0.0.1 --port 8000
```

Do not enable real customer submissions until persistent storage, backups, request limits, the per-bot authentication flow, privacy/legal requirements, and live Telegram end-to-end behavior have been reviewed. Never paste a bot token into GitHub or this chat.
