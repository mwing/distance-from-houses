import csv
import html
import io
import json
from pathlib import Path

from .config import MapSettings
from .models import Destination
from .pipeline import Result

STATIC = Path(__file__).parent / "web" / "static"
CSV_SKIP = {"times", "badges"}


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
        row[f"{d.name} transit min"] = r.times[d.name]["transit"]
        row[f"{d.name} car min"] = r.times[d.name]["car"]
    row["url"] = l.url
    row["other_urls"] = " ".join(l.other_urls)
    row["lat"], row["lon"], row["image"] = l.lat, l.lon, l.image
    row["key"] = f"{l.source}:{l.id}"
    row["times"] = r.times
    row["badges"] = []
    return row


def build_payload(results: list[Result], dests: list[Destination], map_settings: MapSettings) -> dict:
    columns = ["score", "address", "municipality", "house_type", "rooms", "size_m2", "price_eur", "price_per_m2", "build_year"]
    for d in dests:
        columns += [f"{d.name} transit min", f"{d.name} car min"]
    return {
        "rows": [_row(r, dests) for r in results],
        "dests": [
            {"name": d.name, "lat": d.lat, "lon": d.lon, "modes": d.modes, "limits": d.max_minutes, "weight": d.weight}
            for d in dests
        ],
        "columns": columns,
        "map": {
            "greenFactor": map_settings.green_factor,
            "redFactor": map_settings.red_factor,
            "defaultLimit": map_settings.default_max_minutes,
            "fadeKm": map_settings.fade_km,
            "idwPower": map_settings.idw_power,
        },
    }


def csv_text(payload: dict) -> str:
    rows = [{k: v for k, v in r.items() if k not in CSV_SKIP} for r in payload["rows"]]
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
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
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
