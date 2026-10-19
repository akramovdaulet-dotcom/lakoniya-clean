# Telegram prototype (safe test mode)

This is a local prototype for two separate Telegram bots. It uses SQLite on the machine running the program and contains no real tokens or customer data.

## What this prototype does

- Customer bot: demonstrates a request flow with clearly fake sample data.
- Owner bot: displays fake requests and lets an allowlisted owner mark a request as contacted or confirmed.
- Both bots share one local SQLite database file.
- Owner access is checked using `OWNER_TELEGRAM_ID` on every owner command/callback.
- No AI, cold outreach, payments, or automatic messages to customers.

## Important limitations

- This is not deployed and will not run continuously from GitHub. GitHub Actions can run tests, but is not a persistent bot host.
- A real Telegram test requires two bot tokens from BotFather and the numeric Telegram user ID of the owner. Keep these in environment variables on the machine running the prototype; never commit them.
- Do not enter real customer information. Use only synthetic test data.
- Before production, review Russian personal-data requirements, hosting/data location, consent text, privacy policy, backups, and abuse/rate limiting.

## Setup on a computer

1. Install Python 3.11 or newer.
2. Create two test bots via `@BotFather` and copy each token privately.
3. Set environment variables `CUSTOMER_BOT_TOKEN`, `OWNER_BOT_TOKEN`, and `OWNER_TELEGRAM_ID` in the shell or a local ignored `.env` file.
4. Install dependencies: `python -m pip install -r requirements.txt`
5. Run tests: `python -m unittest discover -s tests -v`
6. Start both bots: `python app.py`

No `.env` file or real tokens should be committed. This prototype uses polling and local SQLite; it is for controlled testing only.
