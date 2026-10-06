"""Turn an Avito search link into the items API parameters.

A search link looks like

    https://www.avito.ru/kazan/telefony/mobilnye_telefony/apple/iphone_15-ASgB...?f=ASgB...&q=...

* the first path segment is the city slug; Avito's location suggest only
  understands Cyrillic names, so the slug is turned back into the start of a
  Russian name, candidates are fetched and the one whose transliteration
  equals the slug wins (results are cached in data/locations.json);
* the second segment is the category slug, matched against the category
  tree Avito sends with every catalog answer;
* the filters are a base64 blob (the ``f`` query value, or the tail of the
  last path segment after "-") holding zigzag-encoded varint pairs
  "attribute -> value". The attribute number is usually the API's
  ``params[<id>]``, but not always, so each value is looked up among the
  category's filters, where value ids are unique.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

import avito_client
import main

LOCATION_SUGGEST_URL = f"{main.BASE_URL}/web/1/slocations"
REALTY_CATEGORY_IDS = {"23", "24", "25", "26", "42", "85", "86"}
# Real-estate subsections are collapsed in Avito's category tree.
KNOWN_CATEGORIES = {
    "kvartiry": "24", "komnaty": "23", "doma_dachi_kottedzhi": "25",
    "zemelnye_uchastki": "26", "garazhi_i_mashinomesta": "85",
    "kommercheskaya_nedvizhimost": "42", "nedvizhimost_za_rubezhom": "86",
}
# Any category id makes Avito include its full category tree in the answer.
TREE_PROBE_CATEGORY = "84"
MAX_FILTER_ROUNDS = 4
# Query parameters of a site link that the items API understands as is.
PASSTHROUGH_QUERY = {"q", "user", "pmin", "pmax", "d", "radius", "bt", "footWalkingMetro", "localPriority"}
MULTI_VALUE_TYPES = {"multiselect", "sectionedMultiselect", "checkboxGroup"}

CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", " ": "_", "-": "-",
}
LAT_TO_CYR = (
    ("sch", "щ"), ("zh", "ж"), ("ch", "ч"), ("sh", "ш"), ("ts", "ц"), ("yu", "ю"),
    ("ya", "я"), ("yo", "йо"), ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"),
    ("d", "д"), ("e", "е"), ("z", "з"), ("i", "и"), ("y", "й"), ("k", "к"), ("l", "л"),
    ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"),
    ("u", "у"), ("f", "ф"), ("h", "х"), ("c", "ц"),
)


class LinkError(ValueError):
    """The link cannot be turned into a search; the message says why."""


def transliterate(name: str) -> str:
    """Avito's slug for a Russian place name, e.g. "Нижний Новгород" -> nizhniy_novgorod."""
    return "".join(CYR_TO_LAT.get(char, char) for char in name.lower())


def slug_prefix_in_cyrillic(slug: str, letters: int) -> str:
    """The first ``letters`` Cyrillic letters a slug most likely came from."""
    word = re.split(r"[_-]", slug)[0]
    result = ""
    position = 0
    while position < len(word) and len(result) < letters:
        for latin, cyrillic in LAT_TO_CYR:
            if word.startswith(latin, position):
                result += cyrillic
                position += len(latin)
                break
        else:
            position += 1
    return result[:letters]


