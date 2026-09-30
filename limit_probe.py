#!/usr/bin/env python3
"""Measure how many catalog requests Avito allows before a captcha.

Local experiment, not part of the parser. PoW (HTTP 439) and QRATOR are
passed as usual; on a captcha the script stops at once and never tries to
solve it, so failed attempts cannot prolong the block.

    uv run python limit_probe.py                    # full plan (hours)
    uv run python limit_probe.py burst --delay 2    # one series only
    uv run python limit_probe.py cooldown --every 300
    uv run python limit_probe.py rotate --delay 30 --hours 2

Every request is appended to data/limit-probe-<start>.jsonl.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError,
    IncompleteRead,
    Timeout as CurlTimeout,
)

import main

LOGGER = logging.getLogger("limit_probe")
DATA_DIR = Path(__file__).resolve().parent / "data"
CATALOG_PAGES = 21
MAX_POW_IN_A_ROW = 3


class Recorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()

    def write(self, **event: Any) -> None:
        event = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": round(time.monotonic() - self.started, 1),
            **event,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(
            f"{event['time'][11:]}  "
            + "  ".join(f"{k}={v}" for k, v in event.items() if k not in ("time", "elapsed_s")),
            flush=True,
        )


def classify(response: Any) -> str:
    status = response.status_code
    if status == 200:
        return "ok"
    if status == 439 or (status == 429 and main.response_has_pow_challenge(response)):
        return "pow"
    if main.is_firewall_captcha_dispatcher_response(response):
        return "captcha"
    if status == 429 and main.is_geetest_firewall_html(response.text):
        return "captcha"
    if status in (403, 429):
        return f"blocked({main.forbidden_response_reason(response)[:80]})"
    return f"other({status})"


def new_session() -> Any:
    session = main.requests.Session(
        impersonate=main.HTTP_IMPERSONATE_PROFILE,
        trust_env=False,
    )
    main.set_session_headers(session, {**main.REQUEST_HEADERS, "Referer": main.REFERER})
    return session


def fetch(session: Any, url: str, *, document: bool, context: str) -> Any:
    headers = main.DOCUMENT_REQUEST_HEADERS if document else main.PAGE_REQUEST_HEADERS
    return main.get_with_qrator_recovery(
        session,
        url,
        session_headers=headers,
        request_headers=None if document else main.ITEMS_REQUEST_HEADERS,
        timeout_seconds=main.DOCUMENT_REQUEST_TIMEOUT_SECONDS,
        context=context,
        stream_response_body=True,
    )


def request_with_pow(
    session: Any, url: str, *, document: bool, recorder: Recorder, phase: str, n: int, page: int | None
) -> str:
    """One logical request; PoW is solved and the same URL repeated."""
    for pow_round in range(MAX_POW_IN_A_ROW + 1):
        try:
            response = fetch(session, url, document=document, context=f"{phase} #{n}")
        except (IncompleteRead, CurlConnectionError, CurlTimeout) as exc:
            recorder.write(phase=phase, n=n, page=page, kind=f"transport({type(exc).__name__})")
            return "transport"
        kind = classify(response)
        items = None
        if kind == "ok" and not document:
            items = len(main.parse_catalog_listings(response))
        recorder.write(
            phase=phase, n=n, page=page if not document else "document",
            status=response.status_code, kind=kind, items=items,
        )
        if kind != "pow":
            return kind
        try:
            main.run_pow_verification(session, response)
        except (RuntimeError, ValueError) as exc:
            recorder.write(phase=phase, n=n, kind=f"pow-failed({exc})")
            return "pow-failed"
    return "pow-loop"


def burst(
    recorder: Recorder,
    *,
    delay: float,
    max_requests: int,
    phase: str | None = None,
    deadline: float | None = None,
) -> int | None:
    """Request catalog pages until a captcha; return how many succeeded.

    ``None`` means the very first request of the fresh session was refused.
    """
    phase = phase or f"burst-{delay:g}s"
    session = new_session()
    ok = 0
    kind = request_with_pow(
        session, main.CATALOG_PAGE_URL, document=True,
        recorder=recorder, phase=phase, n=0, page=None,
    )
    if kind != "ok":
        recorder.write(phase=phase, result="stopped-before-start", last=kind)
        return None
    for n in range(1, max_requests + 1):
        if deadline is not None and time.monotonic() + delay > deadline:
            recorder.write(phase=phase, result="time-up", ok=ok)
            return ok
        time.sleep(delay)
        page = (n - 1) % CATALOG_PAGES + 1
        kind = request_with_pow(
            session, main.page_url(page), document=False,
            recorder=recorder, phase=phase, n=n, page=page,
        )
        if kind == "ok":
            ok += 1
            continue
        if kind == "transport":
            continue
        recorder.write(phase=phase, result="limit", ok_before_stop=ok, stopped_by=kind)
        return ok
    recorder.write(phase=phase, result="no-limit-reached", ok=ok)
    return ok


def cooldown(recorder: Recorder, *, every: float, max_hours: float) -> float | None:
    """Probe with one catalog request every ``every`` seconds until it passes."""
    phase = "cooldown"
    started = time.monotonic()
    n = 0
    while time.monotonic() - started < max_hours * 3600:
        n += 1
        kind = request_with_pow(
            new_session(), main.page_url(1), document=False,
            recorder=recorder, phase=phase, n=n, page=1,
        )
        if kind == "ok":
            minutes = (time.monotonic() - started) / 60
            recorder.write(phase=phase, result="unblocked", after_min=round(minutes, 1), probes=n)
            return minutes
        time.sleep(every)
    recorder.write(phase=phase, result="still-blocked", probes=n)
    return None


def rotate(recorder: Recorder, *, delay: float, hours: float) -> None:
    """Steady pace for ``hours``; a new session after every captcha."""
    started = time.monotonic()
    deadline = started + hours * 3600
    sessions: list[dict[str, Any]] = []
    ip_blocks: list[float] = []
    while time.monotonic() < deadline:
        number = len(sessions) + 1
        session_started = time.monotonic()
        ok = burst(
            recorder, delay=delay, max_requests=10**6,
            phase=f"rotate-s{number}", deadline=deadline,
        )
        if ok is None:
            recorder.write(phase="rotate", event="ip-blocked", session=number)
            remaining = (deadline - time.monotonic()) / 3600
            minutes = cooldown(recorder, every=300, max_hours=max(remaining, 0.1))
            if minutes is None:
                break
            ip_blocks.append(minutes)
            continue
        sessions.append({
            "session": number,
            "ok": ok,
            "minutes": round((time.monotonic() - session_started) / 60, 1),
        })
        recorder.write(phase="rotate", event="session-ended", **sessions[-1])
        time.sleep(delay)
    total_ok = sum(entry["ok"] for entry in sessions)
    hours_run = (time.monotonic() - started) / 3600
    recorder.write(
        phase="rotate", result="summary",
        sessions=len(sessions), total_ok_pages=total_ok,
        pages_per_hour=round(total_ok / hours_run) if hours_run else None,
        ok_per_session=[entry["ok"] for entry in sessions],
        ip_blocks=len(ip_blocks), ip_block_minutes=[round(m) for m in ip_blocks],
        hours=round(hours_run, 2),
    )


def main_cli() -> None:
    parser = argparse.ArgumentParser(description="Avito request limit experiment")
    sub = parser.add_subparsers(dest="mode")
    b = sub.add_parser("burst")
    b.add_argument("--delay", type=float, default=2.0)
    b.add_argument("--max", type=int, default=80)
    c = sub.add_parser("cooldown")
    c.add_argument("--every", type=float, default=300)
    c.add_argument("--max-hours", type=float, default=3)
    r = sub.add_parser("rotate")
    r.add_argument("--delay", type=float, default=30.0)
    r.add_argument("--hours", type=float, default=2.0)
    args = parser.parse_args()

    for variable in main.PROXY_ENVIRONMENT_VARIABLES:
        os.environ.pop(variable, None)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    recorder = Recorder(DATA_DIR / f"limit-probe-{datetime.now():%Y%m%d-%H%M}.jsonl")
    print(f"log: {recorder.path}", flush=True)

    if args.mode == "burst":
        burst(recorder, delay=args.delay, max_requests=args.max)
        return
    if args.mode == "rotate":
        rotate(recorder, delay=args.delay, hours=args.hours)
        return
    if args.mode == "cooldown":
        cooldown(recorder, every=args.every, max_hours=args.max_hours)
        return

    # Full plan: fast series, cooldown, slow series, cooldown.
    for delay in (2.0, 30.0):
        first = cooldown(recorder, every=300, max_hours=3)
        if first is None:
            return
        burst(recorder, delay=delay, max_requests=80)
    cooldown(recorder, every=300, max_hours=3)
    recorder.write(phase="plan", result="done")


if __name__ == "__main__":
    main_cli()
