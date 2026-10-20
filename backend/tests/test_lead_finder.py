import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lead_finder.agent import (
    SearchResult,
    classify_result,
    load_seen,
    normalize_url,
    render_digest,
    run_search,
    save_seen,
)


def request(**kwargs):
    defaults = dict(
        title="Ищу генеральную уборку квартиры в Уфе",
        url="https://example.org/request/1",
        description="Нужна уборка после ремонта, хочу заказать на этой неделе.",
        query='"ищу уборку" Уфа',
        source_hint="web",
        published_at="2026-10-18",
    )
    defaults.update(kwargs)
    return SearchResult(**defaults)


def test_normalize_url_removes_tracking_and_fragment():
    assert normalize_url("HTTPS://Example.com/request/?utm_source=x&id=4#details") == "https://example.com/request?id=4"


def test_direct_recent_ufa_request_is_confirmed():
    lead = classify_result(request(), today=date(2026, 10, 20))
    assert lead["status"] == "Подтверждённый кандидат"
    assert lead["score"] >= 80
    assert lead["source"] == "Поиск в интернете"
    assert lead["published_at"] == "2026-10-18"
    assert "явный запрос" in lead["reason"].lower()


def test_unknown_date_never_becomes_confirmed():
    lead = classify_result(request(published_at=None), today=date(2026, 10, 20))
    assert lead["status"] == "Требует проверки"
    assert lead["published_at"] == "Не указано"
    assert "дату" in lead["rejection_reason"].lower()


def test_rejects_job_vacancy():
    lead = classify_result(request(
        title="Вакансия: требуется уборщица в офис",
        description="Работа, график и зарплата",
    ))
    assert lead["status"] == "Отклонён"
    assert "вакансию" in lead["rejection_reason"].lower()


def test_rejects_review_even_with_buying_keywords_in_snippet():
    lead = classify_result(request(
        title="Отзывы о клининге в Уфе",
        description="Ищу уборку квартиры. Отзывы клиентов и рейтинг компании.",
    ))
    assert lead["status"] == "Отклонён"
    assert "отзыв" in lead["rejection_reason"].lower()


def test_rejects_competitor_ad_and_service_catalog():
    lead = classify_result(request(
        title="Клининговая компания в Уфе: наши услуги и прайс-лист",
        description="Закажите у нас профессиональную уборку квартир и офисов.",
    ))
    assert lead["status"] == "Отклонён"


def test_rejects_information_page_without_buying_intent():
    lead = classify_result(request(
        title="Лучшие клининговые компании Уфы",
        description="Рейтинг, обзоры и подборка услуг по уборке.",
    ))
    assert lead["status"] == "Отклонён"


def test_rejects_local_request_without_ufa_evidence():
    lead = classify_result(request(
        title="Ищу человека для уборки в квартире",
        description="Нужна уборка раз в месяц, ищу исполнителя.",
    ))
    assert lead["status"] == "Отклонён"
    assert "уф" in lead["rejection_reason"].lower()


def test_rejects_snow_clearance():
    lead = classify_result(request(
        title="В Уфе нужна уборка снега на улице",
        description="Нужна уборка снега и расчистка улицы.",
    ))
    assert lead["status"] == "Отклонён"


def test_rejects_stale_request():
    lead = classify_result(request(published_at="2026-01-01"), today=date(2026, 10, 20))
    assert lead["status"] == "Отклонён"
    assert "90 дней" in lead["rejection_reason"]


def test_rejects_future_date():
    lead = classify_result(request(published_at="2026-11-01"), today=date(2026, 10, 20))
    assert lead["status"] == "Отклонён"


def test_source_identification_for_supported_platforms():
    assert classify_result(request(url="https://www.avito.ru/ufa/item/123"))["source"] == "Avito"
    assert classify_result(request(url="https://t.me/ufa_group/123"))["source"] == "Telegram"
    assert classify_result(request(url="https://vk.com/wall-1_2"))["source"] == "ВКонтакте"
    assert classify_result(request(url="https://profi.ru/ufa/cleaning/order/1"))["source"] == "Profi.ru"
    assert classify_result(request(url="https://uslugi.yandex.ru/ufа/category/cleaning"))["source"] == "Яндекс Услуги"


def test_deduplicates_same_normalized_url_across_queries():
    class FakeSearcher:
        def search(self, query, limit):
            return [request(url="https://example.org/request?utm_source=" + query[:3], query=query)]

    report = run_search(FakeSearcher(), queries=(("web", "first"), ("web", "second")))
    assert report["summary"]["unique_candidates"] == 1
    assert report["summary"]["duplicates_removed"] == 1


