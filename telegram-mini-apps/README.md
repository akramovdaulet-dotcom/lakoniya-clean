# Telegram Mini Apps — demo UI

This folder contains two **front-end-only** Telegram Mini App prototypes for «Лакония Клин»:

- `customer/index.html` — customer request flow and illustrative price estimate.
- `owner/index.html` — owner dashboard with clearly fake demo requests and agent statuses.

## Important

These pages are UI prototypes only. They do not submit requests, persist business data, or operate AI agents. The owner page is not secure just because it is visually separate; until a backend verifies Telegram init data and authorizes the owner ID, never expose real customer data or use it for actual operations.

The pages load Telegram's official Web App JavaScript SDK when opened inside Telegram. They also work in a regular browser for visual preview, with a fallback layout.

## Testing

Open the HTML files in a browser to preview. For a real Telegram Mini App test, each page must be published at its own HTTPS URL and assigned to the corresponding bot via BotFather's Mini App/menu button settings. Do not paste bot tokens into these files.

## Production checklist

- HTTPS hosting for each Mini App.
- Backend API and database hosted in a suitable location after reviewing applicable Russian personal-data requirements.
- Server-side validation of Telegram `initData` using the bot token kept only on the server.
- Owner allowlist verified server-side on every privileged request.
- Consent/privacy flow reviewed before collecting any real customer information.
- Rate limits, backups, error logging, and tests.
- Real prices and business rules confirmed by the owner.
