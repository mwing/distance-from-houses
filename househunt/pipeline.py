import logging
from dataclasses import dataclass, field

from .cache import Cache
from .config import Config
from .fetchcache import FetchCache
from .geocode import Geocoder
from .models import Destination, Listing
from .progress import Cancelled, Progress
from .routing import TableRouter, TransitRouter
from .services import KINDS as SERVICE_KINDS
from .services import Services, nearest
from .sources import dedupe, etuovi, oikotie

log = logging.getLogger(__name__)

FETCHERS = {"oikotie": oikotie.fetch, "etuovi": etuovi.fetch}


@dataclass
class Result:
    listing: Listing
    times: dict[str, dict[str, float | None]] = field(default_factory=dict)
    score: float | None = None
    too_far: bool = False
    nearby: dict[str, dict | None] = field(default_factory=dict)


def fetch_listings(cfg: Config, progress: Progress | None = None, fetch_cache: FetchCache | None = None) -> list[Listing]:
    progress = progress or Progress()
    listings: list[Listing] = []
    failures = []
    for source in cfg.sources:
        progress.start(f"fetch {source}")
        cached = fetch_cache.get(source, cfg.filters, cfg.max_listings) if fetch_cache else None
        if cached is not None:
            log.info("%s: %d listings (reused from an earlier fetch)", source, len(cached))
            progress.note(f"{len(cached)} listings, reused")
            listings.extend(cached)
            continue
        try:
            got = list(FETCHERS[source](cfg.filters, cfg.max_listings, progress=progress))
        except Cancelled:
            raise
        except Exception as e:
            log.error("Fetching from %s failed: %s", source, e)
            failures.append(f"{source}: {e}")
            continue
        log.info("%s: %d listings", source, len(got))
        if fetch_cache:
            fetch_cache.put(source, cfg.filters, cfg.max_listings, got)
        listings.extend(got)
    if failures and len(failures) == len(cfg.sources):
        raise RuntimeError("Every listing source failed: " + "; ".join(failures))
    listings = list({(l.source, l.id): l for l in listings}.values())
    with_coords = []
    for l in listings:
        if l.lat is None or l.lon is None:
            log.info("Skipping %s, %s (%s): no coordinates", l.address, l.municipality, l.url)
        elif not l.in_uusimaa:
            log.warning("Skipping %s, %s (%s): coordinates %.4f,%.4f are outside Uusimaa", l.address, l.municipality, l.url, l.lat, l.lon)
        else:
            with_coords.append(l)
    unique = dedupe(with_coords)
    log.info("%d unique listings after merging sources", len(unique))
    return unique


def _score(times: dict[str, dict[str, float | None]], dests: list[Destination]) -> float | None:
    total = 0.0
    for d in dests:
        value = next((times[d.name][m] for m in d.modes if times[d.name].get(m) is not None), None)
        if value is None:
            return None
        total += d.weight * value
    return round(total, 1)


def _mode_value(t: dict[str, float | None], mode: str) -> float | None:
    value = t.get(mode)
    if value is None and mode == "transit":
        value = t.get("transit_at_least")
    return value


def _within_limits(times: dict[str, dict[str, float | None]], dests: list[Destination],
                   default_limit: float | None = None) -> bool:
    for d in dests:
        t = times.get(d.name) or {}
        for mode, limit in d.max_minutes.items():
            value = _mode_value(t, mode)
            if value is not None and value > limit:
                return False
        if default_limit is not None:
            main = next((m for m in d.modes if _mode_value(t, m) is not None), None)
            if main and main not in d.max_minutes and _mode_value(t, main) > default_limit:
                return False
    return True


def _format_times(times: dict[str, dict[str, float | None]], dests: list[Destination]) -> str:
    parts = []
    for d in dests:
        t = times[d.name]
        transit = t["transit"] if t["transit"] is not None else (f">{t['transit_at_least']}" if t.get("transit_at_least") else "-")
        parts.append(f"{d.name} transit {transit} / car {t['car'] if t['car'] is not None else '-'} min")
    return "; ".join(parts)


