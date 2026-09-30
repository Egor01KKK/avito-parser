import json
import re
import sys
import types
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import pytest
from curl_cffi.requests.cookies import Cookies

import main


class FakeResponse:
    def __init__(
        self,
        status_code,
        *,
        headers=None,
        json_value=None,
        text="",
        url="https://www.avito.ru/test",
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_value = json_value
        self.text = text
        self.content = text.encode()
        self.url = url
        self.redirect_url = ""
        self.closed = False

    def json(self):
        return self._json_value

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self):
        yield self.content

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, target_responses=None):
        self.headers = {}
        self.cookies = Cookies()
        self.posts = []
        self.gets = []
        self.target_responses = list(target_responses or [])

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs, dict(self.headers)))
        self.cookies.set(
            "gMltIuegZN2COuSe",
            "SERVER_COOKIE",
            domain=".avito.ru",
            path="/",
        )
        return FakeResponse(
            200,
            headers={"server": "QRATOR", "content-type": "application/json"},
            json_value="FT_TOKEN==",
            url=url,
        )

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs, dict(self.headers)))
        if re.fullmatch(r"https://www\.avito\.ru/[0-9a-f]{20}\.js", url):
            return FakeResponse(
                200,
                headers={
                    "server": "QRATOR",
                    "content-type": "application/javascript",
                },
                url=url,
            )
        if url == main.QRATOR_FAVICON_URL:
            self.cookies.set(
                "v",
                "FAVICON_COOKIE",
                domain=".avito.ru",
                path="/",
            )
            return FakeResponse(
                200,
                headers={"server": "QRATOR", "content-type": "image/x-icon"},
                url=url,
            )
        if "/web/1/u?" in url:
            self.cookies.set(
                "_adcc",
                "PIXEL_COOKIE",
                domain=".avito.ru",
                path="/",
            )
            return FakeResponse(
                200,
                headers={"server": "QRATOR", "content-type": "image/gif"},
                url=url,
            )
        if not self.target_responses:
            raise AssertionError(f"unexpected GET {url}")
        return self.target_responses.pop(0)


def items_payload(items=None):
    return {
        "count": 4267,
        "totalCount": 4267,
        "totalElements": 4300,
        "mainCount": 4267,
        "itemsOnPage": 50,
        "catalog": {
            "items": items
            if items is not None
            else [{"id": 1, "title": "first"}, {"id": 2, "title": "second"}],
        },
    }


def test_items_response_counters_and_catalog_hash_are_parsed() -> None:
    first_items = [
        {"id": 1, "nested": {"b": 2, "a": 1}},
        {"id": 2, "title": "товар"},
    ]
    same_items_different_key_order = [
        {"nested": {"a": 1, "b": 2}, "id": 1},
        {"title": "товар", "id": 2},
    ]
    response = FakeResponse(
        200,
        headers={"content-type": "application/json"},
        json_value=items_payload(first_items),
    )

    stats = main.parse_items_page_stats(response)

    assert stats == main.ItemsPageStats(
        count=4267,
        total_count=4267,
        total_elements=4300,
        main_count=4267,
        items_on_page=50,
        items_hash=main.hash_catalog_items(first_items),
    )
    assert re.fullmatch(r"[0-9a-f]{32}", stats.items_hash)
    assert main.hash_catalog_items(first_items) == main.hash_catalog_items(
        same_items_different_key_order
    )
    assert main.hash_catalog_items(first_items) != main.hash_catalog_items(
        list(reversed(first_items))
    )


def test_firefox_user_agent_matches_runtime_platform() -> None:
    assert "Windows NT 10.0; Win64; x64" in main.firefox_user_agent(
        "win32"
    )
    assert "X11; Linux x86_64" in main.firefox_user_agent("linux")
    assert "Macintosh; Intel Mac OS X" in main.firefox_user_agent("darwin")
    assert main.PAGE_REQUEST_HEADERS["User-Agent"] == main.BROWSER_USER_AGENT
    assert main.GEETEST_REQUEST_HEADERS["User-Agent"] == main.BROWSER_USER_AGENT


