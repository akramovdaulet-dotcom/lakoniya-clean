import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

import app


TEST_TOKEN = "unit-test-token-not-a-production-secret"


def make_init_data(*, token=TEST_TOKEN, include_signature=False, user_name="Daulet"):
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE-test-query",
        "user": json.dumps(
            {"id": 123456789, "first_name": user_name, "last_name": "Test"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if include_signature:
        # Telegram adds `signature` (for third-party Ed25519 checks). It is part of
        # the bot-token HMAC data-check-string: only `hash` is excluded there.
        fields["signature"] = "test-ed25519-signature"
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items())
    )
    # Telegram Mini Apps bot-token validation: the constant "WebAppData" is the
    # HMAC key and the bot token is the message.
    secret_key = hmac.new(
        b"WebAppData", token.encode("utf-8"), hashlib.sha256
    ).digest()
    fields["hash"] = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


def test_valid_init_data_passes_hmac_verification():
    result = app.verify_telegram_init_data(make_init_data(), TEST_TOKEN)
    assert result["id"] == 123456789
    assert result["user"]["first_name"] == "Daulet"


def test_signature_field_is_part_of_hmac_data_check_string():
    result = app.verify_telegram_init_data(
        make_init_data(include_signature=True), TEST_TOKEN
    )
    assert result["id"] == 123456789


def test_tampered_signature_field_fails_hmac_verification():
    data = make_init_data(include_signature=True)
    tampered = data.replace("test-ed25519-signature", "forged-signature")
    with pytest.raises(HTTPException) as error:
        app.verify_telegram_init_data(tampered, TEST_TOKEN)
    assert error.value.status_code == 401


def test_tampered_user_data_fails_hmac_verification():
    data = make_init_data()
    tampered = data.replace("Daulet", "Intruder")
    with pytest.raises(HTTPException) as error:
        app.verify_telegram_init_data(tampered, TEST_TOKEN)
    assert error.value.status_code == 401


def test_wrong_bot_token_fails_hmac_verification():
    with pytest.raises(HTTPException) as error:
        app.verify_telegram_init_data(make_init_data(), "different-test-token")
    assert error.value.status_code == 401


def test_expired_init_data_is_rejected():
    fields = {
        "auth_date": str(int(time.time()) - app.INIT_DATA_MAX_AGE_SECONDS - 10),
        "query_id": "AAE-old-query",
        "user": json.dumps({"id": 123456789}, separators=(",", ":")),
    }
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items())
    )
    secret_key = hmac.new(
        b"WebAppData", TEST_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    fields["hash"] = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    with pytest.raises(HTTPException) as error:
        app.verify_telegram_init_data(urlencode(fields), TEST_TOKEN)
    assert error.value.status_code == 401
    assert error.value.detail == "Telegram auth data expired"
