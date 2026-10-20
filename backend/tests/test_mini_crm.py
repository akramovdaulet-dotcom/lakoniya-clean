import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from fastapi.testclient import TestClient

import app


OWNER_TOKEN = "owner-test-token-not-for-production"
OWNER_ID = 547686782


def signed_init_data(user_id=OWNER_ID):
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", OWNER_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def owner_client():
    client = TestClient(app.app)
    client.headers["X-Telegram-Init-Data"] = signed_init_data()
    return client


def test_manual_crm_lead_is_created_without_customer_notification():
    client = owner_client()
    response = client.post("/api/requests/manual", json={
        "service": "Генеральная уборка",
        "address": "Уфа, тестовый район",
        "preferred_time": "2026-11-01",
        "customer_contact": "Тестовый контакт",
        "notes": "Заявка добавлена владельцем",
        "source": "Avito",
        "estimated_amount": 7000,
    })
    assert response.status_code == 201, response.text
    request_id = response.json()["id"]
    row = next(item for item in client.get("/api/requests", params={"q": str(request_id)}).json()["requests"] if item["id"] == request_id)
    assert row["status"] == "Новая"
    assert row["source"] == "Avito"
    assert row["estimated_amount"] == 7000
    assert row["owner_notes"] == ""
    assert row["scheduled_at"] is None


def test_crm_fields_can_be_updated_and_summary_uses_actual_revenue():
    client = owner_client()
    created = client.post("/api/requests/manual", json={
        "service": "Поддерживающая уборка",
        "address": "Тестовый адрес",
        "preferred_time": "2026-11-02",
        "customer_contact": "Тестовый контакт",
        "source": "manual",
        "estimated_amount": 5000,
    })
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]

    update = client.patch(f"/api/requests/{request_id}/crm", json={
        "owner_notes": "Перезвонить утром",
        "scheduled_at": "2026-11-02T10:00",
        "actual_amount": 6200,
        "expenses_amount": 1800,
    })
    assert update.status_code == 200, update.text
    status = client.patch(f"/api/requests/{request_id}/status", json={"status": "Завершена"})
    assert status.status_code == 200, status.text

    row = next(item for item in client.get("/api/requests", params={"q": str(request_id)}).json()["requests"] if item["id"] == request_id)
    assert row["owner_notes"] == "Перезвонить утром"
    assert row["scheduled_at"] == "2026-11-02T10:00"
    assert row["actual_amount"] == 6200
    assert row["expenses_amount"] == 1800

    summary = client.get("/api/crm/summary").json()
    assert summary["total"] >= 1
    assert summary["currency"] == "RUB"
    assert "month_profit" in summary


def test_crm_endpoints_require_owner_access():
    client = TestClient(app.app)
    response = client.post("/api/requests/manual", json={
        "service": "Уборка",
        "address": "Адрес",
        "customer_contact": "Контакт",
    })
    assert response.status_code == 401