def test_catalog_bootstrap_repeats_after_pow_and_sets_location_state(
    tmp_path,
) -> None:
    session = FakeSession()
    responses = [
        FakeResponse(
            439,
            headers={"content-type": "application/json"},
            json_value={"pow_challenge": "challenge"},
            text='{"pow_challenge":"challenge"}',
            url=main.CATALOG_PAGE_URL,
        ),
        FakeResponse(
            200,
            headers={"content-type": "text/html"},
            text="<html>catalog</html>",
            url=main.CATALOG_PAGE_URL,
        ),
    ]

    def document_get(*args, **kwargs):
        response = responses.pop(0)
        if response.status_code == 200:
            for name, value in (
                ("buyer_location_id", "624840"),
                ("luri", "volgograd"),
                ("sx", "state"),
            ):
                session.cookies.set(
                    name,
                    value,
                    domain=".avito.ru",
                    path="/",
                )
        return response

    verification_chain = []
    with (
        patch.object(main, "DEBUG_RESPONSE_DIR", tmp_path),
        patch.object(
            main,
            "get_with_qrator_recovery",
            side_effect=document_get,
        ) as get_document,
        patch.object(
            main,
            "handle_firewall_response",
            return_value=420,
        ) as handle,
    ):
        ttl = main.bootstrap_catalog_session(
            session,
            verification_chain=verification_chain,
        )

    assert ttl == 420
    assert verification_chain == ["firewallPow"]
    assert get_document.call_count == 2
    assert all(
        call.kwargs["session_headers"] == main.DOCUMENT_REQUEST_HEADERS
        for call in get_document.call_args_list
    )
    assert handle.call_args.kwargs["context"] == "catalog bootstrap"
    assert {"buyer_location_id", "luri", "sx"}.issubset(
        session.cookies.keys()
    )
    assert len(list(tmp_path.glob("exchange-*-catalog-bootstrap-*.json"))) == 2


def test_items_url_has_exact_query_and_only_replaces_page() -> None:
    expected = [
        (key, "17" if key == "p" else value)
        for key, value in main.ITEMS_QUERY_PARAMETERS
    ]
    parsed = urlsplit(main.page_url(17))

    assert parsed.scheme == "https"
    assert parsed.netloc == "www.avito.ru"
    assert parsed.path == "/web/1/js/items"
    assert parse_qsl(parsed.query) == expected
    assert dict(expected)["categoryId"] == "24"
    assert dict(expected)["locationId"] == "653240"
    assert dict(expected)["params[549][0]"] == "5697"
    assert dict(expected)["params[110472][0]"] == "437131"
    assert dict(expected)["rootCategoryId"] == "4"
    assert dict(expected)["features[suggestParams][categoryID]"] == "24"
    assert dict(expected)["features[ivaItemRedesign]"] == "true"
    assert "name" not in dict(expected)


def test_qrator_cookie_flow_matches_har_shape() -> None:
    session = FakeSession()
    page_url = "https://www.avito.ru/catalog?p=1"
    qrator_html = (
        '<meta http-equiv="refresh" content="1">'
        '<script src="/f1b0f8f3fb96fe30a8e6.js"></script>'
    )
    with patch.object(main.time, "sleep") as sleep:
        main.run_qrator_cookie_flow(
            session,
            qrator_html=qrator_html,
            page_url=page_url,
            context="test",
        )

    assert len(session.posts) == 1
    post_url, post_kwargs, post_headers = session.posts[0]
    assert post_url == "https://www.avito.ru/web/2/ft"
    assert re.fullmatch(
        r'multipart/form-data;boundary="\d{16}"',
        post_headers["Content-Type"],
    )
    body = post_kwargs["data"].decode("ascii")
    f_value = re.search(r'name="f"\r\n\r\n([^\r]+)', body).group(1)
    s_value = re.search(r'name="s"\r\n\r\n([^\r]+)', body).group(1)
    assert len(f_value) == 882
    assert len(s_value) == 176
    assert session.cookies.get("f") == f_value
    assert session.cookies.get("ft") == json.dumps("FT_TOKEN==")
    assert session.cookies.get("gMltIuegZN2COuSe") == "SERVER_COOKIE"
    assert session.cookies.get("v") == "FAVICON_COOKIE"

    assert len(session.gets) == 3
    script_url, _, script_headers = session.gets[0]
    favicon_url, _, favicon_headers = session.gets[1]
    assert script_url == "https://www.avito.ru/f1b0f8f3fb96fe30a8e6.js"
    assert favicon_url == main.QRATOR_FAVICON_URL
    assert script_headers["Referer"] == page_url
    assert favicon_headers["Priority"] == "u=6"

    pixel_url, pixel_kwargs, pixel_headers = session.gets[2]
    match = re.fullmatch(r"https://www\.avito\.ru/web/1/u\?(\d+)", pixel_url)
    assert match
    assert 0 <= int(match.group(1)) < 0xFFFFFFFF
    assert "=" not in pixel_url
    assert pixel_headers["Sec-Fetch-Dest"] == "image"
    assert session.cookies.get("_adcc") == "PIXEL_COOKIE"
    assert sleep.call_count == 2


