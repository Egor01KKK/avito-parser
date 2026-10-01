#!/usr/bin/env python3
"""Interactive menu over avito.py: paste a link, pick actions by number.

    uv run python avito.py       (no command opens this menu)

Ctrl+C during a long action (collecting, watching) returns to the menu.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import avito
import avito_client
import telegram_notify

try:
    import questionary
except ImportError:  # the numbered fallback below still works
    questionary = None

LINE = "─" * 44
BACK = -1


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise SystemExit(0) from None


def confirm(question: str) -> bool:
    if interactive():
        return bool(questionary.confirm(question, default=False).ask())
    return ask(f"{question} (да/нет) ").lower() in ("да", "y", "yes", "д")


def interactive() -> bool:
    """Arrow-key menus need a real terminal; piped input gets numbers."""
    return questionary is not None and sys.stdin.isatty() and sys.stdout.isatty()


def choose(title: str, options: list[str], *, back: str = "Назад") -> int | None:
    """Pick an option with arrows and Enter (or by number); None means back."""
    if interactive():
        # questionary turns value=None into the title, so "back" needs its own value.
        choices = [questionary.Choice(option, value=index) for index, option in enumerate(options)]
        choices.append(questionary.Choice(back, value=BACK))
        answer = questionary.select(
            title or "Что делаем?", choices=choices, instruction="(стрелки ↑↓, Enter)"
        ).ask()
        return None if answer in (BACK, None) else answer  # None after Ctrl+C too
    if title:
        print(f"\n{title}")
    for number, option in enumerate(options, start=1):
        print(f"  {number}. {option}")
    print(f"  0. {back}")
    while True:
        answer = ask("> ")
        if answer == "0" or answer == "":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print("  Введите номер из списка.")


def run(func, **kwargs) -> bool:
    """Call an avito.py command; failures and Ctrl+C return to the menu."""
    try:
        func(argparse.Namespace(**kwargs))
        return True
    except SystemExit as exc:
        return exc.code in (0, None)
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return False


def scan(name: str) -> bool:
    return run(avito.cmd_scan, name=name, full=False, max_pages=avito.MAX_PAGES,
               delay=avito_client.DEFAULT_DELAY_SECONDS)


def watch(names: list[str]) -> None:
    print("Слежу за новыми объявлениями. Вернуться в меню: Ctrl+C")
    run(avito.cmd_watch, names=names, interval=180, delay=avito_client.DEFAULT_DELAY_SECONDS,
        no_telegram=False)


def open_file(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError:
        print(f"Откройте файл вручную: {path}")


def table(name: str) -> None:
    search = avito.load_search(name)
    path = avito.write_table(search, search.load("seen.json"))
    if path is None:
        print("В базе пока пусто: сначала соберите объявления.")
    else:
        open_file(path)


def searches() -> list[avito.Search]:
    return [avito.load_search(path.stem) for path in sorted(avito.SEARCHES_DIR.glob("*.json"))]


def new_search() -> None:
    print("\nОткройте Авито в браузере, выставьте город, категорию и фильтры,")
    print("скопируйте ссылку из адресной строки и вставьте сюда.")
    url = ask("Ссылка: ").strip('"\' ')
    if not url:
        return
    before = {path.stem for path in avito.SEARCHES_DIR.glob("*.json")}
    print("Проверяю ссылку на Авито, это займёт около минуты…")
    ok = run(avito.cmd_add, url=url, name=None, profile=None, city=None, watch_pages=2,
             dry_run=False, replace=False, delay=5.0)
    added = sorted({path.stem for path in avito.SEARCHES_DIR.glob("*.json")} - before)
    if not ok or not added:
        print("Поиск не добавлен. Проверьте, что это ссылка на страницу поиска, а не на одно объявление.")
        return
    name = added[0]
    choice = choose("Что дальше?", [
        "Собрать все объявления и следить за новыми",
        "Только собрать (таблица Excel)",
        "Только следить за новыми",
    ], back="В меню")
    if choice == 0 and scan(name):
        watch([name])
    elif choice == 1 and scan(name):
        table(name)
    elif choice == 2:
        watch([name])


def my_searches() -> None:
    while True:
        items = searches()
        if not items:
            print("\nПоисков пока нет: начните с «Новый поиск по ссылке».")
            return
        labels = []
        for search in items:
            stored = len(search.load("listings.json"))
            labels.append(f"{search.title}  ({stored} объявл.)")
        picked = choose("Мои поиски:", labels)
        if picked is None:
            return
        search = items[picked]
        action = choose(f"«{search.title}»", [
            "Следить за новыми",
            "Собрать / досбор объявлений",
            "Открыть таблицу Excel",
            "Удалить поиск",
        ])
        if action == 0:
            watch([search.name])
        elif action == 1:
            scan(search.name)
        elif action == 2:
            table(search.name)
        elif action == 3:
            if confirm(f"Удалить «{search.title}»? Собранные данные останутся на диске."):
                (avito.SEARCHES_DIR / f"{search.name}.json").unlink()
                print("Удалено.")


def watch_several() -> None:
    items = searches()
    if not items:
        print("\nПоисков пока нет.")
        return
    if interactive():
        names = questionary.checkbox(
            "За какими поисками следить?",
            choices=[questionary.Choice(search.title, value=search.name) for search in items],
            instruction="(пробел — отметить, Enter — начать)",
        ).ask() or []
        if names:
            watch(names)
        return
    print("\nЗа какими поисками следить? Номера через запятую или «все».")
    for number, search in enumerate(items, start=1):
        print(f"  {number}. {search.title}")
    answer = ask("> ").lower()
    if answer in ("все", "all", "*"):
        names = [search.name for search in items]
    else:
        numbers = [part.strip() for part in answer.split(",")]
        names = [items[int(n) - 1].name for n in numbers if n.isdigit() and 1 <= int(n) <= len(items)]
    if names:
        watch(names)


def telegram_menu() -> None:
    connected = telegram_notify.Notifier.from_settings() is not None
    action = choose(
        f"Telegram: {'подключён ✓' if connected else 'не подключён'}",
        ["Подключить своего бота" if not connected else "Подключить заново", "Отправить тестовое сообщение"],
    )
    if action == 0:
        run(avito.cmd_telegram, action="setup", new_token=connected)
    elif action == 1:
        run(avito.cmd_telegram, action="test", new_token=False)


def main() -> None:
    """The menu loop; Ctrl+C outside an action quits quietly."""
    try:
        loop()
    except KeyboardInterrupt:
        print("\nВыход.")


def loop() -> None:
    avito.JSON_OUTPUT = False
    while True:
        try:
            telegram = "подключён ✓" if telegram_notify.Notifier.from_settings() else "не подключён"
        except telegram_notify.TelegramError:
            telegram = "ошибка настроек"
        print(f"\n{LINE}\n  АВИТО-ПОИСКИ\n{LINE}")
        choice = choose("", [
            "Новый поиск по ссылке",
            f"Мои поиски ({len(searches())})",
            "Следить за несколькими поисками сразу",
            f"Telegram: {telegram}",
        ], back="Выход")
        if choice is None:
            return
        try:
            [new_search, my_searches, watch_several, telegram_menu][choice]()
        except KeyboardInterrupt:
            print("\nВозврат в меню.")


if __name__ == "__main__":
    main()
