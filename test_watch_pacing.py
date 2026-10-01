"""An hour of `watch` on a simulated clock: how many requests reach Avito.

The network is replaced, time.sleep advances a fake clock, and the run stops
after one simulated hour. Nothing here talks to Avito.
"""

import argparse
import json
import random
import time
from urllib.parse import parse_qs, urlsplit

import pytest

import avito
import avito_client
import main
import menu
import telegram_notify

HOUR = 3600.0
REQUEST_SECONDS = 0.3
RUNAWAY_LIMIT = 5000


class Clock:
    def __init__(self, stop_at: float) -> None:
        self.now = 0.0
        self.stop_at = stop_at

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0)
        if self.now >= self.stop_at:
            raise KeyboardInterrupt  # ends cmd_watch the way Ctrl+C does


class FakeResponse:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self):
        return {"catalog": {"items": []}, "totalCount": 0, "itemsOnPage": 0}


class FakeSession:
    def __init__(self, *args, **kwargs):
        self.headers = {}


@pytest.fixture
def simulated(monkeypatch, tmp_path):
    """Run watch for one simulated hour; return the times and URLs of requests."""
    def run_watch(searches: dict[str, int], interval: float, seed: int = 7):
        random.seed(seed)
        clock = Clock(stop_at=HOUR)
        requests = []

        def fake_get(session, url, **kwargs):
            requests.append((clock.now, url))
            clock.now += REQUEST_SECONDS  # a request takes time, as in real life
            if len(requests) > RUNAWAY_LIMIT:
                raise KeyboardInterrupt  # pacing is broken: stop instead of hanging
            return FakeResponse()

        monkeypatch.setattr(time, "monotonic", clock.monotonic)
        monkeypatch.setattr(time, "sleep", clock.sleep)
        monkeypatch.setattr(main, "get_with_qrator_recovery", fake_get)
        monkeypatch.setattr(main.requests, "Session", FakeSession)
        monkeypatch.setattr(telegram_notify.Notifier, "from_settings", classmethod(lambda cls: None))
        monkeypatch.setattr(avito, "SEARCHES_DIR", tmp_path / "searches")
        monkeypatch.setattr(avito, "DATA_DIR", tmp_path / "data")
        avito.SEARCHES_DIR.mkdir(exist_ok=True)
        for name, pages in searches.items():
            (avito.SEARCHES_DIR / f"{name}.json").write_text(json.dumps({
                "title": name, "profile": "goods",
                "catalog_url": f"https://www.avito.ru/kazan/{name}",
                "params": {"categoryId": "84", "locationId": "650400"},
                "watch_pages": pages,
            }), encoding="utf-8")
        avito.cmd_watch(argparse.Namespace(
            names=list(searches), interval=interval,
            delay=avito_client.DEFAULT_DELAY_SECONDS, no_telegram=True,
        ))
        return [(at, url) for at, url in requests if "/web/1/js/items" in url]

    return run_watch


def pages_of(requests):
    return [parse_qs(urlsplit(url).query)["p"][0] for _, url in requests]


def test_after_a_full_scan_watch_reads_only_the_first_page(simulated):
    requests = simulated({"phones": 1}, interval=60)

    assert requests
    assert set(pages_of(requests)) == {"1"}


def test_two_watched_pages_read_page_one_and_two_each_cycle(simulated):
    requests = simulated({"phones": 2}, interval=180)

    pages = pages_of(requests)
    assert pages[:4] == ["1", "2", "1", "2"]


def test_requests_are_never_closer_than_the_pause_allows(simulated):
    requests = simulated({"a": 1, "b": 1, "c": 1}, interval=20)

    gaps = [later - earlier for (earlier, _), (later, _) in zip(requests, requests[1:])]
    assert min(gaps) >= 0.7 * avito_client.DEFAULT_DELAY_SECONDS - 1e-6


@pytest.mark.parametrize("searches, interval, low, high", [
    ({"phones": 1}, 180, 15, 25),                     # table: ~20 per hour
    ({"phones": 1}, 60, 50, 70),                      # ~60 per hour
    ({"phones": 1, "flats": 1}, 60, 70, 90),          # ~80 per hour
    ({"phones": 1}, 20, 100, 130),                    # ~120: the ceiling
    ({"a": 1, "b": 1, "c": 1}, 20, 100, 130),         # more searches, same ceiling
])
def test_requests_per_hour_match_the_table(simulated, searches, interval, low, high):
    requests = simulated(searches, interval=interval)

    assert low <= len(requests) <= high, len(requests)


def test_ceiling_holds_for_any_seed(simulated):
    for seed in range(5):
        assert len(simulated({"a": 1, "b": 1}, interval=0, seed=seed)) <= 130


def test_menu_asks_for_speed_and_passes_the_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(menu, "run", lambda func, **kwargs: calls.append(kwargs) or True)

    monkeypatch.setattr(menu, "choose", lambda *a, **k: 0)
    menu.watch(["phones"])
    assert calls[-1]["interval"] == avito.WATCH_SPEEDS[0][1] == 60
    assert calls[-1]["delay"] == avito_client.DEFAULT_DELAY_SECONDS

    monkeypatch.setattr(menu, "choose", lambda *a, **k: None)  # «Отмена»
    menu.watch(["phones"])
    assert len(calls) == 1


def test_new_searches_and_defaults_watch_one_page():
    assert avito.DEFAULT_WATCH_PAGES == 1
    resolved = __import__("link_resolver").ResolvedSearch(
        title="t", profile="goods", catalog_url="https://www.avito.ru/kazan/telefony",
        params={}, city="Казань", total=0, filters=[], samples=[], warnings=[],
    )
    assert resolved.config()["watch_pages"] == 1
