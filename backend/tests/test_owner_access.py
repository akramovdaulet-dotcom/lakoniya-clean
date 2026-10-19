import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
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
os.environ["NOTIFY_CUSTOMERS"] = "false"

import httpx
import pytest
from fastapi import HTTPException
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
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
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


def test_configured_health_does_not_expose_secrets(monkeypatch):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "configured": True,
        "customer_app_configured": True,
        "database_backend": "sqlite",
        "version": "dev",
    }


def test_health_shows_short_commit_of_the_running_deploy(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "5ee2b3dabcdef0123456789")
    assert client.get("/health").json()["version"] == "5ee2b3d"


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


CUSTOMER_TELEGRAM_ID = 123456789  # the user signed into customer_headers()


class SentMessages:
    """Stands in for httpx.post and records Telegram sendMessage calls per bot."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = []

    def __call__(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return FakeResponse(self.status_code)

    def _sent_with(self, token):
        return [
            body
            for url, body in self.calls
            if url == f"https://api.telegram.org/bot{token}/sendMessage"
        ]

    def to_customers(self):
        return self._sent_with(os.environ["CUSTOMER_BOT_TOKEN"])

    def to_owners(self):
        return self._sent_with(os.environ["TELEGRAM_BOT_TOKEN"])


def manager_headers():
    return {"X-Telegram-Init-Data": signed_init_data(1086767973)}


def create_customer_request():
    response = client.post("/api/requests", json=CUSTOMER_PAYLOAD, headers=customer_headers())
    assert response.status_code == 201
    return response.json()["id"]


def set_status(request_id, status):
    return client.patch(
        f"/api/requests/{request_id}/status",
        json={"status": status},
        headers=manager_headers(),
    )


def test_customer_gets_accepted_message_when_request_is_created(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_CUSTOMERS", True)
    request_id = create_customer_request()
    assert sent.to_customers() == [
        {"chat_id": CUSTOMER_TELEGRAM_ID, "text": f"✅ Заявка №{request_id} принята"}
    ]
    assert sent.to_owners() == []  # owner notifications are switched off in this test


def test_customer_is_told_when_owner_sets_processing_then_handed_over(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    request_id = create_customer_request()  # NOTIFY_CUSTOMERS is off here
    monkeypatch.setattr(api, "NOTIFY_CUSTOMERS", True)
    assert set_status(request_id, "В обработке").status_code == 200
    assert set_status(request_id, "Передана сотруднику").status_code == 200
    assert [message["text"] for message in sent.to_customers()] == [
        f"⏳ Заявка №{request_id} в обработке",
        f"👷 Заявка №{request_id} передана сотруднику",
    ]
    assert {message["chat_id"] for message in sent.to_customers()} == {CUSTOMER_TELEGRAM_ID}


def test_customer_is_not_messaged_for_repeats_or_other_statuses(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    request_id = create_customer_request()
    monkeypatch.setattr(api, "NOTIFY_CUSTOMERS", True)
    assert set_status(request_id, "В обработке").status_code == 200
    for status in ("В обработке", "Новая", "Подтверждена", "В работе", "Завершена", "Отменена"):
        assert set_status(request_id, status).status_code == 200
    assert [message["text"] for message in sent.to_customers()] == [
        f"⏳ Заявка №{request_id} в обработке"
    ]


def test_customer_notification_failures_never_break_status_update(monkeypatch, caplog):
    request_id = create_customer_request()
    monkeypatch.setattr(api, "NOTIFY_CUSTOMERS", True)

    def failing_post(url, json=None, timeout=None):
        raise httpx.ConnectError("boom " + url)

    monkeypatch.setattr(api.httpx, "post", failing_post)
    assert set_status(request_id, "В обработке").status_code == 200

    monkeypatch.setattr(api.httpx, "post", SentMessages(status_code=403))
    with caplog.at_level("WARNING", logger="lakoniya.notify"):
        assert set_status(request_id, "Передана сотруднику").status_code == 200
    assert f"request_id={request_id} status=403" in caplog.text
    assert os.environ["CUSTOMER_BOT_TOKEN"] not in caplog.text
    assert str(CUSTOMER_TELEGRAM_ID) not in caplog.text


def test_request_without_customer_telegram_id_is_skipped_silently(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_CUSTOMERS", True)
    with api.connect_db() as db:  # a request created before this feature existed
        legacy_id = api.execute_db(
            db,
            """INSERT INTO requests
               (service, address, preferred_time, customer_contact, notes, status, created_at)
               VALUES ('Старая', 'Адрес', '2099-01-01', 'Контакт', '', 'Новая', 1)""",
            returning_id=True,
        )
    assert set_status(legacy_id, "Передана сотруднику").status_code == 200
    assert sent.calls == []


def test_status_update_for_missing_request_returns_404():
    assert set_status(987654321, "В обработке").status_code == 404


def test_unknown_status_is_rejected():
    request_id = create_customer_request()
    assert set_status(request_id, "Выдуманный статус").status_code == 422


def test_owner_request_list_does_not_expose_customer_telegram_id():
    create_customer_request()
    response = client.get("/api/requests", headers=manager_headers())
    assert response.status_code == 200
    assert response.json()["requests"]
    for item in response.json()["requests"]:
        assert "customer_telegram_id" not in item


def test_synthetic_request_customer_message_is_marked_as_test(monkeypatch):
    sent = SentMessages()
    monkeypatch.setenv("ENABLE_SYNTHETIC_TEST_REQUESTS", "true")
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_CUSTOMERS", True)
    response = client.post("/api/test-requests", headers=customer_headers())
    assert response.status_code == 201
    assert sent.to_customers() == [
        {
            "chat_id": CUSTOMER_TELEGRAM_ID,
            "text": f"🧪 Тест: ✅ Заявка №{response.json()['id']} принята",
        }
    ]


def test_estimate_amount_uses_server_rates_and_limits():
    assert api.estimate_amount("support", 45) == 8100
    assert api.estimate_amount("deep", 50) == 16000
    assert api.estimate_amount("repair", 10) == 4000
    assert api.estimate_amount("repair", 500) == 200000
    assert api.estimate_amount("support", 9) is None
    assert api.estimate_amount("support", 501) is None
    assert api.estimate_amount("unknown", 45) is None
    assert api.estimate_amount(None, 45) is None
    assert api.estimate_amount("support", None) is None
    assert api.format_amount(8100) == "8 100 ₽"
    assert api.format_amount(200000) == "200 000 ₽"


def test_owner_message_contains_server_calculated_amount(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    payload = {**CUSTOMER_PAYLOAD, "service_code": "deep", "area": 50}
    response = client.post("/api/requests", json=payload, headers=customer_headers())
    assert response.status_code == 201
    messages = sent.to_owners()
    assert len(messages) == 2  # both owners
    assert all("Сумма: 16 000 ₽ (предварительно)" in message["text"] for message in messages)


def test_amount_is_stored_and_shown_to_owner_in_request_list():
    payload = {**CUSTOMER_PAYLOAD, "service_code": "repair", "area": 60}
    request_id = client.post("/api/requests", json=payload, headers=customer_headers()).json()["id"]
    listed = client.get("/api/requests", headers=manager_headers()).json()["requests"]
    assert next(item for item in listed if item["id"] == request_id)["estimated_amount"] == 24000


def test_browser_cannot_set_the_amount():
    payload = {
        **CUSTOMER_PAYLOAD,
        "service_code": "support",
        "area": 45,
        "estimated_amount": 1,  # must be ignored; the server recalculates
    }
    request_id = client.post("/api/requests", json=payload, headers=customer_headers()).json()["id"]
    listed = client.get("/api/requests", headers=manager_headers()).json()["requests"]
    assert next(item for item in listed if item["id"] == request_id)["estimated_amount"] == 8100


def test_request_without_area_still_works_and_says_amount_not_calculated(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    response = client.post("/api/requests", json=CUSTOMER_PAYLOAD, headers=customer_headers())
    assert response.status_code == 201
    assert all("Сумма: не рассчитана" in message["text"] for message in sent.to_owners())
    listed = client.get("/api/requests", headers=manager_headers()).json()["requests"]
    assert next(item for item in listed if item["id"] == response.json()["id"])[
        "estimated_amount"
    ] is None


def test_area_outside_allowed_range_is_rejected():
    for area in (0, 9, 501, 100000):
        payload = {**CUSTOMER_PAYLOAD, "service_code": "support", "area": area}
        response = client.post("/api/requests", json=payload, headers=customer_headers())
        assert response.status_code == 422, area


def test_unknown_service_code_gives_no_amount_but_request_is_created():
    payload = {**CUSTOMER_PAYLOAD, "service_code": "something", "area": 45}
    response = client.post("/api/requests", json=payload, headers=customer_headers())
    assert response.status_code == 201
    listed = client.get("/api/requests", headers=manager_headers()).json()["requests"]
    assert next(item for item in listed if item["id"] == response.json()["id"])[
        "estimated_amount"
    ] is None


def test_synthetic_request_owner_message_shows_fixed_test_amount(monkeypatch):
    sent = SentMessages()
    monkeypatch.setenv("ENABLE_SYNTHETIC_TEST_REQUESTS", "true")
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    assert client.post("/api/test-requests", headers=customer_headers()).status_code == 201
    assert all("Сумма: 8 100 ₽ (предварительно)" in m["text"] for m in sent.to_owners())


@pytest.fixture(autouse=True)
def reset_public_form_limits():
    api.ip_limiter.reset()
    api.global_limiter.reset()
    yield


def future_date(days=3):
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def uniq(prefix):
    """Unique text per call: identical requests within 10 minutes count as duplicates,
    and the test database file survives between runs."""
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def public_payload(**overrides):
    return {
        "name": "Анна",
        "phone": "+7 900 000-00-00",
        "channel": "Telegram",
        "service_code": "deep",
        "area": 50,
        "district": "Советский",
        "address": uniq("ул. Тестовая"),
        "preferred_date": future_date(),
        "notes": "Автотест, не реальные данные",
        "consent": True,
        "website": "",
        **overrides,
    }


def count_requests():
    with api.connect_db() as db:
        return api.execute_db(db, "SELECT COUNT(*) AS n FROM requests").fetchone()[0]


def post_public(payload=None, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return client.post(
        "/api/public-requests",
        json=public_payload() if payload is None else payload,
        headers=headers,
    )


def test_site_request_without_telegram_login_reaches_owners_with_amount(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    monkeypatch.setattr(api, "NOTIFY_CUSTOMERS", True)
    response = post_public(ip="203.0.113.10")
    assert response.status_code == 201
    request_id = response.json()["id"]
    assert request_id > 0
    owners = sent.to_owners()
    assert sorted(m["chat_id"] for m in owners) == [547686782, 1086767973]
    for message in owners:
        text = message["text"]
        assert f"🌐 Новая заявка с сайта №{request_id}" in text
        assert "Сумма: 16 000 ₽ (предварительно)" in text
        assert "Анна, +7 900 000-00-00 (связь: Telegram)" in text
        assert "Генеральная уборка" in text and "Советский" in text
    assert sent.to_customers() == []  # a site visitor has no chat with the customer bot
    listed = client.get("/api/requests", headers=manager_headers()).json()["requests"]
    item = next(i for i in listed if i["id"] == request_id)
    assert item["source"] == "site" and item["estimated_amount"] == 16000
    assert "customer_telegram_id" not in item


def test_status_change_of_a_site_request_sends_nothing_to_customers(monkeypatch):
    sent = SentMessages()
    request_id = post_public().json()["id"]
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_CUSTOMERS", True)
    assert set_status(request_id, "Передана сотруднику").status_code == 200
    assert sent.calls == []


def test_bot_requests_are_marked_with_source_bot():
    request_id = create_customer_request()
    listed = client.get("/api/requests", headers=manager_headers()).json()["requests"]
    assert next(i for i in listed if i["id"] == request_id)["source"] == "bot"


def test_site_request_requires_consent():
    before = count_requests()
    assert post_public(public_payload(consent=False)).status_code == 422
    without = public_payload()
    del without["consent"]
    assert post_public(without).status_code == 422
    assert count_requests() == before


@pytest.mark.parametrize(
    "bad",
    [
        {"name": " "},
        {"name": "А"},
        {"phone": "abc"},
        {"phone": "12345"},
        {"phone": "+7 900 000-00-00; DROP TABLE"},
        {"phone": "1" * 16},
        {"channel": "Голубиная почта"},
        {"service_code": "unknown"},
        {"area": 9},
        {"area": 501},
        {"address": "ул"},
        {"preferred_date": "2020-01-01"},
        {"preferred_date": future_date(400)},
        {"preferred_date": "завтра"},
        {"preferred_date": "2026-13-45"},
        {"notes": "x" * 1001},
    ],
)
def test_site_request_rejects_invalid_data(bad):
    before = count_requests()
    assert post_public(public_payload(**bad)).status_code == 422
    assert count_requests() == before


def test_site_visitor_cannot_set_the_amount():
    payload = public_payload(service_code="support", area=45, estimated_amount=1)
    request_id = post_public(payload).json()["id"]
    listed = client.get("/api/requests", headers=manager_headers()).json()["requests"]
    assert next(i for i in listed if i["id"] == request_id)["estimated_amount"] == 8100


def test_honeypot_requests_look_successful_but_are_dropped(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    before = count_requests()
    response = post_public(public_payload(website="http://spam.example"))
    assert response.status_code == 201 and response.json()["id"] == 0
    assert count_requests() == before and sent.calls == []


def test_per_visitor_rate_limit_returns_429_and_does_not_block_others():
    limit = api.ip_limiter.limit
    for _ in range(limit):
        assert post_public(ip="198.51.100.7").status_code == 201
    blocked = post_public(ip="198.51.100.7")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "3600"
    assert post_public(ip="198.51.100.8").status_code == 201


def test_overall_rate_limit_stops_visitors_who_rotate_addresses(monkeypatch):
    monkeypatch.setattr(api, "global_limiter", api.SlidingWindowLimiter(2, 3600))
    assert post_public(ip="192.0.2.1").status_code == 201
    assert post_public(ip="192.0.2.2").status_code == 201
    assert post_public(ip="192.0.2.3").status_code == 429


def test_repeated_submit_returns_same_request_and_notifies_once(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    payload = public_payload()  # one payload object, posted twice
    first = post_public(payload).json()["id"]
    before = count_requests()
    second = post_public(payload)
    assert second.status_code == 201 and second.json()["id"] == first
    assert count_requests() == before
    assert len(sent.to_owners()) == 2  # two owners, one request


def test_customer_text_cannot_fake_lines_in_the_owner_message(monkeypatch):
    sent = SentMessages()
    monkeypatch.setattr(api.httpx, "post", sent)
    monkeypatch.setattr(api, "NOTIFY_OWNERS", True)
    payload = public_payload(
        name="Иван\nСумма: 1 ₽",
        notes="Привет\nСумма: 1 ₽\nКонтакт: чужой",
    )
    assert post_public(payload).status_code == 201
    text = sent.to_owners()[0]["text"]
    lines = text.split("\n")
    assert len(lines) == 7  # title, service, address, date, amount, contact, comment: no extras
    assert [line for line in lines if line.startswith("Сумма:")] == [
        "Сумма: 16 000 ₽ (предварительно)"
    ]
    assert [line for line in lines if line.startswith("Контакт:")] == [
        "Контакт: Иван Сумма: 1 ₽, +7 900 000-00-00 (связь: Telegram)"
    ]


def test_pricing_endpoint_gives_the_site_the_server_rates():
    body = client.get("/api/pricing").json()
    assert body["rates"] == {"support": 180, "deep": 320, "repair": 400}
    assert set(body["labels"]) == set(body["rates"])
    assert (body["min_area"], body["max_area"]) == (10, 500)


def test_browser_of_the_public_site_is_allowed_by_cors_but_other_sites_are_not():
    def preflight(origin):
        return client.options(
            "/api/public-requests",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    allowed = preflight("https://lakoniya.ru")
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://lakoniya.ru"
    assert "access-control-allow-origin" not in preflight("https://evil.example").headers


def test_sliding_window_limiter_forgets_old_hits_and_bounds_memory():
    limiter = api.SlidingWindowLimiter(2, 60, max_keys=3)
    assert limiter.allow("a", now=0) and limiter.allow("a", now=1)
    assert not limiter.allow("a", now=2)
    assert limiter.allow("a", now=61)  # the first hit has expired
    for key in ("b", "c", "d", "e"):
        limiter.allow(key, now=100)
    assert len(limiter._hits) <= 3


def test_client_ip_prefers_forwarded_header_and_falls_back_to_socket():
    class FakeRequest:
        def __init__(self, headers, host):
            self.headers = headers
            self.client = type("C", (), {"host": host})() if host else None

    assert api.client_ip(FakeRequest({"x-forwarded-for": "203.0.113.5, 10.0.0.1"}, "10.0.0.2")) == "203.0.113.5"
    assert api.client_ip(FakeRequest({}, "10.0.0.2")) == "10.0.0.2"
    assert api.client_ip(FakeRequest({}, None)) == "unknown"


def test_existing_database_without_the_column_is_migrated(monkeypatch, tmp_path):
    import sqlite3

    old_db = tmp_path / "old.sqlite3"
    with sqlite3.connect(old_db) as connection:
        connection.execute(
            """CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL,
                address TEXT NOT NULL, preferred_time TEXT NOT NULL,
                customer_contact TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Новая', created_at BIGINT NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO requests (service, address, preferred_time, customer_contact, created_at)"
            " VALUES ('Старая', 'Адрес', '2099-01-01', 'Контакт', 1)"
        )
    monkeypatch.setattr(api, "DATABASE_PATH", str(old_db))
    api.initialize_db()
    api.initialize_db()  # must be idempotent
    with sqlite3.connect(old_db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(requests)")}
        kept = connection.execute("SELECT service, customer_telegram_id FROM requests").fetchall()
    assert {"customer_telegram_id", "estimated_amount", "source"} <= columns
    assert kept == [("Старая", None)]


# Reference values computed independently with openssl, NOT with this app's code:
#   SK=$(printf %s "$TOKEN" | openssl dgst -sha256 -hmac WebAppData)   # key = "WebAppData"
#   printf %s "$DATA_CHECK_STRING" | openssl dgst -sha256 -mac HMAC -macopt hexkey:$SK
# This guards against swapping the key and message again, which makes the app
# reject genuine Telegram initData while the self-signing test helper still passes.
KNOWN_TOKEN = "123456:TEST-TOKEN-not-real"
KNOWN_FIELDS = {
    "auth_date": "1700000000",
    "query_id": "AAtest",
    "user": '{"id":42,"first_name":"T"}',
}
KNOWN_HASH = "afb5e824bb9ae89f70403d8dbc2b4f1bf833f5dc23d1b6787262181b5bc765e1"
KNOWN_HASH_WITH_SWAPPED_KEY = "7fc381744695e20cd71e682d4089a4173c384e4eb4400b9a5cdddf4759f88698"


def test_known_vector_from_openssl_is_accepted(monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1700000010)
    init_data = urlencode({**KNOWN_FIELDS, "hash": KNOWN_HASH})
    assert api.verify_telegram_init_data(init_data, KNOWN_TOKEN)["id"] == 42


def test_known_vector_with_signature_field_included_is_accepted(monkeypatch):
    # Telegram's `signature` field is part of the bot-token HMAC data-check-string;
    # only `hash` is excluded. Hash computed with openssl over the string that
    # includes `signature=SigTest_abc-123`.
    monkeypatch.setattr(api.time, "time", lambda: 1700000010)
    init_data = urlencode(
        {
            **KNOWN_FIELDS,
            "signature": "SigTest_abc-123",
            "hash": "56a61af1b995dca5f70d1e2dbde04c8f6533b64371b75bb806ba3087f70bf3e8",
        }
    )
    assert api.verify_telegram_init_data(init_data, KNOWN_TOKEN)["id"] == 42


def test_known_vector_with_swapped_key_and_message_is_rejected(monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1700000010)
    init_data = urlencode({**KNOWN_FIELDS, "hash": KNOWN_HASH_WITH_SWAPPED_KEY})
    with pytest.raises(HTTPException) as error:
        api.verify_telegram_init_data(init_data, KNOWN_TOKEN)
    assert error.value.status_code == 401


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