def test_unavailable_source_is_reported_and_other_sources_continue():
    class FakeSearcher:
        def search(self, query, limit):
            if "blocked" in query:
                raise RuntimeError("Поисковый API вернул HTTP 403")
            return [request()]

    report = run_search(FakeSearcher(), queries=(("avito.ru", "blocked"), ("web", "ok")))
    assert "Avito" in report["summary"]["sources_unavailable_or_failed"]
    assert report["summary"]["sources_with_successful_queries"] == 1
    assert len(report["confirmed_candidates"]) == 1
    assert report["summary"]["query_errors"]


def test_empty_results_produce_valid_report_and_honest_digest():
    class EmptySearcher:
        def search(self, query, limit):
            return []

    report = run_search(EmptySearcher(), queries=(("web", "empty"),))
    json.dumps(report, ensure_ascii=False)
    assert report["summary"]["confirmed_candidates"] == 0
    assert "Подтверждённых кандидатов нет" in render_digest(report)


def test_seen_state_round_trip_and_missing_file(tmp_path: Path):
    path = tmp_path / "nested" / "seen.json"
    save_seen(path, {"https://example.org/a", "https://example.org/b"})
    assert load_seen(path) == {"https://example.org/a", "https://example.org/b"}
    assert load_seen(tmp_path / "missing.json") == set()


def test_seen_state_json_is_valid(tmp_path: Path):
    path = tmp_path / "seen.json"
    save_seen(path, {"https://example.org/a"})
    assert json.loads(path.read_text(encoding="utf-8")) == ["https://example.org/a"]


def test_no_url_is_rejected_without_fabricating_source_link():
    lead = classify_result(request(url=""))
    assert lead["status"] == "Отклонён"
    assert lead["url"] == "Не указано"


def test_rejects_avito_service_ad_even_if_snippet_contains_request_words():
    lead = classify_result(request(
        url="https://www.avito.ru/ufa/predlozheniya_uslug/uborka_klining/item",
        title="Генеральная уборка квартир и офисов в Уфе",
        description="Нужна уборка дома? Выполним уборку, бесплатный выезд, закажите уборку.",
    ))
    assert lead["status"] == "Отклонён"
    assert "Avito" in lead["rejection_reason"]


def test_rejects_yandex_category_page():
    lead = classify_result(request(
        url="https://uslugi.yandex.ru/172-ufa/category?text=cleaning",
        title="Уборка и помощь по хозяйству в Уфе",
        description="Закажите уборку и убедитесь сами. Напишите нам, чтобы узнать стоимость.",
    ))
    assert lead["status"] == "Отклонён"
    assert "Категория" in lead["rejection_reason"]


def test_rejects_profi_provider_catalog_page():
    lead = classify_result(request(
        url="https://profi.ru/geo-ufa/domashnii_personal/uborka",
        title="Профессиональная уборка в Уфе",
        description="Прямо сейчас ищут уборку. Услуги клинеров и цены.",
    ))
    assert lead["status"] == "Отклонён"
    assert "Каталог исполнителей" in lead["rejection_reason"]


def test_vk_post_url_is_distinguished_from_community_profile():
    profile = classify_result(request(url="https://vk.com/fedorclean74"))
    post = classify_result(request(url="https://vk.com/wall-123456_789"))
    assert profile["status"] == "Отклонён"
    assert "страница сообщества" in profile["rejection_reason"].lower()
    # The URL alone is not enough to confirm a lead; a real post still needs
    # intent, locality and publication date evidence.
    assert post["status"] == "Подтверждённый кандидат"


def test_rejects_indexed_competitor_landing_page():
    lead = classify_result(request(
        url="https://cleanbros.ru/ufa",
        title="Клининговые услуги в Уфе: квартиры, дома и офисы",
        description="Сколько стоит уборка квартиры в Уфе? Стоимость уборки квартиры начинается от 3000 рублей.",
    ))
    assert lead["status"] == "Отклонён"


def test_rejects_cleaning_directory_with_ad_count():
    lead = classify_result(request(
        url="https://pomogatel.ru/ufa/uborka-kvartir",
        title="Уборка квартир в Уфе (145 объявлений)",
        description="Сколько стоит уборка квартиры в Уфе? Уборка квартиры стоит от 3000 рублей.",
    ))
    assert lead["status"] == "Отклонён"


def test_summary_does_not_claim_direct_source_access():
    class FakeSearcher:
        def search(self, query, limit):
            return []

    report = run_search(FakeSearcher(), queries=(("telegram", "empty"),))
    summary = report["summary"]
    assert summary["sources_with_successful_queries"] == 1
    assert summary["sources_with_results"] == []
    assert summary["sources_with_no_results"] == ["Telegram"]
    assert summary["sources_directly_verified"] == 0
    assert summary["direct_page_fetch_performed"] is False
