from collections.abc import Iterator

import httpx

from ..http import client, request_with_retry
from ..models import Filters, Listing
from ..progress import Progress
from . import matches, normalize_municipality

BASE = "https://www.etuovi.com"
PAGE_SIZE = 100
MAX_PAGES = 50

PROPERTY_TYPES = {
    "kerrostalo": "APARTMENT_HOUSE",
    "rivitalo": "ROW_HOUSE",
    "omakotitalo": "DETACHED_HOUSE",
    "erillistalo": "SEPARATE_HOUSE",
    "paritalo": "SEMI_DETACHED_HOUSE",
    "luhtitalo": "BALCONY_ACCESS_BLOCK",
    "puutalo-osake": "WOODEN_HOUSE_APARTMENT",
}
PROPERTY_TYPE_NAMES = {v: k for k, v in PROPERTY_TYPES.items()}
ROOM_COUNTS = {1: "ONE_ROOM", 2: "TWO_ROOMS", 3: "THREE_ROOMS", 4: "FOUR_ROOMS", 5: "FIVE_ROOMS"}
ROOM_COUNT_NUMBERS = {v: k for k, v in ROOM_COUNTS.items()}
PLOT_HOLDING = {"own": "OWN", "rent": "RENT"}


def build_body(f: Filters, offset: int, limit: int = PAGE_SIZE) -> dict:
    body: dict = {
        "propertyType": "RESIDENTIAL",
        "searchType": "SALE",
        "locationSearchCriteria": {"classifiedLocationTerms": [{"type": "REGION", "code": "FI_UUSIMAA"}]},
        "pagination": {
            "firstResult": offset,
            "maxResults": limit,
            "page": offset // limit + 1,
            "sortingOrder": {"property": "PUBLISHED_OR_UPDATED_AT", "direction": "DESC"},
        },
    }
    if f.price_min is not None:
        body["priceMin"] = f.price_min
    if f.price_max is not None:
        body["priceMax"] = f.price_max
    if f.size_min is not None:
        body["sizeMin"] = f.size_min
    if f.size_max is not None:
        body["sizeMax"] = f.size_max
    if f.house_types:
        body["residentialPropertyTypes"] = [PROPERTY_TYPES[t] for t in f.house_types]
    if f.rooms:
        body["roomCounts"] = [ROOM_COUNTS[r] for r in f.rooms]
    if f.plot_ownership:
        body["plotHoldingTypes"] = [PLOT_HOLDING[p] for p in f.plot_ownership]
    return body


def parse_announcement(a: dict) -> Listing:
    area_parts = (a.get("addressLine2") or "").split()
    image = a.get("mainImageUri")
    if image and not a.get("mainImageHidden"):
        image = image.replace("{imageParameters}", "480x360")
        image = "https:" + image if image.startswith("//") else image
    else:
        image = None
    return Listing(
        source="etuovi",
        id=str(a["friendlyId"]),
        url=f"{BASE}/kohde/{a['friendlyId']}",
        address=a.get("addressLine1") or "",
        municipality=normalize_municipality(area_parts[-1] if area_parts else ""),
        lat=a.get("latitude"),
        lon=a.get("longitude"),
        price=a.get("searchPrice"),
        size=a.get("area"),
        rooms=ROOM_COUNT_NUMBERS.get(a.get("roomCount")),
        house_type=PROPERTY_TYPE_NAMES.get(a.get("propertySubtype")),
        build_year=a.get("constructionFinishedYear"),
        image=image,
    )


def fetch(
    f: Filters, max_listings: int, http: httpx.Client | None = None, progress: Progress | None = None
) -> Iterator[Listing]:
    http = http or client()
    found = 0
    for page in range(MAX_PAGES):
        if progress:
            progress.check()
        body = build_body(f, page * PAGE_SIZE)
        resp = request_with_retry(
            lambda: http.post(f"{BASE}/api/v2/announcements/search/listpage", json=body, headers={"Accept": "application/json"})
        )
        resp.raise_for_status()
        announcements = resp.json().get("announcements") or []
        for a in announcements:
            listing = parse_announcement(a)
            if matches(listing, f):
                yield listing
                found += 1
                if found >= max_listings:
                    return
        if progress:
            progress.advance(message=f"page {page + 1}, {found} listings")
        if len(announcements) < PAGE_SIZE:
            return