def test_original_get_is_retried_after_qrator_flow() -> None:
    target_url = "https://www.avito.ru/catalog?p=1"
    verification_chain = []
    session = FakeSession(
        [
            FakeResponse(
                302,
                headers={"server": "QRATOR", "content-type": "text/html"},
                text=(
                    '<meta http-equiv="refresh" content="1">'
                    '<script src="/f1b0f8f3fb96fe30a8e6.js"></script>'
                ),
                url=target_url,
            ),
            FakeResponse(
                200,
                headers={"server": "QRATOR", "content-type": "text/html"},
                url=target_url,
            ),
        ]
    )

    with (
        patch.object(main, "log_redirect_response"),
        patch.object(main.time, "sleep"),
    ):
        result = main.get_with_qrator_recovery(
            session,
            target_url,
            session_headers=main.PAGE_REQUEST_HEADERS,
            context="page p=1",
            verification_chain=verification_chain,
        )

    assert result.status_code == 200
    target_gets = [call for call in session.gets if call[0] == target_url]
    assert len(target_gets) == 2
    assert len(session.posts) == 1
    assert any("/web/1/u?" in call[0] for call in session.gets)
    assert target_gets[1][2]["Referer"] == main.PAGE_REQUEST_HEADERS["Referer"]
    assert verification_chain == ["QRATOR"]


def test_forbidden_response_is_classified_as_ip_problem() -> None:
    response = FakeResponse(
        403,
        headers={"server": "QRATOR", "content-type": "text/html; charset=utf-8"},
        text="<html><head><title>Доступ ограничен: проблема с IP</title></head></html>",
    )
    assert (
        main.forbidden_response_reason(response)
        == "Доступ ограничен: проблема с IP"
    )


def test_unknown_json_protection_body_is_saved_with_json_extension(
    tmp_path,
) -> None:
    body = (
        '{"too-many-requests":{"message":'
        '"Доступ с вашего IP-адреса временно ограничен",'
        '"link":"ru.avito:\\/\\/1\\/firewall\\/captcha\\/show"}}'
    )
    response = FakeResponse(
        429,
        headers={"content-type": "application/json; charset=utf-8"},
        text=body,
    )

    with patch.object(main, "DEBUG_RESPONSE_DIR", tmp_path):
        main.log_unrecognized_protection_response(
            response,
            context="page p=13",
        )

    saved = tmp_path / "page-p-13-http-429.json"
    assert saved.read_text(encoding="utf-8") == body


def test_json_captcha_dispatcher_is_recognized() -> None:
    response = FakeResponse(
        429,
        headers={
            "content-type": "application/json",
            "x-firewall-show-captcha": "true",
        },
        json_value={
            "too-many-requests": {
                "message": "Доступ временно ограничен",
                "link": "ru.avito://1/firewall/captcha/show",
            }
        },
    )

    assert main.is_firewall_captcha_dispatcher_response(response) is True


def test_http_403_dispatcher_is_routed_to_captcha_flow(tmp_path) -> None:
    body = (
        '{"too-many-requests":{"message":"Доступ временно ограничен",'
        '"link":"ru.avito://1/firewall/captcha/show"}}'
    )
    response = FakeResponse(
        403,
        headers={
            "content-type": "application/json",
            "x-firewall-show-captcha": "true",
        },
        json_value={
            "too-many-requests": {
                "message": "Доступ временно ограничен",
                "link": "ru.avito://1/firewall/captcha/show",
            }
        },
        text=body,
        url="https://www.avito.ru/web/1/js/items?p=13",
    )
    verified = main.GeeTestVerified(lot_number="lot")
    session = FakeSession()

    with (
        patch.object(main, "DEBUG_RESPONSE_DIR", tmp_path),
        patch.object(
            main,
            "run_firewall_captcha_dispatcher",
            return_value=verified,
        ) as dispatcher,
    ):
        result = main.handle_firewall_response(
            session,
            response,
            context="page p=13",
        )

    assert result is verified
    dispatcher.assert_called_once_with(
        session,
        referer=main.PAGE_REQUEST_HEADERS["Referer"],
    )
    assert (tmp_path / "page-p-13-http-403.json").read_text() == body


