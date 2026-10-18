import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Owner and customer Mini Apps belong to different Telegram bots and must use
# separate tokens. TELEGRAM_BOT_TOKEN remains a backwards-compatible owner token.
OWNER_BOT_TOKEN = os.environ.get("OWNER_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
CUSTOMER_BOT_TOKEN = os.environ.get("CUSTOMER_BOT_TOKEN", "")
OWNER_IDS = {
    int(value.strip())
    for value in os.environ.get("OWNER_TELEGRAM_IDS", "").split(",")
    if value.strip().isdigit()
}
DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/lakoniya.sqlite3")
ALLOWED_ORIGINS = [
    value.strip()
    for value in os.environ.get(
        "ALLOWED_ORIGINS", "https://lakoniya-miniapp-test.pages.dev"
    ).split(",")
    if value.strip()
]
INIT_DATA_MAX_AGE_SECONDS = 3600
ALLOWED_STATUSES = {"Новая", "Подтверждена", "В работе", "Завершена", "Отменена"}

app = FastAPI(title="Lakoniya Clean API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)


def connect_db():
    path = Path(DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_db():
    with connect_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT NOT NULL,
                address TEXT NOT NULL,
                preferred_time TEXT NOT NULL,
                customer_contact TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Новая',
                created_at INTEGER NOT NULL
            )
            """
        )


initialize_db()


def verify_telegram_init_data(init_data: str, bot_token: str) -> dict:
    if not bot_token:
        raise HTTPException(status_code=503, detail="This Telegram Mini App is not configured")
    if not init_data or len(init_data) > 8192:
        raise HTTPException(status_code=401, detail="Missing or invalid Telegram auth data")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing Telegram signature")

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid auth date")

    now = int(time.time())
    if auth_date > now + 30 or now - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Telegram auth data expired")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed.items())
    )
    # Telegram Web Apps: secret_key = HMAC_SHA256(key=bot_token, msg="WebAppData")
    secret_key = hmac.new(
        bot_token.encode("utf-8"), b"WebAppData", hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram signature")

    try:
        user = json.loads(parsed["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid Telegram user")

    return {"id": user_id, "user": user}


def authenticated_user(x_telegram_init_data: str = Header(default="")) -> dict:
    return verify_telegram_init_data(x_telegram_init_data, OWNER_BOT_TOKEN)


def customer_user(x_telegram_init_data: str = Header(default="")) -> dict:
    return verify_telegram_init_data(x_telegram_init_data, CUSTOMER_BOT_TOKEN)


def owner_user(user: dict = Depends(authenticated_user)) -> dict:
    if user["id"] not in OWNER_IDS:
        raise HTTPException(status_code=403, detail="Owner access required")
    return user


class RequestCreate(BaseModel):
    service: str = Field(min_length=1, max_length=100)
    address: str = Field(min_length=1, max_length=300)
    preferred_time: str = Field(min_length=1, max_length=100)
    customer_contact: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=1000)


class StatusUpdate(BaseModel):
    status: str


@app.get("/health")
def health():
    return {
        "ok": True,
        "configured": bool(OWNER_BOT_TOKEN and OWNER_IDS),
        "customer_app_configured": bool(CUSTOMER_BOT_TOKEN),
    }


@app.post("/api/requests", status_code=201)
def create_request(payload: RequestCreate, user: dict = Depends(customer_user)):
    # Customer Mini App must send initData signed by the customer bot's token.
    with connect_db() as db:
        cursor = db.execute(
            """INSERT INTO requests
               (service, address, preferred_time, customer_contact, notes, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'Новая', ?)""",
            (
                payload.service,
                payload.address,
                payload.preferred_time,
                payload.customer_contact,
                payload.notes,
                int(time.time()),
            ),
        )
        request_id = cursor.lastrowid
    return {"id": request_id, "status": "Новая"}


@app.post("/api/test-requests", status_code=201)
def create_synthetic_test_request(user: dict = Depends(customer_user)):
    """Create a fixed synthetic request for end-to-end checks; never store form data."""
    if os.environ.get("ENABLE_SYNTHETIC_TEST_REQUESTS", "").lower() != "true":
        raise HTTPException(status_code=503, detail="Synthetic test requests are disabled")
    with connect_db() as db:
        cursor = db.execute(
            """INSERT INTO requests
               (service, address, preferred_time, customer_contact, notes, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'Новая', ?)""",
            (
                "ТЕСТ — поддерживающая уборка",
                "ТЕСТОВЫЙ АДРЕС — не использовать",
                "2099-01-01",
                "ТЕСТОВЫЙ КОНТАКТ — не использовать",
                "SYNTHETIC_TEST_REQUEST; создано тестовым endpoint; реальные данные не передавались",
                int(time.time()),
            ),
        )
        request_id = cursor.lastrowid
    return {"id": request_id, "status": "Новая", "synthetic": True}


@app.get("/api/requests")
def list_requests(_: dict = Depends(owner_user)):
    with connect_db() as db:
        rows = db.execute(
            """SELECT id, service, address, preferred_time, customer_contact,
                      notes, status, created_at
               FROM requests ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {"requests": [dict(row) for row in rows]}


@app.patch("/api/requests/{request_id}/status")
def update_request_status(
    request_id: int,
    payload: StatusUpdate,
    _: dict = Depends(owner_user),
):
    if payload.status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=422, detail="Unsupported status")
    with connect_db() as db:
        cursor = db.execute(
            "UPDATE requests SET status = ? WHERE id = ?",
            (payload.status, request_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Request not found")
    return {"id": request_id, "status": payload.status}
