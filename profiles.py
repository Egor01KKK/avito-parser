"""Search profiles: how listings of one kind are judged, tabulated and alerted.

* ``realty-owner`` — flats: owner/agent verdict, draft message for the owner;
  alerts only for owners' listings.
* ``goods`` — things like phones: model + storage groups, market price;
  alerts for every new listing with its price against the market.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import apartments
import goods


# --- realty-owner -----------------------------------------------------------

def price_text(value: int | None) -> str:
    return f"{value:,} ₽".replace(",", " ") if value else "цена не указана"


def short_address(listing: apartments.Listing) -> str:
    return listing.address.removeprefix("Санкт-Петербург, ").strip()


def draft_message(listing: apartments.Listing) -> str:
    """A first message the realtor can copy into Avito chat and edit.

    It asks about this particular flat and offers something useful; it does
    not presume the owner has a problem and does not invent a buyer.
    """
    place = short_address(listing) or "вашей квартиры"
    flat = f"2-комнатной квартиры по адресу {place}" if listing.address else "2-комнатной квартиры"
    if listing.pays_agent:
        return (
            f"Здравствуйте! Увидела ваше объявление о продаже {flat}. "
            "Вы писали, что готовы работать с агентом, который приведёт покупателя. "
            "Я риэлтор, работаю с вторичкой в этом районе. Квартира ещё продаётся? "
            "Удобно созвониться, чтобы я задала пару вопросов по квартире?"
        )
    if listing.asks_no_agents:
        return (
            f"Здравствуйте! Увидела ваше объявление о продаже {flat}. "
            "Понимаю, что вы продаёте сами, и навязываться не буду. "
            "Если захотите сверить цену с недавними сделками рядом или появится "
            "вопрос по документам — напишите, подскажу бесплатно."
        )
    return (
        f"Здравствуйте! Увидела ваше объявление о продаже {flat} "
        f"за {price_text(listing.price)}. Я риэлтор, работаю с вторичкой в этом районе. "
        "Квартира ещё продаётся? Если интересно, расскажу, за сколько сейчас "
        "реально уходят похожие квартиры рядом, — без обязательств."
    )


def alert_fields(listing: apartments.Listing) -> dict[str, Any]:
    notes = []
    if listing.pays_agent:
        notes.append("готов платить агенту за покупателя")
    if listing.asks_no_agents:
        notes.append("просит агентов не беспокоить")
    if listing.has_own_agent:
        notes.append("у собственника уже есть свой агент")
    if not listing.calls_allowed:
        notes.append("звонки отключены, только чат")
    return {
        "id": listing.id,
        "title": listing.title,
        "price": price_text(listing.price),
        "price_per_m2": price_text(listing.price_per_meter),
        "address": short_address(listing),
        "metro": listing.metro,
        "published": listing.published,
        "verdict": listing.verdict,
        "reasons": listing.reasons,
        "notes": notes,
        "url": listing.url,
        "draft": draft_message(listing),
    }


class RealtyOwnerProfile:
    name = "realty-owner"

    def __init__(self, base: dict[str, Any], home_city: str = "") -> None:
        self.base = base

    def alert(self, item: dict[str, Any]) -> dict[str, Any] | None:
        listing = apartments.classify(apartments.listing_from_item(item))
        if listing.verdict == apartments.VERDICT_AGENT:
            return None
        return alert_fields(listing)

    def write_table(self, path: Path, seen_before: dict[str, str]) -> dict[str, int]:
        listings = [
            apartments.classify(apartments.listing_from_item(item))
            for item in self.base.values()
        ]
        for listing in listings:
            listing.is_new = str(listing.id) not in seen_before
        apartments.write_workbook(path, listings)
        return {
            verdict: sum(listing.verdict == verdict for listing in listings)
            for verdict in (apartments.VERDICT_OWNER, apartments.VERDICT_CHECK, apartments.VERDICT_AGENT)
        }


# --- goods --------------------------------------------------------------------

class GoodsProfile:
    name = "goods"

    def __init__(self, base: dict[str, Any], home_city: str = "") -> None:
        self.base = base
        self.home_city = home_city
        self.refresh_market()

    def refresh_market(self) -> None:
        self.stats = goods.market_stats([goods.offer_from_item(item) for item in self.base.values()])

    def alert(self, item: dict[str, Any]) -> dict[str, Any] | None:
        offer = goods.offer_from_item(item)
        # Listings from other cities come with delivery and flood the feed;
        # they stay in the table but do not alert.
        if self.home_city and offer.city and offer.city != self.home_city:
            return None
        goods.annotate([offer], self.stats, self.home_city)
        return goods.alert_fields(offer)

    def write_table(self, path: Path, seen_before: dict[str, str]) -> dict[str, int]:
        offers = [goods.offer_from_item(item) for item in self.base.values()]
        stats = goods.market_stats(offers)
        goods.annotate(offers, stats, self.home_city)
        for offer in offers:
            offer.listing.is_new = str(offer.listing.id) not in seen_before
        goods.write_workbook(path, offers, stats)
        return {"объявлений": len(offers), "групп с рыночной ценой": len(stats)}


PROFILES = {profile.name: profile for profile in (RealtyOwnerProfile, GoodsProfile)}
