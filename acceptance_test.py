#!/usr/bin/env python3
"""Live acceptance test: do watch alerts bring only fresh listings, and all of them?

1. Runs the real ``watch`` for one search for N minutes (alerts go to
   Telegram as usual).
2. Garbage check: opens every alerted listing's own page on Avito and reads
   its publication date there ("29 сентября в 07:21"), an independent source.
   An alert is garbage if the listing was published more than fresh_hours
   before the alert, or breaks the search's seller/city rules.
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
import re
import signal
import statistics
from datetime import datetime, timedelta
from typing import Any

import avito
import avito_client
import goods
import profiles

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
ITEM_DATE = re.compile(r'data-marker="item-view/item-date"[^>]*>(.*?)</span>', re.S)
MAX_CATALOG_PAGES = 6


def parse_item_date(text: str, now: datetime) -> datetime | None:
    """ "сегодня в 17:05", "вчера в 09:12", "29 сентября в 07:21", "3 мая 2025 в 10:00"."""
    text = re.sub(r"<[^>]+>|·", " ", text).replace("\xa0", " ").strip().lower()
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


def publication_date(client: avito_client.CalmClient, url: str, catalog_url: str) -> tuple[datetime | None, str]:
    if client.session is None:
        client._open_session(catalog_url)
    kind, response = client._get(url, document=True)
    if kind != "ok":
        client.session = None
        return None, f"страница не открылась ({kind})"
    match = ITEM_DATE.search(response.text)
    if not match:
        return None, "дата на странице не найдена (объявление снято?)"
    return parse_item_date(match.group(1), datetime.now()), re.sub(r"<[^>]+>", "", match.group(1)).strip(" ·")


def run_watch(search: avito.Search, minutes: float, interval: float) -> tuple[datetime, datetime]:
    def stop(*_: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGALRM, stop)
    signal.alarm(int(minutes * 60))
    start = datetime.now()
    avito.cmd_watch(argparse.Namespace(
        names=[search.name], interval=interval,
        delay=avito_client.DEFAULT_DELAY_SECONDS, no_telegram=False,
    ))
    signal.alarm(0)
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
        published, raw = publication_date(client, alert["url"], search.catalog_url)
        found = datetime.fromisoformat(alert["found"])
        lag_min = round((found - published).total_seconds() / 60, 1) if published else None
        problems = []
        if published is None:
            problems.append(raw)
        elif lag_min > search.fresh_hours * 60:
            problems.append(f"старое: опубликовано {raw}, за {lag_min / 60:.1f} ч до уведомления")
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
