# Lakoniya Clean API (starter)

This is a backend starter for the customer and owner Telegram Mini Apps. It is not deployed by this commit and does not make the existing demo pages live.

## Security model

- Validates Telegram Mini App `initData` signatures on the server using `TELEGRAM_BOT_TOKEN`.
- Rejects missing, invalid, or older-than-one-hour authentication data.
- Restricts listing and status changes to IDs in `OWNER_TELEGRAM_IDS`.
- The owner ID must be configured in the hosting provider's environment settings. Do not put real IDs or bot tokens in this public repository.
- CORS is restricted to configured HTTPS origins. CORS is not authentication; server-side Telegram signature and role checks are the authorization controls.
- Customer creation requires valid Telegram Mini App authentication.
- SQLite requires persistent storage on the chosen host. Ephemeral filesystems lose data after restart/redeploy.

## Required hosting environment variables

- `TELEGRAM_BOT_TOKEN`: token for the relevant Telegram bot, stored only as a hosting secret.
- `OWNER_TELEGRAM_IDS`: comma-separated numeric Telegram IDs allowed to view requests and update statuses. For the requested manager, configure the numeric ID privately in hosting settings; it is intentionally not committed here.
- `ALLOWED_ORIGINS`: exact HTTPS origins for the published Mini Apps, e.g. `https://lakoniya-miniapp-test.pages.dev`.
- `DATABASE_PATH`: path on persistent storage.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
export TELEGRAM_BOT_TOKEN='set-this-locally'
export OWNER_TELEGRAM_IDS='set-manager-id-locally'
export ALLOWED_ORIGINS='https://lakoniya-miniapp-test.pages.dev'
export DATABASE_PATH='./data/lakoniya.sqlite3'
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

Do not expose the API publicly until tests, HTTPS, persistent storage, request limits, backups, and privacy/legal requirements are reviewed. The current HTML pages still use demo behavior until they are wired to these endpoints and verified end-to-end. Never paste a bot token into GitHub or this chat.
