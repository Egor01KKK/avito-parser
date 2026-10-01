import json

import avito
import telegram_notify


def test_suggested_name_comes_from_link_and_avoids_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(avito, "SEARCHES_DIR", tmp_path)
    url = "https://www.avito.ru/kazan/telefony/mobilnye_telefony/apple/iphone_15-ASgBAgICA0SywA2SoO0RtMANzqs5sMENiPw3"

    assert avito.suggested_name(url) == "iphone_15-kazan"
    (tmp_path / "iphone_15-kazan.json").write_text("{}", encoding="utf-8")
    assert avito.suggested_name(url) == "iphone_15-kazan-2"
    assert avito.suggested_name("https://www.avito.ru/moskva/noutbuki?q=macbook") == "noutbuki-moskva"


def test_json_output_is_one_object_per_event(capsys, monkeypatch):
    monkeypatch.setattr(avito, "JSON_OUTPUT", True)

    avito.say("PAGE", search="x", page=2, items=50, new=3, total=1500)

    event = json.loads(capsys.readouterr().out)
    assert event["event"] == "PAGE" and event["new"] == 3


def test_human_output_is_readable(capsys, monkeypatch):
    monkeypatch.setattr(avito, "JSON_OUTPUT", False)

    avito.say("SESSION-RESET", reason="captcha", page=4)

    assert "капчу" in capsys.readouterr().out


def test_telegram_messages_escape_html_and_carry_link():
    goods_alert = {
        "title": "iPhone 15, 128 ГБ <новый>", "price": "30 000 ₽", "market": "35 000 ₽",
        "vs_market": "-14%", "avito_badge": "Цена ниже рыночной", "seller": "частное лицо",
        "closed_listings": 4, "notes": ["дешевле рынка"], "url": "https://www.avito.ru/x_1",
    }
    text = telegram_notify.message_for("goods", "iphone15-spb", goods_alert)
    assert "&lt;новый&gt;" in text
    assert 'href="https://www.avito.ru/x_1"' in text
    assert "#iphone15_spb" in text

    realty_alert = {
        "title": "2-к. квартира", "price": "12 000 000 ₽", "price_per_m2": "240 000 ₽",
        "address": "Примерная ул., 1", "metro": "Примерная 500 м", "published": "1 час назад",
        "verdict": "собственник", "reasons": ["пишет «собственник»"], "notes": [],
        "url": "https://www.avito.ru/y_2", "draft": "Здравствуйте!",
    }
    text = telegram_notify.message_for("realty-owner", "flats", realty_alert)
    assert "Черновик" in text and "12 000 000 ₽" in text


def test_only_recently_listed_items_are_fresh():
    from datetime import datetime, timedelta
    now = datetime(2026, 10, 1, 17, 0)
    assert avito.is_fresh(now - timedelta(hours=1), 6, now)
    assert not avito.is_fresh(now - timedelta(days=10), 6, now)
    assert avito.is_fresh(None, 6, now)
    stamp = int((now - timedelta(hours=2)).timestamp() * 1000)
    assert avito.listed_since({"sortTimeStamp": stamp}) == now - timedelta(hours=2)


def test_scan_also_walks_newest_first(tmp_path, monkeypatch):
    import argparse
    import avito_client

    monkeypatch.setattr(avito, "SEARCHES_DIR", tmp_path / "searches")
    monkeypatch.setattr(avito, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(avito, "OUTPUT_DIR", tmp_path / "out")
    avito.SEARCHES_DIR.mkdir()
    (avito.SEARCHES_DIR / "phones.json").write_text(json.dumps({
        "title": "phones", "profile": "goods", "catalog_url": "https://www.avito.ru/kazan/telefony",
        "params": {"categoryId": "84", "locationId": "650400", "s": "1"},
    }), encoding="utf-8")
    seen_params = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def page(self, *, catalog_url, params, number):
            seen_params.append(params)
            return avito_client.Page(number=number, items=(), total_count=0, items_on_page=0)

    monkeypatch.setattr(avito.avito_client, "CalmClient", FakeClient)
    avito.cmd_scan(argparse.Namespace(name="phones", full=True, max_pages=5, delay=0))

    assert seen_params and all(params["s"] == "104" for params in seen_params)
    assert seen_params[0]["categoryId"] == "84"
