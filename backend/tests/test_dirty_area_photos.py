from pathlib import Path
import base64
import sys

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
        "preferred_date": "2026-10-21",
        "consent": True,
    }
    payload.update(overrides)
    return payload


def data_url(mime="image/jpeg", raw=b"test-image"):
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def test_public_request_accepts_up_to_five_supported_photos():
    payload = app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url()] * 5))
    assert len(payload.dirty_area_photos) == 5


def test_public_request_rejects_more_than_five_photos():
    with pytest.raises(ValidationError):
        app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url()] * 6))


def test_public_request_rejects_unsupported_photo_type():
    with pytest.raises(ValidationError):
        app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url("image/gif")]))


def test_public_request_rejects_photo_over_five_megabytes():
    with pytest.raises(ValidationError):
        app.PublicRequestCreate(**valid_payload(dirty_area_photos=[data_url(raw=b"x" * (5 * 1024 * 1024 + 1))]))
