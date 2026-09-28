import csv
import dataclasses
import hashlib
import html
import io
import json
from pathlib import Path

from .config import Config, MapSettings
from .services import KINDS as SERVICE_KINDS
from .models import Destination
from .pipeline import Result, _score, _within_limits

STATIC = Path(__file__).parent / "web" / "static"
CSV_SKIP = {"times", "badges", "nearby"}
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _row(r: Result, dests: list[Destination]) -> dict:
    l = r.listing
    row = {
        "score": r.score,
        "within_limits": not r.too_far,
        "source": l.source,
        "address": l.address,
        "municipality": l.municipality,
        "house_type": l.house_type,
        "rooms": l.rooms,
        "size_m2": l.size,
        "price_eur": l.price,
        "price_per_m2": round(l.price / l.size) if l.price and l.size else None,
        "build_year": l.build_year,
    }
    for d in dests:
        for mode in d.modes:
            row[f"{d.name} {mode} min"] = r.times[d.name].get(mode)
    for kind in SERVICE_KINDS:
        row[f"{kind} m"] = (r.nearby.get(kind) or {}).get("m")
    row["nearby"] = r.nearby
    row["url"] = l.url
    row["other_urls"] = " ".join(l.other_urls)
    row["lat"], row["lon"], row["image"] = l.lat, l.lon, l.image
    row["key"] = f"{l.source}:{l.id}"
    row["times"] = r.times
    row["badges"] = []
    return row


def fingerprint(cfg: Config) -> str:
    """Covers what decides the listings and travel times; limits, weights and colours are left out
    so they can change without a new run."""
    relevant = {
        "sources": sorted(cfg.sources),
        "max_listings": cfg.max_listings,
        "filters": dataclasses.asdict(cfg.filters),
        "destinations": [[d.name, d.address, d.lat, d.lon, sorted(d.modes)] for d in cfg.destinations],
        "transit": [cfg.transit.router, cfg.transit.arrive_by, cfg.transit.depart_at, cfg.transit.day],
        "car": cfg.car.rush_hour_factor,
        "nearby": cfg.nearby_services,
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()


def _map_json(map_settings: MapSettings) -> dict:
    return {
        "greenFactor": map_settings.green_factor,
        "redFactor": map_settings.red_factor,
        "defaultLimit": map_settings.default_max_minutes,
        "fadeKm": map_settings.fade_km,
        "idwPower": map_settings.idw_power,
    }


def reevaluate(payload: dict, cfg: Config) -> dict:
    """Mutates payload: applies the current limits, weights and colours to stored travel times,
    or marks it stale when the times themselves would differ."""
    payload["stale"] = payload.get("fingerprint") != fingerprint(cfg)
    if payload["stale"]:
        return payload
    coords = {d["name"]: (d["lat"], d["lon"]) for d in payload["dests"]}
    dests = [dataclasses.replace(d, lat=coords[d.name][0], lon=coords[d.name][1]) for d in cfg.destinations]
    for row in payload["rows"]:
        row["score"] = _score(row["times"], dests)
        row["within_limits"] = _within_limits(row["times"], dests, cfg.map.default_max_minutes)
    payload["dests"] = [
        {"name": d.name, "lat": d.lat, "lon": d.lon, "modes": d.modes, "limits": d.max_minutes, "weight": d.weight}
        for d in dests
    ]
    payload["map"] = _map_json(cfg.map)
    return payload


def build_payload(results: list[Result], dests: list[Destination], map_settings: MapSettings,
                  car_factor: float = 1.0, settings_fingerprint: str | None = None) -> dict:
    columns = ["score", "address", "municipality", "house_type", "rooms", "size_m2", "price_eur", "price_per_m2", "build_year"]
    for d in dests:
        columns += [f"{d.name} {mode} min" for mode in d.modes]
    if any(r.nearby for r in results):
        columns += [f"{kind} m" for kind in SERVICE_KINDS]
    return {
        "rows": [_row(r, dests) for r in results],
        "dests": [
            {"name": d.name, "lat": d.lat, "lon": d.lon, "modes": d.modes, "limits": d.max_minutes, "weight": d.weight}
            for d in dests
        ],
        "columns": columns,
        "carFactor": car_factor,
        "fingerprint": settings_fingerprint,
        "map": _map_json(map_settings),
    }


def _csv_cell(v):
    # Listing text comes from third-party sites; a leading = or + would run as a spreadsheet formula.
    return "'" + v if isinstance(v, str) and v.startswith(FORMULA_START) else v


def csv_text(payload: dict) -> str:
    rows = [{k: _csv_cell(v) for k, v in r.items() if k not in CSV_SKIP} for r in payload["rows"]]
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def summary(payload: dict) -> str:
    within = sum(1 for r in payload["rows"] if r["within_limits"])
    return f"{within} listings within limits, {len(payload['rows']) - within} over"


def write_csv(payload: dict, path: Path) -> None:
    path.write_text(csv_text(payload), encoding="utf-8", newline="")


def write_html(payload: dict, path: Path) -> None:
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    page = (
        TEMPLATE.replace("__CSS__", (STATIC / "results.css").read_text(encoding="utf-8"))
        .replace("__JS__", (STATIC / "results.js").read_text(encoding="utf-8"))
        .replace("__DATA__", data)
        .replace("__COUNT__", html.escape(summary(payload)))
    )
    path.write_text(page, encoding="utf-8")


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>househunt</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<style>
  body { font: 14px system-ui, sans-serif; margin: 0; padding: 0 16px 24px; color: #1d1d1f; background: #fafafa; }
  h1 { font-size: 18px; margin: 12px 0 0; }
  #file-warning { margin: 8px 0; padding: 8px 12px; background: #fff4e5; border: 1px solid #f0c36d; border-radius: 6px; }
  [hidden] { display: none !important; }
  a { color: #0b57d0; }
__CSS__
</style></head><body>
<h1>househunt — __COUNT__</h1>
<div id="file-warning" hidden>Map tiles don't load from a local file. Open this report with <code>python -m househunt serve</code>.</div>
<div id="results"></div>
<script>
__JS__
// tile.openstreetmap.org answers 403 to requests without a Referer, which file:// pages never send.
if (location.protocol === "file:") document.getElementById("file-warning").hidden = false;
renderResults(document.getElementById("results"), __DATA__);
</script></body></html>
"""