def test_http_429_dispatcher_still_routes_to_captcha_flow(tmp_path) -> None:
    body = (
        '{"too-many-requests":{"message":"Доступ временно ограничен",'
        '"link":"ru.avito://1/firewall/captcha/show"}}'
    )
    response = FakeResponse(
        429,
        headers={
            "content-type": "application/json",
            "x-firewall-show-captcha": "true",
        },
        json_value={
            "too-many-requests": {
                "message": "Доступ временно ограничен",
                "link": "ru.avito://1/firewall/captcha/show",
            }
        },
        text=body,
        url="https://www.avito.ru/web/1/js/items?p=13",
    )
    verified = main.GeeTestVerified(lot_number="lot")
    session = FakeSession()

    with (
        patch.object(main, "DEBUG_RESPONSE_DIR", tmp_path),
        patch.object(
            main,
            "run_firewall_captcha_dispatcher",
            return_value=verified,
        ) as dispatcher,
    ):
        result = main.handle_firewall_response(
            session,
            response,
            context="page p=13",
        )

    assert result is verified
    dispatcher.assert_called_once_with(
        session,
        referer=main.PAGE_REQUEST_HEADERS["Referer"],
    )
    assert (tmp_path / "page-p-13-http-429.json").read_text() == body


def test_firewall_captcha_get_selects_geetest_with_exact_request() -> None:
    class CaptchaSession(FakeSession):
        def post(self, url, **kwargs):
            self.posts.append((url, kwargs, dict(self.headers)))
            return FakeResponse(
                200,
                headers={"content-type": "application/json"},
                json_value={
                    "success": {
                        "result": {
                            "captcha": {
                                "geeTest": {"type": "geeTest"},
                            }
                        }
                    }
                },
                url=url,
            )

    session = CaptchaSession()
    selected = main.fetch_firewall_captcha(
        session,
        referer="https://www.avito.ru/web/1/js/items?p=13",
    )

    assert selected == {"type": "geeTest"}
    url, kwargs, headers = session.posts[0]
    assert url == main.FIREWALL_CAPTCHA_GET_URL
    assert kwargs["json"] == {"refreshInternalCaptcha": False}
    assert kwargs["allow_redirects"] is False
    assert headers["Accept"] == "*/*"
    assert headers["Priority"] == "u=4"
    assert headers["Referer"].endswith("/web/1/js/items?p=13")


def test_captcha_dispatcher_uses_bundle_geetest_id() -> None:
    load, _ = geetest_fixture()
    verified = main.GeeTestVerified(lot_number=load.lot_number)
    session = FakeSession()

    with (
        patch.object(
            main,
            "fetch_firewall_captcha",
            return_value={"type": "geeTest"},
        ),
        patch.object(
            main,
            "load_geetest_task",
            return_value=load,
        ) as load_task,
        patch.object(
            main,
            "complete_geetest_verification",
            return_value=verified,
        ) as complete,
    ):
        result = main.run_firewall_captcha_dispatcher(
            session,
            referer="https://www.avito.ru/items?p=13",
        )

    assert result is verified
    load_task.assert_called_once_with(
        session,
        captcha_id=main.GEETEST_CAPTCHA_ID,
    )
    complete.assert_called_once_with(
        session,
        load,
        referer="https://www.avito.ru/items?p=13",
    )


def test_geetest_load_uses_shared_session_and_har_headers() -> None:
    class LoadSession(FakeSession):
        def get(self, url, **kwargs):
            self.gets.append((url, kwargs, dict(self.headers)))
            callback = kwargs["params"]["callback"]
            return FakeResponse(
                200,
                headers={"content-type": "text/javascript"},
                text=(
                    f'{callback}({{"status":"success","data":'
                    '{"lot_number":"lot","captcha_type":"slide"}})'
                ),
                url=url,
            )

    session = LoadSession()
    load = main.load_geetest_task(session, captcha_id="captcha-id")

    assert load.lot_number == "lot"
    assert len(session.gets) == 1
    url, _, headers = session.gets[0]
    assert url == main.GEETEST_LOAD_URL
    assert headers["Referer"] == "https://www.avito.ru/"
    assert headers["Sec-Fetch-Site"] == "cross-site"
    assert headers["User-Agent"] == main.PAGE_REQUEST_HEADERS["User-Agent"]
    assert "Origin" not in headers


def test_qrator_html_is_recognized_when_status_is_429() -> None:
    response = FakeResponse(
        429,
        headers={"server": "QRATOR", "content-type": "text/html"},
        text=(
            '<meta http-equiv="refresh" content="1">'
            '<script src="/f1b0f8f3fb96fe30a8e6.js"></script>'
        ),
    )
    assert main.is_qrator_challenge_response(response)


