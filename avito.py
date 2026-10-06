#!/usr/bin/env python3
"""Avito searches: add by link, collect everything, watch, send to Telegram.

    uv run python avito.py                                 # меню: выбор цифрами
    uv run python avito.py add "<ссылка на поиск Авито>"   # завести поиск по ссылке
    uv run python avito.py list                            # какие поиски есть
    uv run python avito.py scan <поиск>                    # собрать все страницы
    uv run python avito.py watch <поиск> [<поиск> ...]     # следить за новыми
    uv run python avito.py table <поиск>                   # таблица Excel
    uv run python avito.py telegram setup                  # подключить свой бот
    uv run python avito.py telegram test

A search is one JSON file in ``searches/`` (its name is the file name). Every
search keeps its data in ``data/searches/<name>/`` and writes tables to
``output/``. ``--json`` (before the command) prints one JSON object per line
instead of text, for scripts and AI agents.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import apartments
import avito_client
import item_page
import link_resolver
import profiles
import telegram_notify

BASE_DIR = Path(__file__).resolve().parent
SEARCHES_DIR = BASE_DIR / "searches"
DATA_DIR = BASE_DIR / "data" / "searches"
LOCATIONS_CACHE = BASE_DIR / "data" / "locations.json"
OUTPUT_DIR = BASE_DIR / "output"
MAX_PAGES = 100
HEARTBEAT_EVERY_CYCLES = 20
MARKET_REFRESH_EVERY_CYCLES = 20
# A listing alerts only if it entered the catalog this recently. Older ones
# that surface on the first pages (paid promotion, reshuffles, the 1 500 cap)
# are remembered silently. Avito moderation can hold a new listing back for
# most of an hour (47 min seen), so the window is wider than that; old
# re-published listings are caught by their view counter instead.
DEFAULT_FRESH_HOURS = 3.0
# After a full scan only the top of the catalog changes: new listings come
# first, and 50 per page leave plenty of room for promoted ones.
DEFAULT_WATCH_PAGES = 1
DATE_SORT = {"s": "104"}
# Seconds between watch cycles, chosen in the menu as "speed".
WATCH_SPEEDS = (
    ("Быстро — раз в минуту (рекомендую)", 60),
    ("Спокойно — раз в 3 минуты", 180),
    ("Максимум — раз в 20 секунд (упирается в потолок ~120 запросов в час)", 20),
)

load_json = apartments.load_seen
save_json = apartments.save_json
JSON_OUTPUT = False


def _alert_text(f: dict[str, Any]) -> str:
    head = f"🔔 [{f.get('search')}] {f.get('title')} — {f.get('price')}"
    if f.get("listed_at"):
        head += f" · в выдаче с {f['listed_at']}"
    if f.get("vs_market"):
        head += f" (рынок {f.get('market')}, {f['vs_market']})"
    lines = [head]
    if f.get("address") or f.get("metro"):
        lines.append(f"   {f.get('address', '')}" + (f" · {f['metro']}" if f.get("metro") else ""))
    if f.get("reasons"):
        lines.append(f"   {f.get('verdict')}: {'; '.join(f['reasons'])}")
    if f.get("notes"):
        lines.append(f"   ⚠ {'; '.join(f['notes'])}")
    if f.get("views"):
        lines.append(f"   👁 просмотров: {f['views']}")
    lines.append(f"   {f.get('url')}")
    if f.get("draft"):
        lines.append(f"   ✉ {f['draft']}")
    return "\n".join(lines)


HUMAN = {
    "SCAN": lambda f: f"▶ Сбор «{f['search']}» со страницы {f['start_page']}",
    "PAGE": lambda f: f"  стр. {f['page']}: {f['items']} объявлений, новых {f['new']} (по поиску всего {f['total']})",
    "TABLE": lambda f: f"✓ Таблица: {f['path']}\n  " + ", ".join(
        f"{k}: {v}" for k, v in f.items() if k not in ("time", "search", "path")),
    "REPUBLISHED": lambda f: f"  · пропущено переопубликованное старое: {f['title']} {f['price']} "
                             f"({f['total_views']} просмотров, сегодня {f['today_views']})",
    "PRIMED": lambda f: f"  ✓ «{f['search']}»: текущая выдача запомнена ({f['remembered']} новых для базы). "
                        f"Дальше присылаю только то, что появится после запуска.",
    "WATCH": lambda f: f"👀 Слежу за: {', '.join(f['searches'])}. Цикл примерно раз в {f['interval_s']:.0f} с. "
                       f"Telegram: {'да' if f['telegram'] else 'нет'}. Остановить: Ctrl+C",
    "ALERT": _alert_text,
    "SESSION-RESET": lambda f: "  · Авито показал капчу, открываю новую сессию",
    "IP-BLOCK": lambda f: f"  ⏸ Авито ограничил доступ с этого IP, жду {f['wait_min']} мин и пробую снова",
    "TRANSPORT": lambda f: "  · обрыв связи, повторяю",
    "HEARTBEAT": lambda f: f"  · работаю: цикл {f['cycle']}, сессий открыто {f['sessions']}",
    "TELEGRAM-ERROR": lambda f: f"  ✗ Telegram: {f['error']}",
    "ERROR": lambda f: f"✗ Ошибка ({f.get('search', '')}): {f['error']}",
}


def say(kind: str, **fields: Any) -> None:
    """Report an event: readable text, or one JSON line with ``--json``."""
    stamp = datetime.now().strftime("%H:%M:%S")
    if JSON_OUTPUT:
        print(json.dumps({"event": kind, "time": stamp, **fields}, ensure_ascii=False), flush=True)
        return
    render = HUMAN.get(kind)
    text = render(fields) if render else f"{kind} {json.dumps(fields, ensure_ascii=False)}"
    print(f"{stamp} {text}", flush=True)


def fail(message: str) -> None:
    if JSON_OUTPUT:
        print(json.dumps({"event": "FATAL", "error": message}, ensure_ascii=False), flush=True)
    else:
        print(f"✗ {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


@dataclass
class Search:
    name: str
    title: str
    profile: str
    catalog_url: str
    params: dict[str, str]
    watch_pages: int = DEFAULT_WATCH_PAGES
    fresh_hours: float = DEFAULT_FRESH_HOURS
    alert_sellers: str = "private"
    new_only: bool = True

    @property
    def dir(self) -> Path:
        return DATA_DIR / self.name

    def path(self, file: str) -> Path:
        return self.dir / file

    @property
    def request_params(self) -> dict[str, str]:
        """Search parameters with the newest listings first.

        Avito's default order puts paid promotion on top and scatters fresh
        listings over the page; "по дате" (s=104) lists them newest first and
        moves promotion to the end. Measured on page 1 on 2026-10-01: default
        order had fresh (<6 h) listings at positions 25-50, s=104 at 1-9.
        """
        return {**self.params, **DATE_SORT}

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
        known = ", ".join(sorted(p.stem for p in SEARCHES_DIR.glob("*.json"))) or "пока ни одного"
        fail(f"Нет поиска «{name}». Есть: {known}. Новый: avito.py add \"<ссылка>\"")
    if config.get("profile") not in profiles.PROFILES:
        fail(f"{path}: неизвестный profile «{config.get('profile')}»")
    return Search(
        name=name,
        title=config.get("title", name),
        profile=config["profile"],
        catalog_url=config["catalog_url"],
        params={key: str(value) for key, value in config["params"].items()},
        watch_pages=int(config.get("watch_pages", DEFAULT_WATCH_PAGES)),
        fresh_hours=float(config.get("fresh_hours", DEFAULT_FRESH_HOURS)),
        alert_sellers=str(config.get("alert_sellers", "private")),
        new_only=bool(config.get("new_only", True)),
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


def check_newly_created(client: avito_client.CalmClient, search: Search, fields: dict[str, Any]) -> bool:
    """Open the listing's page: a re-published old listing has views from earlier days.

    Adds the views to ``fields``; returns False for a re-published listing.
    If the page cannot be read the alert still goes out, marked unchecked.
    """
    page = item_page.fetch(client, fields["url"], catalog_url=search.catalog_url)
    verdict = item_page.is_newly_created(page) if page else None
    if verdict is None:
        fields["notes"] = [*fields.get("notes", []), "не удалось проверить, новое ли объявление"]
        return True
    fields["views"] = f"{page.total_views} (+{page.today_views} сегодня)"
    if not verdict:
        say("REPUBLISHED", search=search.name, title=fields.get("title"), price=fields.get("price"),
            total_views=page.total_views, today_views=page.today_views, url=fields["url"])
        return False
    return True


def listed_since(item: dict[str, Any]) -> datetime | None:
    """When the listing entered the catalog order (Avito's sortTimeStamp)."""
    stamp = item.get("sortTimeStamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        return datetime.fromtimestamp(stamp / 1000)
    return None


def is_fresh(listed_at: datetime | None, hours: float, now: datetime | None = None) -> bool:
    if listed_at is None:
        return True  # nothing to judge by: better one extra alert than a lost one
    return ((now or datetime.now()) - listed_at).total_seconds() <= hours * 3600


def suggested_name(url: str) -> str:
    """A file-friendly name such as "iphone_15-kazan" or "2-komnatnye-sankt-peterburg"."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    city = segments[0] if segments else "avito"
    tail = segments[-1].split("-ASg")[0] if len(segments) > 1 else "vse"
    tail = re.sub(r"-+$", "", tail) or (segments[1] if len(segments) > 1 else "vse")
    name = re.sub(r"[^a-z0-9_-]+", "-", f"{tail}-{city}".lower()).strip("-")
    candidate, index = name, 2
    while (SEARCHES_DIR / f"{candidate}.json").exists():
        candidate, index = f"{name}-{index}", index + 1
    return candidate


def cmd_add(args: argparse.Namespace) -> None:
    """Turn an Avito search link into a saved search after checking it on Avito."""
    name = args.name or suggested_name(args.url)
    if not re.fullmatch(r"[a-z0-9_-]+", name):
        fail("имя поиска: только латиница, цифры, «-» и «_»")
    if (SEARCHES_DIR / f"{name}.json").exists() and not args.replace:
        fail(f"поиск «{name}» уже есть; другое имя: --name, перезаписать: --replace")
    cache = load_json(LOCATIONS_CACHE)
    client = avito_client.CalmClient(delay=args.delay, log=say)
    try:
        resolved = link_resolver.resolve(
            client, args.url, locations_cache=cache, city=args.city, profile=args.profile
        )
    except link_resolver.LinkError as exc:
        fail(str(exc))
    except (RuntimeError, ValueError, *avito_client.TRANSPORT_ERRORS) as exc:
        fail(f"Авито не ответил: {exc}")
    save_json(LOCATIONS_CACHE, cache)
    summary = {
        "name": name, "title": resolved.title, "profile": resolved.profile, "city": resolved.city,
        "total": resolved.total, "filters": resolved.filters, "samples": resolved.samples,
        "warnings": resolved.warnings, "saved": not args.dry_run,
    }
    if not args.dry_run:
        SEARCHES_DIR.mkdir(parents=True, exist_ok=True)
        (SEARCHES_DIR / f"{name}.json").write_text(
            json.dumps(resolved.config(args.watch_pages), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if JSON_OUTPUT:
        print(json.dumps({"event": "ADDED", **summary}, ensure_ascii=False), flush=True)
        return
    print(f"\n{'Сохранён' if not args.dry_run else 'Проверен (не сохранён)'} поиск «{name}»: {resolved.title}")
    print(f"  Город: {resolved.city} · тип: {resolved.profile} · объявлений сейчас: {resolved.total}"
          + (" (Авито показывает не больше 1500 на поиск)" if resolved.total >= 1500 else ""))
    print("  Фильтры: " + ("; ".join(resolved.filters) if resolved.filters else "без фильтров"))
    for title in resolved.samples:
        print(f"    · {title}")
    for warning in resolved.warnings:
        print(f"  ⚠ {warning}")
    if resolved.total == 0:
        print("  ⚠ Сейчас по этому поиску 0 объявлений. Откройте ссылку в браузере: если там объявления есть,"
              " пришлите ссылку разработчику — программа поняла её неправильно.")
    if not args.dry_run:
        print(f"\nДальше:\n  uv run python avito.py scan {name}\n  uv run python avito.py watch {name}")


def cmd_list(_: argparse.Namespace) -> None:
    rows = []
    for path in sorted(SEARCHES_DIR.glob("*.json")):
        search = load_search(path.stem)
        state = search.load("state.json")
        rows.append({
            "name": search.name, "profile": search.profile, "title": search.title,
            "stored": len(search.load("listings.json")),
            "complete": bool(state.get("complete")), "next_page": state.get("next_page", 1),
        })
    if JSON_OUTPUT:
        for row in rows:
            print(json.dumps({"event": "SEARCH", **row}, ensure_ascii=False))
        return
    if not rows:
        print("Поисков пока нет. Добавить: uv run python avito.py add \"<ссылка на поиск Авито>\"")
    for row in rows:
        progress = "собран целиком" if row["complete"] else f"следующая страница {row['next_page']}"
        print(f"{row['name']:28} {row['profile']:13} объявлений: {row['stored']:5}  {progress}  — {row['title']}")


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
            page = client.page(catalog_url=search.catalog_url, params=search.request_params, number=number)
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
    except KeyboardInterrupt:
        say("ERROR", search=search.name, page=number, error="остановлено; следующий scan продолжит с этой страницы")
    write_table(search, seen_before)


def cmd_watch(args: argparse.Namespace) -> None:
    """Read the first pages of every search in turn; alert on unseen listings."""
    searches = [load_search(name) for name in args.names]
    notifier = None
    if not args.no_telegram:
        try:
            notifier = telegram_notify.Notifier.from_settings()
        except telegram_notify.TelegramError as exc:
            say("TELEGRAM-ERROR", error=str(exc))
    client = avito_client.CalmClient(delay=args.delay, log=say)
    watched = []
    primed: set[str] = set()
    for search in searches:
        base = search.load("listings.json")
        watched.append((search, base, search.load("seen.json"),
                        profiles.PROFILES[search.profile](base, search.home_city, search.alert_sellers)))
        search.dir.mkdir(parents=True, exist_ok=True)
    say("WATCH", searches=[s.name for s in searches], interval_s=args.interval, telegram=notifier is not None)
    cycle = 0
    try:
        while True:
            cycle += 1
            for search, base, seen, profile in watched:
                try:
                    changed = False
                    # The first pass only remembers what is already listed:
                    # alerts are for listings that appear after watch starts.
                    first_pass = search.name not in primed
                    remembered = 0
                    for number in range(1, search.watch_pages + 1):
                        page = client.page(catalog_url=search.catalog_url, params=search.request_params, number=number)
                        stamp = datetime.now().isoformat(timespec="seconds")
                        for item in page.items:
                            key = str(item["id"])
                            if key in seen:
                                continue
                            seen[key] = stamp
                            base[key] = item
                            changed = True
                            if first_pass:
                                remembered += 1
                                continue
                            listed_at = listed_since(item)
                            if not is_fresh(listed_at, search.fresh_hours):
                                continue
                            fields = profile.alert(item)
                            if fields is None:
                                continue
                            fields = {"search": search.name, **fields}
                            if search.new_only and not check_newly_created(client, search, fields):
                                continue
                            if listed_at is not None:
                                fields["listed_at"] = listed_at.strftime("%d.%m %H:%M")
                            with search.path("alerts.jsonl").open("a", encoding="utf-8") as handle:
                                handle.write(json.dumps({"found": stamp, **fields}, ensure_ascii=False) + "\n")
                            say("ALERT", **fields)
                            if notifier is not None:
                                try:
                                    notifier.send(telegram_notify.message_for(search.profile, search.name, fields))
                                except telegram_notify.TelegramError as exc:
                                    say("TELEGRAM-ERROR", error=str(exc))
                    if changed:
                        search.save("listings.json", base)
                        search.save("seen.json", seen)
                    if first_pass:
                        primed.add(search.name)
                        say("PRIMED", search=search.name, remembered=remembered)
                except (RuntimeError, ValueError, *avito_client.TRANSPORT_ERRORS) as exc:
                    say("ERROR", search=search.name, error=f"{type(exc).__name__}: {exc}"[:300])
                if cycle % MARKET_REFRESH_EVERY_CYCLES == 0 and hasattr(profile, "refresh_market"):
                    profile.refresh_market()
            if cycle % HEARTBEAT_EVERY_CYCLES == 0:
                say("HEARTBEAT", cycle=cycle, sessions=client.sessions_opened)
            time.sleep(args.interval * random.uniform(0.8, 1.2))
    except KeyboardInterrupt:
        if not JSON_OUTPUT:
            print("\nОстановлено. Новые объявления сохранены; следующий watch продолжит с этого места.")


def cmd_table(args: argparse.Namespace) -> None:
    search = load_search(args.name)
    if write_table(search, search.load("seen.json")) is None:
        fail("в базе этого поиска пока нет объявлений: сначала scan")


def cmd_telegram(args: argparse.Namespace) -> None:
    try:
        settings = telegram_notify.load_settings()
        if args.action == "setup":
            token = settings.get("token")
            if not token or args.new_token:
                print("Создайте бота у @BotFather в Telegram (команда /newbot) и вставьте его токен.")
                token = getpass.getpass("Токен бота (ввод не отображается): ").strip()
            if not token:
                fail("токен не введён")
            bot = telegram_notify.call(token, "getMe")
            print(f"Бот: @{bot.get('username')}. Напишите ему /start в Telegram, затем нажмите Enter.")
            input()
            chat_id, chat_name = telegram_notify.find_chat_id(token)
            telegram_notify.save_settings({"token": token, "chat_id": chat_id})
            telegram_notify.Notifier(token, chat_id).send("✅ Авито-поиски подключены. Новые объявления будут приходить сюда.")
            print(f"✓ Готово: сообщения пойдут в чат «{chat_name}». Настройки: {telegram_notify.SETTINGS_PATH}")
            return
        notifier = telegram_notify.Notifier.from_settings()
        if notifier is None:
            fail("Telegram не подключён: uv run python avito.py telegram setup")
        notifier.send("🔔 Проверка: уведомления Авито-поисков работают.")
        print("✓ Тестовое сообщение отправлено")
    except telegram_notify.TelegramError as exc:
        fail(str(exc))


def use_utf8_output() -> None:
    """Print Cyrillic and emoji anywhere.

    On Windows, output that goes to a file or another program (an agent
    reading --json, for one) uses the locale code page (cp1251), where "🔔"
    cannot be written and print() raises. UTF-8 works everywhere.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main_cli(argv: list[str] | None = None) -> None:
    global JSON_OUTPUT
    use_utf8_output()
    if not (sys.argv[1:] if argv is None else argv):
        import menu  # no command: the interactive menu

        menu.main()
        return
    parser = argparse.ArgumentParser(
        prog="avito.py",
        description="Поиски на Авито: добавить по ссылке, собрать, следить, таблицы, Telegram",
    )
    parser.add_argument("--json", action="store_true", help="вывод по строке JSON на событие (для скриптов и агентов)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="команда")

    add = sub.add_parser("add", help="добавить поиск по ссылке с Авито")
    add.add_argument("url", help="ссылка на страницу поиска Авито с выставленными фильтрами")
    add.add_argument("--name", help="имя поиска (латиница); по умолчанию из ссылки")
    add.add_argument("--profile", choices=sorted(profiles.PROFILES), help="тип обработки; по умолчанию по категории")
    add.add_argument("--city", help="город по-русски, если не определился из ссылки")
    add.add_argument("--watch-pages", type=int, default=DEFAULT_WATCH_PAGES, help="сколько первых страниц смотреть в watch")
    add.add_argument("--dry-run", action="store_true", help="только проверить ссылку, не сохранять")
    add.add_argument("--replace", action="store_true", help="перезаписать поиск с тем же именем")
    add.add_argument("--delay", type=float, default=5.0, help="секунд между запросами при проверке")
    add.set_defaults(func=cmd_add)

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
    watch.add_argument("--no-telegram", action="store_true", help="не отправлять в Telegram")
    watch.set_defaults(func=cmd_watch)

    table = sub.add_parser("table", help="пересобрать таблицу Excel из базы")
    table.add_argument("name")
    table.set_defaults(func=cmd_table)

    telegram = sub.add_parser("telegram", help="подключить Telegram или проверить его")
    telegram.add_argument("action", choices=("setup", "test"))
    telegram.add_argument("--new-token", action="store_true", help="ввести токен заново")
    telegram.set_defaults(func=cmd_telegram)

    args = parser.parse_args(argv)
    JSON_OUTPUT = args.json
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    sys.exit(main_cli())
