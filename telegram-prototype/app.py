"""Local-only demo of separate customer and owner Telegram bots.

Uses synthetic data only. Secrets are read from environment variables.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DEMO_DB_PATH", str(ROOT / "telegram-prototype.sqlite3")))
TYPE, AREA, DISTRICT, DATE, NAME, PHONE, CONSENT = range(7)
ALLOWED_TYPES = {"Поддерживающая уборка", "Генеральная уборка", "Уборка после ремонта"}


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(connect_db()) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_type TEXT NOT NULL,
                area_m2 INTEGER NOT NULL,
                district TEXT NOT NULL,
                preferred_date TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                consent INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Новая',
                created_at TEXT NOT NULL
            )
        """)
        db.commit()


def create_demo_request(
    service_type: str = "Поддерживающая уборка",
    area_m2: int = 45,
    district: str = "Тестовый район",
    preferred_date: str = "2026-12-31",
    customer_name: str = "Тестовый клиент",
    phone: str = "+70000000000",
    consent: bool = True,
) -> int:
    """Create synthetic test data. Never pass real customer details in this prototype."""
    if service_type not in ALLOWED_TYPES:
        raise ValueError("Unknown service type")
    if not 1 <= area_m2 <= 1000:
        raise ValueError("Area must be between 1 and 1000 m²")
    if not consent:
        raise ValueError("Consent is required in this prototype")
    with closing(connect_db()) as db:
        cur = db.execute(
            """INSERT INTO requests
            (service_type, area_m2, district, preferred_date, customer_name, phone, consent, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'Новая', ?)""",
            (service_type, area_m2, district, preferred_date, customer_name, phone,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
        return int(cur.lastrowid)


def list_requests(limit: int = 20) -> list[sqlite3.Row]:
    with closing(connect_db()) as db:
        return list(db.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall())


def update_status(request_id: int, status: str) -> bool:
    allowed = {"Новая", "Связаться", "Подтверждена", "Выполнена", "Отменена"}
    if status not in allowed:
        raise ValueError("Unknown status")
    with closing(connect_db()) as db:
        cur = db.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))
        db.commit()
        return cur.rowcount == 1


def is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    configured = os.getenv("OWNER_TELEGRAM_ID", "")
    user = update.effective_user
    return bool(configured.isdigit() and user and str(user.id) == configured)


async def customer_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Тестовый бот «Лакония Клин» 🧽\n"
        "Это демонстрация: не отправляйте настоящие персональные данные.\n"
        "Команда /request запускает тестовую форму."
    )


