import datetime as dt
import logging
import math

import httpx

from .cache import Cache
from .http import TOOL_UA, client, request_with_retry, shared_limiter
from .models import UUSIMAA_BBOX

log = logging.getLogger(__name__)

OVERPASS = "https://overpass-api.de/api/interpreter"
CACHE_NS = "services"
CACHE_KEY = "uusimaa:v1"
MAX_AGE = dt.timedelta(days=7)
KINDS = {
    "daycare": ("amenity", "kindergarten"),
    "school": ("amenity", "school"),
    "grocery": ("shop", "supermarket"),
}


def _query() -> str:
    south, west, north, east = UUSIMAA_BBOX
    bbox = f"({south},{west},{north},{east})"
    parts = "".join(f'nwr["{k}"="{v}"]{bbox};' for k, v in KINDS.values())
    return f"[out:json][timeout:120];({parts});out center tags;"


def _parse(elements: list[dict]) -> dict[str, list[list]]:
    out: dict[str, list[list]] = {kind: [] for kind in KINDS}
    for e in elements:
        tags = e.get("tags") or {}
        lat = e.get("lat", (e.get("center") or {}).get("lat"))
        lon = e.get("lon", (e.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        for kind, (k, v) in KINDS.items():
            if tags.get(k) == v:
                out[kind].append([lat, lon, tags.get("name") or ""])
    return out


class Services:
    def __init__(self, cache: Cache, http: httpx.Client | None = None):
        self.cache = cache
        self.http = http or client(TOOL_UA, timeout=180)
        self.limiter = shared_limiter("overpass", 0.2)

    def points(self) -> dict[str, list[list]] | None:
        hit = self.cache.get(CACHE_NS, CACHE_KEY)
        now = dt.datetime.now(dt.timezone.utc)
        if hit and now - dt.datetime.fromisoformat(hit["fetched_at"]) < MAX_AGE:
            return hit["points"]
        try:
            self.limiter.wait()
            resp = request_with_retry(lambda: self.http.post(OVERPASS, data={"data": _query()}), attempts=4)
            resp.raise_for_status()
            points = _parse(resp.json().get("elements") or [])
        except Exception as e:
            log.warning("Overpass failed (%s); %s", e, "using the previous copy" if hit else "skipping nearby services")
            return hit["points"] if hit else None
        self.cache.set(CACHE_NS, CACHE_KEY, {"fetched_at": now.isoformat(), "points": points})
        log.info("Nearby services: %s", ", ".join(f"{len(v)} {k}" for k, v in points.items()))
        return points


def nearest(lat: float, lon: float, points: list[list]) -> dict | None:
    best, best_d = None, math.inf
    kx = math.cos(math.radians(lat)) * 111_320
    ky = 110_570
    for plat, plon, name in points:
        d = math.hypot((plon - lon) * kx, (plat - lat) * ky)
        if d < best_d:
            best, best_d = name, d
    return {"m": round(best_d / 10) * 10, "name": best} if best is not None else None
