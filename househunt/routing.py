import datetime as dt
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import httpx

from .cache import Cache, coord_key
from .config import WEEKDAYS, TransitSettings
from .http import TOOL_UA, client, request_with_retry, shared_limiter
from .progress import Progress

log = logging.getLogger(__name__)

OSRM_MAX_COORDS = 100
DIGITRANSIT = "https://api.digitransit.fi/routing/v2/{router}/gtfs/v1"
HELSINKI = ZoneInfo("Europe/Helsinki")

Point = tuple[float, float]


OSRM_TABLES = {
    "car": ("https://router.project-osrm.org/table/v1/driving", "osrm"),
    "bike": ("https://routing.openstreetmap.de/routed-bike/table/v1/driving", "fossgis"),
    "walk": ("https://routing.openstreetmap.de/routed-foot/table/v1/driving", "fossgis"),
}


class TableRouter:
    """Free-flow OSRM durations; the cache namespace is the mode name."""

    def __init__(self, mode: str, cache: Cache, http: httpx.Client | None = None):
        self.mode = mode
        self.url, limiter = OSRM_TABLES[mode]
        self.cache = cache
        self.http = http or client(TOOL_UA)
        self.limiter = shared_limiter(limiter, 1.0)

    @staticmethod
    def _key(o: Point, d: Point) -> str:
        return f"{coord_key(*o)}>{coord_key(*d)}"

    def minutes(
        self, origins: list[Point], dests: list[Point], progress: Progress | None = None
    ) -> dict[tuple[Point, Point], float | None]:
        missing = [o for o in dict.fromkeys(origins) if any(not self.cache.has(self.mode, self._key(o, d)) for d in dests)]
        chunk = OSRM_MAX_COORDS - len(dests)
        batches = [missing[i : i + chunk] for i in range(0, len(missing), chunk)]
        if progress:
            progress.start(self.mode, len(batches))
        for batch in batches:
            if progress:
                progress.check()
            self._fetch(batch, dests)
            if progress:
                progress.advance()
        return {(o, d): self.cache.get(self.mode, self._key(o, d)) for o in origins for d in dests}

    def _fetch(self, origins: list[Point], dests: list[Point]) -> None:
        coords = ";".join(f"{lon},{lat}" for lat, lon in origins + dests)
        src = ";".join(str(i) for i in range(len(origins)))
        dst = ";".join(str(len(origins) + j) for j in range(len(dests)))
        self.limiter.wait()
        resp = request_with_retry(
            lambda: self.http.get(
                f"{self.url}/{coords}",
                params={"sources": src, "destinations": dst, "annotations": "duration"},
            )
        )
        resp.raise_for_status()
        durations = resp.json()["durations"]
        for i, o in enumerate(origins):
            for j, d in enumerate(dests):
                sec = durations[i][j]
                self.cache.set(self.mode, self._key(o, d), round(sec / 60, 1) if sec is not None else None)


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
        self.limiter = shared_limiter("digitransit", settings.requests_per_second)
        self.fallbacks = 0
        self._fallback_lock = threading.Lock()
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
            with self._fallback_lock:
                self.fallbacks += 1
            result = self._query("finland", o, d)
        self.cache.set(self.cache_ns, key, result)
        return result

    def is_cached(self, o: Point, d: Point) -> bool:
        return self.cache.has(self.cache_ns, f"{coord_key(*o)}>{coord_key(*d)}")

    def minutes(
        self, pairs: list[tuple[Point, Point]], progress: Progress | None = None, workers: int = 4
    ) -> dict[tuple[Point, Point], float | None]:
        progress = progress or Progress()
        unique = list(dict.fromkeys(pairs))
        cached = [p for p in unique if self.is_cached(*p)]
        todo = [p for p in unique if not self.is_cached(*p)]
        seconds = len(todo) / self.settings.requests_per_second
        log.info(
            "Transit: %d routes, %d cached, %d to query (~%d min at %.1f req/s)",
            len(unique), len(cached), len(todo), round(seconds / 60), self.settings.requests_per_second,
        )
        progress.start("transit", len(unique))
        results = {p: self.minutes_one(*p) for p in cached}
        progress.advance(len(cached), cached=len(cached))

        def work(pair):
            progress.check()
            return self.minutes_one(*pair)

        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {pool.submit(work, p): p for p in todo}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
                progress.advance()
        except BaseException:
            progress.cancel.set()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown()
        if self.fallbacks:
            log.info("Transit: %d routes needed the nationwide 'finland' router", self.fallbacks)
        return results
