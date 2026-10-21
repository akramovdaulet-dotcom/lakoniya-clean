import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lead_finder import agent
from lead_finder.agent import (
    SearchResult,
    classify_result,
    extract_date_from_text,
    load_seen,
    mark_previously_seen,
    normalize_url,
    render_digest,
    render_owner_summary,
    run_search,
    save_seen,
    split_for_telegram,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RU_MONTHS_GEN = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def request(**kwargs):
    defaults = dict(
        title="Ищу генеральную уборку квартиры в Уфе",
        url="https://example.org/request/1",
        description="Нужна уборка после ремонта, хочу заказать на этой неделе.",
        query='"ищу уборку" Уфа',
        source_hint="web",
        published_at=days_ago(2),
    )
    defaults.update(kwargs)
    return SearchResult(**defaults)


class ListSearcher:
    """Search double that returns the same prepared results for every query."""

    def __init__(self, results):
        self.results = results

    def search(self, query, limit):
        return list(self.results)


def report_for(results, queries=(("web", "q"),)):
    return run_search(ListSearcher(results), queries=queries)


# ---------------------------------------------------------------- basics


def test_normalize_url_removes_tracking_and_fragment():
    assert normalize_url("HTTPS://Example.com/request/?utm_source=x&id=4#details") == "https://example.com/request?id=4"


def test_direct_recent_ufa_request_is_confirmed():
    lead = classify_result(request(), today=date.today())
    assert lead["status"] == "Подтверждённый кандидат"
    assert lead["score"] >= 80
    assert lead["source"] == "Поиск в интернете"
    assert lead["published_at"] == days_ago(2)
    assert lead["date_source"] == "api"
    assert "явный запрос" in lead["reason"].lower()


def test_unknown_date_never_becomes_confirmed():
    lead = classify_result(request(published_at=None), today=date.today())
    assert lead["status"] == "Требует проверки"
    assert lead["published_at"] == "Не указано"
    assert lead["date_source"] == "none"
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
    lead = classify_result(request(published_at=days_ago(300)), today=date.today())
    assert lead["status"] == "Отклонён"
    assert "90 дней" in lead["rejection_reason"]


def test_rejects_future_date():
    lead = classify_result(request(published_at=(date.today() + timedelta(days=10)).isoformat()), today=date.today())
    assert lead["status"] == "Отклонён"


def test_source_identification_for_supported_platforms():
    assert classify_result(request(url="https://www.avito.ru/ufa/item/123"))["source"] == "Avito"
    assert classify_result(request(url="https://t.me/ufa_group/123"))["source"] == "Telegram"
    assert classify_result(request(url="https://vk.com/wall-1_2"))["source"] == "ВКонтакте"
    assert classify_result(request(url="https://profi.ru/ufa/cleaning/order/1"))["source"] == "Profi.ru"
    assert classify_result(request(url="https://uslugi.yandex.ru/ufa/category/cleaning"))["source"] == "Яндекс Услуги"


def test_urls_in_this_file_are_pure_ascii():
    """A Cyrillic look-alike letter (e.g. «а» in «ufа») silently makes a URL a different host/path."""
    source = Path(__file__).read_text(encoding="utf-8")
    bad = [u for u in re.findall(r"https?://[^\s\"'<>)]+", source) if not u.isascii()]
    assert bad == []


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
    report = run_search(ListSearcher([]), queries=(("telegram", "empty"),))
    summary = report["summary"]
    assert summary["sources_with_successful_queries"] == 1
    assert summary["sources_with_results"] == []
    assert summary["sources_with_no_results"] == ["Telegram"]
    assert summary["sources_directly_verified"] == 0
    assert summary["direct_page_fetch_performed"] is False


# ------------------------------------------------- dates from the snippet


def test_extract_relative_dates_from_snippet():
    today = date.today()
    assert extract_date_from_text("Сегодня в 12:30 ищу уборку", today) == today
    assert extract_date_from_text("Опубликовано вчера", today) == today - timedelta(days=1)
    assert extract_date_from_text("позавчера написали", today) == today - timedelta(days=2)
    assert extract_date_from_text("3 дня назад", today) == today - timedelta(days=3)
    assert extract_date_from_text("5 дней назад", today) == today - timedelta(days=5)
    assert extract_date_from_text("2 недели назад", today) == today - timedelta(days=14)
    assert extract_date_from_text("неделю назад", today) == today - timedelta(days=7)
    assert extract_date_from_text("1 месяц назад", today) == today - timedelta(days=30)
    assert extract_date_from_text("час назад", today) == today
    assert extract_date_from_text("15 минут назад", today) == today


def test_extract_absolute_dates_from_snippet():
    today = date.today()
    d = today - timedelta(days=4)
    assert extract_date_from_text(f"Опубликовано {d.day} {RU_MONTHS_GEN[d.month]}", today) == d
    assert extract_date_from_text(f"{d.day} {RU_MONTHS_GEN[d.month]} {d.year}", today) == d
    assert extract_date_from_text(d.strftime("%d.%m.%Y"), today) == d


def test_day_month_without_year_that_is_still_ahead_means_last_year():
    today = date.today()
    ahead = today + timedelta(days=1)
    if ahead.month == 2 and ahead.day == 29:
        pytest.skip("29 February does not exist in the previous year")
    text = f"{ahead.day} {RU_MONTHS_GEN[ahead.month]}"
    assert extract_date_from_text(text, today) == date(today.year - 1, ahead.month, ahead.day)


def test_extract_ignores_future_dates_and_unrelated_words():
    today = date.today()
    future = today + timedelta(days=20)
    assert extract_date_from_text(future.strftime("%d.%m.%Y"), today) is None
    assert extract_date_from_text(f"{future.day} {RU_MONTHS_GEN[future.month]} {future.year}", today) is None
    assert extract_date_from_text("вчерашний день, предвчера, сегодняшний", today) is None
    assert extract_date_from_text("", today) is None
    assert extract_date_from_text("нужна уборка", today) is None


def test_snippet_date_is_marked_and_never_confirmed():
    lead = classify_result(
        request(published_at=None, description="Нужна уборка после ремонта. Сегодня в 10:00, хочу заказать."),
        today=date.today(),
    )
    assert lead["date_source"] == "snippet"
    assert lead["published_at"] == date.today().isoformat()
    assert lead["score"] >= 80  # a high score alone must not be enough
    assert lead["status"] == "Требует проверки"
    assert "фрагмент" in lead["rejection_reason"].lower()


def test_date_from_source_api_wins_over_snippet_text():
    lead = classify_result(
        request(published_at=days_ago(3), description="Нужна уборка после ремонта. Вчера обсуждали цену."),
        today=date.today(),
    )
    assert lead["date_source"] == "api"
    assert lead["published_at"] == days_ago(3)
    assert lead["status"] == "Подтверждённый кандидат"


def test_old_snippet_date_is_rejected():
    lead = classify_result(
        request(published_at=None, description="Нужна уборка после ремонта. Опубликовано 200 дней назад."),
        today=date.today(),
    )
    assert lead["status"] == "Отклонён"
    assert "90 дней" in lead["rejection_reason"]


@pytest.mark.parametrize("phrase", [
    "Сегодня", "Вчера", "позавчера", "3 дня назад", "1 неделю назад", "час назад",
    date.today().strftime("%d.%m.%Y"),
])
def test_confirmed_is_impossible_without_a_date_from_the_source(phrase):
    lead = classify_result(
        request(published_at=None, description=f"Нужна уборка после ремонта в Уфе. {phrase}. Хочу заказать."),
        today=date.today(),
    )
    assert lead["status"] != "Подтверждённый кандидат"
    report = report_for([request(published_at=None, description=f"Нужна уборка после ремонта в Уфе. {phrase}.")])
    assert report["confirmed_candidates"] == []


# ------------------------------------------------ owner summary and seen


def numbered_results(confirmed, review, start=0):
    items = []
    for i in range(start, start + confirmed):
        items.append(request(url=f"https://example.org/ok/{i}", title=f"Ищу уборку квартиры в Уфе №{i}"))
    for i in range(start, start + review):
        items.append(request(url=f"https://example.org/check/{i}", title=f"Нужна уборка в Уфе №{i}", published_at=None))
    return items


def test_summary_lists_only_new_candidates_and_never_rejected():
    results = numbered_results(2, 1) + [request(url="https://example.org/bad", title="Вакансия: требуется уборщица")]
    report = report_for(results)
    mark_previously_seen(report, {"https://example.org/ok/0"})
    text = render_owner_summary(report)
    assert "https://example.org/ok/1" in text
    assert "https://example.org/check/0" in text
    assert "https://example.org/ok/0" not in text  # seen before: not repeated
    assert "https://example.org/bad" not in text
    assert "Новых подтверждённых: 1; новых на проверку: 1" in text


def test_summary_is_none_when_everything_was_seen_or_rejected():
    report = report_for(numbered_results(1, 1) + [request(url="https://example.org/bad", title="Вакансия: требуется уборщица")])
    seen = {c["url"] for c in report["confirmed_candidates"] + report["needs_review"]}
    mark_previously_seen(report, seen)
    assert render_owner_summary(report) is None
    assert render_owner_summary(report_for([])) is None


def test_summary_is_sent_for_review_candidates_even_without_confirmed():
    report = report_for(numbered_results(0, 2))
    text = render_owner_summary(report)
    assert text is not None and "НА ПРОВЕРКУ (2)" in text and "ПОДТВЕРЖДЁННЫЕ" not in text


def test_summary_limits_and_overflow_note():
    report = report_for(numbered_results(20, 12))
    mark_previously_seen(report, set())
    text = render_owner_summary(report)
    assert "ПОДТВЕРЖДЁННЫЕ (20)" in text and "НА ПРОВЕРКУ (12)" in text
    assert len(re.findall(r"https://example\.org/ok/\d+", text)) == 15
    assert len(re.findall(r"https://example\.org/check/\d+", text)) == 10
    assert "…и ещё 5" in text and "…и ещё 2" in text


def test_summary_entry_has_title_source_date_link_and_short_fragment():
    long_description = "Нужна уборка после ремонта в Уфе. " + "очень срочно " * 60
    report = report_for([request(description=long_description, url="https://vk.com/wall-5_9")])
    text = render_owner_summary(report)
    assert "Ищу генеральную уборку квартиры в Уфе" in text
    assert "ВКонтакте" in text and days_ago(2) in text and "https://vk.com/wall-5_9" in text
    fragment = next(line for line in text.splitlines() if line.startswith("Нужна уборка после ремонта"))
    assert len(fragment) <= 300 and fragment.endswith("…")


def test_summary_marks_snippet_date():
    report = report_for([request(published_at=None, description="Нужна уборка после ремонта в Уфе. Вчера написали.")])
    assert "(по фрагменту)" in render_owner_summary(report)


def test_split_for_telegram_keeps_blocks_and_limit():
    blocks = [f"{i}. " + "x" * 900 for i in range(10)]
    chunks = split_for_telegram("\n\n".join(blocks), limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "\n\n".join(chunks) == "\n\n".join(blocks)
    giant = split_for_telegram("y" * 5000, limit=2000)
    assert [len(c) for c in giant] == [2000, 2000, 1000]


# ------------------------------------------------------------ main() flow


class FakeFirecrawl:
    results: list = []
    fail = False

    def __init__(self, *args, **kwargs):
        pass

    def search(self, query, limit):
        if type(self).fail:
            raise RuntimeError("Поисковый API вернул HTTP 429")
        return list(type(self).results)


@pytest.fixture
def run_agent(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "FirecrawlSearch", FakeFirecrawl)
    monkeypatch.setattr(agent, "send_telegram", lambda text, *a, **k: sent.append(text))
    monkeypatch.setattr(agent.time, "sleep", lambda *_: None)
    FakeFirecrawl.fail = False
    FakeFirecrawl.results = []

    def run(*extra):
        return agent.main(["--limit", "3", "--output", str(tmp_path / "out.json"),
                           "--state", str(tmp_path / "seen.json"), *extra])

    run.sent = sent
    run.state = tmp_path / "seen.json"
    run.output = tmp_path / "out.json"
    return run


def test_main_sends_new_candidates_once_and_stays_silent_on_repeat(run_agent):
    FakeFirecrawl.results = numbered_results(1, 1)
    assert run_agent("--telegram") == 0
    assert len(run_agent.sent) == 1
    assert "https://example.org/ok/0" in run_agent.sent[0] and "https://example.org/check/0" in run_agent.sent[0]
    assert run_agent("--telegram") == 0
    assert len(run_agent.sent) == 1  # the same links are not repeated
    report = json.loads(run_agent.output.read_text(encoding="utf-8"))
    assert all(c["previously_seen"] for c in report["confirmed_candidates"] + report["needs_review"])


def test_main_sends_only_the_new_links_on_the_next_day(run_agent):
    FakeFirecrawl.results = numbered_results(1, 0)
    run_agent("--telegram")
    FakeFirecrawl.results = numbered_results(2, 0)
    run_agent("--telegram")
    assert len(run_agent.sent) == 2
    assert "https://example.org/ok/1" in run_agent.sent[1] and "https://example.org/ok/0" not in run_agent.sent[1]


def test_main_without_telegram_flag_sends_nothing(run_agent):
    FakeFirecrawl.results = numbered_results(1, 1)
    assert run_agent() == 0
    assert run_agent.sent == []


def test_main_keeps_candidates_unseen_when_telegram_fails(run_agent, monkeypatch):
    FakeFirecrawl.results = numbered_results(1, 0)

    def broken(text, *a, **k):
        raise RuntimeError("Ошибка отправки Telegram: URLError")

    monkeypatch.setattr(agent, "send_telegram", broken)
    assert run_agent("--telegram") == 3
    assert not run_agent.state.exists()  # nothing marked as seen: it will be sent next time
    monkeypatch.setattr(agent, "send_telegram", lambda text, *a, **k: run_agent.sent.append(text))
    assert run_agent("--telegram") == 0
    assert len(run_agent.sent) == 1


def test_main_zero_candidates_is_fine_and_silent(run_agent):
    FakeFirecrawl.results = []
    assert run_agent("--telegram") == 0
    assert run_agent.sent == []


def test_main_fails_loudly_when_every_search_call_failed(run_agent):
    FakeFirecrawl.fail = True
    assert run_agent("--telegram") == 4
    assert run_agent.sent == [] and not run_agent.state.exists()


def test_telegram_token_is_never_in_error_text(monkeypatch):
    def boom(*args, **kwargs):
        raise agent.URLError("https://api.telegram.org/botSECRET-TOKEN/sendMessage")

    monkeypatch.setattr(agent, "urlopen", boom)
    with pytest.raises(RuntimeError) as error:
        agent.send_telegram("text", token="SECRET-TOKEN", chat_id="1")
    assert "SECRET-TOKEN" not in str(error.value)


def test_send_telegram_goes_to_every_listed_chat(monkeypatch):
    calls = []

    class Reply:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true}'

    def fake_urlopen(request, timeout=0):
        calls.append(json.loads(request.data.decode())["chat_id"])
        return Reply()

    monkeypatch.setattr(agent, "urlopen", fake_urlopen)
    agent.send_telegram("text", token="T", chat_id=" 111 , 222,,")
    assert calls == ["111", "222"]
    with pytest.raises(RuntimeError):
        agent.send_telegram("text", token="T", chat_id=" , ")


# ------------------------------------------------------------- workflow


def test_workflow_runs_search_only_on_schedule_or_dispatch():
    text = (REPO_ROOT / ".github" / "workflows" / "lead-finder.yml").read_text(encoding="utf-8")
    assert 'cron: "0 4 * * *"' in text
    assert "workflow_dispatch:" in text
    assert "needs: test" in text
    assert "github.event_name != 'pull_request'" in text
    assert "contents: read" in text
    assert "secrets.LEAD_TELEGRAM_BOT_TOKEN" in text and "secrets.LEAD_TELEGRAM_CHAT_ID" in text
    assert "actions/cache@v4" in text and "github.run_id" in text and "restore-keys" in text