def test_page_loop_stops_immediately_on_first_403() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                headers={
                    "server": "QRATOR",
                    "content-type": "application/json",
                },
                json_value=items_payload(),
                url=main.page_url(1),
            ),
            FakeResponse(
                403,
                headers={"server": "QRATOR", "content-type": "text/html"},
                text="<title>Доступ ограничен: проблема с IP</title>",
                url=main.page_url(2),
            ),
        ]
    )
    with patch.object(main.time, "sleep") as sleep:
        results, protection_response = main.request_pages(session)

    assert [result.status_code for result in results] == [200, 403]
    assert protection_response is not None
    assert protection_response.status_code == 403
    assert results[0].stats.total_count == 4267
    assert results[0].stats.items_on_page == 50
    assert re.fullmatch(r"[0-9a-f]{32}", results[0].stats.items_hash)
    first_url, first_kwargs, first_session_headers = session.gets[0]
    assert first_url == main.page_url(1)
    assert first_kwargs["headers"]["X-Source"] == "client-browser"
    assert first_kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert first_session_headers["Sec-Fetch-Mode"] == "cors"
    sleep.assert_called_once_with(main.PAGE_REQUEST_DELAY_SECONDS)


def test_page_loop_saves_http_400_body_and_continues(tmp_path) -> None:
    bad_request_body = '{"error":{"message":"invalid request"}}'
    session = FakeSession(
        [
            FakeResponse(
                400,
                headers={"content-type": "application/json"},
                text=bad_request_body,
                url=main.page_url(31),
            ),
            FakeResponse(
                200,
                headers={"content-type": "application/json"},
                json_value=items_payload(),
                url=main.page_url(32),
            ),
        ]
    )

    with (
        patch.object(main, "DEBUG_RESPONSE_DIR", tmp_path),
        patch.object(main, "PAGES_TO_REQUEST", 32),
        patch.object(main.time, "sleep"),
    ):
        results, protection_response = main.request_pages(
            session,
            start_page=31,
        )

    assert [result.status_code for result in results] == [400, 200]
    assert protection_response is None
    assert (
        tmp_path / "page-p-31-http-400.json"
    ).read_text(encoding="utf-8") == bad_request_body


def test_full_http_exchange_diagnostic_contains_request_and_response(
    tmp_path,
) -> None:
    session = FakeSession()
    session.headers.update({"User-Agent": "Firefox", "X-Test": "session"})
    session.cookies.set(
        "captcha_v4_user",
        "cookie-value",
        domain="gcaptcha4.geevisit.com",
        path="/",
    )
    response = FakeResponse(
        200,
        headers={"content-type": "application/json", "x-response": "yes"},
        text='{"result":"success"}',
        url="https://gcaptcha4.geevisit.com/verify?callback=test",
    )

    with patch.object(main, "DEBUG_RESPONSE_DIR", tmp_path):
        path = main.save_http_exchange(
            session,
            response,
            context="GeeTest-verify",
            method="GET",
            url="https://gcaptcha4.geevisit.com/verify",
            request_headers={"X-Test": "request"},
            params={"lot_number": "lot", "w": "payload"},
        )

    exchange = json.loads(path.read_text(encoding="utf-8"))
    assert exchange["request"]["method"] == "GET"
    assert exchange["request"]["headers"]["X-Test"] == "request"
    assert exchange["request"]["params"]["w"] == "payload"
    assert exchange["request"]["cookies"][0]["value"] == "cookie-value"
    assert exchange["response"]["status"] == 200
    assert exchange["response"]["headers"]["x-response"] == "yes"
    assert exchange["response"]["body"] == '{"result":"success"}'


def test_document_get_retries_one_incomplete_read() -> None:
    target_url = "https://www.avito.ru/catalog?p=3"

    class InterruptedSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def get(self, url, **kwargs):
            self.attempts += 1
            self.gets.append((url, kwargs, dict(self.headers)))
            if self.attempts == 1:
                partial = FakeResponse(
                    200,
                    headers={"content-type": "text/html"},
                    text="<html>",
                    url=url,
                )
                partial.content = b"<html>"
                raise main.IncompleteRead(
                    "partial response",
                    code=18,
                    response=partial,
                )
            return FakeResponse(
                200,
                headers={"content-type": "text/html"},
                text="<html>complete</html>",
                url=url,
            )

    session = InterruptedSession()
    with patch.object(main.time, "sleep") as sleep:
        response = main.get_with_qrator_recovery(
            session,
            target_url,
            session_headers=main.PAGE_REQUEST_HEADERS,
            context="page p=3",
        )

    assert response.status_code == 200
    assert session.attempts == 2
    sleep.assert_called_once_with(main.TRANSPORT_RETRY_DELAY_SECONDS)