def decode_filter_blob(blob: str) -> list[tuple[int, int]]:
    """Attribute/value pairs from an ``f`` blob such as ``ASgBAgICAkSSA8YQygiCWQ``."""
    padded = blob.replace("~", "/").replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        data = base64.b64decode(padded)
    except ValueError as exc:
        raise LinkError(f"фильтры в ссылке не читаются: {exc}") from None
    numbers = []
    position = 0
    while position < len(data):
        value = shift = 0
        while True:
            if position >= len(data):
                raise LinkError("фильтры в ссылке обрываются")
            byte = data[position]
            position += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        numbers.append(value)
    # Header: 1, 40, 1, 2, 2, 2, <pair count>, 68, then the pairs.
    if len(numbers) < 8 or numbers[7] != 68:
        raise LinkError("незнакомый формат фильтров в ссылке")
    count = numbers[6]
    raw = numbers[8:8 + 2 * count]
    if len(raw) != 2 * count:
        raise LinkError("в ссылке меньше фильтров, чем заявлено")
    return [(raw[i] // 2, raw[i + 1] // 2) for i in range(0, len(raw), 2)]


@dataclass
class ParsedLink:
    url: str
    city_slug: str
    category_slug: str
    blob: str
    query: dict[str, str]
    path_slugs: tuple[str, ...] = ()


def parse_link(url: str) -> ParsedLink:
    parts = urlsplit(url.strip())
    if not parts.netloc.endswith("avito.ru"):
        raise LinkError("это не ссылка на avito.ru")
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 1:
        raise LinkError("в ссылке нет города: нужна страница поиска, например avito.ru/kazan/telefony")
    query = dict(parse_qsl(parts.query))
    blob = query.get("f", "")
    if not blob and len(segments) > 1:
        tail = segments[-1].rsplit("-", 1)
        if len(tail) == 2 and tail[1].startswith("ASg"):
            blob = tail[1]
    category_slug = segments[1].split("-ASg")[0] if len(segments) > 1 else ""
    if re.fullmatch(r".*_\d{6,}", segments[-1]):
        raise LinkError("это ссылка на одно объявление, а нужна ссылка на страницу поиска")
    return ParsedLink(
        url=url.strip(),
        city_slug=segments[0],
        category_slug=category_slug,
        blob=blob,
        query=query,
        path_slugs=tuple(segment.split("-ASg")[0] for segment in segments[1:]),
    )


def resolve_location(client: avito_client.CalmClient, slug: str, cache: dict[str, Any], *, referer: str) -> tuple[int, str]:
    """Location id and name for a city slug, asking Avito's suggest."""
    if slug in cache:
        return int(cache[slug]["id"]), cache[slug]["name"]
    for letters in (5, 4, 3):
        prefix = slug_prefix_in_cyrillic(slug, letters)
        if not prefix:
            continue
        payload = client.get_json(
            f"{LOCATION_SUGGEST_URL}?limit=30&q={quote(prefix)}",
            catalog_url=referer,
            context="location suggest",
        )
        for location in (payload.get("result") or {}).get("locations") or []:
            name = (location.get("names") or {}).get("1") or ""
            if transliterate(name) == slug:
                cache[slug] = {"id": location["id"], "name": name}
                return int(location["id"]), name
    raise LinkError(
        f"не нашёл город «{slug}». Укажите его явно: --city \"Название города\""
    )


def resolve_city_name(client: avito_client.CalmClient, name: str, *, referer: str) -> tuple[int, str]:
    payload = client.get_json(
        f"{LOCATION_SUGGEST_URL}?limit=10&q={quote(name)}", catalog_url=referer, context="location suggest"
    )
    for location in (payload.get("result") or {}).get("locations") or []:
        found = (location.get("names") or {}).get("1") or ""
        if found.lower() == name.strip().lower():
            return int(location["id"]), found
    raise LinkError(f"Авито не знает город «{name}»")


def category_slugs(payload: dict[str, Any]) -> dict[str, str]:
    """Category slug -> categoryId from the tree in a catalog answer."""
    found: dict[str, str] = {}
    nodes = list((payload.get("rubricators") or {}).get("categoryTreeTop") or [])
    while nodes:
        node = nodes.pop()
        nodes.extend(node.get("subs") or [])
        segments = [s for s in urlsplit(node.get("url") or "").path.split("/") if s]
        if len(segments) >= 2 and node.get("categoryId"):
            found.setdefault(segments[1].split("-ASg")[0], str(node["categoryId"]))
    return found


def category_from_tree(payload: dict[str, Any], city_slug: str, category_slug: str) -> str | None:
    """The categoryId whose page is /<city>/<category_slug> in Avito's category tree."""
    return category_slugs(payload).get(category_slug)


def resolve_category(
    client: avito_client.CalmClient, slug: str, location_id: int, cache: dict[str, Any], *, referer: str
) -> str:
    if slug in KNOWN_CATEGORIES:
        return KNOWN_CATEGORIES[slug]
    tree = cache.setdefault("categories", {})
    if slug not in tree:
        payload = client.page_payload(
            catalog_url=referer,
            params={"categoryId": TREE_PROBE_CATEGORY, "locationId": str(location_id)},
            number=1,
        )
        tree.update(category_slugs(payload))
    if slug not in tree:
        raise LinkError(f"не нашёл категорию «{slug}» в рубрикаторе Авито")
    return tree[slug]


@dataclass
class Filter:
    id: str
    title: str
    type: str
    values: dict[str, str] = field(default_factory=dict)


def filters_of(payload: dict[str, Any]) -> dict[str, Filter]:
    """Every filter Avito offers for the category, by its parameter id."""
    found: dict[str, Filter] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), str) and "type" in node and node["id"] not in found:
                values = {
                    str(value.get("value")): str(value.get("name"))
                    for value in node.get("values") or []
                    if isinstance(value, dict)
                }
                found[node["id"]] = Filter(
                    id=node["id"], title=node.get("defaultTitle") or node.get("title") or node["id"],
                    type=str(node.get("type")), values=values,
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload.get("filtersV2"))
    return found


