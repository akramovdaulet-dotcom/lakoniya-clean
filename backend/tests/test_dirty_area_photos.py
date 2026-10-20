from datetime import date, timedelta
import base64
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def valid_payload(**overrides):
    payload = {
        "name": "Иван",
        "phone": "+79001234567",
        "service_code": "support",
        "area": 45,
        "address": "Уфа, Ленина 1",
        "preferred_date": (date.today() + timedelta(days=3)).isoformat(),
        "consent": True,
    }
    payload.update(overrides)
    return payload


def data_url(mime="image/jpeg", raw=None):
    if raw is None:
        raw = bytes.fromhex("FFD8FF") + b"valid-test-image-data"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def test_public_request_accepts_up_to_five_valid_signature_photos():
    model = app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url()] * 5))
    assert len(model.dirty_area_photos) == 5


def test_public_request_rejects_six_photos():
    with pytest.raises(ValidationError):
        app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url()] * 6))


def test_public_request_rejects_unsupported_mime():
    with pytest.raises(ValidationError):
        app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url("image/gif", b"GIF89a")] ))


def test_public_request_rejects_text_with_jpeg_header():
    with pytest.raises(ValidationError):
        app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url(raw=b"not an image at all")]))


def test_public_request_rejects_broken_base64():
    with pytest.raises(ValidationError):
        app.PublicRequestCreate(**valid_payload(dirty_area_photos=["data:image/jpeg;base64,%%%broken%%%"]))


def test_public_request_rejects_photo_over_three_megabytes():
    raw = bytes.fromhex("FFD8FF") + b"x" * (3 * 1024 * 1024)
    with pytest.raises(ValidationError):
        app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url(raw=raw)]))


def test_public_request_rejects_total_over_ten_megabytes():
    raw = bytes.fromhex("FFD8FF") + b"x" * (2 * 1024 * 1024)
    photos = [data_url(raw=raw)] * 5
    with pytest.raises(ValidationError):
        app.PublicRequestCreate(**valid_payload(dirty_area_photos=photos))


def test_mini_app_request_model_has_no_photo_field():
    """The Mini App path never carries photos: the field must not exist, so nothing is silently dropped."""
    assert "dirty_area_photos" not in app.RequestCreate.model_fields


def public_payload(**overrides):
    payload = valid_payload()
    payload.update({"channel": "Любой способ", "district": "", "website": ""})
    payload.update(overrides)
    return payload


def test_public_request_with_photos_is_created_and_count_is_stored(monkeypatch):
    monkeypatch.setattr(app, "NOTIFY_OWNERS", False)
    monkeypatch.setattr(app.ip_limiter, "allow", lambda key: True)
    monkeypatch.setattr(app.global_limiter, "allow", lambda key: True)
    client = TestClient(app.app)
    phone = "+7900" + str(int(__import__("time").time()))[-7:]
    photos = [data_url(), data_url()]
    response = client.post("/api/public-requests", json=public_payload(phone=phone, dirty_area_photos=photos))
    assert response.status_code == 201, response.text
    request_id = response.json()["id"]
    with app.connect_db() as db:
        row = db.execute("SELECT dirty_area_photo_count FROM requests WHERE id = ?", (request_id,)).fetchone()
    assert row["dirty_area_photo_count"] == 2

def test_owner_photo_notification_uses_media_group(monkeypatch):
    calls = []
    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(app.httpx, "post", fake_post)
    monkeypatch.setattr(app, "NOTIFY_OWNERS", True)
    monkeypatch.setattr(app, "OWNER_BOT_TOKEN", "test-token")
    monkeypatch.setattr(app, "OWNER_IDS", {12345})
    app.notify_owners("Заявка №77\nФото: 2 шт. (оценка вручную)", [data_url(), data_url()], 77)
    assert any("sendMessage" in url and "Фото: 2 шт. (оценка вручную)" in kwargs["json"]["text"] for url, kwargs in calls)
    media_calls = [(url, kwargs) for url, kwargs in calls if "sendMediaGroup" in url]
    assert len(media_calls) == 1
    assert "Заявка №77" in media_calls[0][1]["data"]["media"]

def test_public_request_without_photos_still_succeeds(monkeypatch):
    monkeypatch.setattr(app, "NOTIFY_OWNERS", False)
    monkeypatch.setattr(app.ip_limiter, "allow", lambda key: True)
    monkeypatch.setattr(app.global_limiter, "allow", lambda key: True)
    response = TestClient(app.app).post("/api/public-requests", json=public_payload(phone="+79001230002"))
    assert response.status_code == 201, response.text


def test_photo_validation_error_never_echoes_base64():
    secret_photo_data = "data:image/jpeg;base64," + base64.b64encode(b"private-photo-content").decode()
    payload = public_payload(dirty_area_photos=[secret_photo_data, "data:image/jpeg;base64,%%%"])
    response = TestClient(app.app).post("/api/public-requests", json=payload)
    assert response.status_code == 422
    assert secret_photo_data not in response.text
    assert base64.b64encode(b"private-photo-content").decode() not in response.text


def _open_limits(monkeypatch):
    monkeypatch.setattr(app, "NOTIFY_OWNERS", False)
    monkeypatch.setattr(app.ip_limiter, "allow", lambda key: True)
    monkeypatch.setattr(app.global_limiter, "allow", lambda key: True)


def test_public_request_body_over_limit_is_rejected_with_413(monkeypatch):
    _open_limits(monkeypatch)
    monkeypatch.setattr(app, "MAX_PUBLIC_BODY_BYTES", 2000)
    response = TestClient(app.app).post("/api/public-requests", json=public_payload(notes="x" * 900, address="а" * 1500))
    assert response.status_code == 413


def test_public_request_body_limit_also_applies_to_chunked_upload(monkeypatch):
    _open_limits(monkeypatch)
    monkeypatch.setattr(app, "MAX_PUBLIC_BODY_BYTES", 2000)

    def chunks():
        for _ in range(5):
            yield b"x" * 1000  # no Content-Length: the size is only known while reading

    response = TestClient(app.app).post(
        "/api/public-requests", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


def test_body_limit_fits_the_largest_allowed_photo_set():
    biggest_json = app.MAX_PHOTOS_TOTAL_BYTES * 4 // 3 + 10_000  # base64 + the rest of the JSON
    assert app.MAX_PUBLIC_BODY_BYTES > biggest_json


def test_png_photo_with_valid_signature_is_accepted():
    png = bytes.fromhex("89504E470D0A1A0A") + b"png-body"
    assert len(app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url("image/png", png)])).dirty_area_photos) == 1


def test_owner_list_reports_photo_count(monkeypatch):
    _open_limits(monkeypatch)
    phone = "+7900" + str(int(__import__("time").time() * 1000))[-7:]
    created = TestClient(app.app).post(
        "/api/public-requests",
        json=public_payload(phone=phone, address="Уфа, Фото-тест " + phone, dirty_area_photos=[data_url()] * 3),
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    listed = app.list_requests({})["requests"]
    item = next(row for row in listed if row["id"] == request_id)
    assert item["dirty_area_photo_count"] == 3
    assert "customer_telegram_id" not in item
