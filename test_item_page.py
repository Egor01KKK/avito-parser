from datetime import datetime

import item_page

REAL_SNIPPET = (
    '<span class="" data-marker="item-view/item-id">№ 8157171618</span>'
    '<span class="" data-marker="item-view/item-date"> · <!-- -->29 сентября в 07:21</span>'
    '<span class=""> · <span class="" data-marker="item-view/total-views">8526\xa0просмотров</span>'
    '<span class="" data-marker="item-view/today-views"> <!-- -->(+109 сегодня)</span></span>'
)


def test_real_item_page_markup_is_parsed():
    page = item_page.parse(REAL_SNIPPET, now=datetime(2026, 10, 1, 18, 0))

    assert page.published == datetime(2026, 9, 29, 7, 21)
    assert (page.total_views, page.today_views, page.earlier_views) == (8526, 109, 8417)


def test_republished_and_new_listings_are_told_apart():
    noon = datetime(2026, 10, 1, 12, 0)
    old = item_page.ItemPage("сегодня в 11:50", None, total_views=8526, today_views=109)
    new = item_page.ItemPage("сегодня в 11:50", None, total_views=12, today_views=12)
    few_earlier = item_page.ItemPage("", None, total_views=15, today_views=6)

    assert item_page.is_newly_created(old, noon) is False
    assert item_page.is_newly_created(new, noon) is True
    assert item_page.is_newly_created(few_earlier, noon) is True   # 9 earlier views: within the limit


def test_after_midnight_yesterdays_views_are_tolerated():
    fresh_at_2350 = item_page.ItemPage("", None, total_views=40, today_views=3)

    assert item_page.is_newly_created(fresh_at_2350, datetime(2026, 10, 2, 0, 20)) is True
    assert item_page.is_newly_created(fresh_at_2350, datetime(2026, 10, 2, 14, 0)) is False


def test_page_without_counters_is_unknown():
    assert item_page.is_newly_created(item_page.parse("<html></html>")) is None


def test_dates_on_the_page():
    now = datetime(2026, 10, 1, 18, 0)
    assert item_page.parse_date("сегодня в 17:05", now) == datetime(2026, 10, 1, 17, 5)
    assert item_page.parse_date("вчера в 23:10", now) == datetime(2026, 9, 30, 23, 10)
    assert item_page.parse_date("5 января 2025 в 10:00", now) == datetime(2025, 1, 5, 10, 0)
    assert item_page.parse_date("15 ноября в 12:00", now) == datetime(2025, 11, 15, 12, 0)
