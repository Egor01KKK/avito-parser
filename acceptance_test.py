#!/usr/bin/env python3
"""Live acceptance test: do watch alerts bring only fresh listings, and all of them?

1. Runs the real ``watch`` for one search for N minutes (alerts go to
   Telegram as usual).
2. Garbage check: opens every alerted listing's own page on Avito and reads
   its publication date there ("29 сентября в 07:21"), an independent source.
   An alert is garbage if the listing was published more than fresh_hours
   before the alert, has views from earlier days (an old re-published one),
   or breaks the search's seller/city rules.
3. Miss check: walks the catalog newest first back to the start of the test
   and collects every listing that entered it during the test and passes the
   same rules; each must have alerted.

    uv run python acceptance_test.py iphone15-spb --minutes 60

Writes data/acceptance-<search>-<time>.json. Local experiment, not part of
the parser.
"""

from __future__ import annotations

import argparse
import json
import _thread
import threading
import statistics
from datetime import datetime, timedelta
from typing import Any

import avito
import avito_client
import goods
import item_page
import profiles

MAX_CATALOG_PAGES = 6


def run_watch(search: avito.Search, minutes: float, interval: float) -> tuple[datetime, datetime]:
    # A timer that interrupts the main thread works on Windows too (SIGALRM
    # does not exist there); watch treats it like Ctrl+C.
    timer = threading.Timer(minutes * 60, _thread.interrupt_main)
    timer.daemon = True
    timer.start()
    start = datetime.now()
    try:
        avito.cmd_watch(argparse.Namespace(
            names=[search.name], interval=interval,
            delay=avito_client.DEFAULT_DELAY_SECONDS, no_telegram=False,
        ))
    finally:
        timer.cancel()
    return start, datetime.now()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("search")
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    search = avito.load_search(args.search)
    seen_before = search.load("seen.json")

    print(f"=== 1. Наблюдение «{search.title}» {args.minutes:.0f} мин, проверка раз в {args.interval:.0f} с")
    start, end = run_watch(search, args.minutes, args.interval)
    alerts = [
        json.loads(line)
        for line in search.path("alerts.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip() and start <= datetime.fromisoformat(json.loads(line)["found"]) <= end
    ]
    print(f"\n=== 2. Проверка {len(alerts)} уведомлений по страницам объявлений")
    client = avito_client.CalmClient(log=avito.say)
    checked = []
    for alert in alerts:
        page = item_page.fetch(client, alert["url"], catalog_url=search.catalog_url)
        published = page.published if page else None
        raw = page.published_text if page else "страница не открылась (объявление снято?)"
        found = datetime.fromisoformat(alert["found"])
        lag_min = round((found - published).total_seconds() / 60, 1) if published else None
        problems = []
        if published is None:
            problems.append(raw)
        elif lag_min > search.fresh_hours * 60:
            problems.append(f"старое: опубликовано {raw}, за {lag_min / 60:.1f} ч до уведомления")
        if page and item_page.is_newly_created(page) is False:
            problems.append(f"переопубликованное: {page.total_views} просмотров, сегодня {page.today_views}")
        if search.profile == "goods" and search.alert_sellers == "private":
            if alert.get("seller") != "частное лицо":
                problems.append(f"продавец: {alert.get('seller')}")
            elif (alert.get("closed_listings") or 0) >= goods.SHOP_CLOSED_LISTINGS:
                problems.append(f"похоже на магазин: {alert.get('closed_listings')} завершённых")
        if search.profile == "realty-owner" and alert.get("verdict") == "агент":
            problems.append("агент")
        checked.append({**alert, "published_on_page": raw, "lag_min": lag_min, "problems": problems})
        print(f"  {'✗' if problems else '✓'} {alert['title']} {alert['price']} · на странице: {raw}"
              f" · пришло через {lag_min} мин" + (f" · {'; '.join(problems)}" if problems else ""))

    print("\n=== 3. Поиск пропусков: выдача по дате до начала теста")
    base = search.load("listings.json")
    profile = profiles.PROFILES[search.profile](base, search.home_city, search.alert_sellers)
    entered: dict[str, dict[str, Any]] = {}
    # The first pass only remembers what is already listed, so the window for
    # "should have alerted" opens once that pass is over.
    start_ms = (start + timedelta(seconds=args.interval + 60)).timestamp() * 1000
    # Listings entering in the last minutes may not have been polled yet.
    end_ms = (end - timedelta(seconds=2 * args.interval)).timestamp() * 1000
    for number in range(1, MAX_CATALOG_PAGES + 1):
        page = client.page(catalog_url=search.catalog_url, params=search.request_params, number=number)
        stamps = [item.get("sortTimeStamp") or 0 for item in page.items]
        for item in page.items:
            if start_ms <= (item.get("sortTimeStamp") or 0) <= end_ms:
                entered[str(item["id"])] = item
        if not stamps or sorted(stamps)[len(stamps) // 2] < start_ms or page.is_last:
            break
    alerted = {str(alert["id"]) for alert in alerts}
    expected, missed, known_before = [], [], []
    for key, item in entered.items():
        if profile.alert(item) is None:
            continue  # the rules say: no alert for this one (company, other city, agent…)
        expected.append(key)
        if key in alerted:
            continue
        if key in seen_before:
            known_before.append(key)  # an old listing raised to the top: not new by design
        else:
            missed.append({"id": key, "title": item.get("title"), "url": avito_client.main.BASE_URL + str(item.get("urlPath", "")).split("?")[0]})
    for entry in missed:
        print(f"  ✗ пропущено: {entry['title']} {entry['url']}")

    lags = [c["lag_min"] for c in checked if c["lag_min"] is not None and not c["problems"]]
    garbage = [c for c in checked if c["problems"]]
    report = {
        "search": search.name, "start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds"),
        "alerts": len(alerts), "garbage": len(garbage), "entered_during_test": len(entered),
        "expected_alerts": len(expected), "missed": len(missed), "raised_old_listings": len(known_before),
        "lag_minutes_median": statistics.median(lags) if lags else None, "lag_minutes_max": max(lags) if lags else None,
        "checked": checked, "missed_list": missed,
    }
    path = avito.DATA_DIR.parent / f"acceptance-{search.name}-{start:%Y%m%d-%H%M}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== Итог")
    print(f"  Уведомлений: {len(alerts)}; мусора: {len(garbage)}")
    print(f"  Появилось за время теста и подходит под правила: {len(expected)}; пропущено: {len(missed)}"
          f"; старых поднятых (не новые, без уведомления): {len(known_before)}")
    if lags:
        print(f"  Задержка от публикации до уведомления: медиана {statistics.median(lags)} мин, максимум {max(lags)} мин")
    print(f"  Отчёт: {path}")


if __name__ == "__main__":
    main()