def test_complete_streamed_html_is_kept_after_curl_trailer_error() -> None:
    target_url = "https://www.avito.ru/catalog?p=4"

    class CompleteThenErrorResponse(FakeResponse):
        def iter_content(self):
            yield b"<html><body>complete</body></html>"
            raise main.CurlConnectionError(
                "connection ended after body",
                code=55,
                response=self,
            )

    response = CompleteThenErrorResponse(
        200,
        headers={"content-type": "text/html"},
        url=target_url,
    )
    session = FakeSession([response])

    result = main.get_with_qrator_recovery(
        session,
        target_url,
        session_headers=main.PAGE_REQUEST_HEADERS,
        context="page p=4",
        stream_response_body=True,
    )

    assert result is response
    assert result.content == b"<html><body>complete</body></html>"
    assert len(session.gets) == 1
    assert session.gets[0][1]["stream"] is True


def geetest_fixture() -> tuple[main.GeeTestLoad, dict[str, str]]:
    seccode = {
        "captcha_id": "2d9c743cf7d63dbc9db578a608196bcd",
        "lot_number": "c622cb37692d4428b8992da29908e3ba",
        "pass_token": "p" * 64,
        "gen_time": "1785169913",
        "captcha_output": "output",
    }
    data = {
        "lot_number": seccode["lot_number"],
        "captcha_type": "slide",
        "payload": "payload",
        "process_token": "process-token",
        "pt": "1",
    }
    load = main.GeeTestLoad(
        captcha_id=seccode["captcha_id"],
        challenge="challenge",
        callback="geetest_1",
        lot_number=seccode["lot_number"],
        captcha_type="slide",
        data=data,
    )
    return load, seccode


def test_existing_geetest_load_data_is_passed_to_geeked_submit() -> None:
    load, seccode = geetest_fixture()
    observed = {}

    class FakeGeeked:
        def __init__(
            self,
            captcha_id,
            lang,
                *,
                session,
                request_headers,
                exchange_logger,
            ):
            observed["captcha_id"] = captcha_id
            observed["lang"] = lang
            observed["session"] = session
            observed["request_headers"] = request_headers
            observed["exchange_logger"] = exchange_logger
            self.lot_number = None
            self.session = session
            self.base_url = ""

        def submit_captcha(self, data):
            observed["data"] = data
            observed["lot_number"] = self.lot_number
            observed["solver_cookie"] = self.session.cookies.get(
                "captcha_v4_user"
            )
            return seccode

    fake_module = types.ModuleType("geeked")
    fake_module.CaptchaSolveRejected = type(
        "CaptchaSolveRejected",
        (Exception,),
        {},
    )
    fake_module.Geeked = FakeGeeked
    source_session = FakeSession()
    source_session.cookies.set(
        "captcha_v4_user",
        "GEETEST_COOKIE",
        domain="gcaptcha4.geevisit.com",
        path="/",
    )
    with patch.dict(sys.modules, {"geeked": fake_module}):
        result = main.solve_geetest_load(
            load,
            source_session=source_session,
        )

    assert result == seccode
    assert observed["data"] is load.data
    assert observed["lot_number"] == load.lot_number
    assert observed["session"] is source_session
    assert observed["request_headers"] is main.GEETEST_REQUEST_HEADERS
    assert callable(observed["exchange_logger"])
    assert observed["solver_cookie"] == "GEETEST_COOKIE"


def test_avito_geetest_verify_payload_matches_bundle() -> None:
    load, seccode = geetest_fixture()

    class VerifySession(FakeSession):
        def post(self, url, **kwargs):
            self.posts.append((url, kwargs, dict(self.headers)))
            return FakeResponse(
                200,
                headers={"content-type": "application/json"},
                json_value={"success": {"result": {"verified": True}}},
                url=url,
            )

    session = VerifySession()
    verified = main.verify_geetest_with_avito(
        session,
        load,
        seccode,
        referer="https://www.avito.ru/catalog?p=1",
    )

    assert verified.lot_number == seccode["lot_number"]
    url, kwargs, headers = session.posts[0]
    assert url == main.FIREWALL_CAPTCHA_VERIFY_URL
    assert list(kwargs["json"]) == [
        "captcha",
        "hCaptchaResponse",
        "captcha_id",
        "lot_number",
        "pass_token",
        "gen_time",
        "captcha_output",
    ]
    assert kwargs["json"]["captcha"] == ""
    assert kwargs["json"]["hCaptchaResponse"] == ""
    assert re.fullmatch(r"\d{2}", headers["X-Cube"])
    assert headers["Priority"] == "u=4"
    assert headers["Referer"] == "https://www.avito.ru/catalog?p=1"


