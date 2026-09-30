#!/usr/bin/env python3
"""Run the Avito items XHR with PoW, Qrator, and GeeTest recovery."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlsplit

from curl_cffi import requests
from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError,
    IncompleteRead,
    Timeout as CurlTimeout,
)

from firewallpow_payload_reference import build_get_payload, build_verify_payload
from generate_qrator_variants import (
    DEFAULT_BASE_F,
    DEFAULT_BASE_S,
    generate_variants,
    load_f_base,
    load_s_base,
)

BASE_URL = "https://www.avito.ru"
GET_URL = f"{BASE_URL}/web/3/firewallPow/get"
VERIFY_URL = f"{BASE_URL}/web/3/firewallPow/verify"
FIREWALL_CAPTCHA_GET_URL = f"{BASE_URL}/web/5/firewallCaptcha/get"
FIREWALL_CAPTCHA_VERIFY_URL = f"{BASE_URL}/web/3/firewallCaptcha/verify"
GEETEST_LOAD_URL = "https://gcaptcha4.geevisit.com/load"
GEETEST_BASE_URL = "https://gcaptcha4.geevisit.com"
GEETEST_CAPTCHA_ID = "2d9c743cf7d63dbc9db578a608196bcd"
QRATOR_FT_URL = f"{BASE_URL}/web/2/ft"
QRATOR_PIXEL_URL = f"{BASE_URL}/web/1/u"
QRATOR_FAVICON_URL = f"{BASE_URL}/favicon.ico"
# Search: Saint Petersburg, flats for sale, 2 rooms, "Частные" sellers.
# The slug and ``f`` value are the canonical catalog URL that Avito itself
# returns for exactly these filters.
CATALOG_PAGE_URL = (
    f"{BASE_URL}/sankt-peterburg/kvartiry/prodam/2-komnatnye-ASgBAgICAkSSA8YQygiCWQ"
    "?f=ASgBAgICA0SSA8YQygiCWZC~DZauNQ"
)
REFERER = CATALOG_PAGE_URL
ITEMS_URL = f"{BASE_URL}/web/1/js/items"
SAINT_PETERSBURG_LOCATION_ID = "653240"
FLATS_CATEGORY_ID = "24"
ITEMS_QUERY_PARAMETERS = (
    ("categoryId", FLATS_CATEGORY_ID),
    ("locationId", SAINT_PETERSBURG_LOCATION_ID),
    ("cd", "0"),
    ("p", "1"),
    # "Купить" / sale listings.
    ("params[201]", "1059"),
    # "Количество комнат": 2 комнаты.
    ("params[549][0]", "5697"),
    # "Продавцы": Частные (agencies and developers are excluded by Avito).
    ("params[110472][0]", "437131"),
    ("verticalCategoryId", "1"),
    ("rootCategoryId", "4"),
    ("localPriority", "0"),
    ("updateListOnly", "true"),
    ("features[imageAspectRatio]", "1:1"),
    ("features[noPlaceholders]", "true"),
    ("features[justSpa]", "true"),
    ("features[responsive]", "true"),
    ("features[useReload]", "true"),
    ("features[stickyCatalogFilters]", "false"),
    ("features[adsInMapTest][step7_3]", "false"),
    ("features[adsInMapTest][step5]", "false"),
    ("features[adsInMapTest][step7]", "false"),
    ("features[mapButtonSlimTest]", "false"),
    ("features[listVip]", "false"),
    ("features[newDoublesUxTest]", "false"),
    ("features[newDoublesUxRealtyTest]", "false"),
    ("features[newDoublesMapRealtyTest]", "false"),
    ("features[simpleCounters]", "true"),
    ("features[isRatingExperiment]", "true"),
    ("features[isContactsButtonRedesigned]", "false"),
    ("features[desktopPublishFromSerpTest]", "false"),
    ("features[desktopPinPositionVrTop]", "false"),
    ("features[desktopHideContextPositionOnReject]", "false"),
    ("features[desktopShowBigContextPositions]", "false"),
    ("features[desktopSpaInFilters]", "false"),
    ("features[isReMapPreviewAb]", "false"),
    ("features[isReItemNewViewAb]", "false"),
    ("features[isReNewSortAb]", "false"),
    ("features[isReItemXlAb]", "false"),
    ("features[isSplitAdvertBlock]", "false"),
    ("features[suggestParams][categoryID]", FLATS_CATEGORY_ID),
    ("features[suggestParams][locationID]", SAINT_PETERSBURG_LOCATION_ID),
    ("features[suggestParams][presentationType]", "serp"),
    ("features[isShowWithPhotoFilter]", "false"),
    ("features[reverseVisualRubricator]", "false"),
    ("features[isReInterestingHouseAb]", "false"),
    ("features[jobsConsentDisclaimer]", "false"),
    ("features[altViewedBadgeDesktopAb]", "false"),
    ("features[isHideRecommendationsInfinite]", "false"),
    ("features[ivaItemRedesign]", "true"),
    ("features[shouldSendRreLayoutEvents]", "false"),
    ("features[isRedesignZhkSerp]", "false"),
    ("features[isHotelsSnippetRedesign]", "false"),
)
PAGES_TO_REQUEST = 100
# This is only a final loop-safety cap. GeeTest has its own stricter limit
# below; the larger value allows intervening PoW/QRATOR branches without
# preventing all five consecutive GeeTest attempts from running.
MAX_PROTECTION_TRANSITIONS_PER_PAGE = 25
MAX_CONSECUTIVE_GEETEST_FAILURES = 5
MAX_QRATOR_RETRIES_PER_REQUEST = 2
# A calm pace between catalog pages; the whole search is only ~20 pages.
PAGE_REQUEST_DELAY_SECONDS = 2.0
QRATOR_PRE_FT_DELAY_SECONDS = 1.0
QRATOR_POST_PIXEL_DELAY_SECONDS = 2.0
TRANSPORT_RETRY_DELAY_SECONDS = 1.0
MAX_TRANSIENT_GET_RETRIES = 2
REQUEST_TIMEOUT_SECONDS = 5
DOCUMENT_REQUEST_TIMEOUT_SECONDS = 10
HTTP_IMPERSONATE_PROFILE = "firefox147"
LOGGER = logging.getLogger(__name__)
LOG_BODY_PREVIEW_LENGTH = 4_000
LOG_HEADERS_PREVIEW_LENGTH = 2_000
DEBUG_RESPONSE_DIR = Path("firewall-debug-responses")
GEEKED_TEST_ROOT = Path(__file__).resolve().parent / "GeekedTest"
PROXY_ENVIRONMENT_VARIABLES = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


def firefox_user_agent(platform_name: str | None = None) -> str:
    """Return a Firefox 152 UA whose OS agrees with the current TCP stack."""
    platform_name = platform_name or sys.platform
    if platform_name.startswith("win"):
        platform_token = "Windows NT 10.0; Win64; x64"
    elif platform_name == "darwin":
        platform_token = "Macintosh; Intel Mac OS X 10.15"
    else:
        platform_token = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform_token}; rv:152.0) "
        "Gecko/20100101 Firefox/152.0"
    )


BROWSER_USER_AGENT = firefox_user_agent()

# Headers sent by the browser XHR in the source HAR.  Content-Type is supplied
# by ``json=...`` and Cookie is managed by the session's cookie jar.
REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Origin": BASE_URL,
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-GPC": "1",
    "TE": "trailers",
    "User-Agent": BROWSER_USER_AGENT,
}

# Headers of the main same-origin items XHR. Protection service requests
# temporarily replace them and the exact set is restored before every retry.
PAGE_REQUEST_HEADERS = {
    **REQUEST_HEADERS,
    "Referer": CATALOG_PAGE_URL,
}

DOCUMENT_REQUEST_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "Priority": "u=0, i",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-GPC": "1",
    "Upgrade-Insecure-Requests": "1",
    "TE": "trailers",
    "User-Agent": BROWSER_USER_AGENT,
}

ITEMS_REQUEST_HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "X-Source": "client-browser",
}

QRATOR_FT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Origin": BASE_URL,
    "Referer": PAGE_REQUEST_HEADERS["Referer"],
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-GPC": "1",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "TE": "trailers",
    "User-Agent": PAGE_REQUEST_HEADERS["User-Agent"],
}

QRATOR_PIXEL_HEADERS = {
    "Accept": (
        "image/avif,image/webp,image/png,image/svg+xml,"
        "image/*;q=0.8,*/*;q=0.5"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Referer": PAGE_REQUEST_HEADERS["Referer"],
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-GPC": "1",
    "Priority": "u=5, i",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "TE": "trailers",
    "User-Agent": PAGE_REQUEST_HEADERS["User-Agent"],
}

QRATOR_SCRIPT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Referer": PAGE_REQUEST_HEADERS["Referer"],
    "Sec-Fetch-Dest": "script",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-GPC": "1",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "TE": "trailers",
    "User-Agent": PAGE_REQUEST_HEADERS["User-Agent"],
}

QRATOR_FAVICON_HEADERS = {
    "Accept": (
        "image/avif,image/webp,image/png,image/svg+xml,"
        "image/*;q=0.8,*/*;q=0.5"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Referer": PAGE_REQUEST_HEADERS["Referer"],
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-GPC": "1",
    "Priority": "u=6",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "TE": "trailers",
    "User-Agent": PAGE_REQUEST_HEADERS["User-Agent"],
}

CAPTCHA_VERIFY_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": PAGE_REQUEST_HEADERS["Referer"],
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-GPC": "1",
    "Priority": "u=4",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "TE": "trailers",
    "User-Agent": PAGE_REQUEST_HEADERS["User-Agent"],
}

GEETEST_REQUEST_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Referer": f"{BASE_URL}/",
    "Sec-Fetch-Storage-Access": "none",
    "Sec-Fetch-Dest": "script",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
    "Sec-GPC": "1",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "TE": "trailers",
    "User-Agent": PAGE_REQUEST_HEADERS["User-Agent"],
}


@dataclass(frozen=True)
class GeeTestLoad:
    """The initial GeeTest task returned after an HTTP 429 firewall page."""

    captcha_id: str
    challenge: str
    callback: str
    lot_number: str | None
    captcha_type: str | None
    data: dict[str, Any]


@dataclass(frozen=True)
class GeeTestVerified:
    """A GeeTest solution accepted by Avito's firewallCaptcha endpoint."""

    lot_number: str


