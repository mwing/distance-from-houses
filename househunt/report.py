import csv
import html
import json
from pathlib import Path

from .models import Destination
from .pipeline import Result


def _row(r: Result, dests: list[Destination]) -> dict:
    l = r.listing
    row = {
        "score": r.score,
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
    return row


def write_csv(results: list[Result], dests: list[Destination], path: Path) -> None:
    rows = [_row(r, dests) for r in results]
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_html(results: list[Result], dests: list[Destination], path: Path) -> None:
    rows = [_row(r, dests) for r in results]
    dest_json = [{"name": d.name, "lat": d.lat, "lon": d.lon} for d in dests]
    columns = ["score", "address", "municipality", "house_type", "rooms", "size_m2", "price_eur", "price_per_m2", "build_year"]
    for d in dests:
        columns += [f"{d.name} transit min", f"{d.name} car min"]
    data = json.dumps({"rows": rows, "dests": dest_json, "columns": columns}, ensure_ascii=False).replace("</", "<\\/")
    path.write_text(TEMPLATE.replace("__DATA__", data).replace("__COUNT__", html.escape(str(len(rows)))), encoding="utf-8")


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>househunt</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<style>
  body { font: 14px system-ui, sans-serif; margin: 0; color: #1d1d1f; background: #fafafa; }
  header { padding: 12px 16px; }
  h1 { font-size: 18px; margin: 0; }
  #map { height: 45vh; }
  #map .leaflet-tile-pane { filter: grayscale(.85) contrast(.95); }
  .wrap { overflow-x: auto; padding: 0 16px 24px; }
  table { border-collapse: collapse; width: 100%; background: #fff; }
  th, td { padding: 6px 8px; border-bottom: 1px solid #e5e5e5; text-align: left; white-space: nowrap; }
  th { cursor: pointer; position: sticky; top: 0; background: #f0f0f0; user-select: none; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr:hover td { background: #f5f8ff; }
  tr.sel td { background: #e3ecff; }
  a { color: #0b57d0; }
</style></head><body>
<header><h1>househunt — __COUNT__ listings</h1></header>
<div id="map"></div>
<div class="wrap"><table><thead></thead><tbody></tbody></table></div>
<script>
const DATA = __DATA__;
const cols = DATA.columns;
let sortCol = "score", asc = true;
const map = L.map("map");
// No key and no Referer needed, so it loads from file://. OSM's tile server 403s without a Referer; CARTO now needs a key.
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {
  maxZoom: 19,
  attribution: "Tiles &copy; Esri &mdash; Sources: Esri, HERE, Garmin, &copy; OpenStreetMap contributors"
}).addTo(map);
const markers = {};
const bounds = [];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
DATA.dests.forEach(d => {
  L.circleMarker([d.lat, d.lon], {radius: 9, color: "#c62828", fillOpacity: .9}).addTo(map).bindTooltip(esc(d.name), {permanent: true});
  bounds.push([d.lat, d.lon]);
});
DATA.rows.forEach((r, i) => {
  const img = r.image ? `<img src="${esc(r.image)}" width="220"><br>` : "";
  const times = DATA.dests.map(d => `${esc(d.name)}: ${r[d.name + " transit min"] ?? "–"} min transit / ${r[d.name + " car min"] ?? "–"} min car`).join("<br>");
  markers[i] = L.circleMarker([r.lat, r.lon], {radius: 6, color: "#1565c0"}).addTo(map)
    .bindPopup(`${img}<b><a href="${esc(r.url)}" target="_blank">${esc(r.address)}, ${esc(r.municipality)}</a></b><br>${esc(r.house_type)} · ${r.rooms ?? "?"} h · ${r.size_m2 ?? "?"} m² · ${r.price_eur?.toLocaleString("fi-FI") ?? "?"} €<br>${times}`);
  bounds.push([r.lat, r.lon]);
});
if (bounds.length) map.fitBounds(bounds, {padding: [20, 20]});
function render() {
  const rows = DATA.rows.map((r, i) => [r, i]).sort(([a], [b]) => {
    const x = a[sortCol], y = b[sortCol];
    if (x == null) return 1; if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
  });
  document.querySelector("thead").innerHTML = "<tr>" + cols.map(c => `<th data-c="${esc(c)}">${esc(c)}${c === sortCol ? (asc ? " ▲" : " ▼") : ""}</th>`).join("") + "<th>link</th></tr>";
  document.querySelector("tbody").innerHTML = rows.map(([r, i]) => "<tr data-i='" + i + "'>" + cols.map(c => {
    const v = r[c];
    return typeof v === "number" ? `<td class="num">${v.toLocaleString("fi-FI")}</td>` : `<td>${esc(v)}</td>`;
  }).join("") + `<td><a href="${esc(r.url)}" target="_blank">${esc(r.source)}</a>${r.other_urls ? ` <a href="${esc(r.other_urls.split(" ")[0])}" target="_blank">+1</a>` : ""}</td></tr>`).join("");
}
document.querySelector("thead").addEventListener("click", e => {
  const c = e.target.closest("th")?.dataset.c; if (!c) return;
  asc = c === sortCol ? !asc : true; sortCol = c; render();
});
document.querySelector("tbody").addEventListener("click", e => {
  const tr = e.target.closest("tr"); if (!tr || e.target.tagName === "A") return;
  document.querySelectorAll("tr.sel").forEach(t => t.classList.remove("sel")); tr.classList.add("sel");
  const m = markers[tr.dataset.i]; map.setView(m.getLatLng(), 14); m.openPopup();
});
render();
</script></body></html>
"""
