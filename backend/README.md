# Lakoniya Clean API (starter)

This is a backend starter. It is not yet a complete production system and the existing customer/owner HTML pages remain demo-only until separately wired to these endpoints.

## Access model

- `OWNER_TELEGRAM_IDS` is a comma-separated allowlist of Telegram numeric user IDs. Every ID in the list receives the same currently implemented owner-level API permissions: list requests and update request statuses.
- For the manager who should have the same permissions as the primary owner, set `OWNER_TELEGRAM_IDS` in Render to a comma-separated list containing BOTH the primary owner's Telegram ID and `1086767973`. Do not commit real IDs to this public repository.
- This does not create bot-settings or staff-management features; those APIs and UI do not exist yet. Do not describe the current release as having those capabilities.
- Validates Telegram Mini App `initData` signatures server-side using `TELEGRAM_BOT_TOKEN`; rejects missing, invalid, or older-than-one-hour authentication data.
- Customer creation requires valid Telegram Mini App authentication. Before wiring the customer app, the backend must be adapted to validate the customer bot's distinct token; do not reuse the owner bot token for both bots.
- CORS is restricted to configured HTTPS origins. CORS is not authentication; signature and allowlist checks provide authorization.
- SQLite requires persistent storage on the chosen host. Render free local filesystem is ephemeral; do not store real customer data there.

## Hosting environment variables

- `TELEGRAM_BOT_TOKEN`: token for the Telegram bot whose Mini App is authenticating. Store only as a hosting secret.
- `OWNER_TELEGRAM_IDS`: comma-separated numeric IDs with equal access to the implemented owner endpoints.
- `ALLOWED_ORIGINS`: exact HTTPS origins allowed to call the API.
- `DATABASE_PATH`: path on persistent storage.

## Tests

From this directory, run `pytest -q`. Tests use fake IDs and a test-only token; never use a production token in tests.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='set-this-locally'
export OWNER_TELEGRAM_IDS='primary-owner-id,1086767973'
export ALLOWED_ORIGINS='https://lakoniya-miniapp-test.pages.dev'
export DATABASE_PATH='./data/lakoniya.sqlite3'
uvicorn app:app --host 127.0.0.1 --port 8000
```

Do not expose the API for real customer data until tests, persistent storage, backups, request limits, the correct per-bot authentication flow, and privacy/legal requirements are reviewed. Never paste a bot token into GitHub or this chat.
