from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse
from urllib.parse import parse_qs, unquote

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from google.oauth2 import service_account
from googleapiclient.discovery import build


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sources.yml"
SENT_PATH = ROOT / "data" / "sent_events.json"
STATE_PATH = ROOT / "data" / "run_state.json"

AI_KEYWORDS = re.compile(
    r"\b(ai|artificial intelligence|machine learning|ml|deep learning|llm|generative ai|genai|data science)\b",
    re.IGNORECASE,
)
FREE_KEYWORDS = re.compile(r"\b(free|no cost|complimentary|gratis)\b", re.IGNORECASE)
ONLINE_KEYWORDS = re.compile(r"\b(online|virtual|webinar|livestream|remote)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Event:
    source: str
    title: str
    date: datetime
    category: str
    organizer: str
    registration_url: str
    description: str
    is_free: bool = False
    is_online: bool = False

    @property
    def event_id(self) -> str:
        normalized = "|".join(
            [
                self.source.lower().strip(),
                self.title.lower().strip(),
                self.date.date().isoformat(),
                canonical_url(self.registration_url),
            ]
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def main() -> int:
    args = parse_args()
    now = datetime.now(timezone.utc)

    if not args.force and not should_run(now):
        print("Skipping: fewer than 3 days since last successful run.")
        return 0

    config = load_yaml(CONFIG_PATH)
    sent_history = load_json(SENT_PATH, {"sent": []})
    sent_ids = {item["id"] for item in sent_history.get("sent", []) if "id" in item}

    events = discover_events(config, now)
    new_events = [event for event in events if event.event_id not in sent_ids]
    grouped = group_by_source(new_events)

    if not grouped:
        print("No new qualifying events found.")
        if not args.dry_run:
            save_json(STATE_PATH, {"last_successful_run": now.isoformat()})
        return 0

    for source, source_events in grouped.items():
        send_source_email(source, source_events, dry_run=args.dry_run)

    append_to_google_sheet(new_events, dry_run=args.dry_run)

    if not args.dry_run:
        sent_history.setdefault("sent", [])
        for event in new_events:
            sent_history["sent"].append(
                {
                    "id": event.event_id,
                    "source": event.source,
                    "title": event.title,
                    "date": event.date.isoformat(),
                    "registration_url": event.registration_url,
                    "sent_at": now.isoformat(),
                }
            )
        save_json(SENT_PATH, sent_history)
        save_json(STATE_PATH, {"last_successful_run": now.isoformat()})

    print(f"Processed {len(new_events)} new events across {len(grouped)} sources.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Collect only; do not email, update Sheets, or write history.")
    parser.add_argument("--force", action="store_true", help="Ignore the 3-day run gate.")
    return parser.parse_args()


def should_run(now: datetime) -> bool:
    state = load_json(STATE_PATH, {"last_successful_run": None})
    last_run = state.get("last_successful_run")
    if not last_run:
        return True
    try:
        last = ensure_aware(date_parser.parse(last_run))
    except (ValueError, TypeError):
        return True
    return (now - last).days >= 3


def discover_events(config: dict[str, Any], now: datetime) -> list[Event]:
    events: list[Event] = []
    seen_urls: set[str] = set()

    for source_config in config["sources"]:
        source_name = source_config["name"]
        candidate_urls = list(source_config.get("seed_urls", []))
        candidate_urls.extend(search_source_urls(config.get("search_terms", []), source_config))

        for url in candidate_urls:
            normalized_url = canonical_url(url)
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)

            try:
                page_events = extract_events_from_page(source_name, url)
            except requests.RequestException as exc:
                print(f"Could not fetch {url}: {exc}", file=sys.stderr)
                continue

            for event in page_events:
                if qualifies(event, now):
                    events.append(event)

    deduped = {event.event_id: event for event in events}
    return sorted(deduped.values(), key=lambda item: (item.source, item.date, item.title.lower()))


def search_source_urls(search_terms: list[str], source_config: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for domain in source_config.get("domains", []):
        for term in search_terms:
            query = f"site:{domain} {term}"
            search_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
            try:
                response = fetch(search_url)
            except requests.RequestException:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for link in soup.select("a.result__a"):
                href = link.get("href")
                result_url = unwrap_search_url(href or "")
                if result_url and domain in result_url:
                    urls.append(result_url)
    return urls[:80]


def extract_events_from_page(source: str, url: str) -> list[Event]:
    response = fetch(url)
    soup = BeautifulSoup(response.text, "html.parser")
    events = extract_jsonld_events(source, url, soup)
    if events:
        return events
    fallback = extract_fallback_event(source, url, soup)
    return [fallback] if fallback else []


def extract_jsonld_events(source: str, url: str, soup: BeautifulSoup) -> list[Event]:
    events: list[Event] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        for node in iter_json_nodes(raw):
            if jsonld_type(node) != "Event":
                continue
            event = event_from_jsonld(source, url, node)
            if event:
                events.append(event)
    return events


def iter_json_nodes(raw: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []

    nodes: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            nodes.append(value)
            graph = value.get("@graph")
            if isinstance(graph, list):
                for child in graph:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(parsed)
    return nodes


def jsonld_type(node: dict[str, Any]) -> str:
    value = node.get("@type", "")
    if isinstance(value, list):
        return "Event" if "Event" in value else ""
    return str(value)


def event_from_jsonld(source: str, url: str, node: dict[str, Any]) -> Event | None:
    title = clean_text(node.get("name"))
    raw_date = node.get("startDate") or node.get("startTime")
    if not title or not raw_date:
        return None
    try:
        event_date = ensure_aware(date_parser.parse(str(raw_date)))
    except (ValueError, TypeError):
        return None

    offers = node.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    organizer = node.get("organizer") or {}
    category = node.get("eventType") or node.get("keywords") or "AI/ML"
    registration_url = (
        clean_text(offers.get("url") if isinstance(offers, dict) else None)
        or clean_text(node.get("url"))
        or url
    )
    offer_text = clean_text(offers)
    attendance_mode = clean_text(node.get("eventAttendanceMode"))
    is_free = bool(re.search(r"\b0(?:\.00)?\b|free", offer_text, re.IGNORECASE))
    is_online = "OnlineEventAttendanceMode" in attendance_mode or bool(ONLINE_KEYWORDS.search(attendance_mode))

    return Event(
        source=source,
        title=title,
        date=event_date,
        category=clean_text(category) or "AI/ML",
        organizer=extract_name(organizer),
        registration_url=registration_url,
        description=clean_text(node.get("description"))[:1200],
        is_free=is_free,
        is_online=is_online,
    )


def extract_fallback_event(source: str, url: str, soup: BeautifulSoup) -> Event | None:
    title = clean_text(meta_content(soup, "og:title") or (soup.title.string if soup.title else ""))
    description = clean_text(meta_content(soup, "og:description") or meta_content(soup, "description"))
    page_text = clean_text(soup.get_text(" "))
    raw_date = find_date(page_text)
    if not title or not raw_date:
        return None
    try:
        event_date = ensure_aware(date_parser.parse(raw_date, fuzzy=True))
    except (ValueError, TypeError):
        return None
    return Event(
        source=source,
        title=title,
        date=event_date,
        category="AI/ML",
        organizer=source,
        registration_url=url,
        description=description or page_text[:1200],
    )


def qualifies(event: Event, now: datetime) -> bool:
    searchable = " ".join([event.title, event.category, event.organizer, event.description, event.registration_url])
    if event.date < now:
        return False
    if not AI_KEYWORDS.search(searchable):
        return False
    if not event.is_online and not ONLINE_KEYWORDS.search(searchable):
        return False
    if not event.is_free and not FREE_KEYWORDS.search(searchable):
        return False
    return True


def group_by_source(events: list[Event]) -> dict[str, list[Event]]:
    grouped: dict[str, list[Event]] = {}
    for event in sorted(events, key=lambda item: item.date):
        grouped.setdefault(event.source, []).append(event)
    return grouped


def send_source_email(source: str, events: list[Event], dry_run: bool) -> None:
    if len(events) == 1:
        subject = events[0].title
    else:
        subject = f"[{source}] {len(events)} Upcoming Free Online AI/ML Events"

    body = build_email_body(source, events)
    if dry_run:
        print(f"[dry-run] Would email {source}: {subject}")
        return

    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing email secrets: {', '.join(missing)}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ["EMAIL_FROM"]
    message["To"] = os.environ["EMAIL_TO"]
    message.set_content(body)
    message.add_alternative(build_email_html(source, events), subtype="html")

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


def build_email_body(source: str, events: list[Event]) -> str:
    lines = [f"{source} upcoming free online AI/ML events", ""]
    for event in events:
        lines.extend(
            [
                event.title,
                f"Date: {event.date.strftime('%Y-%m-%d %H:%M %Z')}",
                f"Category: {event.category}",
                f"Organizer: {event.organizer}",
                f"Registration URL: {event.registration_url}",
                f"Description: {event.description}",
                "",
            ]
        )
    return "\n".join(lines)


def build_email_html(source: str, events: list[Event]) -> str:
    chunks = [f"<h2>{html.escape(source)} upcoming free online AI/ML events</h2>"]
    for event in events:
        chunks.append(
            "<section>"
            f"<h3>{html.escape(event.title)}</h3>"
            f"<p><strong>Date:</strong> {html.escape(event.date.strftime('%Y-%m-%d %H:%M %Z'))}</p>"
            f"<p><strong>Category:</strong> {html.escape(event.category)}</p>"
            f"<p><strong>Organizer:</strong> {html.escape(event.organizer)}</p>"
            f"<p><strong>Registration URL:</strong> <a href=\"{html.escape(event.registration_url)}\">{html.escape(event.registration_url)}</a></p>"
            f"<p><strong>Description:</strong> {html.escape(event.description)}</p>"
            "</section>"
        )
    return "\n".join(chunks)


def append_to_google_sheet(events: list[Event], dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] Would append {len(events)} rows to Google Sheets.")
        return
    credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    tab_name = os.getenv("GOOGLE_SHEET_TAB") or "Events"
    if not credentials_json or not sheet_id:
        raise RuntimeError("Missing Google Sheets secrets: GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID")

    credentials_info = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    service = build("sheets", "v4", credentials=credentials)
    ensure_sheet_tab(service, sheet_id, tab_name)
    rows = [
        [
            event.source,
            event.title,
            event.date.isoformat(),
            event.category,
            event.organizer,
            event.registration_url,
            event.description,
            event.event_id,
            datetime.now(timezone.utc).isoformat(),
        ]
        for event in sorted(events, key=lambda item: (item.source, item.date))
    ]
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A:I",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def ensure_sheet_tab(service: Any, sheet_id: str, tab_name: str) -> None:
    spreadsheet = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets = spreadsheet.get("sheets", [])
    existing_titles = {sheet["properties"]["title"] for sheet in sheets}
    if tab_name not in existing_titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
        ).execute()
        header = [["Source", "Title", "Date", "Category", "Organizer", "Registration URL", "Description", "Event ID", "Added At"]]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A:I",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": header},
        ).execute()


def fetch(url: str) -> requests.Response:
    response = requests.get(
        url,
        timeout=25,
        headers={
            "User-Agent": "Mozilla/5.0 AI-ML-Event-Digest/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    response.raise_for_status()
    return response


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def unwrap_search_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "uddg" in params and params["uddg"]:
        return unquote(params["uddg"][0])
    return url


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)
    return re.sub(r"\s+", " ", str(value)).strip()


def extract_name(value: Any) -> str:
    if isinstance(value, dict):
        return clean_text(value.get("name") or value.get("legalName")) or "Unknown"
    if isinstance(value, list) and value:
        return extract_name(value[0])
    return clean_text(value) or "Unknown"


def meta_content(soup: BeautifulSoup, name: str) -> str:
    selector = f'meta[property="{name}"], meta[name="{name}"]'
    tag = soup.select_one(selector)
    return clean_text(tag.get("content")) if tag else ""


def find_date(text: str) -> str | None:
    patterns = [
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)?",
        r"\b\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}(?::\d{2})?)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
