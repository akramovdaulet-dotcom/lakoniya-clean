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
# Tests must never call the real Telegram API; notification tests enable it explicitly.
os.environ["NOTIFY_OWNERS"] = "false"

import httpx
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


def test_synthetic_test_request_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_SYNTHETIC_TEST_REQUESTS", raising=False)
    response = client.post(
        "/api/test-requests",
        headers={
            "X-Telegram-Init-Data": signed_init_data(
                123456789, bot_token=os.environ["CUSTOMER_BOT_TOKEN"]
            )
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Synthetic test requests are disabled"


def test_synthetic_test_request_stores_only_fixed_test_data(monkeypatch):
    monkeypatch.setenv("ENABLE_SYNTHETIC_TEST_REQUESTS", "true")
    response = client.post(
        "/api/test-requests",
        json={
            "address": "REAL ADDRESS MUST BE IGNORED",
            "customer_contact": "REAL PHONE MUST BE IGNORED",
        },
        headers={
            "X-Telegram-Init-Data": signed_init_data(
                123456789, bot_token=os.environ["CUSTOMER_BOT_TOKEN"]
            )
        },
    )
    assert response.status_code == 201
    assert response.json()["synthetic"] is True
    request_id = response.json()["id"]
    with api.connect_db() as db:
        row = db.execute(
            "SELECT address, customer_contact, notes FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()
    assert row["address"] == "ТЕСТОВЫЙ АДРЕС — не использовать"
    assert row["customer_contact"] == "ТЕСТОВЫЙ КОНТАКТ — не использовать"
    assert "SYNTHETIC_TEST_REQUEST" in row["notes"]


def test_configured_health_does_not_expose_secrets():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "configured": True,
        "customer_app_configured": True,
        "database_backend": "sqlite",
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


def test_clean_token_strips_whitespace_newlines_and_quotes():
    assert api.clean_token("abc:123") == "abc:123"
    assert api.clean_token("  abc:123\n") == "abc:123"
    assert api.clean_token('"abc:123"') == "abc:123"
    assert api.clean_token("'abc:123'\r\n") == "abc:123"
    assert api.clean_token(' " abc:123 " ') == "abc:123"
    assert api.clean_token("") == ""
    assert api.clean_token(None) == ""


def test_customer_request_works_when_env_token_had_stray_whitespace(monkeypatch):
    real_token = os.environ["CUSTOMER_BOT_TOKEN"]
    # Simulates a hosting env value pasted with quotes and a trailing newline.
    monkeypatch.setattr(api, "CUSTOMER_BOT_TOKEN", api.clean_token(f'"{real_token}"\n'))
    response = client.post(
        "/api/requests",
        json={
            "service": "Тест",
            "address": "Тестовый адрес",
            "preferred_time": "2099-01-01",
            "customer_contact": "Тестовый контакт",
        },
        headers={
            "X-Telegram-Init-Data": signed_init_data(123456789, bot_token=real_token)
        },
    )
    assert response.status_code == 201


CUSTOMER_PAYLOAD = {
    "service": "Генеральная уборка",
    "address": "ул. Тестовая, 1",
    "preferred_time": "2099-01-01",
    "customer_contact": "Тест · +70000000000",
    "notes": "Автоматический тест",
}


def customer_headers():
    return {
        "X-Telegram-Init-Data": signed_init_data(
            123456789, bot_token=os.environ["CUSTOMER_BOT_TOKEN"]
        )
    }


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


def test_new_request_is_sent_to_every_owner_via_owner_bot(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return FakeResponse(200)

    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    monkeypatch.setattr(api.httpx, "post", fake_post)
    response = client.post("/api/requests", json=CUSTOMER_PAYLOAD, headers=customer_headers())
    assert response.status_code == 201
    assert sorted(call[1]["chat_id"] for call in calls) == [547686782, 1086767973]
    for url, body in calls:
        assert url == (
            "https://api.telegram.org/bot"
            + os.environ["TELEGRAM_BOT_TOKEN"]
            + "/sendMessage"
        )
        assert f"№{response.json()['id']}" in body["text"]
        assert "Генеральная уборка" in body["text"]
        assert "parse_mode" not in body


def test_notification_failure_does_not_break_request_creation(monkeypatch):
    def failing_post(url, json=None, timeout=None):
        raise httpx.ConnectError("boom " + url)

    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    monkeypatch.setattr(api.httpx, "post", failing_post)
    response = client.post("/api/requests", json=CUSTOMER_PAYLOAD, headers=customer_headers())
    assert response.status_code == 201


def test_rejected_notification_log_never_contains_bot_token(monkeypatch, caplog):
    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    monkeypatch.setattr(api.httpx, "post", lambda url, json=None, timeout=None: FakeResponse(403))
    with caplog.at_level("WARNING", logger="lakoniya.notify"):
        response = client.post("/api/requests", json=CUSTOMER_PAYLOAD, headers=customer_headers())
    assert response.status_code == 201
    assert "status=403" in caplog.text
    assert os.environ["TELEGRAM_BOT_TOKEN"] not in caplog.text


def test_notifications_can_be_turned_off(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "NOTIFY_OWNERS", False)
    monkeypatch.setattr(api.httpx, "post", lambda *a, **k: calls.append(1))
    response = client.post("/api/requests", json=CUSTOMER_PAYLOAD, headers=customer_headers())
    assert response.status_code == 201
    assert calls == []


def test_synthetic_request_notification_is_marked_as_test(monkeypatch):
    texts = []
    monkeypatch.setenv("ENABLE_SYNTHETIC_TEST_REQUESTS", "true")
    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    monkeypatch.setattr(
        api.httpx,
        "post",
        lambda url, json=None, timeout=None: texts.append(json["text"]) or FakeResponse(200),
    )
    response = client.post("/api/test-requests", headers=customer_headers())
    assert response.status_code == 201
    assert len(texts) == 2
    assert all("ТЕСТОВАЯ заявка" in text for text in texts)


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



def test_manager_can_update_request_status_created_by_customer():
    create_response = client.post(
        "/api/requests",
        json={
            "service": "Тестовая уборка",
            "address": "Тестовый адрес, 2",
            "preferred_time": "2099-01-02",
            "customer_contact": "Тестовый клиент · +70000000001",
            "notes": "Автоматический тест; не реальные данные",
        },
        headers={
            "X-Telegram-Init-Data": signed_init_data(
                123456789,
                bot_token=os.environ["CUSTOMER_BOT_TOKEN"],
            )
        },
    )
    assert create_response.status_code == 201
    request_id = create_response.json()["id"]

    update_response = client.patch(
        f"/api/requests/{request_id}/status",
        json={"status": "В работе"},
        headers={
            "X-Telegram-Init-Data": signed_init_data(1086767973),
        },
    )
    assert update_response.status_code == 200
    assert update_response.json() == {"id": request_id, "status": "В работе"}
