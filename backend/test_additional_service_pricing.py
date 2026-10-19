"""Regression tests for server-side additional cleaning service pricing."""
try:
    # The backend workflow runs pytest with backend/ as its working directory.
    from app import (
        ADDITIONAL_SERVICE_RATES,
        estimate_amount,
        format_request_notification,
        normalize_additional_services,
    )
except ModuleNotFoundError:
    # Also support running the suite from the repository root.
    from backend.app import (
        ADDITIONAL_SERVICE_RATES,
        estimate_amount,
        format_request_notification,
        normalize_additional_services,
    )


def test_additional_service_rates_are_configured():
    assert ADDITIONAL_SERVICE_RATES == {"windows": 400, "ironing": 500, "dishes": 500}


def test_estimate_includes_additional_services():
    assert estimate_amount("support", 45, {"windows": 2, "ironing": 1, "dishes": 2}) == 45 * 180 + 2 * 400 + 500 + 2 * 500


def test_empty_additional_services_keep_base_price():
    assert estimate_amount("deep", 30, {}) == 30 * 320


def test_unknown_service_is_rejected():
    try:
        normalize_additional_services({"mystery": 1})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown additional service must be rejected")


def test_quantity_limits_are_enforced():
    for value in ({"windows": 41}, {"ironing": 13}, {"dishes": -1}, {"windows": True}, {"ironing": 1.5}):
        try:
            normalize_additional_services(value)
        except ValueError:
            continue
        raise AssertionError(f"invalid quantity accepted: {value}")


def test_owner_notification_lists_extra_services():
    message = format_request_notification(
        1, "Генеральная уборка", "Уфа", "завтра", "Тест", "",
        estimated_amount=2500, additional_services={"windows": 2, "ironing": 1},
    )
    assert "Мытьё окон × 2 створка = 800 ₽" in message
    assert "Глажка вещей × 1 час = 500 ₽" in message
