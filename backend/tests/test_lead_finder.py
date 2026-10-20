import json
from pathlib import Path

from lead_finder.agent import (
    SearchResult,
    find_leads,
    load_seen,
    normalize_url,
    render_digest,
    save_seen,
    score_result,
)


def test_normalize_url_removes_tracking_and_fragment():
    assert normalize_url("HTTPS://Example.com/request/?utm_source=x&id=4#details") == "https://example.com/request?id=4"


def test_scores_public_customer_request_for_ufa():
    lead = score_result(SearchResult(
        title="Ищу уборку квартиры в Уфе",
        url="https://example.org/request/1",
        description="Нужна генеральная уборка после ремонта",
        query='"ищу уборку" Уфа',
    ))
    assert lead is not None
    assert lead.score >= 60
    assert "Уфу" in lead.reason


def test_rejects_job_vacancy_not_customer_lead():
    result = SearchResult(
        title="Вакансия: требуется уборщица в офис",
        url="https://example.org/job",
        description="Работа, график и зарплата",
        query="нужна уборщица Уфа",
    )
    assert score_result(result) is None


def test_rejects_competitor_ad_without_buying_intent():
    result = SearchResult(
        title="Клининговая компания в Уфе",
        url="https://example.org/services",
        description="Профессиональная уборка квартир и офисов",
        query="клининг Уфа",
    )
    assert score_result(result) is None


def test_deduplicates_same_url_across_queries():
    class FakeSearcher:
        def search(self, query, limit):
            return [SearchResult(
                title="Ищу уборку квартиры в Уфе",
                url="https://example.org/request?utm_source=" + query[:3],
                description="Нужна уборка после ремонта",
                query=query,
            )]

    leads = find_leads(FakeSearcher(), queries=("first", "second"))
    assert len(leads) == 1


def test_seen_state_round_trip(tmp_path: Path):
    path = tmp_path / "nested" / "seen.json"
    save_seen(path, {"https://example.org/a", "https://example.org/b"})
    assert load_seen(path) == {"https://example.org/a", "https://example.org/b"}


def test_seen_state_handles_missing_file(tmp_path: Path):
    assert load_seen(tmp_path / "missing.json") == set()


def test_digest_is_honest_when_no_leads():
    assert "не найдено" in render_digest([])


def test_seen_state_json_is_valid(tmp_path: Path):
    path = tmp_path / "seen.json"
    save_seen(path, {"https://example.org/a"})
    assert json.loads(path.read_text(encoding="utf-8")) == ["https://example.org/a"]