def run(cfg: Config, progress: Progress | None = None, fetch_cache_hours: float | None = None) -> list[Result]:
    progress = progress or Progress()
    cache = Cache(cfg.cache_path)
    geocoder = Geocoder(cache)
    progress.start("destinations", len(cfg.destinations))
    dests = []
    for d in cfg.destinations:
        progress.check()
        dests.append(geocoder.resolve(d))
        progress.advance()
    for d in dests:
        log.info("Destination %s at %.5f,%.5f (%s)", d.name, d.lat, d.lon, "Uusimaa" if d.in_uusimaa else "outside Uusimaa")

    fetch_cache = FetchCache(cache, fetch_cache_hours) if fetch_cache_hours else None
    listings = fetch_listings(cfg, progress, fetch_cache)
    if not listings:
        return []

    points = {l.id: (l.lat, l.lon) for l in listings}
    dest_points = [(d.lat, d.lon) for d in dests]
    free_flow = TableRouter("car", cache).minutes(list(points.values()), dest_points, progress)
    other = {}
    for mode in ("bike", "walk"):
        mode_dests = [(d.lat, d.lon) for d in dests if mode in d.modes]
        if mode_dests:
            other[mode] = TableRouter(mode, cache).minutes(list(points.values()), list(dict.fromkeys(mode_dests)), progress)

    factor = cfg.car.rush_hour_factor
    results = []
    raw_car: dict[tuple[str, str], float | None] = {}
    for l in listings:
        times = {}
        for d in dests:
            pair = (points[l.id], (d.lat, d.lon))
            raw = free_flow[pair]
            raw_car[(l.id, d.name)] = raw
            t = {"car": round(raw * factor, 1) if raw is not None else None, "transit": None}
            for mode, table in other.items():
                if mode in d.modes:
                    t[mode] = table[pair]
            times[d.name] = t
        results.append(Result(l, times))

    transit_router = None
    if cfg.transit.api_key:
        transit_router = TransitRouter(cfg.transit, cache)
    elif any("transit" in d.modes for d in dests):
        log.warning("No Digitransit API key (DIGITRANSIT_API_KEY); using car times only")

    if transit_router:
        pairs = []
        for r in results:
            for d in dests:
                if "transit" not in d.modes or not d.in_uusimaa:
                    continue
                limit = d.max_minutes.get("transit")
                car_min = raw_car[(r.listing.id, d.name)]
                # Free-flow driving (before the rush-hour factor) is nearly always faster than transit,
                # so it's a safe lower bound.
                if limit is not None and car_min is not None and car_min > limit:
                    r.times[d.name]["transit_at_least"] = car_min
                    continue
                pairs.append((r, d))
        transit = transit_router.minutes([(points[r.listing.id], (d.lat, d.lon)) for r, d in pairs], progress)
        for r, d in pairs:
            r.times[d.name]["transit"] = transit[(points[r.listing.id], (d.lat, d.lon))]

    if cfg.nearby_services:
        progress.start("services")
        pois = Services(cache).points()
        if pois:
            for r in results:
                r.nearby = {kind: nearest(r.listing.lat, r.listing.lon, pois.get(kind, [])) for kind in SERVICE_KINDS}

    for r in results:
        r.score = _score(r.times, dests)
        r.too_far = not _within_limits(r.times, dests, cfg.map.default_max_minutes)
        log.debug("%s, %s: %s", r.listing.address, r.listing.municipality, _format_times(r.times, dests))
    results.sort(key=lambda r: (r.too_far, r.score is None, r.score or 0))
    log.info("%d listings, %d within travel limits", len(results), sum(not r.too_far for r in results))
    progress.start("done", message=f"{len(results)} listings")
    return results
