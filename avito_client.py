"""Calm Avito catalog client shared by every search.

Pace and recovery follow what was measured on a home IP: ~30 s between
requests, PoW (HTTP 439) and QRATOR passed as usual, and a fresh session
after a captcha instead of solving it. A fresh session that is refused on its
very first request means the whole IP is blocked; the client then waits.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode

from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError,
    IncompleteRead,
    Timeout as CurlTimeout,
)

import main

DEFAULT_DELAY_SECONDS = 30.0
IP_BLOCK_WAIT_SECONDS = 600
MAX_POW_IN_A_ROW = 3
MAX_SESSION_RESETS_PER_PAGE = 3
FULL_PAGE_SIZE = 50

TRANSPORT_ERRORS = (IncompleteRead, CurlConnectionError, CurlTimeout)


@dataclass(frozen=True)
class Page:
    number: int
    items: tuple[dict[str, Any], ...]
    total_count: int
    items_on_page: int

    @property
    def is_last(self) -> bool:
        # itemsOnPage is the number on *this* page, so a short page is the last.
        return (
            not self.items
            or self.items_on_page < FULL_PAGE_SIZE
            or self.number * FULL_PAGE_SIZE >= self.total_count
        )


def items_url(params: dict[str, str], page: int) -> str:
    query = [(key, str(value)) for key, value in params.items()]
    query += [("p", str(page)), ("updateListOnly", "true")]
    return f"{main.ITEMS_URL}?{urlencode(query)}"


def response_kind(response: Any) -> str:
    status = response.status_code
    if status == 200:
        return "ok"
    if status == 439 or (status == 429 and main.response_has_pow_challenge(response)):
        return "pow"
    if main.is_firewall_captcha_dispatcher_response(response) or (
        status == 429 and main.is_geetest_firewall_html(response.text)
    ):
        return "captcha"
    return f"refused({status})"


class CalmClient:
    """One Avito session at a time, replaced whenever a captcha appears."""

    def __init__(
        self,
        *,
        delay: float = DEFAULT_DELAY_SECONDS,
        log: Callable[..., None] | None = None,
    ) -> None:
        for variable in main.PROXY_ENVIRONMENT_VARIABLES:
            os.environ.pop(variable, None)
        self.delay = delay
        self.log = log or (lambda kind, **fields: None)
        self.session: Any | None = None
        self.referer: str | None = None
        self.last_request = 0.0
        self.sessions_opened = 0

    def _pause(self) -> None:
        wait = self.delay * random.uniform(0.7, 1.3) - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, *, document: bool) -> tuple[str, Any]:
        page_headers = {**main.REQUEST_HEADERS, "Referer": self.referer or main.REFERER}
        headers = main.DOCUMENT_REQUEST_HEADERS if document else page_headers
        for _ in range(MAX_POW_IN_A_ROW + 1):
            self._pause()
            response = main.get_with_qrator_recovery(
                self.session,
                url,
                session_headers=headers,
                request_headers=None if document else main.ITEMS_REQUEST_HEADERS,
                timeout_seconds=main.DOCUMENT_REQUEST_TIMEOUT_SECONDS,
                context="calm-client",
                stream_response_body=True,
            )
            self.last_request = time.monotonic()
            kind = response_kind(response)
            if kind != "pow":
                return kind, response
            main.run_pow_verification(self.session, response)
        return "pow-loop", None

    def _open_session(self, catalog_url: str) -> None:
        """Open sessions until one loads the catalog page, waiting out IP blocks."""
        while True:
            self.session = main.requests.Session(
                impersonate=main.HTTP_IMPERSONATE_PROFILE,
                trust_env=False,
            )
            main.set_session_headers(self.session, {**main.REQUEST_HEADERS, "Referer": catalog_url})
            self.referer = catalog_url
            kind, _ = self._get(catalog_url, document=True)
            if kind == "ok":
                self.sessions_opened += 1
                return
            self.session = None
            self.log("IP-BLOCK", reason=kind, wait_min=IP_BLOCK_WAIT_SECONDS // 60)
            time.sleep(IP_BLOCK_WAIT_SECONDS)

    def get_json(self, url: str, *, catalog_url: str, context: str = "request") -> dict[str, Any]:
        """GET a same-origin JSON endpoint, replacing the session after captchas."""
        for _ in range(MAX_SESSION_RESETS_PER_PAGE + 1):
            if self.session is None:
                self._open_session(catalog_url)
            # Switching searches keeps the session; only the page we "came
            # from" changes, as when a person opens another search tab.
            self.referer = catalog_url
            try:
                kind, response = self._get(url, document=False)
            except TRANSPORT_ERRORS as exc:
                self.log("TRANSPORT", context=context, error=type(exc).__name__)
                self.session = None
                continue
            if kind == "ok":
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError(f"{context}: answer is not a JSON object")
                return payload
            self.log("SESSION-RESET", reason=kind, context=context)
            self.session = None
        raise RuntimeError(f"{context}: no answer after {MAX_SESSION_RESETS_PER_PAGE} new sessions")

    def page_payload(self, *, catalog_url: str, params: dict[str, str], number: int) -> dict[str, Any]:
        """The full items answer: listings, counters, filters, category tree."""
        return self.get_json(
            items_url(params, number), catalog_url=catalog_url, context=f"page {number}"
        )

    def page(self, *, catalog_url: str, params: dict[str, str], number: int) -> Page:
        """Fetch one catalog page of listings."""
        payload = self.page_payload(catalog_url=catalog_url, params=params, number=number)
        return page_from_payload(payload, number)


def page_from_payload(payload: dict[str, Any], number: int) -> Page:
    catalog = payload.get("catalog") if isinstance(payload.get("catalog"), dict) else {}
    items = catalog.get("items") if isinstance(catalog.get("items"), list) else []

    def counter(name: str) -> int:
        value = payload.get(name)
        return value if type(value) is int else 0

    return Page(
        number=number,
        items=tuple(
            item for item in items
            if isinstance(item, dict) and item.get("type", "item") == "item"
        ),
        total_count=counter("totalCount"),
        items_on_page=counter("itemsOnPage"),
    )
