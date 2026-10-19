import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

def clean_token(value: str) -> str:
    """Strip whitespace, newlines and wrapping quotes pasted into a host's env UI.

    A stray space, line break or quote changes the HMAC key and makes every
    Telegram signature check fail even though the token itself is correct.
    """
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


# Owner and customer Mini Apps belong to different Telegram bots and must use
# separate tokens. TELEGRAM_BOT_TOKEN remains a backwards-compatible owner token.
OWNER_BOT_TOKEN = clean_token(
    os.environ.get("OWNER_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
)
CUSTOMER_BOT_TOKEN = clean_token(os.environ.get("CUSTOMER_BOT_TOKEN", ""))
OWNER_IDS = {
    int(value.strip())
    for value in os.environ.get("OWNER_TELEGRAM_IDS", "").split(",")
    if value.strip().isdigit()
}
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/lakoniya.sqlite3")
USE_POSTGRES = bool(DATABASE_URL)
ALLOWED_ORIGINS = [
    value.strip()
    for value in os.environ.get(
        "ALLOWED_ORIGINS", "https://lakoniya-miniapp-test.pages.dev"
    ).split(",")
    if value.strip()
]
# Origins of the public website that submits requests directly (no Telegram involved).
# Merged with ALLOWED_ORIGINS, so existing hosting settings keep working.
SITE_ORIGINS = [
    value.strip()
    for value in os.environ.get(
        "SITE_ORIGINS",
        "https://lakoniya.ru,https://www.lakoniya.ru,https://akramovdaulet-dotcom.github.io",
    ).split(",")
    if value.strip()
]
INIT_DATA_MAX_AGE_SECONDS = 3600
ALLOWED_STATUSES = {
    "Новая",
    "В обработке",
    "Передана сотруднику",
    "Подтверждена",
    "В работе",
    "Завершена",
    "Отменена",
}

app = FastAPI(title="Lakoniya Clean API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys(ALLOWED_ORIGINS + SITE_ORIGINS)),
    # Permit this project's Cloudflare Pages preview deployments as well as
    # the stable production origin, without allowing arbitrary websites.
    allow_origin_regex=r"https://(?:[a-z0-9-]+\.)?lakoniya-miniapp-test\.pages\.dev",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)


def connect_db():
    if USE_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return psycopg.connect(url, row_factory=dict_row)
    path = Path(DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def execute_db(db, sql, params=(), *, returning_id=False):
    if USE_POSTGRES:
        sql = sql.replace("?", "%s")
        if returning_id:
            sql += " RETURNING id"
        cursor = db.execute(sql, params)
        return cursor.fetchone()["id"] if returning_id else cursor
    cursor = db.execute(sql, params)
    return cursor.lastrowid if returning_id else cursor


def initialize_db():
    id_column = "BIGSERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with connect_db() as db:
        db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS requests (
                id {id_column},
                service TEXT NOT NULL,
                address TEXT NOT NULL,
                preferred_time TEXT NOT NULL,
                customer_contact TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Новая',
                created_at BIGINT NOT NULL
            )
            """
        )
        # Telegram user id of the customer (private chat id with the customer bot),
        # used to send status updates. Added later, so migrate existing databases.
        add_column_if_missing(db, "requests", "customer_telegram_id", "BIGINT")
        # Preliminary amount in rubles, computed on the server (NULL = not calculated).
        add_column_if_missing(db, "requests", "estimated_amount", "BIGINT")
        # Where the request came from: 'site' (public website form) or 'bot' (Telegram
        # Mini App). NULL on rows created before this column existed (those came from the bot).
        add_column_if_missing(db, "requests", "source", "TEXT")
        # JSON snapshot of selected extra services and quantities.
        add_column_if_missing(db, "requests", "additional_services_json", "TEXT")


def add_column_if_missing(db, table: str, column: str, column_type: str) -> None:
    """Idempotent ALTER TABLE for SQLite and PostgreSQL (names are code constants)."""
    if USE_POSTGRES:
        db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}")
        return
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


initialize_db()


def verify_telegram_init_data(init_data: str, bot_token: str) -> dict:
    if not bot_token:
        raise HTTPException(status_code=503, detail="This Telegram Mini App is not configured")
    if not init_data or len(init_data) > 8192:
        import logging
        logging.getLogger("lakoniya.telegram_auth").warning(
            "Telegram initData rejected before parsing: present=%s length=%d",
            bool(init_data), len(init_data or ""),
        )
        raise HTTPException(status_code=401, detail="Missing or invalid Telegram auth data")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    # For bot-token HMAC validation, include all fields except `hash`.
    # The optional `signature` field is excluded only from the separate
    # third-party Ed25519 validation data-check-string, so it stays here.
    if not received_hash:
        import logging
        logging.getLogger("lakoniya.telegram_auth").warning(
            "Telegram initData rejected: hash_field_missing fields=%s",
            ",".join(sorted(parsed.keys())),
        )
        raise HTTPException(status_code=401, detail="Missing Telegram signature")

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        import logging
        logging.getLogger("lakoniya.telegram_auth").warning(
            "Telegram initData rejected: auth_date_invalid fields=%s",
            ",".join(sorted(parsed.keys())),
        )
        raise HTTPException(status_code=401, detail="Invalid auth date")

    now = int(time.time())
    if auth_date > now + 30 or now - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        import logging
        logging.getLogger("lakoniya.telegram_auth").warning(
            "Telegram initData rejected: auth_date_out_of_range age_seconds=%d",
            now - auth_date,
        )
        raise HTTPException(status_code=401, detail="Telegram auth data expired")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed.items())
    )
    # Telegram Mini Apps: the constant "WebAppData" is the KEY and the bot token is
    # the MESSAGE (shell equivalent: printf %s "$TOKEN" | openssl dgst -sha256 -hmac WebAppData).
    # Reversing them rejects every genuine initData; see test_known_vector_*.
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        # Safe diagnostics only: never log initData, token, hash, user or contact data.
        # These fields help distinguish malformed input from a token/algorithm mismatch.
        import logging
        logger = logging.getLogger("lakoniya.telegram_auth")
        logger.warning(
            "Telegram initData HMAC mismatch: init_data_len=%d fields=%s "
            "has_signature=%s auth_date_age_seconds=%d token_len=%d",
            len(init_data),
            ",".join(sorted(parsed.keys())),
            "signature" in parsed,
            now - auth_date,
            len(bot_token),
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid Telegram signature (diagnostic logged safely)",
        )

    try:
        user = json.loads(parsed["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        import logging
        logging.getLogger("lakoniya.telegram_auth").warning(
            "Telegram initData HMAC valid but user field invalid"
        )
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


# Demonstration rates in rubles per square metre. Keep in sync with `rates` in
# telegram-mini-apps/customer/index.html (the form only displays the estimate;
# the amount owners see is always computed here, never taken from the browser).
SERVICE_RATES = {"support": 180, "deep": 320, "repair": 400}
ADDITIONAL_SERVICE_RATES = {"windows": 400, "ironing": 500, "dishes": 500}
ADDITIONAL_SERVICE_LABELS = {"windows": "Мытьё окон", "ironing": "Глажка вещей", "dishes": "Мытьё посуды"}
ADDITIONAL_SERVICE_UNITS = {"windows": "створка", "ironing": "час", "dishes": "час"}
ADDITIONAL_SERVICE_MAX = {"windows": 40, "ironing": 12, "dishes": 12}
MIN_AREA, MAX_AREA = 10, 500


def normalize_additional_services(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("additional_services must be an object")
    result = {}
    for code, quantity in value.items():
        if code not in ADDITIONAL_SERVICE_RATES:
            raise ValueError("unknown additional service")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError("quantity must be an integer")
        if quantity < 0 or quantity > ADDITIONAL_SERVICE_MAX[code]:
            raise ValueError("quantity out of range")
        if quantity:
            result[code] = quantity
    return result


def estimate_amount(service_code, area, additional_services=None):
    """Calculate estimate on the server; prices sent by clients are never trusted."""
    rate = SERVICE_RATES.get(service_code or "")
    if rate is None or isinstance(area, bool) or not isinstance(area, int) or not MIN_AREA <= area <= MAX_AREA:
        return None
    try:
        extras = normalize_additional_services(additional_services)
    except ValueError:
        return None
    return rate * area + sum(ADDITIONAL_SERVICE_RATES[k] * v for k, v in extras.items())


def format_amount(amount) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


# Service names shown to owners; keys match SERVICE_RATES and the forms' <select> values.
SERVICE_LABELS = {
    "support": "Поддерживающая уборка",
    "deep": "Генеральная уборка",
    "repair": "Уборка после ремонта",
}
CONTACT_CHANNELS = {"Telegram", "MAX", "Телефон", "Электронная почта", "Любой способ"}
PHONE_ALLOWED = re.compile(r"\+?[0-9 ()\-]+")


class RequestCreate(BaseModel):
    service: str = Field(min_length=1, max_length=100)
    address: str = Field(min_length=1, max_length=300)
    preferred_time: str = Field(min_length=1, max_length=100)
    customer_contact: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=1000)
    # Optional so an older cached form without these fields keeps working
    # (such requests simply have no amount).
    service_code: str | None = Field(default=None, max_length=20)
    area: int | None = Field(default=None, ge=MIN_AREA, le=MAX_AREA)
    additional_services: dict[str, int] = Field(default_factory=dict)
    dirty_area_photos: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("dirty_area_photos")
    @classmethod
    def validate_dirty_area_photos(cls, value):
        allowed = {"image/jpeg", "image/png", "image/webp"}
        total_bytes = 0
        for photo in value:
            if not isinstance(photo, str) or "," not in photo:
                raise ValueError("photos must be base64 data URLs")
            header, encoded = photo.split(",", 1)
            mime = header.removeprefix("data:").removesuffix(";base64")
            if mime not in allowed or not header.endswith(";base64"):
                raise ValueError("photos must be JPEG, PNG or WebP")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error):
                raise ValueError("invalid photo encoding")
            if not decoded or len(decoded) > 5 * 1024 * 1024:
                raise ValueError("each photo must be no larger than 5 MB")
            total_bytes += len(decoded)
        if total_bytes > 20 * 1024 * 1024:
            raise ValueError("photos total must be no larger than 20 MB")
        return value

    @field_validator("additional_services")
    @classmethod
    def validate_additional_services(cls, value):
        return normalize_additional_services(value)


def collapse_spaces(value: str) -> str:
    """Single-line text: newlines, tabs and repeated spaces become single spaces."""
    return " ".join(str(value).split())


class PublicRequestCreate(BaseModel):
    """Request submitted from the public website (no Telegram login)."""

    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=5, max_length=30)
    channel: str = Field(default="Любой способ", max_length=30)
    service_code: str = Field(max_length=20)
    area: int = Field(ge=MIN_AREA, le=MAX_AREA)
    district: str = Field(default="", max_length=60)
    address: str = Field(min_length=3, max_length=300)
    preferred_date: str = Field(min_length=10, max_length=10)
    notes: str = Field(default="", max_length=1000)
    consent: bool  # required: a missing value is rejected too
    website: str = Field(default="", max_length=200)  # honeypot: real visitors never fill it
    additional_services: dict[str, int] = Field(default_factory=dict)

    @field_validator("additional_services")
    @classmethod
    def validate_additional_services(cls, value):
        return normalize_additional_services(value)

    @field_validator("name", "address", "district", "channel", mode="before")
    @classmethod
    def _single_line(cls, value):
        return collapse_spaces(value) if isinstance(value, str) else value

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, value):
        value = collapse_spaces(value) if isinstance(value, str) else value
        if not isinstance(value, str) or not PHONE_ALLOWED.fullmatch(value):
            raise ValueError("phone may contain only digits, +, spaces, brackets and dashes")
        if not 10 <= sum(ch.isdigit() for ch in value) <= 15:
            raise ValueError("phone must contain 10 to 15 digits")
        return value

    @field_validator("channel")
    @classmethod
    def _channel(cls, value):
        if value not in CONTACT_CHANNELS:
            raise ValueError("unsupported contact channel")
        return value

    @field_validator("service_code")
    @classmethod
    def _service_code(cls, value):
        if value not in SERVICE_RATES:
            raise ValueError("unknown service")
        return value

    @field_validator("preferred_date")
    @classmethod
    def _preferred_date(cls, value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD") from None
        today = datetime.now(timezone.utc).date()
        # One day of slack for time zones ahead of UTC (Ufa is UTC+5).
        if not today - timedelta(days=1) <= parsed <= today + timedelta(days=365):
            raise ValueError("date must be within the next year")
        return parsed.isoformat()

    @field_validator("consent")
    @classmethod
    def _consent(cls, value):
        if value is not True:
            raise ValueError("consent to personal data processing is required")
        return value


class SlidingWindowLimiter:
    """In-memory request counter per key. Resets on restart; fine for a single instance."""

    def __init__(self, limit: int, window_seconds: int, max_keys: int = 5000):
        self.limit, self.window, self.max_keys = limit, window_seconds, max_keys
        self._hits: dict[str, deque] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self.max_keys:
                self._prune(now)
            hits = self._hits[key] = deque()
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def _prune(self, now: float) -> None:
        self._hits = {k: h for k, h in self._hits.items() if h and now - h[-1] < self.window}
        if len(self._hits) >= self.max_keys:
            self._hits.clear()  # under attack: forget everyone rather than grow without bound

    def reset(self) -> None:
        self._hits.clear()


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


# Per visitor and overall caps for the public form (requests per hour).
ip_limiter = SlidingWindowLimiter(_env_int("PUBLIC_RATE_LIMIT_PER_HOUR", 5), 3600)
global_limiter = SlidingWindowLimiter(_env_int("PUBLIC_RATE_LIMIT_GLOBAL_PER_HOUR", 60), 3600)


def client_ip(request) -> str:
    """Visitor address behind the hosting proxy. X-Forwarded-For can be forged by a
    determined attacker, which is why global_limiter exists as a backstop."""
    first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if first:
        return first[:64]
    return request.client.host if request.client else "unknown"


class StatusUpdate(BaseModel):
    status: str


NOTIFY_OWNERS = os.environ.get("NOTIFY_OWNERS", "true").strip().lower() != "false"
TELEGRAM_API_URL = "https://api.telegram.org"
notify_logger = logging.getLogger("lakoniya.notify")


def format_request_notification(
    request_id: int,
    service: str,
    address: str,
    preferred_time: str,
    customer_contact: str,
    notes: str,
    *,
    estimated_amount=None,
    additional_services=None,
    source: str = "bot",
    synthetic: bool = False,
) -> str:
    """Plain-text message for owners; no parse_mode, so customer text cannot inject markup."""

    def clip(value: str, limit: int = 300) -> str:
        # One line per field: a newline in customer text must not be able to fake
        # an extra line such as "Сумма: 1 ₽" in the owner's message.
        return collapse_spaces(value or "")[:limit] or "—"

    if synthetic:
        title = "🧪 ТЕСТОВАЯ заявка"
    elif source == "site":
        title = "🌐 Новая заявка с сайта"
    else:
        title = "🧹 Новая заявка"
    return "\n".join(
        [
            f"{title} №{request_id}",
            f"Услуга: {clip(service)}",
            f"Адрес: {clip(address)}",
            f"Когда: {clip(preferred_time, 100)}",
            "Сумма: "
            + (
                f"{format_amount(estimated_amount)} (предварительно)"
                if estimated_amount is not None
                else "не рассчитана"
            ),
            f"Контакт: {clip(customer_contact)}",
            *(["Доп. услуги: " + "; ".join(ADDITIONAL_SERVICE_LABELS[k] + " × " + str(v) + " " + ADDITIONAL_SERVICE_UNITS[k] + " = " + format_amount(ADDITIONAL_SERVICE_RATES[k] * v) for k, v in normalize_additional_services(additional_services).items())] if additional_services else []),
            f"Комментарий: {clip(notes, 500)}",
        ]
    )


def notify_owners(text: str) -> None:
    """Send `text` to every owner through the owner bot.

    Runs after the response is sent. Failures are logged without the bot token
    or any customer data and never affect the stored request.
    """
    if not NOTIFY_OWNERS:
        return
    if not OWNER_BOT_TOKEN or not OWNER_IDS:
        notify_logger.warning("Owner notification skipped: owner bot or owner ids not configured")
        return
    url = f"{TELEGRAM_API_URL}/bot{OWNER_BOT_TOKEN}/sendMessage"
    for chat_id in sorted(OWNER_IDS):
        try:
            response = httpx.post(
                url,
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=10,
            )
        except httpx.HTTPError as exc:
            # Log only the exception class: httpx messages can include the request URL (token).
            notify_logger.warning("Owner notification failed: %s", type(exc).__name__)
            continue
        if response.status_code != 200:
            # 403 usually means the owner has not pressed Start in the owner bot yet.
            notify_logger.warning(
                "Owner notification rejected: owner_id=%d status=%d", chat_id, response.status_code
            )


NOTIFY_CUSTOMERS = os.environ.get("NOTIFY_CUSTOMERS", "true").strip().lower() != "false"
# Messages the customer receives in the customer bot, keyed by request status.
# "Новая" is sent automatically when the request is created; the others are sent
# when an owner moves the request to that status.
CUSTOMER_STATUS_MESSAGES = {
    "Новая": "✅ Заявка №{id} принята",
    "В обработке": "⏳ Заявка №{id} в обработке",
    "Передана сотруднику": "👷 Заявка №{id} передана сотруднику",
}


def customer_status_message(request_id: int, status: str, *, synthetic: bool = False):
    """Text for the customer, or None when this status sends no message."""
    template = CUSTOMER_STATUS_MESSAGES.get(status)
    if template is None:
        return None
    text = template.format(id=request_id)
    return f"🧪 Тест: {text}" if synthetic else text


def notify_customer(chat_id, text, request_id: int) -> None:
    """Send a status message to the customer through the customer bot.

    Runs after the response is sent and never affects the stored request. Logs
    only the request number, status code and exception class: no token, no
    Telegram id, no customer data.
    """
    if not NOTIFY_CUSTOMERS or not chat_id or not text:
        return
    if not CUSTOMER_BOT_TOKEN:
        notify_logger.warning("Customer notification skipped: customer bot not configured")
        return
    url = f"{TELEGRAM_API_URL}/bot{CUSTOMER_BOT_TOKEN}/sendMessage"
    try:
        response = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except httpx.HTTPError as exc:
        notify_logger.warning(
            "Customer notification failed: request_id=%d %s", request_id, type(exc).__name__
        )
        return
    if response.status_code != 200:
        # 403 means the customer has not started (or has blocked) the customer bot.
        notify_logger.warning(
            "Customer notification rejected: request_id=%d status=%d",
            request_id,
            response.status_code,
        )


@app.get("/health")
def health():
    return {
        "ok": True,
        "configured": bool(OWNER_BOT_TOKEN and OWNER_IDS),
        "customer_app_configured": bool(CUSTOMER_BOT_TOKEN),
        "database_backend": "postgres" if USE_POSTGRES else "sqlite",
        # Short git commit of the running deploy (set by Render), to see which code is live.
        "version": os.environ.get("RENDER_GIT_COMMIT", "")[:7] or "dev",
    }


@app.get("/api/pricing")
def pricing():
    """Rates the website uses for its live estimate, so site and server never disagree."""
    return {
        "rates": SERVICE_RATES,
        "labels": SERVICE_LABELS,
        "additional_services": {k: {"label": ADDITIONAL_SERVICE_LABELS[k], "rate": v, "unit": ADDITIONAL_SERVICE_UNITS[k], "max_quantity": ADDITIONAL_SERVICE_MAX[k]} for k, v in ADDITIONAL_SERVICE_RATES.items()},
        "min_area": MIN_AREA,
        "max_area": MAX_AREA,
    }


@app.post("/api/public-requests", status_code=201)
def create_public_request(
    payload: PublicRequestCreate, request: Request, background_tasks: BackgroundTasks
):
    """Request from the public website: stored, then sent to the owners' Telegram bot."""
    if not ip_limiter.allow(client_ip(request)) or not global_limiter.allow("all"):
        notify_logger.warning("Public request rejected: rate limit")
        raise HTTPException(
            status_code=429,
            detail="Too many requests, try again later",
            headers={"Retry-After": "3600"},
        )
    if payload.website.strip():
        # Honeypot filled: pretend success so the bot learns nothing, store nothing.
        notify_logger.info("Public request ignored: honeypot field filled")
        return {"id": 0, "status": "Новая"}

    amount = estimate_amount(payload.service_code, payload.area, payload.additional_services)
    extras_json = json.dumps(payload.additional_services, ensure_ascii=False, sort_keys=True)
    address = (f"{payload.district}, " if payload.district else "") + (
        f"{payload.address} ({payload.area} м²)"
    )
    contact = f"{payload.name}, {payload.phone} (связь: {payload.channel})"
    note = payload.notes.strip()
    notes = (note + " · " if note else "") + "Согласие на обработку данных подтверждено на сайте."
    now = int(time.time())
    with connect_db() as db:
        # A retry after a timeout or a double click must not create a second request.
        duplicate = execute_db(
            db,
            """SELECT id FROM requests
               WHERE source = 'site' AND customer_contact = ? AND address = ?
                 AND preferred_time = ? AND created_at > ?
               ORDER BY id DESC LIMIT 1""",
            (contact, address, payload.preferred_date, now - 600),
        ).fetchone()
        if duplicate is not None:
            return {"id": duplicate["id"], "status": "Новая"}
        request_id = execute_db(
            db,
            """INSERT INTO requests
               (service, address, preferred_time, customer_contact, notes, status,
                created_at, customer_telegram_id, estimated_amount, source, additional_services_json)
               VALUES (?, ?, ?, ?, ?, 'Новая', ?, NULL, ?, 'site', ?)""",
            (
                SERVICE_LABELS[payload.service_code],
                address,
                payload.preferred_date,
                contact,
                notes,
                now,
                amount,
                extras_json,
            ),
            returning_id=True,
        )
    background_tasks.add_task(
        notify_owners,
        format_request_notification(
            request_id,
            SERVICE_LABELS[payload.service_code],
            address,
            payload.preferred_date,
            contact,
            notes,
            estimated_amount=amount,
            additional_services=payload.additional_services,
            source="site",
        ),
    )
    return {"id": request_id, "status": "Новая"}


@app.post("/api/requests", status_code=201)
def create_request(
    payload: RequestCreate,
    background_tasks: BackgroundTasks,
    user: dict = Depends(customer_user),
):
    # Customer Mini App must send initData signed by the customer bot's token.
    amount = estimate_amount(payload.service_code, payload.area, payload.additional_services)
    extras_json = json.dumps(payload.additional_services, ensure_ascii=False, sort_keys=True)
    with connect_db() as db:
        request_id = execute_db(
            db,
            """INSERT INTO requests
               (service, address, preferred_time, customer_contact, notes, status,
                created_at, customer_telegram_id, estimated_amount, source, additional_services_json)
               VALUES (?, ?, ?, ?, ?, 'Новая', ?, ?, ?, 'bot', ?)""",
            (
                payload.service,
                payload.address,
                payload.preferred_time,
                payload.customer_contact,
                payload.notes,
                int(time.time()),
                user["id"],
                amount,
                extras_json,
            ),
            returning_id=True,
        )
    background_tasks.add_task(
        notify_owners,
        format_request_notification(
            request_id,
            payload.service,
            payload.address,
            payload.preferred_time,
            payload.customer_contact,
            payload.notes,
            estimated_amount=amount,
            additional_services=payload.additional_services,
        ),
    )
    background_tasks.add_task(
        notify_customer, user["id"], customer_status_message(request_id, "Новая"), request_id
    )
    return {"id": request_id, "status": "Новая"}


@app.post("/api/test-requests", status_code=201)
def create_synthetic_test_request(
    background_tasks: BackgroundTasks, user: dict = Depends(customer_user)
):
    """Create a fixed synthetic request for end-to-end checks; never store form data."""
    if os.environ.get("ENABLE_SYNTHETIC_TEST_REQUESTS", "").lower() != "true":
        raise HTTPException(status_code=503, detail="Synthetic test requests are disabled")
    amount = estimate_amount("support", 45)  # fixed test values: 45 m² x 180 = 8 100 ₽
    with connect_db() as db:
        request_id = execute_db(
            db,
            """INSERT INTO requests
               (service, address, preferred_time, customer_contact, notes, status,
                created_at, customer_telegram_id, estimated_amount, source)
               VALUES (?, ?, ?, ?, ?, 'Новая', ?, ?, ?, 'bot')""",
            (
                "ТЕСТ — поддерживающая уборка",
                "ТЕСТОВЫЙ АДРЕС — не использовать",
                "2099-01-01",
                "ТЕСТОВЫЙ КОНТАКТ — не использовать",
                "SYNTHETIC_TEST_REQUEST; создано тестовым endpoint; реальные данные не передавались",
                int(time.time()),
                user["id"],
                amount,
            ),
            returning_id=True,
        )
    background_tasks.add_task(
        notify_owners,
        format_request_notification(
            request_id,
            "ТЕСТ — поддерживающая уборка",
            "ТЕСТОВЫЙ АДРЕС — не использовать",
            "2099-01-01",
            "ТЕСТОВЫЙ КОНТАКТ — не использовать",
            "SYNTHETIC_TEST_REQUEST",
            estimated_amount=amount,
            synthetic=True,
        ),
    )
    background_tasks.add_task(
        notify_customer,
        user["id"],
        customer_status_message(request_id, "Новая", synthetic=True),
        request_id,
    )
    return {"id": request_id, "status": "Новая", "synthetic": True}


@app.get("/api/requests")
def list_requests(_: dict = Depends(owner_user)):
    with connect_db() as db:
        rows = execute_db(
            db,
            """SELECT id, service, address, preferred_time, customer_contact,
                      notes, status, created_at, estimated_amount, source
               FROM requests ORDER BY created_at DESC LIMIT 500"""
        ).fetchall()
    return {"requests": [dict(row) for row in rows]}


@app.patch("/api/requests/{request_id}/status")
def update_request_status(
    request_id: int,
    payload: StatusUpdate,
    background_tasks: BackgroundTasks,
    _: dict = Depends(owner_user),
):
    if payload.status not in ALLOWED_STATUSES:
        raise HTTPException(status_code=422, detail="Unsupported status")
    with connect_db() as db:
        row = execute_db(
            db,
            "SELECT status, customer_telegram_id FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        execute_db(
            db,
            "UPDATE requests SET status = ? WHERE id = ?",
            (payload.status, request_id),
        )
    # Tell the customer only about a real change, and never re-send "принята" when
    # an owner moves a request back to "Новая". Requests created before this
    # feature have no customer_telegram_id and are skipped silently.
    if payload.status != row["status"] and payload.status != "Новая":
        background_tasks.add_task(
            notify_customer,
            row["customer_telegram_id"],
            customer_status_message(request_id, payload.status),
            request_id,
        )
    return {"id": request_id, "status": payload.status}
