#!/usr/bin/env python3
"""Avito searches: collect everything, watch for new listings, build tables.

A search is one JSON file in ``searches/`` (its name is the file name):

    {"title": ..., "profile": "realty-owner" | "goods",
     "catalog_url": <the search page on avito.ru>,
     "params": {<items API parameters>}, "watch_pages": 2}

Every search keeps its own data in ``data/searches/<name>/`` and writes its
tables to ``output/``.

    uv run python avito.py list
    uv run python avito.py scan iphone15-spb            # walk all pages
    uv run python avito.py watch iphone15-spb flats-2k-spb-owners
    uv run python avito.py table iphone15-spb           # rebuild the table
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import apartments
import avito_client
import profiles

BASE_DIR = Path(__file__).resolve().parent
SEARCHES_DIR = BASE_DIR / "searches"
DATA_DIR = BASE_DIR / "data" / "searches"
OUTPUT_DIR = BASE_DIR / "output"
MAX_PAGES = 100
HEARTBEAT_EVERY_CYCLES = 20
MARKET_REFRESH_EVERY_CYCLES = 20

load_json = apartments.load_seen
save_json = apartments.save_json


def say(kind: str, **fields: Any) -> None:
    """One stdout line per event, easy to follow and to grep."""
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"{kind} " + json.dumps({"time": stamp, **fields}, ensure_ascii=False), flush=True)


@dataclass
class Search:
    name: str
    title: str
    profile: str
    catalog_url: str
    params: dict[str, str]
    watch_pages: int = 2

    @property
    def dir(self) -> Path:
        return DATA_DIR / self.name

    def path(self, file: str) -> Path:
        return self.dir / file

    @property
    def home_city(self) -> str:
        """The city slug of the search page, e.g. "sankt-peterburg"."""
        return urlsplit(self.catalog_url).path.strip("/").split("/")[0]

    def load(self, file: str) -> dict[str, Any]:
        return load_json(self.path(file))

    def save(self, file: str, data: dict[str, Any]) -> None:
        save_json(self.path(file), data)


def load_search(name: str) -> Search:
    path = SEARCHES_DIR / f"{name}.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        known = ", ".join(sorted(p.stem for p in SEARCHES_DIR.glob("*.json")))
        raise SystemExit(f"Нет поиска «{name}». Есть: {known}") from None
    if config.get("profile") not in profiles.PROFILES:
        raise SystemExit(f"{path}: неизвестный profile «{config.get('profile')}»")
    return Search(
        name=name,
        title=config.get("title", name),
        profile=config["profile"],
        catalog_url=config["catalog_url"],
        params={key: str(value) for key, value in config["params"].items()},
        watch_pages=int(config.get("watch_pages", 2)),
    )


def write_table(search: Search, seen_before: dict[str, str]) -> Path | None:
    base = search.load("listings.json")
    if not base:
        return None
    profile = profiles.PROFILES[search.profile](base, search.home_city)
    path = OUTPUT_DIR / f"{search.name}-{datetime.now():%Y-%m-%d_%H-%M}.xlsx"
    counts = profile.write_table(path, seen_before)
    say("TABLE", search=search.name, path=str(path), **counts)
    return path


def cmd_list(_: argparse.Namespace) -> None:
    for path in sorted(SEARCHES_DIR.glob("*.json")):
        search = load_search(path.stem)
        state = search.load("state.json")
        stored = len(search.load("listings.json"))
        progress = "пройден целиком" if state.get("complete") else f"следующая страница {state.get('next_page', 1)}"
        print(f"{search.name:24} {search.profile:13} объявлений: {stored:5}  {progress}  — {search.title}")


def cmd_scan(args: argparse.Namespace) -> None:
    """Walk the search from the saved page to the last one, saving every page."""
    search = load_search(args.name)
    state = search.load("state.json")
    start = 1 if args.full or state.get("complete") else int(state.get("next_page", 1))
    seen = search.load("seen.json")
    seen_before = dict(seen)
    base = search.load("listings.json")
    client = avito_client.CalmClient(delay=args.delay, log=say)
    say("SCAN", search=search.name, start_page=start)
    last_page = min(start + args.max_pages - 1, MAX_PAGES)
    number = start
    try:
        while number <= last_page:
            page = client.page(catalog_url=search.catalog_url, params=search.params, number=number)
            stamp = datetime.now().isoformat(timespec="seconds")
            new = 0
            for item in page.items:
                key = str(item["id"])
                base[key] = item
                if key not in seen:
                    seen[key] = stamp
                    new += 1
            search.save("listings.json", base)
            search.save("seen.json", seen)
            complete = page.is_last or number >= MAX_PAGES
            search.save("state.json", {"complete": complete, "next_page": 1 if complete else number + 1})
            say("PAGE", search=search.name, page=number, items=len(page.items), new=new, total=page.total_count)
            if complete:
                break
            number += 1
    except (RuntimeError, ValueError, *avito_client.TRANSPORT_ERRORS) as exc:
        say("ERROR", search=search.name, page=number, error=f"{type(exc).__name__}: {exc}"[:300])
    write_table(search, seen_before)


def cmd_watch(args: argparse.Namespace) -> None:
    """Read the first pages of every search in turn; alert on unseen listings."""
    searches = [load_search(name) for name in args.names]
    client = avito_client.CalmClient(delay=args.delay, log=say)
    watched = []
    for search in searches:
        base = search.load("listings.json")
        watched.append((search, base, search.load("seen.json"), profiles.PROFILES[search.profile](base, search.home_city)))
        search.dir.mkdir(parents=True, exist_ok=True)
    say("WATCH", searches=[s.name for s in searches], interval_s=args.interval)
    cycle = 0
    while True:
        cycle += 1
        for search, base, seen, profile in watched:
            try:
                changed = False
                for number in range(1, search.watch_pages + 1):
                    page = client.page(catalog_url=search.catalog_url, params=search.params, number=number)
                    stamp = datetime.now().isoformat(timespec="seconds")
                    for item in page.items:
                        key = str(item["id"])
                        if key in seen:
                            continue
                        seen[key] = stamp
                        base[key] = item
                        changed = True
                        fields = profile.alert(item)
                        if fields is None:
                            continue
                        fields = {"search": search.name, **fields}
                        with search.path("alerts.jsonl").open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps({"found": stamp, **fields}, ensure_ascii=False) + "\n")
                        say("ALERT", **fields)
                if changed:
                    search.save("listings.json", base)
                    search.save("seen.json", seen)
            except (RuntimeError, ValueError, *avito_client.TRANSPORT_ERRORS) as exc:
                say("ERROR", search=search.name, error=f"{type(exc).__name__}: {exc}"[:300])
            if cycle % MARKET_REFRESH_EVERY_CYCLES == 0 and hasattr(profile, "refresh_market"):
                profile.refresh_market()
        if cycle % HEARTBEAT_EVERY_CYCLES == 0:
            say("HEARTBEAT", cycle=cycle, sessions=client.sessions_opened)
        time.sleep(args.interval * random.uniform(0.8, 1.2))


def cmd_table(args: argparse.Namespace) -> None:
    search = load_search(args.name)
    if write_table(search, search.load("seen.json")) is None:
        print("В базе этого поиска пока нет объявлений: сначала scan.")


def main_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Поиски на Авито: сбор, наблюдение, таблицы")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="показать поиски").set_defaults(func=cmd_list)
    scan = sub.add_parser("scan", help="собрать все страницы поиска")
    scan.add_argument("name")
    scan.add_argument("--full", action="store_true", help="начать с первой страницы")
    scan.add_argument("--max-pages", type=int, default=MAX_PAGES)
    scan.add_argument("--delay", type=float, default=avito_client.DEFAULT_DELAY_SECONDS)
    scan.set_defaults(func=cmd_scan)
    watch = sub.add_parser("watch", help="следить за новыми объявлениями")
    watch.add_argument("names", nargs="+")
    watch.add_argument("--interval", type=float, default=180, help="секунд между циклами")
    watch.add_argument("--delay", type=float, default=avito_client.DEFAULT_DELAY_SECONDS)
    watch.set_defaults(func=cmd_watch)
    table = sub.add_parser("table", help="пересобрать таблицу из базы")
    table.add_argument("name")
    table.set_defaults(func=cmd_table)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    sys.exit(main_cli())
