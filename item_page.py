"""What a listing's own page says that the catalog does not: views and date.

The catalog shows a re-published old listing exactly like a new one (same
"сегодня" date, same timestamps). Its page gives it away: "8526 просмотров
(+109 сегодня)" — a listing created today has almost all its views today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import avito_client

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
DATE_MARKER = re.compile(r'data-marker="item-view/item-date"[^>]*>(.*?)</span>', re.S)
TOTAL_VIEWS = re.compile(r'data-marker="item-view/total-views"[^>]*>(.*?)</span>', re.S)
TODAY_VIEWS = re.compile(r'data-marker="item-view/today-views"[^>]*>(.*?)</span>', re.S)
# Views before today allowed for a listing still counted as newly created.
MAX_EARLIER_VIEWS = 10
# Right after midnight yesterday's views of a fresh listing are "earlier" too.
MAX_EARLIER_VIEWS_AFTER_MIDNIGHT = 100
AFTER_MIDNIGHT_HOURS = 2


@dataclass(frozen=True)
class ItemPage:
    published_text: str
    published: datetime | None
    total_views: int | None
    today_views: int | None

    @property
    def earlier_views(self) -> int | None:
        if self.total_views is None:
            return None
        return self.total_views - (self.today_views or 0)


def _text(html_fragment: str) -> str:
    return re.sub(r"<[^>]+>|·", " ", html_fragment).replace("\xa0", " ").strip()


def _number(fragment: str | None) -> int | None:
    if fragment is None:
        return None
    digits = re.sub(r"\D", "", _text(fragment))
    return int(digits) if digits else None


def parse_date(text: str, now: datetime) -> datetime | None:
    """ "сегодня в 17:05", "вчера в 09:12", "29 сентября в 07:21", "3 мая 2025 в 10:00"."""
    text = _text(text).lower()
    clock = re.search(r"в (\d{1,2}):(\d{2})", text)
    if not clock:
        return None
    hour, minute = int(clock[1]), int(clock[2])
    if "сегодня" in text:
        day = now.date()
    elif "вчера" in text:
        day = (now - timedelta(days=1)).date()
    else:
        found = re.search(r"(\d{1,2}) ([а-я]+)(?: (\d{4}))?", text)
        if not found or found[2] not in MONTHS:
            return None
        year = int(found[3]) if found[3] else now.year
        day = datetime(year, MONTHS[found[2]], int(found[1])).date()
        if not found[3] and day > now.date():
            day = day.replace(year=year - 1)
    return datetime(day.year, day.month, day.day, hour, minute)


def parse(html: str, now: datetime | None = None) -> ItemPage:
    now = now or datetime.now()
    date = DATE_MARKER.search(html)
    total = TOTAL_VIEWS.search(html)
    today = TODAY_VIEWS.search(html)
    return ItemPage(
        published_text=_text(date.group(1)) if date else "",
        published=parse_date(date.group(1), now) if date else None,
        total_views=_number(total.group(1)) if total else None,
        today_views=_number(today.group(1)) if today else (0 if total else None),
    )


def is_newly_created(page: ItemPage, now: datetime | None = None) -> bool | None:
    """True for a listing created recently, False for a re-published old one, None if unknown."""
    earlier = page.earlier_views
    if earlier is None:
        return None
    now = now or datetime.now()
    limit = MAX_EARLIER_VIEWS_AFTER_MIDNIGHT if now.hour < AFTER_MIDNIGHT_HOURS else MAX_EARLIER_VIEWS
    return earlier <= limit


def fetch(client: avito_client.CalmClient, url: str, *, catalog_url: str) -> ItemPage | None:
    """Open the listing page with the calm client; None if it would not open."""
    html = client.get_document(url, catalog_url=catalog_url)
    return parse(html) if html else None