class GeeTestSolveFailed(RuntimeError):
    """A generated solution was explicitly rejected by GeeTest."""

    def __init__(
        self,
        *,
        captcha_type: str | None,
        lot_number: str | None,
        result: str | None,
        fail_count: int | None,
    ):
        self.captcha_type = captcha_type
        self.lot_number = lot_number
        self.result = result
        self.fail_count = fail_count
        super().__init__(
            "GeeTest rejected solver payload; "
            f"type={captcha_type or 'unknown'}; "
            f"lot_number={lot_number or 'unknown'}; "
            f"result={result or 'unknown'}; "
            f"fail_count={fail_count if fail_count is not None else 'unknown'}"
        )


@dataclass(frozen=True)
class ItemsPageStats:
    """Top-level counters returned by a successful items API response."""

    count: int
    total_count: int
    total_elements: int
    main_count: int
    items_on_page: int
    items_hash: str | None


@dataclass(frozen=True)
class PageRequestResult:
    """HTTP result from one page request made after a successful PoW/no-op."""

    page: int
    status_code: int
    redirect_location: str | None = None
    stats: ItemsPageStats | None = None
    items: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CompletedFlow:
    """A completed no-firewall or PoW flow plus the following page requests."""

    pow_unblock_ttl: int | None
    page_requests: tuple[PageRequestResult, ...]
    verification_chain: tuple[str, ...] = ()


def format_verification_chain(chain: list[str] | tuple[str, ...]) -> str:
    """Format the ordered protection path for human-readable logs."""
    return " -> ".join(chain) if chain else "none"


def parse_items_page_stats(response: Any) -> ItemsPageStats | None:
    """Parse the five top-level counters from a successful items response."""
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        LOGGER.warning("items response HTTP 200 is not valid JSON")
        return None
    if not isinstance(payload, dict):
        LOGGER.warning("items response HTTP 200 JSON is not an object")
        return None
    field_names = (
        "count",
        "totalCount",
        "totalElements",
        "mainCount",
        "itemsOnPage",
    )
    values = {name: payload.get(name) for name in field_names}
    invalid = [
        name for name, value in values.items() if type(value) is not int
    ]
    if invalid:
        LOGGER.warning(
            "items response is missing integer counters: %s",
            ", ".join(invalid),
        )
        return None
    catalog = payload.get("catalog")
    items = catalog.get("items") if isinstance(catalog, dict) else None
    items_hash = hash_catalog_items(items) if isinstance(items, list) else None
    if items_hash is None:
        LOGGER.warning("items response has no catalog.items array")
    return ItemsPageStats(
        count=values["count"],
        total_count=values["totalCount"],
        total_elements=values["totalElements"],
        main_count=values["mainCount"],
        items_on_page=values["itemsOnPage"],
        items_hash=items_hash,
    )


def parse_catalog_listings(response: Any) -> tuple[dict[str, Any], ...]:
    """Return the listing objects (``type == "item"``) of an items response."""
    if response.status_code != 200:
        return ()
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return ()
    catalog = payload.get("catalog") if isinstance(payload, dict) else None
    items = catalog.get("items") if isinstance(catalog, dict) else None
    if not isinstance(items, list):
        return ()
    return tuple(
        item
        for item in items
        if isinstance(item, dict) and item.get("type", "item") == "item"
    )


def is_last_catalog_page(page: int, stats: ItemsPageStats) -> bool:
    """Whether ``page`` already covers every listing Avito reports."""
    if stats.items_on_page <= 0:
        return True
    return page * stats.items_on_page >= stats.total_count


def hash_catalog_items(items: list[Any]) -> str:
    """Return a fast deterministic BLAKE2b-128 hash of the complete array."""
    canonical = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(canonical, digest_size=16).hexdigest()


def response_json(response: Any, *, endpoint: str) -> dict[str, Any]:
    """Raise a useful error for a non-JSON or unsuccessful endpoint response."""
    response.raise_for_status()
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{endpoint}: response is not JSON") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"{endpoint}: JSON response must be an object")
    return body


