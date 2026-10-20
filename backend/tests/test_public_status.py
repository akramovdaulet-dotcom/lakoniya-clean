import uuid
from datetime import date, timedelta

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

import app

OWNER = {"id": 1086767973}


def payload(**over):
    data = {
        "name": "Анна", "phone": "+7 900 000-00-00", "channel": "Любой способ", "service_code": "support",
        "area": 45, "district": "", "address": "ул. Статусная " + uuid.uuid4().hex[:10],
        "preferred_date": (date.today() + timedelta(days=3)).isoformat(), "notes": "", "consent": True, "website": "",
    }
    data.update(over)
    return data


def create(monkeypatch, **over):
    monkeypatch.setattr(app, "NOTIFY_OWNERS", False)
    monkeypatch.setattr(app.ip_limiter, "allow", lambda key: True)
    monkeypatch.setattr(app.global_limiter, "allow", lambda key: True)
    monkeypatch.setattr(app.status_limiter, "allow", lambda key: True)
    response = TestClient(app.app).post("/api/public-requests", json=payload(**over))
    assert response.status_code == 201, response.text
    return response.json()


def status(request_id, token):
    return TestClient(app.app).get(f"/api/public-requests/{request_id}/status", params={"token": token})


def test_site_request_returns_secret_token_and_status_is_readable(monkeypatch):
    created = create(monkeypatch)
    assert len(created["token"]) >= 20
    response = status(created["id"], created["token"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "Новая" and "принята" in body["message"]
    assert set(body) == {"id", "status", "message", "created_at"}  # nothing personal
    assert response.headers["cache-control"] == "no-store"


def test_wrong_token_and_unknown_request_look_the_same(monkeypatch):
    created = create(monkeypatch)
    wrong = status(created["id"], "x" * 22)
    unknown = status(999999999, created["token"])
    assert wrong.status_code == unknown.status_code == 404
    assert wrong.json() == unknown.json()


def test_status_follows_owner_changes(monkeypatch):
    created = create(monkeypatch)
    app.update_request_status(created["id"], app.StatusUpdate(status="Передана сотруднику"), BackgroundTasks(), OWNER)
    body = status(created["id"], created["token"]).json()
    assert body["status"] == "Передана сотруднику" and "сотруднику" in body["message"]


def test_every_allowed_status_has_customer_text():
    assert set(app.PUBLIC_STATUS_TEXT) == app.ALLOWED_STATUSES


def test_retry_of_same_request_returns_same_token(monkeypatch):
    first = create(monkeypatch, address="ул. Повторная " + uuid.uuid4().hex[:10])
    again = create(monkeypatch, address=first_address(first))
    assert again["id"] == first["id"] and again["token"] == first["token"]


def first_address(created):
    with app.connect_db() as db:
        return app.execute_db(db, "SELECT address FROM requests WHERE id = ?", (created["id"],)).fetchone()["address"].split(" (")[0]


def test_token_is_never_listed_for_owners(monkeypatch):
    created = create(monkeypatch)
    item = next(r for r in app.list_requests({})["requests"] if r["id"] == created["id"])
    assert "status_token" not in item and created["token"] not in str(item)


def test_bot_requests_have_no_public_status(monkeypatch):
    with app.connect_db() as db:
        bot_id = app.execute_db(
            db,
            """INSERT INTO requests (service, address, preferred_time, customer_contact, notes, status, created_at, source, status_token)
               VALUES ('Т', 'бот-адрес', 'завтра', 'к', '', 'Новая', 1, 'bot', 'bot-token-should-not-work')""",
            (), returning_id=True,
        )
    monkeypatch.setattr(app.status_limiter, "allow", lambda key: True)
    assert status(bot_id, "bot-token-should-not-work").status_code == 404


def test_status_polling_is_rate_limited(monkeypatch):
    created = create(monkeypatch)
    monkeypatch.setattr(app, "status_limiter", app.SlidingWindowLimiter(2, 3600))
    codes = [status(created["id"], created["token"]).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_honeypot_returns_no_token(monkeypatch):
    created = create(monkeypatch, website="http://spam.example")
    assert created["id"] == 0 and "token" not in created


def test_confirmed_status_says_we_come_to_the_customer():
    text = app.PUBLIC_STATUS_TEXT["Подтверждена"]
    assert "приедем" in text and "ждём вас" not in text.lower()
