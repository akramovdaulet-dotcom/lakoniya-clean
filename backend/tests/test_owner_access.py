import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

# Configure environment before importing the application module.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-do-not-use-in-production")
os.environ.setdefault("OWNER_TELEGRAM_IDS", "111111,1086767973")
os.environ.setdefault("DATABASE_PATH", "/tmp/lakoniya-test.sqlite3")
os.environ.setdefault("ALLOWED_ORIGINS", "https://lakoniya-miniapp-test.pages.dev")

from fastapi.testclient import TestClient
import app as api


def signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    auth_date = int(time.time()) if auth_date is None else auth_date
    fields = {
        "auth_date": str(auth_date),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(
        os.environ["TELEGRAM_BOT_TOKEN"].encode(), b"WebAppData", hashlib.sha256
    ).digest()
    fields["hash"] = hmac.new(
        secret_key, check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


client = TestClient(api.app)


def test_configured_health_does_not_expose_secrets():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "configured": True}


def test_manager_id_has_same_request_access_as_owner_allowlist():
    response = client.get(
        "/api/requests",
        headers={"X-Telegram-Init-Data": signed_init_data(1086767973)},
    )
    assert response.status_code == 200
    assert "requests" in response.json()


def test_unlisted_user_cannot_list_requests():
    response = client.get(
        "/api/requests",
        headers={"X-Telegram-Init-Data": signed_init_data(999999999)},
    )
    assert response.status_code == 403


def test_missing_init_data_is_rejected():
    response = client.get("/api/requests")
    assert response.status_code == 401


def test_expired_init_data_is_rejected():
    response = client.get(
        "/api/requests",
        headers={
            "X-Telegram-Init-Data": signed_init_data(
                1086767973, auth_date=int(time.time()) - 7200
            )
        },
    )
    assert response.status_code == 401
