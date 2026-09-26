import httpx

from .cache import Cache, coord_key
from .http import TOOL_UA, RateLimiter, client
from .models import UUSIMAA_MUNICIPALITIES, Destination

NOMINATIM = "https://nominatim.openstreetmap.org"
UUSIMAA_ISO = "FI-18"


def _is_uusimaa(address: dict) -> bool:
    if address.get("ISO3166-2-lvl4") == UUSIMAA_ISO:
        return True
    town = (address.get("city") or address.get("town") or address.get("village") or "").lower()
    return town in UUSIMAA_MUNICIPALITIES


class Geocoder:
    def __init__(self, cache: Cache, http: httpx.Client | None = None):
        self.cache = cache
        self.http = http or client(TOOL_UA)
        # Nominatim usage policy: at most 1 request per second.
        self.limiter = RateLimiter(1.0)

    def _get(self, path: str, params: dict) -> list | dict:
        self.limiter.wait()
        resp = self.http.get(f"{NOMINATIM}/{path}", params={**params, "format": "jsonv2", "addressdetails": 1})
        resp.raise_for_status()
        return resp.json()

    def resolve(self, dest: Destination) -> Destination:
        if dest.lat is None or dest.lon is None:
            key = dest.address.strip().lower()
            hit = self.cache.get("geocode", key)
            if hit is None:
                results = self._get("search", {"q": dest.address, "limit": 1, "countrycodes": "fi"})
                if not results:
                    raise ValueError(f"Could not geocode destination {dest.name!r}: {dest.address!r}")
                r = results[0]
                hit = {"lat": float(r["lat"]), "lon": float(r["lon"]), "uusimaa": _is_uusimaa(r.get("address") or {})}
                self.cache.set("geocode", key, hit)
        else:
            key = coord_key(dest.lat, dest.lon)
            hit = self.cache.get("reverse", key)
            if hit is None:
                r = self._get("reverse", {"lat": dest.lat, "lon": dest.lon, "zoom": 10})
                hit = {"lat": dest.lat, "lon": dest.lon, "uusimaa": _is_uusimaa(r.get("address") or {})}
                self.cache.set("reverse", key, hit)
        dest.lat, dest.lon, dest.in_uusimaa = hit["lat"], hit["lon"], hit["uusimaa"]
        return dest
