"""Read-only lead finder for Lakoniya Klin (Ufa).

Searches public web results, scores likely customer requests, deduplicates them,
and optionally sends a digest to the owner's Telegram chat. It never contacts leads.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SEARCH_ENDPOINT = "https://api.firecrawl.dev/v2/search"
TELEGRAM_ENDPOINT = "https://api.telegram.org"
DEFAULT_LIMIT = 8
SEARCH_QUERIES = (
    '"ищу уборку" Уфа квартира',
    '"нужна уборка" Уфа',
    '"заказать клининг" Уфа',
    '"уборка после ремонта" Уфа "ищу"',
    '"нужна уборщица" Уфа квартира частный заказ',
    'site:profi.ru/geo-ufa уборка заказ',
    'site:youdo.com уборка Уфа заказчик',
)

SERVICE_TERMS = (
    "убор", "клининг", "клинингов", "мытье окон", "мытьё окон",
    "после ремонта", "генеральн", "поддерживающ",
)
BUYING_INTENT_TERMS = (
    "ищу убор", "ищем убор", "нужна убор", "нужен клининг",
    "нужна уборщиц", "нужен уборщик", "заказать убор", "заказать клининг",
    "требуется уборка", "ищу клинингов", "нужна клинингов",
    "кто убер", "посоветуйте убор", "посоветуйте клининг",
    "после ремонта", "сколько стоит убор",
)
NEGATIVE_TERMS = (
    "вакансия", "вакансии", "ищем сотрудник", "требуется сотрудник",
    "требуется уборщица на работу", "работа уборщицей", "резюме",
    "устроиться уборщ", "зарплата уборщ", "обучение клинингу",
    "купить оборудование", "франшиза клининга",
)
TRACKING_KEYS = {"fbclid", "gclid", "yclid", "ref", "source", "utm_source", "utm_medium", "utm_campaign"}


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str = ""
    query: str = ""


@dataclass(frozen=True)
class Lead:
    title: str
    url: str
    description: str
    query: str
    score: int
    reason: str


def normalize_url(url: str) -> str:
    """Remove tracking params and fragments to make duplicate detection stable."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_KEYS and not k.lower().startswith("utm_")]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def score_result(result: SearchResult) -> Lead | None:
    text = f"{result.title} {result.description} {result.query}".lower()
    if not result.url.startswith(("http://", "https://")):
        return None
    if any(term in text for term in NEGATIVE_TERMS):
        return None
    if not any(term in text for term in SERVICE_TERMS):
        return None

    buying_hits = [term for term in BUYING_INTENT_TERMS if term in text]
    city_hit = any(term in text for term in ("уфа", "уфе", "уфы", "уфу")) or "ufa" in result.url.lower()
    if not buying_hits:
        return None

    score = 45 + min(30, 10 * len(buying_hits)) + (15 if city_hit else 0)
    reason_parts = ["есть признаки запроса услуги"]
    if city_hit:
        reason_parts.append("есть указание на Уфу")
    if len(buying_hits) > 1:
        reason_parts.append("несколько признаков клиентского намерения")
    return Lead(
        title=(result.title or "Без названия").strip()[:240],
        url=normalize_url(result.url),
        description=re.sub(r"\s+", " ", result.description).strip()[:1000],
        query=result.query,
        score=min(score, 100),
        reason="; ".join(reason_parts),
    )


class FirecrawlSearch:
    """Small stdlib-only client for Firecrawl's public Search API."""

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
        results = []
        for item in items:
            url = item.get("url") or item.get("link") or ""
            if not url:
                continue
            results.append(SearchResult(
                title=str(item.get("title") or ""),
                url=str(url),
                description=str(item.get("description") or item.get("snippet") or ""),
                query=query,
            ))
        return results


def find_leads(searcher: Any, queries: tuple[str, ...] = SEARCH_QUERIES,
               limit_per_query: int = DEFAULT_LIMIT) -> list[Lead]:
    by_url: dict[str, Lead] = {}
    for query in queries:
        for result in searcher.search(query, limit=limit_per_query):
            lead = score_result(result)
            if lead is None:
                continue
            current = by_url.get(lead.url)
            if current is None or lead.score > current.score:
                by_url[lead.url] = lead
        time.sleep(0.05)  # be polite to the provider and avoid bursty calls
    return sorted(by_url.values(), key=lambda lead: (-lead.score, lead.title.lower()))


def load_seen(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def save_seen(path: Path, urls: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(urls), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_digest(leads: list[Lead]) -> str:
    if not leads:
        return "Лакония Клин · Уфа\nНовых подходящих публикаций не найдено."
    lines = [f"Лакония Клин · потенциальные клиенты в Уфе: {len(leads)}", ""]
    for index, lead in enumerate(leads[:20], start=1):
        lines.extend([
            f"{index}. {lead.title}",
            f"Оценка релевантности: {lead.score}/100",
            f"Почему отобрано: {lead.reason}",
            (lead.description[:350] if lead.description else "Описание не предоставлено поиском"),
            lead.url,
            "",
        ])
    return "\n".join(lines).strip()


def send_telegram(text: str, token: str | None = None, chat_id: str | None = None) -> None:
    token = (token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        raise RuntimeError("Для отправки в Telegram задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [text]
    for chunk in chunks:
        payload = json.dumps({"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}).encode()
        request = Request(
            f"{TELEGRAM_ENDPOINT}/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ошибка отправки Telegram: {type(exc).__name__}") from None
        if not body.get("ok"):
            raise RuntimeError("Telegram отклонил сообщение")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Поиск публичных заявок на уборку в Уфе.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Результатов на один поисковый запрос (1–10)")
    parser.add_argument("--min-score", type=int, default=45, help="Минимальная оценка релевантности (0–100)")
    parser.add_argument("--output", type=Path, default=Path("lead-finder-results.json"), help="Путь для JSON-отчёта")
    parser.add_argument("--state", type=Path, default=Path(".lead-finder-seen.json"), help="Файл истории уже показанных ссылок")
    parser.add_argument("--telegram", action="store_true", help="Отправить новые результаты только в Telegram владельца")
    parser.add_argument("--include-seen", action="store_true", help="Показывать также результаты из прошлых запусков")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 10:
        parser.error("--limit должен быть от 1 до 10")
    if not 0 <= args.min_score <= 100:
        parser.error("--min-score должен быть от 0 до 100")

    try:
        leads = [lead for lead in find_leads(FirecrawlSearch(), limit_per_query=args.limit)
                 if lead.score >= args.min_score]
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    seen = load_seen(args.state)
    fresh = leads if args.include_seen else [lead for lead in leads if lead.url not in seen]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps([asdict(lead) for lead in fresh], ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    save_seen(args.state, seen | {lead.url for lead in leads})
    print(render_digest(fresh))
    print(f"\nJSON-отчёт: {args.output}")
    if args.telegram:
        try:
            send_telegram(render_digest(fresh))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        print("Отчёт отправлен в Telegram владельца. Клиентам сообщения не отправлялись.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