def challenge_jwt_from_get(body: dict[str, Any]) -> str:
    """Return ``success.result.challenge_jwt`` from ``firewallPow/get``."""
    try:
        challenge_jwt = body["success"]["result"]["challenge_jwt"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("firewallPow/get: no success.result.challenge_jwt") from exc
    if not isinstance(challenge_jwt, str) or not challenge_jwt:
        raise RuntimeError("firewallPow/get: challenge_jwt must be a non-empty string")
    return challenge_jwt


def verified_from_response(body: dict[str, Any]) -> tuple[bool, int | None]:
    """Read the verification status without accepting a truthy non-boolean."""
    try:
        result = body["success"]["result"]
        verified = result["verified"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("firewallPow/verify: no success.result.verified") from exc
    if verified is not True:
        return False, None
    unblock_ttl = result.get("unblock_ttl")
    return True, unblock_ttl if isinstance(unblock_ttl, int) else None


def pow_challenge_from_response(response: Any) -> str:
    """Read the fresh challenge issued by the captured HTTP 439 response."""
    try:
        pow_challenge = response.json()["pow_challenge"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "challenge source: no pow_challenge in JSON response"
        ) from exc
    if not isinstance(pow_challenge, str) or not pow_challenge:
        raise RuntimeError("challenge source: pow_challenge must be a non-empty string")
    return pow_challenge


def response_has_pow_challenge(response: Any) -> bool:
    """Return whether a protection response selects the firewallPow branch."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("pow_challenge"), str)
        and bool(payload["pow_challenge"])
    )


def is_firewall_captcha_dispatcher_response(response: Any) -> bool:
    """Recognize the JSON instruction to open Avito's generic captcha flow."""
    if response.status_code not in (403, 429):
        return False
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    details = payload.get("too-many-requests")
    if not isinstance(details, dict):
        return False
    link = details.get("link")
    show_captcha = response.headers.get(
        "x-firewall-show-captcha", ""
    ).strip().lower()
    return (
        show_captcha == "true"
        and isinstance(link, str)
        and link.endswith("/firewall/captcha/show")
    )


def geetest_captcha_id_from_html(html: str) -> str:
    """Extract the GeeTest id embedded in the HTTP 429 firewall HTML."""
    match = re.search(r"const\s+captchaId\s*=\s*['\"]([^'\"]+)['\"]", html)
    if not match:
        raise RuntimeError("HTTP 429 firewall page: no GeeTest captchaId in HTML")
    return match.group(1)


def is_geetest_firewall_html(html: str) -> bool:
    """Return whether a firewall HTML page explicitly selects GeeTest.

    The 429 page can contain markup for several captcha providers.  The
    additional HAR selects GeeTest only when its dedicated container, loader,
    initializer, and dynamically inserted captcha id are all present.
    """
    return (
        re.search(r'id\s*=\s*["\']geetest_captcha["\']', html, re.IGNORECASE)
        is not None
        and re.search(r"initGeetest4\s*\(", html) is not None
        and re.search(
            r'(?:src\s*=\s*["\'])https://www\.avito\.st/s/captcha/gt4\.js',
            html,
            re.IGNORECASE,
        )
        is not None
        and re.search(r"const\s+captchaId\s*=\s*['\"][^'\"]+['\"]", html) is not None
    )


def parse_jsonp(body: str, *, callback: str) -> dict[str, Any]:
    """Parse the single callback invocation returned by GeeTest ``/load``."""
    match = re.fullmatch(rf"\s*{re.escape(callback)}\((.*)\)\s*;?\s*", body, re.DOTALL)
    if not match:
        raise RuntimeError("GeeTest /load: unexpected JSONP response")
    try:
        result = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError("GeeTest /load: invalid JSONP payload") from exc
    if not isinstance(result, dict):
        raise RuntimeError("GeeTest /load: response must contain an object")
    return result


def fetch_firewall_captcha(
    session: requests.Session,
    *,
    referer: str,
) -> dict[str, Any]:
    """Ask Avito which concrete captcha variant the dispatcher selected."""
    set_session_headers(
        session,
        {
            **CAPTCHA_VERIFY_HEADERS,
            "Referer": referer,
        },
    )
    request_cookies = session_cookie_snapshot(session)
    response = session.post(
        FIREWALL_CAPTCHA_GET_URL,
        json={"refreshInternalCaptcha": False},
        timeout=REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    save_http_exchange(
        session,
        response,
        context="firewallCaptcha-get",
        method="POST",
        url=FIREWALL_CAPTCHA_GET_URL,
        request_headers=dict(session.headers),
        request_cookies=request_cookies,
        json_body={"refreshInternalCaptcha": False},
    )
    if response.status_code != 200:
        saved_path = save_response_body(
            response,
            context="firewallCaptcha-get",
        )
        raise RuntimeError(
            "firewallCaptcha/get: expected HTTP 200, received "
            f"HTTP {response.status_code}; body saved to {saved_path}"
        )
    body = response_json(response, endpoint="firewallCaptcha/get")
    try:
        captcha = body["success"]["result"]["captcha"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "firewallCaptcha/get: no success.result.captcha"
        ) from exc
    if not isinstance(captcha, dict):
        raise RuntimeError("firewallCaptcha/get: captcha must be an object")
    if isinstance(captcha.get("type"), str):
        selected = captcha
    else:
        selected = next(
            (
                value
                for key in ("internalCaptcha", "hCaptcha", "geeTest")
                if isinstance((value := captcha.get(key)), dict)
                and isinstance(value.get("type"), str)
            ),
            None,
        )
    if selected is None:
        raise RuntimeError(
            "firewallCaptcha/get: no supported captcha variant in response"
        )
    LOGGER.info(
        "firewallCaptcha/get selected type=%s",
        selected["type"],
    )
    return selected


def load_geetest_task(
    session: requests.Session,
    *,
    captcha_id: str,
) -> GeeTestLoad:
    """Load a fresh GeeTest task for Avito's bundle-level captcha id."""
    challenge = str(uuid.uuid4())
    callback = f"geetest_{int(time.time() * 1000) + secrets.randbelow(10_000)}"
    set_session_headers(session, GEETEST_REQUEST_HEADERS)
    request_cookies = session_cookie_snapshot(session)
    load_response = session.get(
        GEETEST_LOAD_URL,
        params={
            "callback": callback,
            "captcha_id": captcha_id,
            "challenge": challenge,
            "client_type": "web",
            "lang": "rus",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    save_http_exchange(
        session,
        load_response,
        context="GeeTest-load",
        method="GET",
        url=GEETEST_LOAD_URL,
        request_headers=dict(session.headers),
        request_cookies=request_cookies,
        params={
            "callback": callback,
            "captcha_id": captcha_id,
            "challenge": challenge,
            "client_type": "web",
            "lang": "rus",
        },
    )
    load_response.raise_for_status()
    load_body = parse_jsonp(load_response.text, callback=callback)
    if load_body.get("status") != "success" or not isinstance(
        load_body.get("data"), dict
    ):
        raise RuntimeError("GeeTest /load: server did not return a successful task")
    data = load_body["data"]
    return GeeTestLoad(
        captcha_id=captcha_id,
        challenge=challenge,
        callback=callback,
        lot_number=data.get("lot_number")
        if isinstance(data.get("lot_number"), str)
        else None,
        captcha_type=data.get("captcha_type")
        if isinstance(data.get("captcha_type"), str)
        else None,
        data=data,
    )


def start_geetest(
    session: requests.Session,
    firewall_html: str,
    *,
    referer: str,
) -> GeeTestLoad:
    """Perform the 429 branch through the GeeTest initial ``/load`` request.

    The page gives the captcha id.  The captured ``gt4.js`` generates the UUID
    challenge and JSONP callback client-side, which is reproduced here.
    """
    if not is_geetest_firewall_html(firewall_html):
        raise RuntimeError("HTTP 429 firewall page does not select the GeeTest branch")
    captcha_id = geetest_captcha_id_from_html(firewall_html)
    captcha = fetch_firewall_captcha(session, referer=referer)
    captcha_type = captcha["type"]
    if captcha_type != "geeTest":
        raise RuntimeError(
            f"firewallCaptcha/get: unsupported captcha type {captcha_type!r}"
        )
    return load_geetest_task(session, captcha_id=captcha_id)


def solve_geetest_load(
    load: GeeTestLoad,
    *,
    source_session: requests.Session,
) -> dict[str, str]:
    """Pass the existing ``/load`` data to GeekedTest's verify function."""
    root = str(GEEKED_TEST_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from geeked import CaptchaSolveRejected, Geeked
    except ImportError as exc:
        raise RuntimeError(
            "GeekedTest dependencies are missing; run `uv sync`"
        ) from exc

    solver = Geeked(
        load.captcha_id,
        lang="rus",
        session=source_session,
        request_headers=GEETEST_REQUEST_HEADERS,
        exchange_logger=lambda **exchange: save_http_exchange(
            source_session,
            **exchange,
        ),
    )
    solver.lot_number = load.lot_number
    solver.base_url = GEETEST_BASE_URL
    try:
        seccode = solver.submit_captcha(load.data)
    except CaptchaSolveRejected as exc:
        rejection = exc.response
        raise GeeTestSolveFailed(
            captcha_type=load.captcha_type,
            lot_number=load.lot_number,
            result=rejection.get("result")
            if isinstance(rejection.get("result"), str)
            else None,
            fail_count=rejection.get("fail_count")
            if type(rejection.get("fail_count")) is int
            else None,
        ) from exc

    required = (
        "captcha_id",
        "lot_number",
        "pass_token",
        "gen_time",
        "captcha_output",
    )
    if not isinstance(seccode, dict) or not set(required).issubset(seccode):
        raise RuntimeError("GeekedTest verify returned an incomplete seccode")
    result = {key: seccode[key] for key in required}
    if not all(isinstance(value, str) and value for value in result.values()):
        raise RuntimeError("GeekedTest seccode fields must be non-empty strings")
    return result


def verify_geetest_with_avito(
    session: requests.Session,
    load: GeeTestLoad,
    seccode: dict[str, str],
    *,
    referer: str,
) -> GeeTestVerified:
    """Submit the GeeTest seccode using the exact Avito bundle payload."""
    payload: dict[str, str] = {
        "captcha": "",
        "hCaptchaResponse": "",
        **seccode,
    }
    cube_result = str(10 + secrets.randbelow(90))
    set_session_headers(
        session,
        {
            **CAPTCHA_VERIFY_HEADERS,
            "Referer": referer,
            "X-Cube": cube_result,
        },
    )
    request_cookies = session_cookie_snapshot(session)
    response = session.post(
        FIREWALL_CAPTCHA_VERIFY_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    save_http_exchange(
        session,
        response,
        context="firewallCaptcha-verify",
        method="POST",
        url=FIREWALL_CAPTCHA_VERIFY_URL,
        request_headers=dict(session.headers),
        request_cookies=request_cookies,
        json_body=payload,
    )
    body = response_json(response, endpoint="firewallCaptcha/verify")
    try:
        verified = body["success"]["result"]["verified"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "firewallCaptcha/verify: no success.result.verified"
        ) from exc
    if verified is not True:
        saved_path = save_response_body(
            response,
            context="GeeTest-firewallCaptcha-verify",
        )
        LOGGER.warning(
            "GeeTest seccode was accepted by GeeTest but rejected by Avito; "
            "type=%s; lot_number=%s; body saved to %s",
            load.captcha_type or "unknown",
            load.lot_number or "unknown",
            saved_path,
        )
        raise GeeTestSolveFailed(
            captcha_type=load.captcha_type,
            lot_number=load.lot_number,
            result="avito_verified_false",
            fail_count=None,
        )
    lot_number = seccode["lot_number"]
    LOGGER.info(
        "GeeTest verification succeeded; lot_number=%s; X-Cube=%s",
        lot_number,
        cube_result,
    )
    return GeeTestVerified(lot_number=lot_number)


def complete_geetest_verification(
    session: requests.Session,
    load: GeeTestLoad,
    *,
    referer: str,
) -> GeeTestVerified:
    """Solve an already loaded task and submit it to Avito."""
    LOGGER.info(
        "GeeTest task loaded; captcha_id=%s; lot_number=%s; type=%s",
        load.captcha_id,
        load.lot_number,
        load.captcha_type,
    )
    LOGGER.info("GeeTest: generating and submitting local solver payload")
    seccode = solve_geetest_load(load, source_session=session)
    LOGGER.info("GeeTest /verify accepted the solver payload; verifying with Avito")
    return verify_geetest_with_avito(
        session,
        load,
        seccode,
        referer=referer,
    )


def run_geetest_verification(
    session: requests.Session,
    firewall_html: str,
    *,
    referer: str,
) -> GeeTestVerified:
    """Load, solve, and submit a GeeTest branch selected by firewall HTML."""
    load = start_geetest(session, firewall_html, referer=referer)
    return complete_geetest_verification(
        session,
        load,
        referer=referer,
    )


def run_firewall_captcha_dispatcher(
    session: requests.Session,
    *,
    referer: str,
) -> GeeTestVerified:
    """Resolve JSON captcha dispatcher and complete its selected provider."""
    captcha = fetch_firewall_captcha(session, referer=referer)
    captcha_type = captcha["type"]
    if captcha_type != "geeTest":
        raise RuntimeError(
            "firewallCaptcha/get selected unsupported captcha type "
            f"{captcha_type!r}"
        )
    LOGGER.info(
        "captcha dispatcher: starting GeeTest with bundle captcha_id=%s",
        GEETEST_CAPTCHA_ID,
    )
    load = load_geetest_task(
        session,
        captcha_id=GEETEST_CAPTCHA_ID,
    )
    return complete_geetest_verification(
        session,
        load,
        referer=referer,
    )


def forbidden_response_reason(response: Any) -> str:
    """Return a concise human-readable reason from a Qrator 403 response."""
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            details = payload.get("too-many-requests")
            if isinstance(details, dict) and isinstance(details.get("message"), str):
                return details["message"]
    title = re.search(
        r"<title\b[^>]*>(.*?)</title>",
        response.text,
        re.IGNORECASE | re.DOTALL,
    )
    if title:
        return re.sub(r"\s+", " ", title.group(1)).strip()
    return "unclassified HTTP 403"


def log_forbidden_response(response: Any, *, context: str) -> None:
    """Log and preserve a 403 body so its protection branch can be identified."""
    reason = forbidden_response_reason(response)
    body = preview(response.text, limit=LOG_BODY_PREVIEW_LENGTH)
    saved_path = save_response_body(response, context=context)
    LOGGER.warning(
        "%s: HTTP 403 (%s); response body saved to %s",
        context,
        reason,
        saved_path,
    )
    LOGGER.debug(
        "%s: HTTP 403 (%s); headers=%s; response body preview:\n%s",
        context,
        reason,
        preview(repr(dict(response.headers)), limit=LOG_HEADERS_PREVIEW_LENGTH),
        body,
    )


def log_unrecognized_protection_response(response: Any, *, context: str) -> None:
    """Preserve an unrecognized protection response before stopping."""
    reason = forbidden_response_reason(response)
    saved_path = save_response_body(response, context=context)
    LOGGER.warning(
        "%s: HTTP %s is not a recognized PoW or GeeTest branch (%s); "
        "response body saved to %s",
        context,
        response.status_code,
        reason,
        saved_path,
    )
    LOGGER.debug(
        "%s: unrecognized HTTP %s; headers=%s; response body preview:\n%s",
        context,
        response.status_code,
        preview(repr(dict(response.headers)), limit=LOG_HEADERS_PREVIEW_LENGTH),
        preview(response.text, limit=LOG_BODY_PREVIEW_LENGTH),
    )


def log_bad_request_response(response: Any, *, context: str) -> Path:
    """Preserve an HTTP 400 response for later diagnosis and keep the loop going."""
    saved_path = save_response_body(response, context=context)
    LOGGER.warning(
        "%s: HTTP 400; response body saved to %s; continuing with the next page",
        context,
        saved_path,
    )
    LOGGER.debug(
        "%s: HTTP 400; url=%s; headers=%s; response body preview:\n%s",
        context,
        response.url,
        preview(repr(dict(response.headers)), limit=LOG_HEADERS_PREVIEW_LENGTH),
        preview(response.text, limit=LOG_BODY_PREVIEW_LENGTH),
    )
    return saved_path


def preview(value: str, *, limit: int) -> str:
    """Keep diagnostics useful without flooding the terminal with HTML."""
    return value if len(value) <= limit else f"{value[:limit]} … [truncated]"


def set_session_headers(session: requests.Session, headers: dict[str, str]) -> None:
    """Replace headers instead of mixing document and XHR request contexts."""
    session.headers.clear()
    session.headers.update(headers)


def session_cookie_names(session: requests.Session) -> tuple[str, ...]:
    """Return cookie names only, without leaking their values into diagnostics."""
    return tuple(sorted(set(session.cookies.keys())))


def session_cookie_snapshot(
    session: requests.Session,
) -> list[dict[str, Any]]:
    """Capture the complete cookie jar at one exact point in the flow."""
    return [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": bool(cookie.secure),
        }
        for cookie in session.cookies.jar
    ]


def is_qrator_challenge_response(response: Any) -> bool:
    """Recognize the Qrator HTML flow independently of 302 versus 429."""
    has_meta_refresh = re.search(
        r"<meta\b[^>]*\bhttp-equiv\s*=\s*[\"']refresh[\"']",
        response.text,
        re.IGNORECASE,
    )
    has_fingerprint_script = re.search(
        r"<script\b[^>]*\bsrc\s*=\s*[\"']/[0-9a-f]{20}\.js[\"']",
        response.text,
        re.IGNORECASE,
    )
    return (
        response.status_code in (302, 429)
        and response.headers.get("server", "").strip().upper() == "QRATOR"
        and has_meta_refresh is not None
        and has_fingerprint_script is not None
    )


def is_qrator_redirect(response: Any) -> bool:
    """Backward-compatible alias for the Qrator HTML classifier."""
    return is_qrator_challenge_response(response)


def build_qrator_multipart(
    f_value: str,
    s_value: str,
    *,
    boundary: str | None = None,
) -> tuple[str, bytes]:
    """Build the exact two-field multipart body emitted by the Qrator bundle."""
    boundary = boundary or f"{secrets.randbelow(10**16):016d}"
    if not re.fullmatch(r"\d{16}", boundary):
        raise ValueError("Qrator multipart boundary must contain 16 digits")
    body = (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="f"'
        f"\r\n\r\n{f_value}"
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="s"'
        f"\r\n\r\n{s_value}"
        f"\r\n--{boundary}--\r\n"
    ).encode("ascii")
    return f'multipart/form-data;boundary="{boundary}"', body


def qrator_ft_token(response: Any) -> str:
    """Read the JSON string which the bundle stores verbatim as cookie ``ft``."""
    response.raise_for_status()
    try:
        token = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Qrator /web/2/ft: response is not JSON") from exc
    if not isinstance(token, str) or not token:
        raise RuntimeError("Qrator /web/2/ft: response must be a non-empty string")
    return token


def qrator_pixel_id() -> int:
    """Reproduce ``floor(Math.random() * 4294967295)`` from the bundle."""
    return secrets.randbelow(0xFFFFFFFF)


def qrator_script_url(qrator_html: str, *, page_url: str) -> str:
    """Extract the same-origin fingerprint bundle referenced by the 302 HTML."""
    script_sources = re.findall(
        r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']",
        qrator_html,
        re.IGNORECASE,
    )
    for source in script_sources:
        absolute_url = urljoin(page_url, source)
        parsed = urlsplit(absolute_url)
        if (
            parsed.netloc == urlsplit(BASE_URL).netloc
            and re.fullmatch(r"/[0-9a-f]{20}\.js", parsed.path)
        ):
            return absolute_url
    raise RuntimeError("Qrator HTTP 302 page: fingerprint script URL not found")


def load_qrator_page_resources(
    session: requests.Session,
    *,
    qrator_html: str,
    page_url: str,
    context: str,
) -> None:
    """Load the script and favicon requested by Firefox before ``/web/2/ft``."""
    script_url = qrator_script_url(qrator_html, page_url=page_url)
    set_session_headers(
        session,
        {**QRATOR_SCRIPT_HEADERS, "Referer": page_url},
    )
    script_response = session.get(
        script_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if script_response.status_code != 200:
        raise RuntimeError(
            f"{context}: Qrator fingerprint script returned HTTP "
            f"{script_response.status_code}"
        )

    set_session_headers(
        session,
        {**QRATOR_FAVICON_HEADERS, "Referer": page_url},
    )
    favicon_response = session.get(
        QRATOR_FAVICON_URL,
        timeout=REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if favicon_response.status_code != 200:
        raise RuntimeError(
            f"{context}: Qrator favicon returned HTTP "
            f"{favicon_response.status_code}"
        )


def run_qrator_cookie_flow(
    session: requests.Session,
    *,
    qrator_html: str,
    page_url: str,
    context: str,
) -> None:
    """Reproduce the complete 302 HAR sequence and retain resulting cookies."""
    sequence_started = time.monotonic()
    load_qrator_page_resources(
        session,
        qrator_html=qrator_html,
        page_url=page_url,
        context=context,
    )
    remaining_delay = QRATOR_PRE_FT_DELAY_SECONDS - (
        time.monotonic() - sequence_started
    )
    if remaining_delay > 0:
        time.sleep(remaining_delay)

    variant = generate_variants(
        load_f_base(DEFAULT_BASE_F),
        load_s_base(DEFAULT_BASE_S),
        count=1,
        timestamp=int(time.time()),
    )[0]
    f_value = variant["f"]
    s_value = variant["s"]
    if not isinstance(f_value, str) or not isinstance(s_value, str):
        raise RuntimeError("Qrator payload generator returned invalid f/s values")

    session.cookies.delete("f")
    session.cookies.delete("ft")
    session.cookies.set("f", f_value, domain=".avito.ru", path="/")
    content_type, multipart_body = build_qrator_multipart(f_value, s_value)
    set_session_headers(
        session,
        {
            **QRATOR_FT_HEADERS,
            "Referer": page_url,
            "Content-Type": content_type,
        },
    )
    ft_response = session.post(
        QRATOR_FT_URL,
        data=multipart_body,
        timeout=REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if ft_response.status_code != 200:
        if ft_response.status_code == 403:
            log_forbidden_response(
                ft_response,
                context=f"{context}: Qrator /web/2/ft",
            )
        else:
            LOGGER.debug(
                "%s: Qrator /web/2/ft headers=%r; body=%s",
                context,
                dict(ft_response.headers),
                preview(ft_response.text, limit=LOG_BODY_PREVIEW_LENGTH),
            )
        raise RuntimeError(
            f"{context}: Qrator /web/2/ft returned HTTP "
            f"{ft_response.status_code}"
        )
    token = qrator_ft_token(ft_response)
    # The bundle assigns responseText rather than parsed JSON. Consequently
    # the cookie value contains the JSON string's surrounding quotes.
    session.cookies.set(
        "ft",
        json.dumps(token, ensure_ascii=False, separators=(",", ":")),
        domain=".avito.ru",
        path="/",
    )

    pixel_id = qrator_pixel_id()
    set_session_headers(
        session,
        {**QRATOR_PIXEL_HEADERS, "Referer": page_url},
    )
    pixel_response = session.get(
        f"{QRATOR_PIXEL_URL}?{pixel_id}",
        timeout=REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if pixel_response.status_code != 200:
        if pixel_response.status_code == 403:
            log_forbidden_response(
                pixel_response,
                context=f"{context}: Qrator /web/1/u",
            )
        raise RuntimeError(
            f"{context}: Qrator /web/1/u returned HTTP "
            f"{pixel_response.status_code}"
        )
    content_type = pixel_response.headers.get("content-type", "").lower()
    if content_type and "image/gif" not in content_type:
        raise RuntimeError(
            f"{context}: Qrator /web/1/u returned unexpected "
            f"Content-Type {content_type!r}"
        )
    LOGGER.info(
        "%s: Qrator cookies initialized; pixel_id=%s; ft_length=%s",
        context,
        pixel_id,
        len(token),
    )
    time.sleep(QRATOR_POST_PIXEL_DELAY_SECONDS)


def get_with_qrator_recovery(
    session: requests.Session,
    url: str,
    *,
    session_headers: dict[str, str],
    request_headers: dict[str, str] | None = None,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    context: str,
    stream_response_body: bool = False,
    verification_chain: list[str] | None = None,
) -> Any:
    """GET a URL and retry the exact request after a Qrator 302 flow."""
    for attempt in range(MAX_QRATOR_RETRIES_PER_REQUEST + 1):
        set_session_headers(session, session_headers)
        for transport_attempt in range(MAX_TRANSIENT_GET_RETRIES + 1):
            try:
                response = session.get(
                    url,
                    headers=request_headers,
                    timeout=timeout_seconds,
                    allow_redirects=False,
                    stream=stream_response_body,
                )
                if stream_response_body:
                    chunks: list[bytes] = []
                    try:
                        chunks.extend(response.iter_content())
                    except (
                        IncompleteRead,
                        CurlConnectionError,
                        CurlTimeout,
                    ) as stream_exc:
                        response.content = b"".join(chunks)
                        # curl occasionally reports a connection/trailer error
                        # after Avito's complete multi-megabyte HTML has already
                        # arrived. Keep that valid body instead of downloading
                        # it again; genuinely truncated HTML is retried below.
                        if (
                            response.status_code == 200
                            and re.search(
                                rb"</html>\s*$",
                                response.content,
                                re.IGNORECASE,
                            )
                        ):
                            LOGGER.warning(
                                "%s: curl ended with %s after a complete "
                                "HTTP 200 HTML body (%s bytes); accepting body",
                                context,
                                type(stream_exc).__name__,
                                len(response.content),
                            )
                        else:
                            if stream_exc.response is None:
                                stream_exc.response = response
                            raise
                    else:
                        response.content = b"".join(chunks)
                break
            except (IncompleteRead, CurlConnectionError, CurlTimeout) as exc:
                partial_response = exc.response
                LOGGER.debug(
                    "%s: transient transport error %s (curl code %s); "
                    "status=%s; headers=%r; received_body_bytes=%s",
                    context,
                    type(exc).__name__,
                    exc.code,
                    getattr(partial_response, "status_code", None),
                    dict(getattr(partial_response, "headers", {}) or {}),
                    len(getattr(partial_response, "content", b"") or b""),
                )
                if (
                    partial_response is not None
                    and partial_response.status_code in (403, 429, 439)
                ):
                    response = partial_response
                    break
                if transport_attempt == MAX_TRANSIENT_GET_RETRIES:
                    raise
                LOGGER.warning(
                    "%s: transient transport error %s; retrying GET "
                    "(attempt %s/%s)",
                    context,
                    type(exc).__name__,
                    transport_attempt + 2,
                    MAX_TRANSIENT_GET_RETRIES + 1,
                )
                time.sleep(TRANSPORT_RETRY_DELAY_SECONDS)
        if not is_qrator_challenge_response(response):
            return response
        log_redirect_response(response, context=context)
        if attempt == MAX_QRATOR_RETRIES_PER_REQUEST:
            raise RuntimeError(
                f"{context}: Qrator challenge remained after "
                f"{MAX_QRATOR_RETRIES_PER_REQUEST} cookie flows"
            )
        LOGGER.info(
            "%s: HTTP %s from QRATOR, starting /web/2/ft -> /web/1/u",
            context,
            response.status_code,
        )
        run_qrator_cookie_flow(
            session,
            qrator_html=response.text,
            page_url=response.url or url,
            context=context,
        )
        if verification_chain is not None:
            verification_chain.append("QRATOR")
            LOGGER.info(
                "verification chain: %s",
                format_verification_chain(verification_chain),
            )
        LOGGER.info(
            "%s: QRATOR cleared; repeating the same GET",
            context,
        )

    raise AssertionError("unreachable")


def redirect_target(response: Any) -> str | None:
    """Return the redirect destination exposed by curl_cffi, if any."""
    return (
        response.headers.get("location")
        or getattr(response, "redirect_url", None)
        or None
    )


def save_response_body(
    response: Any,
    *,
    context: str,
    suffix: str | None = None,
) -> Path:
    """Save a complete response body using a content-type-aware extension."""
    DEBUG_RESPONSE_DIR.mkdir(parents=True, exist_ok=True)
    safe_context = re.sub(r"[^a-zA-Z0-9._-]+", "-", context).strip("-")
    if suffix is None:
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            suffix = "json"
        elif "html" in content_type:
            suffix = "html"
        elif "xml" in content_type:
            suffix = "xml"
        else:
            suffix = "txt"
    filename = (
        f"{safe_context or 'response'}-http-{response.status_code}.{suffix}"
    )
    path = DEBUG_RESPONSE_DIR / filename
    content = getattr(response, "content", None)
    if not isinstance(content, bytes):
        encoding = getattr(response, "encoding", None) or "utf-8"
        content = response.text.encode(encoding, errors="replace")
    path.write_bytes(content)
    return path.resolve()


def save_http_exchange(
    session: requests.Session,
    response: Any,
    *,
    context: str,
    method: str,
    url: str,
    request_headers: dict[str, Any] | None = None,
    request_cookies: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> Path:
    """Save a complete request/response exchange for cross-machine diagnosis.

    These diagnostics intentionally contain cookie and challenge values. They
    are stored only in the gitignored debug directory and must be treated as
    sensitive session data.
    """
    DEBUG_RESPONSE_DIR.mkdir(parents=True, exist_ok=True)
    safe_context = re.sub(r"[^a-zA-Z0-9._-]+", "-", context).strip("-")
    merged_headers = dict(session.headers)
    if request_headers:
        merged_headers.update(request_headers)
    cookies_after = session_cookie_snapshot(session)
    cookies_before = (
        request_cookies
        if request_cookies is not None
        else cookies_after
    )
    response_body = getattr(response, "text", "")
    if not isinstance(response_body, str):
        response_body = str(response_body)
    exchange = {
        "context": context,
        "request": {
            "method": method.upper(),
            "url": url,
            "headers": merged_headers,
            "cookies": cookies_before,
            "params": params,
            "json": json_body,
        },
        "response": {
            "status": response.status_code,
            "url": str(getattr(response, "url", "") or ""),
            "headers": dict(response.headers),
            "body": response_body,
            "session_cookies_after": cookies_after,
        },
    }
    filename = (
        f"exchange-{time.time_ns()}-"
        f"{safe_context or 'http'}.json"
    )
    path = DEBUG_RESPONSE_DIR / filename
    path.write_text(
        json.dumps(exchange, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    resolved = path.resolve()
    LOGGER.info("%s: full HTTP diagnostic saved to %s", context, resolved)
    return resolved


def save_html_response(response: Any, *, context: str) -> Path:
    """Save the complete HTML response under a filesystem-safe name."""
    return save_response_body(response, context=context, suffix="html")


def log_redirect_response(response: Any, *, context: str) -> None:
    """Preserve an HTML Qrator challenge or opaque redirect without Location."""
    if redirect_target(response):
        return
    body = preview(response.text, limit=LOG_BODY_PREVIEW_LENGTH)
    meta_refresh = bool(
        re.search(r'<meta\s+http-equiv=["\']refresh["\']', response.text, re.IGNORECASE)
    )
    kind = "QRATOR meta-refresh" if meta_refresh and response.headers.get("server") == "QRATOR" else "no Location"
    html_path = save_html_response(response, context=context)
    LOGGER.warning(
        "%s: HTTP %s (%s); HTML saved to %s",
        context,
        response.status_code,
        kind,
        html_path,
    )
    LOGGER.debug(
        "%s: HTTP %s without Location; url=%s; redirect_url=%r; headers=%r; body follows:\n%s",
        context,
        response.status_code,
        response.url,
        getattr(response, "redirect_url", None),
        preview(repr(dict(response.headers)), limit=LOG_HEADERS_PREVIEW_LENGTH),
        body,
    )


def run_pow_verification(session: requests.Session, response: Any) -> int | None:
    """Complete ``firewallPow/get -> verify`` using an HTTP 439 response."""
    set_session_headers(session, {**REQUEST_HEADERS, "Referer": REFERER})
    pow_challenge = pow_challenge_from_response(response)
    get_response = session.post(
        GET_URL,
        json=build_get_payload(pow_challenge),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    challenge_jwt = challenge_jwt_from_get(
        response_json(get_response, endpoint="firewallPow/get")
    )
    LOGGER.info("firewallPow/get succeeded; computing verify payload")
    verify_payload = build_verify_payload(challenge_jwt)
    LOGGER.info("firewallPow payload computed; submitting verify")
    verify_response = session.post(
        VERIFY_URL,
        json=verify_payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    verified, unblock_ttl = verified_from_response(
        response_json(verify_response, endpoint="firewallPow/verify")
    )
    if not verified:
        raise RuntimeError("firewallPow/verify: server returned verified=false")
    LOGGER.debug(
        "firewallPow/verify cookie names: %s",
        session_cookie_names(session),
    )
    LOGGER.info(
        "firewallPow verification succeeded; unblock_ttl=%s",
        unblock_ttl,
    )
    return unblock_ttl


def handle_firewall_response(
    session: requests.Session, response: Any, *, context: str
) -> int | GeeTestVerified | None:
    """Classify and handle the protection response from any request.

    HTTP 439 is the JSON PoW branch; HTTP 429 is considered GeeTest only after
    its response HTML passes the dedicated GeeTest classifier.
    """
    if response.status_code == 200:
        return None
    if is_firewall_captcha_dispatcher_response(response):
        saved_path = save_response_body(response, context=context)
        LOGGER.info(
            "%s: HTTP %s requests captcha dispatcher; body saved to %s",
            context,
            response.status_code,
            saved_path,
        )
        return run_firewall_captcha_dispatcher(
            session,
            referer=PAGE_REQUEST_HEADERS["Referer"],
        )
    if response.status_code == 403:
        log_forbidden_response(response, context=context)
        raise RuntimeError(
            f"{context}: HTTP 403 ({forbidden_response_reason(response)})"
        )
    if response.status_code == 439:
        LOGGER.info("%s: HTTP 439, starting firewallPow", context)
        return run_pow_verification(session, response)
    if response.status_code == 429:
        if response_has_pow_challenge(response):
            LOGGER.info(
                "%s: HTTP 429 contains pow_challenge, starting firewallPow",
                context,
            )
            return run_pow_verification(session, response)
        if not is_geetest_firewall_html(response.text):
            log_unrecognized_protection_response(response, context=context)
            raise RuntimeError(
                f"{context}: HTTP 429 does not select PoW or GeeTest "
                f"({forbidden_response_reason(response)})"
            )
        html_path = save_html_response(response, context=context)
        LOGGER.info(
            "%s: full HTTP 429 HTML saved to %s",
            context,
            html_path,
        )
        LOGGER.info("%s: HTTP 429, starting GeeTest", context)
        return run_geetest_verification(
            session,
            response.text,
            referer=PAGE_REQUEST_HEADERS["Referer"],
        )
    raise RuntimeError(
        f"{context}: expected HTTP 200, 403, 429, or 439; "
        f"received HTTP {response.status_code}"
    )


def page_url(page: int) -> str:
    """Build the main items XHR while replacing only its ``p`` parameter."""
    query = [
        (key, str(page) if key == "p" else value)
        for key, value in ITEMS_QUERY_PARAMETERS
    ]
    return f"{ITEMS_URL}?{urlencode(query)}"


def bootstrap_catalog_session(
    session: requests.Session,
    *,
    verification_chain: list[str],
) -> int | None:
    """Visit the browser document before items XHR and clear its protections."""
    pow_unblock_ttl = None
    transitions = 0
    consecutive_geetest_failures = 0
    attempt = 0
    while True:
        attempt += 1
        set_session_headers(session, DOCUMENT_REQUEST_HEADERS)
        request_cookies = session_cookie_snapshot(session)
        response = get_with_qrator_recovery(
            session,
            CATALOG_PAGE_URL,
            session_headers=DOCUMENT_REQUEST_HEADERS,
            timeout_seconds=DOCUMENT_REQUEST_TIMEOUT_SECONDS,
            context="catalog bootstrap",
            stream_response_body=True,
            verification_chain=verification_chain,
        )
        save_http_exchange(
            session,
            response,
            context=f"catalog-bootstrap-attempt-{attempt}",
            method="GET",
            url=CATALOG_PAGE_URL,
            request_headers=DOCUMENT_REQUEST_HEADERS,
            request_cookies=request_cookies,
        )
        LOGGER.info(
            "catalog bootstrap attempt %s: HTTP %s",
            attempt,
            response.status_code,
        )
        if response.status_code == 200:
            expected_cookies = {"buyer_location_id", "luri", "sx"}
            cookie_names = set(session_cookie_names(session))
            missing = sorted(expected_cookies - cookie_names)
            if missing:
                LOGGER.warning(
                    "catalog bootstrap HTTP 200 did not set expected cookies: %s",
                    ", ".join(missing),
                )
            else:
                LOGGER.info(
                    "catalog bootstrap established location cookies: %s",
                    ", ".join(sorted(expected_cookies)),
                )
            return pow_unblock_ttl

        transitions += 1
        if transitions > MAX_PROTECTION_TRANSITIONS_PER_PAGE:
            raise RuntimeError(
                "catalog bootstrap exceeded "
                f"{MAX_PROTECTION_TRANSITIONS_PER_PAGE} protection transitions"
            )
        try:
            stage_result = handle_firewall_response(
                session,
                response,
                context="catalog bootstrap",
            )
        except GeeTestSolveFailed as exc:
            consecutive_geetest_failures += 1
            LOGGER.warning(
                "catalog bootstrap: GeeTest failure %s/%s; type=%s; "
                "lot_number=%s; result=%s",
                consecutive_geetest_failures,
                MAX_CONSECUTIVE_GEETEST_FAILURES,
                exc.captcha_type or "unknown",
                exc.lot_number or "unknown",
                exc.result or "unknown",
            )
            if (
                consecutive_geetest_failures
                >= MAX_CONSECUTIVE_GEETEST_FAILURES
            ):
                raise RuntimeError(
                    "catalog bootstrap: GeeTest failed "
                    f"{MAX_CONSECUTIVE_GEETEST_FAILURES} consecutive times; "
                    f"last task type={exc.captcha_type or 'unknown'}; "
                    f"lot_number={exc.lot_number or 'unknown'}; "
                    f"result={exc.result or 'unknown'}"
                ) from exc
            continue

        if isinstance(stage_result, GeeTestVerified):
            consecutive_geetest_failures = 0
            verification_chain.append("GeeTest")
            LOGGER.info(
                "catalog bootstrap verification chain: %s",
                format_verification_chain(verification_chain),
            )
            continue
        if stage_result is not None:
            consecutive_geetest_failures = 0
            pow_unblock_ttl = stage_result
            verification_chain.append("firewallPow")
            LOGGER.info(
                "catalog bootstrap verification chain: %s",
                format_verification_chain(verification_chain),
            )
            continue

        raise RuntimeError(
            "catalog bootstrap protection handler returned no result for "
            f"HTTP {response.status_code}"
        )


def request_pages(
    session: requests.Session,
    *,
    start_page: int = 1,
    verification_chain: list[str] | None = None,
    first_page_diagnostic_context: str | None = None,
    stop_after: Callable[[PageRequestResult], bool] | None = None,
) -> tuple[tuple[PageRequestResult, ...], Any | None]:
    """Request items pages from ``start_page`` through the configured limit.

    ``stop_after`` may end the loop early after any successful page, e.g.
    once a page holds no listings the caller has not seen before.
    """
    if not 1 <= start_page <= PAGES_TO_REQUEST:
        raise ValueError(
            f"start_page must be between 1 and {PAGES_TO_REQUEST}"
        )
    set_session_headers(session, PAGE_REQUEST_HEADERS)
    results = []
    for page in range(start_page, PAGES_TO_REQUEST + 1):
        if page != start_page:
            time.sleep(PAGE_REQUEST_DELAY_SECONDS)
        try:
            request_cookies = session_cookie_snapshot(session)
            response = get_with_qrator_recovery(
                session,
                page_url(page),
                session_headers=PAGE_REQUEST_HEADERS,
                request_headers=ITEMS_REQUEST_HEADERS,
                timeout_seconds=DOCUMENT_REQUEST_TIMEOUT_SECONDS,
                context=f"page p={page}",
                stream_response_body=True,
                verification_chain=verification_chain,
            )
        except (IncompleteRead, CurlConnectionError, CurlTimeout) as exc:
            LOGGER.warning(
                "page p=%s: transport failed after %s attempts (%s); "
                "continuing with the next page",
                page,
                MAX_TRANSIENT_GET_RETRIES + 1,
                type(exc).__name__,
            )
            results.append(PageRequestResult(page=page, status_code=0))
            continue
        diagnostic_context = None
        if page == start_page and first_page_diagnostic_context:
            diagnostic_context = first_page_diagnostic_context
        elif response.status_code in (403, 429, 439):
            diagnostic_context = f"page-p-{page}-protection"
        if diagnostic_context:
            save_http_exchange(
                session,
                response,
                context=diagnostic_context,
                method="GET",
                url=page_url(page),
                request_headers={
                    **PAGE_REQUEST_HEADERS,
                    **ITEMS_REQUEST_HEADERS,
                },
                request_cookies=request_cookies,
            )
        stats = parse_items_page_stats(response)
        result = PageRequestResult(
            page=page,
            status_code=response.status_code,
            redirect_location=redirect_target(response),
            stats=stats,
            items=parse_catalog_listings(response),
        )
        results.append(result)
        if stats is not None:
            LOGGER.info(
                "page p=%s: HTTP %s; totalCount=%s; itemsOnPage=%s; "
                "itemsHash=%s",
                page,
                response.status_code,
                stats.total_count,
                stats.items_on_page,
                stats.items_hash or "unavailable",
            )
        else:
            LOGGER.info("page p=%s: HTTP %s", page, response.status_code)
        LOGGER.debug(
            "page p=%s: session cookie names=%s",
            page,
            session_cookie_names(session),
        )
        if 300 <= response.status_code < 400:
            log_redirect_response(response, context=f"page p={page}")
        if response.status_code == 400:
            log_bad_request_response(response, context=f"page p={page}")
        if response.status_code in (403, 429, 439):
            return tuple(results), response
        if stats is not None and is_last_catalog_page(page, stats):
            LOGGER.info(
                "page p=%s is the last one for totalCount=%s",
                page,
                stats.total_count,
            )
            break
        if (
            stop_after is not None
            and response.status_code == 200
            and stop_after(result)
        ):
            LOGGER.info("page p=%s: caller requested to stop here", page)
            break
    return tuple(results), None


def run(
    on_page_results: Callable[[tuple[PageRequestResult, ...]], None]
    | None = None,
    *,
    start_page: int = 1,
    stop_after: Callable[[PageRequestResult], bool] | None = None,
) -> CompletedFlow:
    """Run the items XHR loop and clear each protection branch it selects.

    ``on_page_results`` receives every finished segment of page requests as
    soon as it is available, so callers keep the listings collected so far
    even when a later protection branch aborts the run.
    """
    # This flow must use the machine's public connection. In particular, do
    # not silently inherit a desktop/VPN proxy such as 127.0.0.1:2080. Removing
    # these variables also covers the image downloads made inside GeekedTest.
    for variable in PROXY_ENVIRONMENT_VARIABLES:
        os.environ.pop(variable, None)

    # curl_cffi's Firefox profile supplies an HTTP/2/TLS fingerprint close to
    # the Firefox 152 browser recorded in the HAR. The explicit headers below
    # retain the exact Firefox 152 request values.
    session = requests.Session(
        impersonate=HTTP_IMPERSONATE_PROFILE,
        trust_env=False,
    )
    LOGGER.info(
        "runtime platform=%s; browser user-agent=%s",
        sys.platform,
        BROWSER_USER_AGENT,
    )
    set_session_headers(session, {**REQUEST_HEADERS, "Referer": REFERER})

    verification_chain: list[str] = []
    pow_unblock_ttl = bootstrap_catalog_session(
        session,
        verification_chain=verification_chain,
    )
    next_page = start_page
    all_page_requests: list[PageRequestResult] = []
    last_protected_page: int | None = None
    protection_transitions_on_page = 0
    consecutive_geetest_failures = 0
    next_page_diagnostic_context: str | None = None
    while True:
        diagnostic_context = next_page_diagnostic_context
        next_page_diagnostic_context = None
        page_requests, protection_response = request_pages(
            session,
            start_page=next_page,
            verification_chain=verification_chain,
            first_page_diagnostic_context=diagnostic_context,
            stop_after=stop_after,
        )
        all_page_requests.extend(page_requests)
        if on_page_results is not None:
            on_page_results(page_requests)
        if protection_response is None:
            LOGGER.info(
                "items loop completed; verification chain: %s",
                format_verification_chain(verification_chain),
            )
            return CompletedFlow(
                pow_unblock_ttl=pow_unblock_ttl,
                page_requests=tuple(all_page_requests),
                verification_chain=tuple(verification_chain),
            )
        page = page_requests[-1].page
        if page == last_protected_page:
            protection_transitions_on_page += 1
        else:
            last_protected_page = page
            protection_transitions_on_page = 1
        if (
            protection_transitions_on_page
            > MAX_PROTECTION_TRANSITIONS_PER_PAGE
        ):
            raise RuntimeError(
                f"page p={page}: more than "
                f"{MAX_PROTECTION_TRANSITIONS_PER_PAGE} consecutive "
                "protection transitions"
            )
        LOGGER.info("page p=%s returned a protection response; classifying it", page)
        try:
            stage_result = handle_firewall_response(
                session, protection_response, context=f"page p={page}"
            )
        except GeeTestSolveFailed as exc:
            consecutive_geetest_failures += 1
            LOGGER.warning(
                "page p=%s: GeeTest solver failure %s/%s; type=%s; "
                "lot_number=%s; result=%s; fail_count=%s",
                page,
                consecutive_geetest_failures,
                MAX_CONSECUTIVE_GEETEST_FAILURES,
                exc.captcha_type or "unknown",
                exc.lot_number or "unknown",
                exc.result or "unknown",
                exc.fail_count
                if exc.fail_count is not None
                else "unknown",
            )
            if (
                consecutive_geetest_failures
                >= MAX_CONSECUTIVE_GEETEST_FAILURES
            ):
                raise RuntimeError(
                    "GeeTest failed "
                    f"{MAX_CONSECUTIVE_GEETEST_FAILURES} consecutive times; "
                    f"last task type={exc.captcha_type or 'unknown'}; "
                    f"lot_number={exc.lot_number or 'unknown'}; "
                    f"result={exc.result or 'unknown'}; "
                    "server fail_count="
                    f"{exc.fail_count if exc.fail_count is not None else 'unknown'}"
                ) from exc
            LOGGER.info(
                "page p=%s: restarting GeeTest from the original GET",
                page,
            )
            next_page_diagnostic_context = (
                f"page-p-{page}-after-geetest-failure-"
                f"{consecutive_geetest_failures}"
            )
            next_page = page
            continue
        if isinstance(stage_result, GeeTestVerified):
            consecutive_geetest_failures = 0
            verification_chain.append("GeeTest")
            LOGGER.info(
                "verification chain: %s",
                format_verification_chain(verification_chain),
            )
            LOGGER.info(
                "page p=%s: GeeTest cleared; retrying items loop from p=%s",
                page,
                page,
            )
            next_page_diagnostic_context = (
                f"page-p-{page}-after-geetest-verify"
            )
            next_page = page
            continue
        if stage_result is not None:
            consecutive_geetest_failures = 0
            pow_unblock_ttl = stage_result
            verification_chain.append("firewallPow")
            LOGGER.info(
                "verification chain: %s",
                format_verification_chain(verification_chain),
            )
            LOGGER.info(
                "page p=%s: firewallPow cleared; retrying items loop from p=%s",
                page,
                page,
            )
            next_page_diagnostic_context = (
                f"page-p-{page}-after-firewallPow-verify"
            )
            next_page = page
            continue


if __name__ == "__main__":
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    file_handler = logging.FileHandler("firewall-debug.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[console_handler, file_handler],
    )
    try:
        result = run()
    except (
        RuntimeError,
        IncompleteRead,
        CurlConnectionError,
        CurlTimeout,
    ) as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from None
    successful_pages = sum(
        page.status_code == 200 for page in result.page_requests
    )
    transport_failures = sum(
        page.status_code == 0 for page in result.page_requests
    )
    print(
        "run completed; "
        f"HTTP 200 pages={successful_pages}/{len(result.page_requests)}; "
        f"transport failures={transport_failures}; "
        "verification chain="
        f"{format_verification_chain(result.verification_chain)}"
    )