async def request_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [[InlineKeyboardButton(s, callback_data=f"service:{s}")] for s in sorted(ALLOWED_TYPES)]
    await update.effective_message.reply_text(
        "Выберите услугу для тестовой заявки:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TYPE


async def choose_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    service = query.data.removeprefix("service:")
    if service not in ALLOWED_TYPES:
        await query.edit_message_text("Неизвестная услуга. Начните заново: /request")
        return ConversationHandler.END
    context.user_data["service_type"] = service
    await query.edit_message_text("Укажите тестовую площадь в м² (например, 45). Только вымышленные данные.")
    return AREA


async def area_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        area = int(update.effective_message.text.strip())
        if not 1 <= area <= 1000:
            raise ValueError
    except (ValueError, AttributeError):
        await update.effective_message.reply_text("Введите целое число от 1 до 1000.")
        return AREA
    context.user_data["area_m2"] = area
    await update.effective_message.reply_text("Укажите вымышленный район (например, Тестовый район).")
    return DISTRICT


async def district_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    district = update.effective_message.text.strip()
    if not 2 <= len(district) <= 80:
        await update.effective_message.reply_text("Введите район длиной от 2 до 80 символов.")
        return DISTRICT
    context.user_data["district"] = district
    await update.effective_message.reply_text("Желаемая дата в формате ГГГГ-ММ-ДД (например, 2026-12-31).")
    return DATE


async def date_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.effective_message.text.strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        await update.effective_message.reply_text("Дата должна быть в формате ГГГГ-ММ-ДД.")
        return DATE
    context.user_data["preferred_date"] = value
    await update.effective_message.reply_text("Введите тестовое имя, например «Тестовый клиент».")
    return NAME


async def name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.effective_message.text.strip()
    if not 2 <= len(value) <= 80:
        await update.effective_message.reply_text("Введите имя длиной от 2 до 80 символов.")
        return NAME
    context.user_data["customer_name"] = value
    await update.effective_message.reply_text("Введите вымышленный телефон, например +70000000000.")
    return PHONE


async def phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.effective_message.text.strip()
    if not 7 <= len(value) <= 20 or any(ch not in "+0123456789 ()-" for ch in value):
        await update.effective_message.reply_text("Введите тестовый номер из цифр и символов + ( ) -.")
        return PHONE
    context.user_data["phone"] = value
    keyboard = [[
        InlineKeyboardButton("Согласен (только тест)", callback_data="consent:yes"),
        InlineKeyboardButton("Не согласен", callback_data="consent:no"),
    ]]
    await update.effective_message.reply_text(
        "Для демонстрации нажмите «Согласен». Используйте только вымышленные данные; "
        "это не юридическое согласие для реального бизнеса.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CONSENT


async def consent_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data != "consent:yes":
        await query.edit_message_text("Заявка не создана: согласие не предоставлено.")
        context.user_data.clear()
        return ConversationHandler.END
    data = context.user_data
    request_id = create_demo_request(
        service_type=data["service_type"],
        area_m2=data["area_m2"],
        district=data["district"],
        preferred_date=data["preferred_date"],
        customer_name=data["customer_name"],
        phone=data["phone"],
        consent=True,
    )
    await query.edit_message_text(
        f"Тестовая заявка №{request_id} сохранена локально.\n"
        "Это демо: реальный владелец не получил уведомление, а данные не отправлены в интернет."
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("Действие отменено. Для новой тестовой заявки: /request")
    return ConversationHandler.END


async def owner_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await update.effective_message.reply_text("Доступ запрещён.")
        return
    await update.effective_message.reply_text(
        "Закрытый тестовый бот владельца.\nКоманды: /requests — заявки, /help — подсказка."
    )


async def owner_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await update.effective_message.reply_text("Доступ запрещён.")
        return
    rows = list_requests()
    if not rows:
        await update.effective_message.reply_text("Пока нет тестовых заявок. Создайте её через клиентского бота.")
        return
    for row in rows:
        text = (
            f"Тестовая заявка №{row['id']}\n"
            f"Услуга: {row['service_type']}\nПлощадь: {row['area_m2']} м²\n"
            f"Район: {row['district']}\nДата: {row['preferred_date']}\n"
            f"Имя: {row['customer_name']}\nТелефон: {row['phone']}\nСтатус: {row['status']}"
        )
        keyboard = [[
            InlineKeyboardButton("Связаться", callback_data=f"status:{row['id']}:Связаться"),
            InlineKeyboardButton("Подтвердить", callback_data=f"status:{row['id']}:Подтверждена"),
        ], [
            InlineKeyboardButton("Выполнена", callback_data=f"status:{row['id']}:Выполнена"),
            InlineKeyboardButton("Отменить", callback_data=f"status:{row['id']}:Отменена"),
        ]]
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def owner_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_owner(update, context):
        await query.answer("Доступ запрещён.", show_alert=True)
        return
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Некорректное действие.", show_alert=True)
        return
    try:
        request_id = int(parts[1])
        status = parts[2]
    except ValueError:
        await query.answer("Некорректный номер заявки.", show_alert=True)
        return
    changed = update_status(request_id, status)
    await query.answer("Статус обновлён." if changed else "Заявка не найдена.", show_alert=True)
    if changed:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Тестовая заявка №{request_id}: статус «{status}».")


def main() -> None:
    customer_token = os.getenv("CUSTOMER_BOT_TOKEN", "")
    owner_token = os.getenv("OWNER_BOT_TOKEN", "")
    owner_id = os.getenv("OWNER_TELEGRAM_ID", "")
    if not customer_token or not owner_token:
        raise SystemExit("Set CUSTOMER_BOT_TOKEN and OWNER_BOT_TOKEN as environment variables.")
    if not owner_id.isdigit():
        raise SystemExit("Set OWNER_TELEGRAM_ID to your numeric Telegram user ID.")
    if customer_token == owner_token:
        raise SystemExit("Use two different bot tokens.")
    init_db()

    customer = Application.builder().token(customer_token).build()
    flow = ConversationHandler(
        entry_points=[CommandHandler("request", request_start)],
        states={
            TYPE: [CallbackQueryHandler(choose_service, pattern=r"^service:")],
            AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, area_received)],
            DISTRICT: [MessageHandler(filters.TEXT & ~filters.COMMAND, district_received)],
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, date_received)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_received)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_received)],
            CONSENT: [CallbackQueryHandler(consent_received, pattern=r"^consent:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    customer.add_handler(CommandHandler("start", customer_start))
    customer.add_handler(flow)

    owner = Application.builder().token(owner_token).build()
    owner.add_handler(CommandHandler("start", owner_start))
    owner.add_handler(CommandHandler("requests", owner_requests))
    owner.add_handler(CallbackQueryHandler(owner_status, pattern=r"^status:"))

    print("Starting local demo bots. Stop with Ctrl+C.")
    customer.run_polling(drop_pending_updates=True, close_loop=False)
    owner.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
