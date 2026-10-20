"""Safe, isolated defaults for every pytest module before app is imported."""
import os
import tempfile
from pathlib import Path

_TEST_DATA = Path(tempfile.gettempdir()) / "lakoniya-pytest"
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["TELEGRAM_BOT_TOKEN"] = "owner-test-token-not-for-production"
os.environ["OWNER_BOT_TOKEN"] = "owner-test-token-not-for-production"
os.environ["CUSTOMER_BOT_TOKEN"] = "customer-test-token-not-for-production"
os.environ["OWNER_TELEGRAM_IDS"] = "547686782,1086767973"
os.environ["DATABASE_PATH"] = str(_TEST_DATA / "test.sqlite3")
os.environ["NOTIFY_OWNERS"] = "false"
os.environ["NOTIFY_CUSTOMERS"] = "false"
os.environ["ALLOWED_ORIGINS"] = "https://lakoniya-miniapp-test.pages.dev"
os.environ.pop("DATABASE_URL", None)
