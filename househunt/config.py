import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import HOUSE_TYPES, PLOT_OWNERSHIP, Destination, Filters

MODES = ("transit", "car", "bike", "walk")
DEFAULT_MODES = ("transit", "car")


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
    default_max_minutes: float = 60.0
    fade_km: float = 3.0
    idw_power: float = 2.0


@dataclass
class CarSettings:
    rush_hour_factor: float = 1.2


@dataclass
class Config:
    filters: Filters
    destinations: list[Destination]
    sources: list[str] = field(default_factory=lambda: ["oikotie", "etuovi"])
    max_listings: int = 500
    transit: TransitSettings = field(default_factory=TransitSettings)
    map: MapSettings = field(default_factory=MapSettings)
    car: CarSettings = field(default_factory=CarSettings)
    nearby_services: bool = True
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
        build_year_min=raw.get("build_year_min"),
        build_year_max=raw.get("build_year_max"),
        municipalities=[str(m).lower() for m in _as_list(raw.get("municipalities"))],
    )
    bad = set(f.house_types) - set(HOUSE_TYPES)
    if bad:
        raise ValueError(f"Unknown house_types {sorted(bad)}; allowed: {', '.join(HOUSE_TYPES)}")
    bad = set(f.plot_ownership) - set(PLOT_OWNERSHIP)
    if bad:
        raise ValueError(f"Unknown plot_ownership {sorted(bad)}; allowed: {', '.join(PLOT_OWNERSHIP)}")
    for name in ("price_min", "price_max", "size_min", "size_max"):
        v = getattr(f, name)
        if v is not None and (not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0):
            raise ValueError(f"{name} must be a number of zero or more")
    for name in ("build_year_min", "build_year_max"):
        v = getattr(f, name)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1700 <= v <= 2100):
            raise ValueError(f"{name} must be a year between 1700 and 2100")
    if any(r < 1 or r > 5 for r in f.rooms):
        raise ValueError("rooms must be between 1 and 5 (5 means 5 or more)")
    return f


def _parse_destination(raw: dict) -> Destination:
    if not str(raw.get("name") or "").strip():
        raise ValueError("Every destination needs a name")
    if not raw.get("address") and (raw.get("lat") is None or raw.get("lon") is None):
        raise ValueError(f"Destination {raw['name']!r} needs an address or lat/lon")
    modes = [str(m).lower() for m in _as_list(raw.get("modes")) or list(DEFAULT_MODES)]
    bad = set(modes) - set(MODES)
    if bad:
        raise ValueError(f"Destination {raw['name']!r}: unknown modes {sorted(bad)}")
    max_minutes = {str(k).lower(): float(v) for k, v in (raw.get("max_minutes") or {}).items() if v is not None}
    if set(max_minutes) - set(MODES) or not all(math.isfinite(v) and v > 0 for v in max_minutes.values()):
        raise ValueError(f"Destination {raw['name']!r}: max_minutes needs positive values for {'/'.join(MODES)}")
    weight = float(raw.get("weight", 1.0))
    if not math.isfinite(weight) or weight < 0:
        raise ValueError(f"Destination {raw['name']!r}: weight must be zero or more")
    for key in ("lat", "lon"):
        if raw.get(key) is not None and not math.isfinite(float(raw[key])):
            raise ValueError(f"Destination {raw['name']!r}: {key} must be a number")
    return Destination(
        name=raw["name"],
        address=raw.get("address"),
        lat=raw.get("lat"),
        lon=raw.get("lon"),
        modes=modes,
        weight=weight,
        max_minutes=max_minutes,
    )


MAX_DESTINATIONS = 10
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ROUTERS = ("hsl", "finland")


def _parse_hhmm(value, field_name: str) -> str | None:
    if value is None:
        return None
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value).strip())
    if not m:
        raise ValueError(f"{field_name} must be HH:MM, got {value!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def config_from_dict(raw: dict, env_api_key: bool = True) -> Config:
    try:
        return _config_from_dict(raw or {}, env_api_key)
    except (TypeError, AttributeError) as e:
        raise ValueError(f"Invalid settings: {e}") from e


def _config_from_dict(raw: dict, env_api_key: bool) -> Config:
    destinations = [_parse_destination(d) for d in raw.get("destinations") or []]
    if not destinations:
        raise ValueError("Config needs at least one destination")
    if len(destinations) > MAX_DESTINATIONS:
        raise ValueError(f"At most {MAX_DESTINATIONS} destinations")
    names = [d.name for d in destinations]
    if len(set(names)) != len(names):
        raise ValueError("Destination names must be unique")
    if not any(d.weight > 0 for d in destinations):
        raise ValueError("At least one destination needs a weight above zero")
    t = raw.get("transit") or {}
    depart_at = _parse_hhmm(t.get("depart_at"), "transit.depart_at")
    arrive_by = _parse_hhmm(t.get("arrive_by"), "transit.arrive_by")
    if arrive_by and depart_at:
        raise ValueError("Set only one of transit.arrive_by and transit.depart_at")
    if not arrive_by and not depart_at:
        arrive_by = "09:00"
    day = str(t.get("day", "tuesday")).lower()
    if day not in WEEKDAYS:
        raise ValueError(f"transit.day must be one of {', '.join(WEEKDAYS)}")
    router = str(t.get("router", "hsl")).lower()
    if router not in ROUTERS:
        raise ValueError(f"transit.router must be one of {', '.join(ROUTERS)}")
    transit = TransitSettings(
        api_key=t.get("api_key") or (os.environ.get("DIGITRANSIT_API_KEY") if env_api_key else None),
        router=router,
        arrive_by=arrive_by,
        depart_at=depart_at,
        day=day,
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
        default_max_minutes=float(m.get("default_max_minutes", 60.0)),
        fade_km=float(m.get("fade_km", 3.0)),
        idw_power=float(m.get("idw_power", 2.0)),
    )
    if not 0 <= map_settings.green_factor < map_settings.red_factor:
        raise ValueError("map: need 0 <= green_factor < red_factor")
    numbers = (map_settings.green_factor, map_settings.red_factor, map_settings.default_max_minutes,
               map_settings.fade_km, map_settings.idw_power)
    if not all(math.isfinite(v) for v in numbers) or min(numbers[2:]) <= 0:
        raise ValueError("map: default_max_minutes, fade_km and idw_power must be positive numbers")
    max_listings = int(raw.get("max_listings", 500))
    if not 1 <= max_listings <= 5000:
        raise ValueError("max_listings must be between 1 and 5000")
    car = CarSettings(rush_hour_factor=float((raw.get("car") or {}).get("rush_hour_factor", 1.2)))
    if not (math.isfinite(car.rush_hour_factor) and 1 <= car.rush_hour_factor <= 3):
        raise ValueError("car.rush_hour_factor must be between 1 and 3")
    return Config(
        map=map_settings,
        car=car,
        nearby_services=bool(raw.get("nearby_services", True)),
        filters=_parse_filters(raw.get("filters") or {}),
        destinations=destinations,
        sources=sources,
        max_listings=max_listings,
        transit=transit,
        cache_path=Path(raw.get("cache_path", ".cache/househunt.sqlite")),
        output_dir=Path(raw.get("output_dir", "output")),
    )


def load_config(path: str | Path) -> Config:
    return config_from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
