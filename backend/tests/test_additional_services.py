from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def test_additional_service_rates_are_included_in_server_estimate():
    assert app.estimate_amount("support", 10, {"windows": 2, "ironing": 1, "dishes": 2}) == 1800 + 800 + 500 + 1000
    assert app.estimate_amount("deep", 20, {"windows": 1}) == 6800


def test_empty_or_missing_additional_services_preserve_existing_pricing():
    assert app.estimate_amount("support", 45) == 8100
    assert app.estimate_amount("support", 45, {}) == 8100
    assert app.normalize_additional_services(None) == {}


def test_zero_quantities_are_ignored():
    assert app.normalize_additional_services({"windows": 0, "dishes": 2}) == {"dishes": 2}


def test_unknown_service_and_invalid_quantities_are_rejected():
    for extras in (
        {"unknown": 1},
        {"windows": -1},
        {"windows": 41},
        {"ironing": 13},
        {"dishes": 1.5},
        {"dishes": True},
        [],
    ):
        try:
            app.normalize_additional_services(extras)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected invalid extras to be rejected: {extras!r}")


def test_invalid_base_service_or_area_does_not_produce_estimate():
    assert app.estimate_amount("unknown", 45, {"windows": 1}) is None
    assert app.estimate_amount("support", 9, {"windows": 1}) is None
    assert app.estimate_amount("support", True, {}) is None


def test_owner_notification_shows_extra_services_and_costs():
    message = app.format_request_notification(
        123, "Уборка", "Адрес", "завтра", "Контакт", "",
        estimated_amount=9000, additional_services={"windows": 2, "ironing": 1},
    )
    assert "Мытьё окон × 2 створка = 800 ₽" in message
    assert "Глажка вещей × 1 час = 500 ₽" in message
