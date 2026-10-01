import pytest

import link_resolver as lr

FLATS = (
    "https://www.avito.ru/sankt-peterburg/kvartiry/prodam/2-komnatnye-ASgBAgICAkSSA8YQygiCWQ"
    "?f=ASgBAgICA0SSA8YQygiCWZC~DZauNQ"
)
IPHONES = (
    "https://www.avito.ru/sankt-peterburg/telefony/mobilnye_telefony/apple/"
    "iphone_15-ASgBAgICA0SywA2SoO0RtMANzqs5sMENiPw3"
)


@pytest.mark.parametrize("name, slug", [
    ("Санкт-Петербург", "sankt-peterburg"),
    ("Нижний Новгород", "nizhniy_novgorod"),
    ("Казань", "kazan"),
    ("Ростов-на-Дону", "rostov-na-donu"),
    ("Московская область", "moskovskaya_oblast"),
])
def test_transliteration_matches_avito_slugs(name, slug):
    assert lr.transliterate(name) == slug


def test_slug_prefix_goes_back_to_cyrillic():
    assert lr.slug_prefix_in_cyrillic("sankt-peterburg", 5) == "санкт"
    assert lr.slug_prefix_in_cyrillic("kazan", 4) == "каза"
    assert lr.slug_prefix_in_cyrillic("nizhniy_novgorod", 4) == "нижн"


def test_filter_blob_decodes_attribute_value_pairs():
    assert lr.decode_filter_blob("ASgBAgICA0SSA8YQygiCWZC~DZauNQ") == [
        (201, 1059), (549, 5697), (110536, 437131),
    ]
    assert lr.decode_filter_blob("ASgBAgICA0SywA2SoO0RtMANzqs5sMENiPw3") == [
        (110617, 18720777), (110618, 469735), (110680, 458500),
    ]


def test_link_parts_prefer_query_blob_over_path_blob():
    link = lr.parse_link(FLATS)
    assert (link.city_slug, link.category_slug) == ("sankt-peterburg", "kvartiry")
    assert link.blob == "ASgBAgICA0SSA8YQygiCWZC~DZauNQ"

    phones = lr.parse_link(IPHONES)
    assert phones.category_slug == "telefony"
    assert phones.blob == "ASgBAgICA0SywA2SoO0RtMANzqs5sMENiPw3"


def test_listing_link_and_foreign_site_are_rejected():
    with pytest.raises(lr.LinkError):
        lr.parse_link("https://www.avito.ru/sankt-peterburg/telefony/iphone_15_128_gb_8157171618")
    with pytest.raises(lr.LinkError):
        lr.parse_link("https://example.org/kazan/telefony")


def test_filters_are_matched_by_unique_value_when_attribute_differs():
    filters = {
        "params[201]": lr.Filter("params[201]", "Тип", "select", {"1059": "Купить"}),
        "params[549]": lr.Filter("params[549]", "Количество комнат", "multiselect", {"5697": "2 комнаты"}),
        "params[110472]": lr.Filter("params[110472]", "Продавцы", "multiselect", {"437131": "Частные"}),
    }

    params, warnings = lr.filter_params([(201, 1059), (549, 5697), (110536, 437131)], filters)

    assert params == {
        "params[201]": "1059",
        "params[549][0]": "5697",
        "params[110472][0]": "437131",
    }
    assert warnings == []


def test_category_is_found_in_avito_category_tree():
    payload = {"rubricators": {"categoryTreeTop": [
        {"url": "/kazan/transport?cd=1", "categoryId": 1, "subs": [
            {"url": "/kazan/avtomobili?cd=1", "categoryId": 9, "subs": []},
        ]},
        {"url": "/kazan/telefony?cd=1", "categoryId": 84, "subs": []},
    ]}}

    assert lr.category_from_tree(payload, "kazan", "telefony") == "84"
    assert lr.category_from_tree(payload, "kazan", "avtomobili") == "9"
    assert lr.category_from_tree(payload, "kazan", "noutbuki") is None