def value_by_slug(f: Filter, slugs: tuple[str, ...]) -> str | None:
    """The filter value whose name is spelled in the link path, e.g. "512 ГБ" ~ 512_gb."""
    for value, name in f.values.items():
        if transliterate(name) in slugs:
            return value
    return None


def filter_params(
    pairs: list[tuple[int, int]],
    filters: dict[str, Filter],
    slugs: tuple[str, ...] = (),
) -> tuple[dict[str, str], list[str]]:
    """API parameters for decoded pairs, plus warnings about unknown ones.

    A value is matched directly, then by its unique id among all filters,
    then — when the link encodes a value id the API does not use (memory
    "512 ГБ" is 757887 in a link and 757885 in the API) — by the value's
    name spelled in the link path. A value that matches nothing in a filter
    Avito does offer is left out with a warning: passed blindly it would make
    the search return nothing.
    """
    by_value = {value: f for f in filters.values() for value in f.values}
    chosen: dict[str, list[str]] = {}
    warnings = []
    for attribute, value in pairs:
        direct = filters.get(f"params[{attribute}]")
        if direct and (not direct.values or str(value) in direct.values):
            target, chosen_value = direct, str(value)
        elif str(value) in by_value:
            target, chosen_value = by_value[str(value)], str(value)
        elif direct and value_by_slug(direct, slugs):
            target, chosen_value = direct, value_by_slug(direct, slugs)
        elif direct:
            warnings.append(
                f"фильтр «{direct.title}» из ссылки не распознан и пропущен; "
                f"доступные значения: {', '.join(direct.values.values())}"
            )
            continue
        else:
            target, chosen_value = Filter(id=f"params[{attribute}]", title=f"params[{attribute}]", type="select"), str(value)
            warnings.append(f"фильтр {attribute}={value} не найден среди фильтров категории; передаю как есть")
        chosen.setdefault(target.id, []).append(chosen_value)
    params: dict[str, str] = {}
    for filter_id, values in chosen.items():
        kind = filters[filter_id].type if filter_id in filters else "select"
        if kind in MULTI_VALUE_TYPES or len(values) > 1:
            for index, value in enumerate(values):
                params[f"{filter_id}[{index}]"] = value
        else:
            params[filter_id] = values[0]
    return params, warnings


