import logging
from dataclasses import dataclass, field

from .cache import Cache
from .config import Config
from .geocode import Geocoder
from .models import Destination, Listing
from .routing import CarRouter, TransitRouter
from .sources import dedupe, etuovi, oikotie

log = logging.getLogger(__name__)

FETCHERS = {"oikotie": oikotie.fetch, "etuovi": etuovi.fetch}


@dataclass
class Result:
    listing: Listing
    times: dict[str, dict[str, float | None]] = field(default_factory=dict)
    score: float | None = None
    too_far: bool = False


def fetch_listings(cfg: Config) -> list[Listing]:
    listings: list[Listing] = []
    for source in cfg.sources:
        try:
            got = list(FETCHERS[source](cfg.filters, cfg.max_listings))
        except Exception as e:
            log.error("Fetching from %s failed: %s", source, e)
            continue
        log.info("%s: %d listings", source, len(got))
        listings.extend(got)
    with_coords = [l for l in listings if l.lat is not None and l.lon is not None]
    if len(with_coords) < len(listings):
        log.info("Skipping %d listings without coordinates", len(listings) - len(with_coords))
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


def _within_limits(times: dict[str, dict[str, float | None]], dests: list[Destination]) -> bool:
    for d in dests:
        for mode, limit in d.max_minutes.items():
            value = times[d.name].get(mode)
            if value is not None and value > limit:
                return False
    return True


def run(cfg: Config) -> list[Result]:
    cache = Cache(cfg.cache_path)
    geocoder = Geocoder(cache)
    dests = [geocoder.resolve(d) for d in cfg.destinations]
    for d in dests:
        log.info("Destination %s at %.5f,%.5f (%s)", d.name, d.lat, d.lon, "Uusimaa" if d.in_uusimaa else "outside Uusimaa")

    listings = fetch_listings(cfg)
    if not listings:
        return []

    points = {l.id: (l.lat, l.lon) for l in listings}
    dest_points = [(d.lat, d.lon) for d in dests]
    car = CarRouter(cache).minutes(list(points.values()), dest_points)

    results = []
    for l in listings:
        times = {d.name: {"car": car[(points[l.id], (d.lat, d.lon))], "transit": None} for d in dests}
        results.append(Result(l, times))

    transit_router = None
    if cfg.transit.api_key:
        transit_router = TransitRouter(cfg.transit, cache)
    elif any("transit" in d.modes for d in dests):
        log.warning("No Digitransit API key (DIGITRANSIT_API_KEY); using car times only")

    if transit_router:
        pairs = []
        for r in results:
            if not r.listing.in_uusimaa:
                continue
            for d in dests:
                if "transit" not in d.modes or not d.in_uusimaa:
                    continue
                limit = d.max_minutes.get("transit")
                car_min = r.times[d.name]["car"]
                # Free-flow driving is nearly always faster than transit, so it prunes hopeless pairs.
                if limit is not None and car_min is not None and car_min > limit:
                    r.too_far = True
                    continue
                pairs.append((r, d))
        log.info("Querying %d transit routes (cached ones are free)", len(pairs))
        transit = transit_router.minutes([(points[r.listing.id], (d.lat, d.lon)) for r, d in pairs])
        for r, d in pairs:
            r.times[d.name]["transit"] = transit[(points[r.listing.id], (d.lat, d.lon))]

    kept = []
    for r in results:
        if r.too_far or not _within_limits(r.times, dests):
            continue
        r.score = _score(r.times, dests)
        kept.append(r)
    kept.sort(key=lambda r: (r.score is None, r.score or 0))
    log.info("%d listings within travel limits", len(kept))
    return kept