def test_geetest_rejection_preserves_task_diagnostics() -> None:
    load, _ = geetest_fixture()

    class CaptchaSolveRejected(Exception):
        def __init__(self, response):
            self.response = response

    class RejectingGeeked:
        def __init__(
            self,
            captcha_id,
            lang,
                *,
                session,
                request_headers,
                exchange_logger,
            ):
            self.lot_number = None
            self.session = session
            self.base_url = ""

        def submit_captcha(self, data):
            raise CaptchaSolveRejected(
                {
                    "lot_number": load.lot_number,
                    "result": "fail",
                    "fail_count": 1,
                    "payload": "large-opaque-value",
                }
            )

    fake_module = types.ModuleType("geeked")
    fake_module.CaptchaSolveRejected = CaptchaSolveRejected
    fake_module.Geeked = RejectingGeeked

    with (
        patch.dict(sys.modules, {"geeked": fake_module}),
        pytest.raises(main.GeeTestSolveFailed) as error,
    ):
        main.solve_geetest_load(load, source_session=FakeSession())

    assert error.value.captcha_type == "slide"
    assert error.value.lot_number == load.lot_number
    assert error.value.result == "fail"
    assert error.value.fail_count == 1
    assert "large-opaque-value" not in str(error.value)


def test_avito_verified_false_becomes_retryable_geetest_failure(
    tmp_path,
) -> None:
    load, seccode = geetest_fixture()

    class RejectingVerifySession(FakeSession):
        def post(self, url, **kwargs):
            return FakeResponse(
                200,
                headers={"content-type": "application/json"},
                json_value={
                    "success": {
                        "result": {
                            "captcha": {"type": "geeTest"},
                            "verified": False,
                        }
                    }
                },
                text='{"success":{"result":{"verified":false}}}',
                url=url,
            )

    with (
        patch.object(main, "DEBUG_RESPONSE_DIR", tmp_path),
        pytest.raises(main.GeeTestSolveFailed) as error,
    ):
        main.verify_geetest_with_avito(
            RejectingVerifySession(),
            load,
            seccode,
            referer=main.PAGE_REQUEST_HEADERS["Referer"],
        )

    assert error.value.captcha_type == "slide"
    assert error.value.lot_number == load.lot_number
    assert error.value.result == "avito_verified_false"
    assert (
        tmp_path / "GeeTest-firewallCaptcha-verify-http-200.json"
    ).exists()


def test_run_retries_the_same_page_after_geetest_and_pow() -> None:
    session = FakeSession()
    geetest_response = FakeResponse(429, url=main.page_url(13))
    pow_response = FakeResponse(439, url=main.page_url(13))
    completed_segment = (
        main.PageRequestResult(page=13, status_code=200),
        main.PageRequestResult(page=14, status_code=200),
    )

    with (
        patch.object(main.requests, "Session", return_value=session),
        patch.object(main, "bootstrap_catalog_session", return_value=None),
        patch.object(
            main,
            "request_pages",
            side_effect=[
                (
                    (main.PageRequestResult(page=13, status_code=429),),
                    geetest_response,
                ),
                (
                    (main.PageRequestResult(page=13, status_code=439),),
                    pow_response,
                ),
                (completed_segment, None),
            ],
        ) as request_pages,
        patch.object(
            main,
            "handle_firewall_response",
            side_effect=[
                main.GeeTestVerified(lot_number="lot"),
                420,
            ],
        ),
    ):
        result = main.run()

    assert [
        call.kwargs["start_page"] for call in request_pages.call_args_list
    ] == [1, 13, 13]
    assert result.verification_chain == ("GeeTest", "firewallPow")
    assert [(item.page, item.status_code) for item in result.page_requests] == [
        (13, 429),
        (13, 439),
        (13, 200),
        (14, 200),
    ]


