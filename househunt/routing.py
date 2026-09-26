import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import httpx

from .cache import Cache, coord_key
from .config import TransitSettings
from .http import TOOL_UA, RateLimiter, client, request_with_retry

log = logging.getLogger(__name__)

OSRM = "https://router.project-osrm.org"
OSRM_MAX_COORDS = 100
DIGITRANSIT = "https://api.digitransit.fi/routing/v2/{router}/gtfs/v1"
HELSINKI = ZoneInfo("Europe/Helsinki")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

Point = tuple[float, float]


class CarRouter:
    def __init__(self, cache: Cache, http: httpx.Client | None = None):
        self.cache = cache
        self.http = http or client(TOOL_UA)
        self.limiter = RateLimiter(1.0)

    @staticmethod
    def _key(o: Point, d: Point) -> str:
        return f"{coord_key(*o)}>{coord_key(*d)}"

    def minutes(self, origins: list[Point], dests: list[Point]) -> dict[tuple[Point, Point], float | None]:
        missing = [o for o in dict.fromkeys(origins) if any(not self.cache.has("car", self._key(o, d)) for d in dests)]
        chunk = OSRM_MAX_COORDS - len(dests)
        for i in range(0, len(missing), chunk):
            self._fetch(missing[i : i + chunk], dests)
        return {(o, d): self.cache.get("car", self._key(o, d)) for o in origins for d in dests}

    def _fetch(self, origins: list[Point], dests: list[Point]) -> None:
        coords = ";".join(f"{lon},{lat}" for lat, lon in origins + dests)
        src = ";".join(str(i) for i in range(len(origins)))
        dst = ";".join(str(len(origins) + j) for j in range(len(dests)))
        self.limiter.wait()
        resp = request_with_retry(
            lambda: self.http.get(
                f"{OSRM}/table/v1/driving/{coords}",
                params={"sources": src, "destinations": dst, "annotations": "duration"},
            )
        )
        resp.raise_for_status()
        durations = resp.json()["durations"]
        for i, o in enumerate(origins):
            for j, d in enumerate(dests):
                sec = durations[i][j]
                self.cache.set("car", self._key(o, d), round(sec / 60, 1) if sec is not None else None)


PLAN_QUERY = """
query($from: PlanCoordinateInput!, $to: PlanCoordinateInput!, $dateTime: PlanDateTimeInput!) {
  planConnection(origin: {location: {coordinate: $from}}, destination: {location: {coordinate: $to}},
                 dateTime: $dateTime, first: 3) {
    edges { node { duration legs { mode } } }
  }
}
"""


def target_datetime(settings: TransitSettings, today: dt.date | None = None) -> tuple[str, str]:
    today = today or dt.datetime.now(HELSINKI).date()
    weekday = WEEKDAYS.index(settings.day)
    days_ahead = (weekday - today.weekday()) % 7 or 7
    day = today + dt.timedelta(days=days_ahead)
    arrive = settings.arrive_by is not None
    hhmm = settings.arrive_by if arrive else settings.depart_at
    hour, minute = (int(x) for x in hhmm.split(":"))
    when = dt.datetime.combine(day, dt.time(hour, minute), HELSINKI).isoformat()
    return ("latestArrival" if arrive else "earliestDeparture"), when


class TransitRouter:
    def __init__(self, settings: TransitSettings, cache: Cache, http: httpx.Client | None = None):
        if not settings.api_key:
            raise ValueError("Transit routing needs a Digitransit API key")
        self.settings = settings
        self.cache = cache
        self.http = http or client(TOOL_UA)
        self.http.headers["digitransit-subscription-key"] = settings.api_key
        self.limiter = RateLimiter(settings.requests_per_second)
        mode = "arrive" if settings.arrive_by else "depart"
        self.cache_ns = f"transit:{mode}:{settings.day}:{settings.arrive_by or settings.depart_at}"

    def _query(self, router: str, o: Point, d: Point) -> float | None:
        kind, when = target_datetime(self.settings)
        variables = {
            "from": {"latitude": o[0], "longitude": o[1]},
            "to": {"latitude": d[0], "longitude": d[1]},
            "dateTime": {kind: when},
        }
        self.limiter.wait()
        resp = request_with_retry(
            lambda: self.http.post(DIGITRANSIT.format(router=router), json={"query": PLAN_QUERY, "variables": variables})
        )
        if resp.status_code in (401, 403):
            raise RuntimeError("Digitransit rejected the API key (HTTP %d)" % resp.status_code)
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            log.warning("Digitransit %s error for %s -> %s: %s", router, o, d, body["errors"][0].get("message"))
            return None
        edges = ((body.get("data") or {}).get("planConnection") or {}).get("edges") or []
        durations = [e["node"]["duration"] for e in edges if e.get("node")]
        return round(min(durations) / 60, 1) if durations else None

    def minutes_one(self, o: Point, d: Point) -> float | None:
        key = f"{coord_key(*o)}>{coord_key(*d)}"
        if self.cache.has(self.cache_ns, key):
            return self.cache.get(self.cache_ns, key)
        result = self._query(self.settings.router, o, d)
        if result is None and self.settings.router != "finland":
            result = self._query("finland", o, d)
        self.cache.set(self.cache_ns, key, result)
        return result

    def minutes(self, pairs: list[tuple[Point, Point]], workers: int = 4) -> dict[tuple[Point, Point], float | None]:
        unique = list(dict.fromkeys(pairs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda p: self.minutes_one(*p), unique))
        return dict(zip(unique, results))
