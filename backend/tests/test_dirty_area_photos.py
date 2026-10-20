from datetime import date, timedelta
import base64
import sys
from pathlib import Path

import pytest
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


def test_request_create_uses_same_photo_validator():
    model = app.RequestCreate(
        service="Тест", address="Тестовый адрес", preferred_time="завтра",
        customer_contact="тест", dirty_area_photos=[data_url()],
    )
    assert len(model.dirty_area_photos) == 1