def applied_filters(payload: dict[str, Any]) -> list[str]:
    """Human-readable filters Avito reports as active, e.g. "Количество комнат: 2 комнаты"."""
    lines = []
    for f in filters_of(payload).values():
        current = None

        def find(node: Any) -> None:
            nonlocal current
            if isinstance(node, dict):
                if node.get("id") == f.id and "currentValue" in node:
                    current = node["currentValue"]
                    return
                for value in node.values():
                    find(value)
            elif isinstance(node, list):
                for value in node:
                    find(value)

        find(payload.get("filtersV2"))
        if current in (None, "", [], False, 0, "0", "default", "101") or current == {"from": None, "to": None}:
            continue
        if f.id in ("categoryId",):
            continue
        values = current if isinstance(current, list) else [current]
        if isinstance(current, dict):
            shown = f"от {current.get('from') or '…'} до {current.get('to') or '…'}"
        else:
            shown = ", ".join(f.values.get(str(v), str(v)) for v in values)
        lines.append(shown if f.title.startswith("params[") else f"{f.title}: {shown}")
    return lines


@dataclass
class ResolvedSearch:
    title: str
    profile: str
    catalog_url: str
    params: dict[str, str]
    city: str
    total: int
    filters: list[str]
    samples: list[str]
    warnings: list[str]

    def config(self, watch_pages: int = 1) -> dict[str, Any]:
        return {
            "title": self.title,
            "profile": self.profile,
            "catalog_url": self.catalog_url,
            "params": self.params,
            "watch_pages": watch_pages,
            "source_link": self.catalog_url,
        }


def resolve(
    client: avito_client.CalmClient,
    url: str,
    *,
    locations_cache: dict[str, Any],
    city: str | None = None,
    profile: str | None = None,
) -> ResolvedSearch:
    """Everything needed to save a search for ``url``, checked against Avito."""
    link = parse_link(url)
    referer = link.url
    if city:
        location_id, city_name = resolve_city_name(client, city, referer=referer)
    else:
        cities = locations_cache.setdefault("cities", {})
        location_id, city_name = resolve_location(client, link.city_slug, cities, referer=referer)
    params: dict[str, str] = {"locationId": str(location_id)}
    for key, value in link.query.items():
        if key in PASSTHROUGH_QUERY or key.startswith("params["):
            params[key] = value

    category_id = None
    if link.category_slug:
        category_id = resolve_category(
            client, link.category_slug, location_id, locations_cache, referer=referer
        )
        params = {"categoryId": category_id, **params}

    warnings: list[str] = []
    check: dict[str, Any] | None = None
    if link.blob:
        pairs = decode_filter_blob(link.blob)
        # Some filters appear only once others are chosen (rooms and sellers
        # exist for "Купить", not for the category as a whole), so map, apply
        # what is known, ask for the filter list again and map the rest.
        extra: dict[str, str] = {}
        for _ in range(MAX_FILTER_ROUNDS):
            check = client.page_payload(catalog_url=referer, params={**params, **extra}, number=1)
            mapped, warnings = filter_params(pairs, filters_of(check), link.path_slugs)
            if mapped == extra:
                break
            extra = mapped
            check = None
        params.update(extra)

    if check is None:
        check = client.page_payload(catalog_url=referer, params=params, number=1)
    page = avito_client.page_from_payload(check, 1)
    seo = check.get("seo") or {}
    title = str(seo.get("h1") or seo.get("title") or link.url).split("|")[0].strip()
    chosen_profile = profile or ("realty-owner" if category_id in REALTY_CATEGORY_IDS else "goods")
    return ResolvedSearch(
        title=title,
        profile=chosen_profile,
        catalog_url=link.url,
        params=params,
        city=city_name,
        total=page.total_count,
        filters=([f"Запрос: «{params['q']}»"] if params.get("q") else []) + applied_filters(check),
        samples=[str(item.get("title") or "") for item in page.items[:5]],
        warnings=warnings,
    )
