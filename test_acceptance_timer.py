import time

import acceptance_test


def test_watch_is_stopped_by_a_cross_platform_timer(monkeypatch):
    """No SIGALRM: the timer interrupts the main thread like Ctrl+C."""
    def endless_watch(args):
        try:
            while True:
                time.sleep(0.01)
        except KeyboardInterrupt:
            return

    monkeypatch.setattr(acceptance_test.avito, "cmd_watch", endless_watch)
    search = acceptance_test.avito.Search(
        name="x", title="x", profile="goods", catalog_url="https://www.avito.ru/kazan/telefony", params={},
    )
    started = time.monotonic()

    acceptance_test.run_watch(search, minutes=0.5 / 60, interval=60)

    assert time.monotonic() - started < 5
