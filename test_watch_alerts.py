"""What watch alerts on: only listings that appear after it starts, and are fresh."""

import argparse
import json
import time
from datetime import datetime, timedelta

import pytest

import avito
import avito_client
import telegram_notify


def listing(item_id: int, minutes_ago: float, *, seller: str | None = None) -> dict:
    stamp = (datetime.now() - timedelta(minutes=minutes_ago)).timestamp() * 1000
    item = {
        "id": item_id, "type": "item", "title": "iPhone 15, 128 ГБ",
        "urlPath": f"/kazan/telefony/iphone_15_{item_id}",
        "priceDetailed": {"value": 35_000}, "sortTimeStamp": stamp,
        "closedItemsText": "3 завершённых объявления", "contacts": {"phone": True},
        "iva": {"SecondLineStep": [{"payload": {"value": seller}}] if seller else []},
    }
    return item


def item_html(*, total: int, today: int | None) -> str:
    today_part = f'<span data-marker="item-view/today-views"> <!-- -->(+{today} сегодня)</span>' if today is not None else ""
    return (
        '<span data-marker="item-view/item-date"> · <!-- -->сегодня в 18:22</span>'
        f'<span data-marker="item-view/total-views">{total}\xa0просмотров</span>{today_part}'
    )


@pytest.fixture
def watch_cycles(monkeypatch, tmp_path):
    """Run watch over scripted first pages, one list per cycle; return alerted ids."""
    def run(cycles: list[list[dict]], pages: dict[int, str | None] | None = None) -> tuple[list[int], list[dict]]:
        pages = pages or {}
        monkeypatch.setattr(avito, "SEARCHES_DIR", tmp_path / "searches")
        monkeypatch.setattr(avito, "DATA_DIR", tmp_path / "data")
        avito.SEARCHES_DIR.mkdir(exist_ok=True)
        (avito.SEARCHES_DIR / "phones.json").write_text(json.dumps({
            "title": "phones", "profile": "goods", "catalog_url": "https://www.avito.ru/kazan/telefony",
            "params": {"categoryId": "84", "locationId": "650400"},
        }), encoding="utf-8")
        script = iter(cycles)

        class FakeClient:
            sessions_opened = 1

            def __init__(self, **kwargs):
                pass

            def page(self, *, catalog_url, params, number):
                return avito_client.Page(number=1, items=tuple(next(script)), total_count=1500, items_on_page=50)

            def get_document(self, url, *, catalog_url):
                item_id = int(url.rsplit("_", 1)[1])
                return pages.get(item_id, item_html(total=6, today=6))

        events = []
        monkeypatch.setattr(avito.avito_client, "CalmClient", FakeClient)
        monkeypatch.setattr(avito, "say", lambda kind, **fields: events.append({"event": kind, **fields}))
        monkeypatch.setattr(telegram_notify.Notifier, "from_settings", classmethod(lambda cls: None))
        calls = {"n": 0}

        def sleep(_seconds):
            calls["n"] += 1
            if calls["n"] >= len(cycles):
                raise KeyboardInterrupt

        monkeypatch.setattr(time, "sleep", sleep)
        avito.cmd_watch(argparse.Namespace(names=["phones"], interval=60, delay=30, no_telegram=True))
        alerted = [e["id"] for e in events if e["event"] == "ALERT"]
        return alerted, events

    return run


def test_listings_already_listed_at_start_do_not_alert(watch_cycles):
    already_there = [listing(1, minutes_ago=5), listing(2, minutes_ago=40), listing(3, minutes_ago=200)]

    alerted, events = watch_cycles([already_there, already_there])

    assert alerted == []
    assert any(e["event"] == "PRIMED" and e["remembered"] == 3 for e in events)


def test_listing_that_appears_after_start_alerts_once(watch_cycles):
    start = [listing(1, minutes_ago=30)]
    fresh = listing(10, minutes_ago=1)

    alerted, _ = watch_cycles([start, [fresh] + start, [fresh] + start])

    assert alerted == [10]


def test_old_listing_surfacing_after_start_does_not_alert(watch_cycles):
    start = [listing(1, minutes_ago=30)]
    raised_old = listing(20, minutes_ago=3 * 24 * 60)   # promoted or raised, 3 days old
    just_over = listing(21, minutes_ago=181)             # entered the catalog over 3 hours ago

    alerted, _ = watch_cycles([start, [raised_old, just_over] + start])

    assert alerted == []


def test_new_company_listing_does_not_alert_for_goods(watch_cycles):
    start = [listing(1, minutes_ago=30)]

    alerted, _ = watch_cycles([start, [listing(30, minutes_ago=1, seller="Компания")] + start])

    assert alerted == []


def test_default_freshness_window_covers_slow_moderation():
    assert avito.DEFAULT_FRESH_HOURS == 3.0


def test_republished_old_listing_does_not_alert(watch_cycles):
    start = [listing(1, minutes_ago=30)]
    republished = listing(40, minutes_ago=2)   # catalog says "fresh", the page says otherwise

    alerted, events = watch_cycles(
        [start, [republished] + start], pages={40: item_html(total=8526, today=109)}
    )

    assert alerted == []
    assert any(e["event"] == "REPUBLISHED" and e["total_views"] == 8526 for e in events)


def test_newly_created_listing_alerts_with_its_views(watch_cycles):
    start = [listing(1, minutes_ago=30)]

    alerted, events = watch_cycles([start, [listing(41, minutes_ago=2)] + start], pages={41: item_html(total=7, today=7)})

    assert alerted == [41]
    alert = next(e for e in events if e["event"] == "ALERT")
    assert alert["views"] == "7 (+7 сегодня)"


def test_unreadable_page_still_alerts_but_says_so(watch_cycles):
    start = [listing(1, minutes_ago=30)]

    alerted, events = watch_cycles([start, [listing(42, minutes_ago=2)] + start], pages={42: None})

    assert alerted == [42]
    alert = next(e for e in events if e["event"] == "ALERT")
    assert "не удалось проверить, новое ли объявление" in alert["notes"]
