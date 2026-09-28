import json
import re
from collections.abc import Iterator

import httpx

from ..http import client, request_with_retry
from ..models import Filters, Listing
from ..progress import Progress
from . import matches, normalize_municipality, parse_number

BASE = "https://asunnot.oikotie.fi"
UUSIMAA = [[2, 7, "Uusimaa"]]
PAGE_SIZE = 100
MAX_PAGES = 50

BUILDING_TYPES = {
    "kerrostalo": 1,
    "rivitalo": 2,
    "omakotitalo": 4,
    "erillistalo": 32,
    "paritalo": 64,
    "luhtitalo": 256,
    "puutalo-osake": 512,
}
BUILDING_TYPE_NAMES = {v: k for k, v in BUILDING_TYPES.items()}
LOT_OWNERSHIP = {"own": 1, "rent": 2}


def _auth_headers(http: httpx.Client) -> dict[str, str]:
    html = http.get(f"{BASE}/myytavat-asunnot").text
    headers = {}
    for meta, header in (("api-token", "OTA-token"), ("loaded", "OTA-loaded"), ("cuid", "OTA-cuid")):
        m = re.search(rf'<meta name="{meta}" content="([^"]*)"', html)
        if not m:
            raise RuntimeError(f"Oikotie: could not find <meta name={meta!r}>; the site layout may have changed")
        headers[header] = m.group(1)
    return headers


def build_params(f: Filters, offset: int, limit: int = PAGE_SIZE) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [
        ("cardType", "100"),
        ("limit", str(limit)),
        ("offset", str(offset)),
        ("sortBy", "published_sort_desc"),
        ("locations", json.dumps(UUSIMAA, separators=(",", ":"), ensure_ascii=False)),
    ]
    if f.price_min is not None:
        params.append(("price[min]", str(f.price_min)))
    if f.price_max is not None:
        params.append(("price[max]", str(f.price_max)))
    if f.size_min is not None:
        params.append(("size[min]", str(f.size_min)))
    if f.size_max is not None:
        params.append(("size[max]", str(f.size_max)))
    if f.build_year_min is not None:
        params.append(("constructionYear[min]", str(f.build_year_min)))
    if f.build_year_max is not None:
        params.append(("constructionYear[max]", str(f.build_year_max)))
    for r in f.rooms:
        counts = range(5, 8) if r == 5 else [r]
        params.extend(("roomCount[]", str(c)) for c in counts)
    for t in f.house_types:
        params.append(("buildingType[]", str(BUILDING_TYPES[t])))
    for p in f.plot_ownership:
        params.append(("lotOwnershipType[]", str(LOT_OWNERSHIP[p])))
    return params


def parse_card(card: dict) -> Listing:
    data = card.get("data") or {}
    loc = card.get("location") or {}
    medias = card.get("medias") or []
    size = data.get("sizeMin")
    if size is None:
        size = parse_number(data.get("size"))
    return Listing(
        source="oikotie",
        id=str(card["cardId"]),
        url=card.get("url") or f"{BASE}/myytavat-asunnot/{card['cardId']}",
        address=loc.get("address") or "",
        municipality=normalize_municipality(loc.get("city") or ""),
        lat=loc.get("latitude"),
        lon=loc.get("longitude"),
        price=parse_number(data.get("price")),
        size=float(size) if size is not None else None,
        rooms=data.get("rooms"),
        house_type=BUILDING_TYPE_NAMES.get(card.get("cardSubType")),
        build_year=data.get("buildYear"),
        image=medias[0].get("imageLargeJPEG") if medias else None,
    )


def fetch(
    f: Filters, max_listings: int, http: httpx.Client | None = None, progress: Progress | None = None
) -> Iterator[Listing]:
    http = http or client()
    headers = _auth_headers(http)
    found = 0
    for page in range(MAX_PAGES):
        if progress:
            progress.check()
        resp = request_with_retry(
            lambda: http.get(f"{BASE}/api/search", params=build_params(f, page * PAGE_SIZE), headers=headers)
        )
        resp.raise_for_status()
        cards = resp.json().get("cards") or []
        for card in cards:
            listing = parse_card(card)
            if matches(listing, f):
                yield listing
                found += 1
                if found >= max_listings:
                    return
        if progress:
            progress.advance(message=f"page {page + 1}, {found} listings")
        if len(cards) < PAGE_SIZE:
            return
