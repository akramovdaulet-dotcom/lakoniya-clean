import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

# Isolated test-only configuration. Never use production secrets in tests.
os.environ["TELEGRAM_BOT_TOKEN"] = "owner-test-token-not-for-production"
os.environ["CUSTOMER_BOT_TOKEN"] = "customer-test-token-not-for-production"
os.environ["OWNER_TELEGRAM_IDS"] = "547686782,1086767973"
os.environ["DATABASE_PATH"] = "/tmp/lakoniya-test.sqlite3"
os.environ["ALLOWED_ORIGINS"] = "https://lakoniya-miniapp-test.pages.dev"

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as api


def signed_init_data(
    user_id: int,
    *,
    bot_token: str | None = None,
    auth_date: int | None = None,
) -> str:
    auth_date = int(time.time()) if auth_date is None else auth_date
    token = bot_token or os.environ["TELEGRAM_BOT_TOKEN"]
    fields = {
        "auth_date": str(auth_date),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(token.encode(), b"WebAppData", hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret_key, check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


client = TestClient(api.app)


def test_configured_health_does_not_expose_secrets():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "configured": True,
        "customer_app_configured": True,
    }


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


def test_customer_can_create_request_with_customer_bot_signature():
    payload = {
        "service": "Тестовая уборка",
        "address": "Тестовый адрес, 1",
        "preferred_time": "2099-01-01",
        "customer_contact": "Тестовый клиент · +70000000000",
        "notes": "Автоматический тест; не реальные данные",
    }
    response = client.post(
        "/api/requests",
        json=payload,
        headers={
            "X-Telegram-Init-Data": signed_init_data(
                123456789,
                bot_token=os.environ["CUSTOMER_BOT_TOKEN"],
            )
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "Новая"
    assert isinstance(response.json()["id"], int)


def test_owner_bot_signature_cannot_create_customer_request():
    response = client.post(
        "/api/requests",
        json={
            "service": "Тест",
            "address": "Тестовый адрес",
            "preferred_time": "2099-01-01",
            "customer_contact": "Тестовый контакт",
        },
        headers={"X-Telegram-Init-Data": signed_init_data(123456789)},
    )
    assert response.status_code == 401
