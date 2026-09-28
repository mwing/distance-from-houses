import re
from collections.abc import Iterable

from ..models import Filters, Listing


def parse_number(text) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d\s ]*(?:[.,]\d+)?", str(text))
    if not m:
        return None
    return float(re.sub(r"[\s ]", "", m.group(0)).replace(",", "."))


def normalize_municipality(name: str) -> str:
    name = name.strip()
    return name.capitalize() if name.isupper() else name


def matches(listing: Listing, f: Filters) -> bool:
    if f.municipalities and listing.municipality.lower() not in f.municipalities:
        return False
    if f.house_types and listing.house_type and listing.house_type not in f.house_types:
        return False
    if f.rooms and listing.rooms is not None:
        wanted = listing.rooms if listing.rooms < 5 else 5
        if wanted not in f.rooms:
            return False
    if listing.price is not None:
        if f.price_min is not None and listing.price < f.price_min:
            return False
        if f.price_max is not None and listing.price > f.price_max:
            return False
    if listing.build_year is not None:
        if f.build_year_min is not None and listing.build_year < f.build_year_min:
            return False
        if f.build_year_max is not None and listing.build_year > f.build_year_max:
            return False
    if listing.size is not None:
        if f.size_min is not None and listing.size < f.size_min:
            return False
        if f.size_max is not None and listing.size > f.size_max:
            return False
    return True


def _dedupe_key(l: Listing):
    street = " ".join(l.address.lower().split()[:2])
    return (street, l.municipality.lower(), l.price, round(l.size or 0))


def dedupe(listings: Iterable[Listing]) -> list[Listing]:
    seen: dict[tuple, Listing] = {}
    out: list[Listing] = []
    for l in listings:
        key = _dedupe_key(l)
        if l.price is not None and key in seen:
            first = seen[key]
            if l.url != first.url and l.url not in first.other_urls:
                first.other_urls.append(l.url)
            continue
        seen[key] = l
        out.append(l)
    return out
