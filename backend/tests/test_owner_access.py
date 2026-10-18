import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

# Use isolated test configuration; never read real production secrets in tests.
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-do-not-use-in-production"
os.environ["OWNER_TELEGRAM_IDS"] = "547686782,1086767973"
os.environ["DATABASE_PATH"] = "/tmp/lakoniya-test.sqlite3"
os.environ["ALLOWED_ORIGINS"] = "https://lakoniya-miniapp-test.pages.dev"

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


def test_primary_owner_can_list_requests():
    response = client.get(
        "/api/requests",
        headers={"X-Telegram-Init-Data": signed_init_data(547686782)},
    )
    assert response.status_code == 200
    assert "requests" in response.json()


def test_manager_has_same_request_access_as_primary_owner():
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
