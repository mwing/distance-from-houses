import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import HOUSE_TYPES, PLOT_OWNERSHIP, Destination, Filters

MODES = ("transit", "car")


@dataclass
class TransitSettings:
    api_key: str | None = None
    router: str = "hsl"
    arrive_by: str | None = "09:00"
    depart_at: str | None = None
    day: str = "tuesday"
    requests_per_second: float = 2.0


@dataclass
class MapSettings:
    green_factor: float = 0.5
    red_factor: float = 1.0
    default_max_minutes: float = 45.0
    fade_km: float = 3.0
    idw_power: float = 2.0


@dataclass
class Config:
    filters: Filters
    destinations: list[Destination]
    sources: list[str] = field(default_factory=lambda: ["oikotie", "etuovi"])
    max_listings: int = 500
    transit: TransitSettings = field(default_factory=TransitSettings)
    map: MapSettings = field(default_factory=MapSettings)
    cache_path: Path = Path(".cache/househunt.sqlite")
    output_dir: Path = Path("output")


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _parse_filters(raw: dict) -> Filters:
    f = Filters(
        house_types=[str(t).lower() for t in _as_list(raw.get("house_types"))],
        rooms=[int(r) for r in _as_list(raw.get("rooms"))],
        plot_ownership=[str(p).lower() for p in _as_list(raw.get("plot_ownership"))],
        price_min=raw.get("price_min"),
        price_max=raw.get("price_max"),
        size_min=raw.get("size_min"),
        size_max=raw.get("size_max"),
        municipalities=[str(m).lower() for m in _as_list(raw.get("municipalities"))],
    )
    bad = set(f.house_types) - set(HOUSE_TYPES)
    if bad:
        raise ValueError(f"Unknown house_types {sorted(bad)}; allowed: {', '.join(HOUSE_TYPES)}")
    bad = set(f.plot_ownership) - set(PLOT_OWNERSHIP)
    if bad:
        raise ValueError(f"Unknown plot_ownership {sorted(bad)}; allowed: {', '.join(PLOT_OWNERSHIP)}")
    if any(r < 1 or r > 5 for r in f.rooms):
        raise ValueError("rooms must be between 1 and 5 (5 means 5 or more)")
    return f


def _parse_destination(raw: dict) -> Destination:
    if "name" not in raw:
        raise ValueError(f"Destination is missing a name: {raw}")
    if not raw.get("address") and (raw.get("lat") is None or raw.get("lon") is None):
        raise ValueError(f"Destination {raw['name']!r} needs an address or lat/lon")
    modes = [str(m).lower() for m in _as_list(raw.get("modes")) or list(MODES)]
    bad = set(modes) - set(MODES)
    if bad:
        raise ValueError(f"Destination {raw['name']!r}: unknown modes {sorted(bad)}")
    max_minutes = {str(k).lower(): float(v) for k, v in (raw.get("max_minutes") or {}).items()}
    return Destination(
        name=raw["name"],
        address=raw.get("address"),
        lat=raw.get("lat"),
        lon=raw.get("lon"),
        modes=modes,
        weight=float(raw.get("weight", 1.0)),
        max_minutes=max_minutes,
    )


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    destinations = [_parse_destination(d) for d in raw.get("destinations") or []]
    if not destinations:
        raise ValueError("Config needs at least one destination")
    t = raw.get("transit") or {}
    transit = TransitSettings(
        api_key=t.get("api_key") or os.environ.get("DIGITRANSIT_API_KEY"),
        router=t.get("router", "hsl"),
        arrive_by=t.get("arrive_by", None if t.get("depart_at") else "09:00"),
        depart_at=t.get("depart_at"),
        day=str(t.get("day", "tuesday")).lower(),
        requests_per_second=float(t.get("requests_per_second", 2.0)),
    )
    sources = [str(s).lower() for s in _as_list(raw.get("sources")) or ["oikotie", "etuovi"]]
    bad = set(sources) - {"oikotie", "etuovi"}
    if bad:
        raise ValueError(f"Unknown sources {sorted(bad)}")
    m = raw.get("map") or {}
    map_settings = MapSettings(
        green_factor=float(m.get("green_factor", 0.5)),
        red_factor=float(m.get("red_factor", 1.0)),
        default_max_minutes=float(m.get("default_max_minutes", 45.0)),
        fade_km=float(m.get("fade_km", 3.0)),
        idw_power=float(m.get("idw_power", 2.0)),
    )
    if not 0 <= map_settings.green_factor < map_settings.red_factor:
        raise ValueError("map: need 0 <= green_factor < red_factor")
    return Config(
        map=map_settings,
        filters=_parse_filters(raw.get("filters") or {}),
        destinations=destinations,
        sources=sources,
        max_listings=int(raw.get("max_listings", 500)),
        transit=transit,
        cache_path=Path(raw.get("cache_path", ".cache/househunt.sqlite")),
        output_dir=Path(raw.get("output_dir", "output")),
    )
