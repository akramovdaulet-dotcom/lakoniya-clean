"""Read-only public lead discovery for Lakoniya Klin in Ufa.

Search results are treated as candidates, not verified facts. No login, CAPTCHA,
access-control bypass, contact harvesting, or automatic customer messaging.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SEARCH_ENDPOINT = "https://api.firecrawl.dev/v2/search"
TELEGRAM_ENDPOINT = "https://api.telegram.org"
DEFAULT_LIMIT = 5

# Search-engine discovery only. A query being run does not mean that the site
# itself was accessible or that every result was checked.
SEARCH_QUERIES = (
    ('avito.ru', 'site:avito.ru Уфа ("нужна уборка" OR "ищу уборку" OR "заказать уборку")'),
    ('telegram', '(site:t.me OR site:telegram.me) Уфа ("ищу уборку" OR "нужна уборка" OR "посоветуйте клининг")'),
    ('vk.com', 'site:vk.com Уфа ("ищу уборку" OR "нужна уборка" OR "посоветуйте клининг")'),
    ('profi.ru', 'site:profi.ru Уфа ("заказ" OR "ищу") уборка квартиры'),
    ('uslugi.yandex.ru', 'site:uslugi.yandex.ru Уфа ("заказать уборку" OR "нужна уборка")'),
    ('web', '"ищу клининг" Уфа'),
    ('web', '"нужна генеральная уборка" Уфа'),
    ('web', '"кто может помыть окна" Уфа'),
    ('web', '"ищу человека для уборки" Уфа'),
    ('web', '"сколько стоит уборка" Уфа "заказать"'),
)

SERVICE_TERMS = (
    "убор", "клининг", "мытье окон", "мытьё окон", "после ремонта",
    "генеральн", "поддерживающ", "помыть окна", "уборка квартиры",
)
INTENT_TERMS = (
    "ищу убор", "ищем убор", "нужна убор", "нужен клининг",
    "заказать убор", "заказать клининг", "требуется уборка",
    "ищу клинингов", "нужна клинингов", "кто убер", "кто может убраться",
    "посоветуйте убор", "посоветуйте клининг", "заказ на убор",
    "заказ на клининг", "сколько стоит убор", "кто может помыть окна",
    "ищу исполнителя", "нужен исполнитель",
)
NEGATIVE_TERMS = (
    "вакансия", "вакансии", "ищем сотрудник", "требуется сотрудник",
    "требуется уборщица на работу", "работа уборщицей", "резюме",
    "устроиться уборщ", "зарплата уборщ", "обучение клинингу",
    "купить оборудование", "франшиза клининга", "уборка снега",
    "расчистка снега", "покос травы", "газонов от мусора",
    "спиливание веток", "отзывы", "отзыв о клининге", "рейтинг клинингов",
    "топ клинингов", "лучшие клининговые компании", "каталог клинингов",
    "прайс-лист", "прайс лист", "наши услуги", "закажите у нас",
    "мы предлагаем уборку", "предлагаем услуги уборки",
)
TRACKING_KEYS = {"fbclid", "gclid", "yclid", "ref", "source", "utm_source", "utm_medium", "utm_campaign"}


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str = ""
    query: str = ""
    source_hint: str = ""
    published_at: str | None = None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_KEYS and not k.lower().startswith("utm_")]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def identify_source(url: str, hint: str = "") -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    if "avito.ru" in host:
        return "Avito"
    if host in {"t.me", "telegram.me"} or "telegram" in host:
        return "Telegram"
    if "vk.com" in host or "vkontakte.ru" in host:
        return "ВКонтакте"
    if "profi.ru" in host:
        return "Profi.ru"
    if "uslugi.yandex.ru" in host:
        return "Яндекс Услуги"
    return "Поиск в интернете" if hint == "web" or not hint else hint


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    candidate = value.strip()
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(candidate[:10], fmt).date()
        except ValueError:
            continue
    return None


def service_type(text: str) -> str:
    lowered = text.lower()
    if "окн" in lowered or "помыть окна" in lowered:
        return "Мытьё окон"
    if "после ремонта" in lowered or "послеремонт" in lowered:
        return "Уборка после ремонта"
    if "генераль" in lowered:
        return "Генеральная уборка"
    if "офис" in lowered:
        return "Уборка офиса"
    if "регуляр" in lowered or "поддерживающ" in lowered:
        return "Поддерживающая уборка"
    if "квартир" in lowered or "дом" in lowered:
        return "Уборка квартиры/дома"
    return "Другое / не уточнено"


def classify_result(result: SearchResult, today: date | None = None) -> dict[str, Any]:
    """Return a full candidate record, including rejected candidates for auditability."""
    today = today or datetime.now(timezone.utc).date()
    title = re.sub(r"\s+", " ", result.title or "").strip()
    excerpt = re.sub(r"\s+", " ", result.description or "").strip()
    text = f"{title} {excerpt}".lower()
    url = normalize_url(result.url) if result.url else ""
    source = identify_source(result.url, result.source_hint) if result.url else (
        result.source_hint or "Неизвестно"
    )
    published = parse_date(result.published_at)
    record: dict[str, Any] = {
        "title": title or "Не указано",
        "source": source,
        "url": url or "Не указано",
        "published_at": published.isoformat() if published else "Не указано",
        "excerpt": excerpt[:1000] or "Не указано",
        "service_type": service_type(text),
        "area": "Уфа" if any(x in text for x in ("уфа", "уфе", "уфы", "уфу")) else "Не указано",
        "score": 0,
        "reason": "",
        "status": "Отклонён",
        "rejection_reason": "",
    }

    def reject(reason: str) -> dict[str, Any]:
        record["rejection_reason"] = reason
        record["reason"] = reason
        return record

    if not result.url or url == "Не указано" or urlsplit(url).scheme not in {"http", "https"}:
        return reject("Нет корректной ссылки на первоисточник")
    if any(term in text for term in NEGATIVE_TERMS):
        return reject("Похоже на отзыв, рекламу, каталог, информационную страницу или вакансию")
    if not any(term in text for term in SERVICE_TERMS):
        return reject("Нет достаточных признаков услуги уборки")
    intent_hits = [term for term in INTENT_TERMS if term in text]
    if not intent_hits:
        return reject("Не найдено явного намерения заказать услугу")
    if record["area"] == "Не указано":
        return reject("Нет подтверждения, что запрос относится к Уфе")
    if any(term in text for term in ("снега", "снег", "улиц", "дорожн")):
        return reject("Услуга вне профиля бытового клининга")

    # A high score cannot replace proof of intent. Unknown publication date keeps
    # the candidate in manual review, never confirmed.
    score = 50 + 20 + min(10, 5 * (len(intent_hits) - 1))
    if published:
        age_days = (today - published).days
        if age_days < 0:
            return reject("Дата публикации находится в будущем или некорректна")
        if age_days <= 30:
            score += 20
        elif age_days <= 90:
            score += 10
        else:
            return reject("Публикация старше 90 дней; актуальность не подтверждена")
    else:
        score += 0
    score = min(score, 100)
    record["score"] = score
    record["reason"] = "Явный запрос на уборку; указана Уфа" + (
        "; дата публикации доступна" if published else "; дата публикации не найдена"
    )
    if score >= 80 and published is not None:
        record["status"] = "Подтверждённый кандидат"
    else:
        record["status"] = "Требует проверки"
        record["rejection_reason"] = (
            "Не удалось подтвердить дату публикации" if published is None
            else "Недостаточная оценка для подтверждённого кандидата"
        )
    return record


def score_result(result: SearchResult) -> dict[str, Any] | None:
    """Compatibility helper: return only plausible non-rejected candidates."""
    record = classify_result(result)
    return record if record["status"] != "Отклонён" else None


class FirecrawlSearch:
    def __init__(self, api_key: str | None = None, timeout: int = 25):
        self.api_key = api_key or os.getenv("FIRECRAWL_API_KEY", "").strip()
        self.timeout = timeout

    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[SearchResult]:
        payload = {"query": query, "limit": max(1, min(limit, 10)), "sources": ["web"], "country": "RU"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(SEARCH_ENDPOINT, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"Поисковый API вернул HTTP {exc.code}") from None
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ошибка поискового API: {type(exc).__name__}") from None
        if body.get("success") is False:
            raise RuntimeError("Поисковый API сообщил об ошибке")
        data = body.get("data") or {}
        items = data.get("web") or data.get("results") or []
        results: list[SearchResult] = []
        for item in items:
            url = item.get("url") or item.get("link") or ""
            if not url:
                continue
            results.append(SearchResult(
                title=str(item.get("title") or ""),
                url=str(url),
                description=str(item.get("description") or item.get("snippet") or ""),
                query=query,
                source_hint=str(item.get("source") or ""),
                published_at=(
                    str(item.get("publishedDate") or item.get("published_at") or item.get("date") or "")
                    or None
                ),
            ))
        return results


def run_search(searcher: Any, queries: tuple[tuple[str, str], ...] = SEARCH_QUERIES,
               limit_per_query: int = DEFAULT_LIMIT) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    source_stats: dict[str, dict[str, Any]] = {}
    attempted_sources: set[str] = set()
    query_errors: list[dict[str, str]] = []
    raw_count = 0
    for hint, query in queries:
        source_label = {
            "avito.ru": "Avito", "telegram": "Telegram", "vk.com": "ВКонтакте",
            "profi.ru": "Profi.ru", "uslugi.yandex.ru": "Яндекс Услуги",
            "web": "Поиск в интернете",
        }.get(hint, hint)
        attempted_sources.add(source_label)
        stat = source_stats.setdefault(source_label, {"queries_attempted": 0, "queries_succeeded": 0, "results": 0, "errors": []})
        stat["queries_attempted"] += 1
        try:
            results = searcher.search(query, limit=limit_per_query)
            stat["queries_succeeded"] += 1
            stat["results"] += len(results)
            raw_count += len(results)
        except (RuntimeError, OSError) as exc:
            message = str(exc)[:200]
            stat["errors"].append(message)
            query_errors.append({"source": source_label, "error": message})
            continue
        for item in results:
            candidate = classify_result(item)
            key = candidate["url"]
            if key == "Не указано":
                key = f"invalid:{candidate['title']}:{candidate['excerpt'][:80]}"
            existing = records.get(key)
            if existing is None:
                records[key] = candidate
            else:
                # Keep the stronger evidence, but do not let a duplicate upgrade
                # a missing date or turn a rejected page into a confirmed lead.
                if candidate["score"] > existing["score"]:
                    records[key] = candidate
        time.sleep(0.05)

    all_records = list(records.values())
    confirmed = [r for r in all_records if r["status"] == "Подтверждённый кандидат"]
    review = [r for r in all_records if r["status"] == "Требует проверки"]
    rejected = [r for r in all_records if r["status"] == "Отклонён"]
    duplicate_count = max(0, raw_count - len(records))
    summary = {
        "sources_attempted": len(attempted_sources),
        "sources_with_successful_queries": sum(
            1 for s in source_stats.values() if s["queries_succeeded"] > 0
        ),
        "sources_unavailable_or_failed": [
            name for name in sorted(attempted_sources)
            if source_stats[name]["queries_succeeded"] == 0
        ],
        "candidate_results_received": raw_count,
        "unique_candidates": len(all_records),
        "confirmed_candidates": len(confirmed),
        "needs_review": len(review),
        "rejected": len(rejected),
        "duplicates_removed": duplicate_count,
        "query_errors": query_errors,
        "source_details": source_stats,
        "rejection_reasons": {},
    }
    for candidate in rejected:
        reason = candidate["rejection_reason"] or "Причина не указана"
        summary["rejection_reasons"][reason] = summary["rejection_reasons"].get(reason, 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "city": "Уфа",
        "company": "Лакония Клин",
        "summary": summary,
        "confirmed_candidates": confirmed,
        "needs_review": review,
        "rejected_candidates": rejected,
    }


def load_seen(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def save_seen(path: Path, urls: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(urls), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_digest(report: dict[str, Any]) -> str:
    summary = report["summary"]
    confirmed = report["confirmed_candidates"]
    lines = [
        "Лакония Клин · поиск публичных заявок в Уфе",
        f"Проверок источников: {summary['sources_attempted']}; с успешными запросами: {summary['sources_with_successful_queries']}",
        f"Получено результатов: {summary['candidate_results_received']}; уникальных кандидатов: {summary['unique_candidates']}",
        f"Подтверждено: {summary['confirmed_candidates']}; требуют проверки: {summary['needs_review']}; отклонено: {summary['rejected']}",
        f"Удалено дублей: {summary['duplicates_removed']}",
    ]
    if summary["sources_unavailable_or_failed"]:
        lines.append("Источники без успешных запросов: " + ", ".join(summary["sources_unavailable_or_failed"]))
    if not confirmed:
        lines.append("Подтверждённых кандидатов нет. Результаты с неизвестной датой оставлены на ручную проверку.")
        return "\n".join(lines)
    lines.append("")
    for i, lead in enumerate(confirmed[:15], 1):
        lines.extend([f"{i}. {lead['title']}", f"{lead['source']} · {lead['published_at']} · {lead['score']}/100", lead["excerpt"][:300], lead["url"], ""])
    return "\n".join(lines).strip()


def send_telegram(text: str, token: str | None = None, chat_id: str | None = None) -> None:
    token = (token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        raise RuntimeError("Для отправки в Telegram задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]
    for chunk in chunks:
        payload = json.dumps({"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}).encode()
        request = Request(f"{TELEGRAM_ENDPOINT}/bot{token}/sendMessage", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ошибка отправки Telegram: {type(exc).__name__}") from None
        if not body.get("ok"):
            raise RuntimeError("Telegram отклонил сообщение")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Поиск публичных заявок на уборку в Уфе.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Результатов на запрос (1–10)")
    parser.add_argument("--output", type=Path, default=Path("lead-finder-results.json"))
    parser.add_argument("--state", type=Path, default=Path(".lead-finder-seen.json"))
    parser.add_argument("--telegram", action="store_true", help="Отправить сводку только владельцу")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 10:
        parser.error("--limit должен быть от 1 до 10")
    report = run_search(FirecrawlSearch(), limit_per_query=args.limit)
    seen = load_seen(args.state)
    all_candidates = report["confirmed_candidates"] + report["needs_review"] + report["rejected_candidates"]
    for candidate in all_candidates:
        if candidate["url"] in seen:
            candidate["previously_seen"] = True
        else:
            candidate["previously_seen"] = False
    # Do not send empty/no-new-lead reports as customer notifications. Owner-only
    # summary is optional and explicitly requested with --telegram.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    save_seen(args.state, seen | {c["url"] for c in all_candidates if c["url"] != "Не указано"})
    digest = render_digest(report)
    print(digest)
    print(f"\nJSON-отчёт: {args.output}")
    if args.telegram and report["confirmed_candidates"]:
        try:
            send_telegram(digest)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        print("Сводка отправлена владельцу. Сообщения потенциальным клиентам не отправлялись.")
    elif args.telegram:
        print("Telegram не отправлен: нет подтверждённых кандидатов.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