def test_run_restarts_original_get_after_geetest_solver_failure() -> None:
    session = FakeSession()
    protection_response = FakeResponse(403, url=main.page_url(13))
    failed_page = (main.PageRequestResult(page=13, status_code=403),)
    completed_page = (main.PageRequestResult(page=13, status_code=200),)
    rejection = main.GeeTestSolveFailed(
        captcha_type="slide",
        lot_number="failed-lot",
        result="fail",
        fail_count=1,
    )

    with (
        patch.object(main.requests, "Session", return_value=session),
        patch.object(main, "bootstrap_catalog_session", return_value=None),
        patch.object(
            main,
            "request_pages",
            side_effect=[
                (failed_page, protection_response),
                (failed_page, protection_response),
                (completed_page, None),
            ],
        ) as request_pages,
        patch.object(
            main,
            "handle_firewall_response",
            side_effect=[
                rejection,
                main.GeeTestVerified(lot_number="fresh-lot"),
            ],
        ),
    ):
        result = main.run()

    assert [
        call.kwargs["start_page"] for call in request_pages.call_args_list
    ] == [1, 13, 13]
    assert result.verification_chain == ("GeeTest",)


def test_run_stops_after_five_consecutive_geetest_failures() -> None:
    session = FakeSession()
    protection_response = FakeResponse(403, url=main.page_url(13))
    failed_page = (main.PageRequestResult(page=13, status_code=403),)
    rejection = main.GeeTestSolveFailed(
        captcha_type="slide",
        lot_number="fifth-lot",
        result="fail",
        fail_count=1,
    )

    with (
        patch.object(main.requests, "Session", return_value=session),
        patch.object(main, "bootstrap_catalog_session", return_value=None),
        patch.object(
            main,
            "request_pages",
            side_effect=[(failed_page, protection_response)] * 5,
        ) as request_pages,
        patch.object(
            main,
            "handle_firewall_response",
            side_effect=[rejection] * 5,
        ),
        pytest.raises(RuntimeError) as error,
    ):
        main.run()

    assert "failed 5 consecutive times" in str(error.value)
    assert "type=slide" in str(error.value)
    assert "lot_number=fifth-lot" in str(error.value)
    assert len(request_pages.call_args_list) == 5


def test_intervening_pow_does_not_exhaust_geetest_retry_budget() -> None:
    session = FakeSession()
    rejection = main.GeeTestSolveFailed(
        captcha_type="slide",
        lot_number="rejected-lot",
        result="avito_verified_false",
        fail_count=None,
    )
    protection_statuses = [429, 403, 439, 403, 403, 429]
    page_segments = [
        (
            (main.PageRequestResult(page=1, status_code=status),),
            FakeResponse(status, url=main.page_url(1)),
        )
        for status in protection_statuses
    ]
    page_segments.append(
        ((main.PageRequestResult(page=1, status_code=200),), None)
    )

    with (
        patch.object(main.requests, "Session", return_value=session),
        patch.object(main, "bootstrap_catalog_session", return_value=None),
        patch.object(
            main,
            "request_pages",
            side_effect=page_segments,
        ) as request_pages,
        patch.object(
            main,
            "handle_firewall_response",
            side_effect=[
                rejection,
                rejection,
                420,
                rejection,
                rejection,
                main.GeeTestVerified(lot_number="accepted-lot"),
            ],
        ),
    ):
        result = main.run()

    assert len(request_pages.call_args_list) == 7
    assert all(
        call.kwargs["start_page"] == 1
        for call in request_pages.call_args_list
    )
    assert result.verification_chain == ("firewallPow", "GeeTest")
    assert result.pow_unblock_ttl == 420


def test_page_loop_stops_after_last_page_and_keeps_listings() -> None:
    last_page_payload = items_payload(
        [{"id": 1, "type": "item"}, {"id": 2, "type": "banner"}]
    )
    last_page_payload["totalCount"] = 60
    session = FakeSession(
        [
            FakeResponse(
                200,
                headers={"content-type": "application/json"},
                json_value=last_page_payload,
                url=main.page_url(2),
            ),
        ]
    )

    with patch.object(main.time, "sleep"):
        results, protection_response = main.request_pages(
            session, start_page=2
        )

    assert protection_response is None
    assert [result.page for result in results] == [2]
    assert results[0].items == ({"id": 1, "type": "item"},)


def test_page_loop_stops_when_caller_asks() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                headers={"content-type": "application/json"},
                json_value=items_payload(),
                url=main.page_url(1),
            ),
        ]
    )

    with patch.object(main.time, "sleep"):
        results, _ = main.request_pages(
            session, stop_after=lambda result: result.page == 1
        )

    assert [result.page for result in results] == [1]
