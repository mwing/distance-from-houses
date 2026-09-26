import csv
import html
import json
from pathlib import Path

from .config import MapSettings
from .models import Destination
from .pipeline import Result


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


def write_html(results: list[Result], dests: list[Destination], map_settings: MapSettings, path: Path) -> None:
    rows = []
    for r in results:
        row = _row(r, dests)
        row["times"] = r.times
        rows.append(row)
    dest_json = [
        {"name": d.name, "lat": d.lat, "lon": d.lon, "modes": d.modes, "limits": d.max_minutes, "weight": d.weight}
        for d in dests
    ]
    columns = ["score", "address", "municipality", "house_type", "rooms", "size_m2", "price_eur", "price_per_m2", "build_year"]
    for d in dests:
        columns += [f"{d.name} transit min", f"{d.name} car min"]
    data = json.dumps(
        {
            "rows": rows,
            "dests": dest_json,
            "columns": columns,
            "map": {
                "greenFactor": map_settings.green_factor,
                "redFactor": map_settings.red_factor,
                "defaultLimit": map_settings.default_max_minutes,
                "fadeKm": map_settings.fade_km,
                "idwPower": map_settings.idw_power,
            },
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    within = sum(not r.too_far for r in results)
    count = f"{within} listings within limits, {len(results) - within} over"
    path.write_text(
        TEMPLATE.replace("__COLOR_JS__", COLOR_JS).replace("__DATA__", data).replace("__COUNT__", html.escape(count)),
        encoding="utf-8",
    )


COLOR_JS = """
function ttColor(minutes, limit, greenFactor, redFactor) {
  if (minutes == null || !isFinite(minutes)) return null;
  const t = Math.min(1, Math.max(0, (minutes / limit - greenFactor) / (redFactor - greenFactor)));
  const h = 120 * (1 - t) / 360, s = 0.8, l = 0.42;
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const ch = x => {
    x = (x + 1) % 1;
    if (x < 1 / 6) return p + (q - p) * 6 * x;
    if (x < 1 / 2) return q;
    if (x < 2 / 3) return p + (q - p) * (2 / 3 - x) * 6;
    return p;
  };
  return [ch(h + 1 / 3), ch(h), ch(h - 1 / 3)].map(v => Math.round(v * 255));
}
"""

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>househunt</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<style>
  body { font: 14px system-ui, sans-serif; margin: 0; color: #1d1d1f; background: #fafafa; }
  header { padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: center; }
  h1 { font-size: 18px; margin: 0; }
  #file-warning { margin: 0 16px 8px; padding: 8px 12px; background: #fff4e5; border: 1px solid #f0c36d; border-radius: 6px; }
  [hidden] { display: none !important; }
  #map { height: 55vh; }
  .wrap { overflow-x: auto; padding: 0 16px 24px; }
  table { border-collapse: collapse; width: 100%; background: #fff; }
  th, td { padding: 6px 8px; border-bottom: 1px solid #e5e5e5; text-align: left; white-space: nowrap; }
  th { cursor: pointer; position: sticky; top: 0; background: #f0f0f0; user-select: none; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr:hover td { background: #f5f8ff; }
  tr.sel td { background: #e3ecff; }
  tr.far td { color: #888; }
  a { color: #0b57d0; }
  .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  #combine-dests { display: inline-flex; flex-wrap: wrap; gap: 4px 12px; }
  .legend { background: #fff; padding: 8px 10px; border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,.25); font-size: 12px; line-height: 1.4; }
  .legend .bar { width: 180px; height: 10px; border-radius: 3px; margin: 4px 0 2px; }
  .legend .ticks { display: flex; justify-content: space-between; font-variant-numeric: tabular-nums; }
</style></head><body>
<header>
  <h1>househunt — __COUNT__</h1>
  <label>Colour by <select id="colour-by"></select></label>
  <span id="combine-dests" hidden></span>
  <label><input type="checkbox" id="show-far"> Show houses over the limit</label>
</header>
<div id="file-warning" hidden>Map tiles don't load from a local file. Open this report with <code>python -m househunt serve</code>.</div>
<div id="map"></div>
<div class="wrap"><table><thead></thead><tbody></tbody></table></div>
<script>
__COLOR_JS__
const DATA = __DATA__;
const cols = DATA.columns;
const M = DATA.map;
let sortCol = "score", asc = true;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const css = rgb => rgb ? `rgb(${rgb.join(",")})` : "#9e9e9e";

// tile.openstreetmap.org answers 403 to requests without a Referer, which file:// pages never send.
if (location.protocol === "file:") document.getElementById("file-warning").hidden = false;
const map = L.map("map");
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}).addTo(map);
map.createPane("travel");
map.getPane("travel").style.zIndex = 350;
map.getPane("travel").style.opacity = 0.5;
map.getPane("travel").style.pointerEvents = "none";

function travelValue(row, dest, mode) {
  const t = row.times[dest] || {};
  if (t[mode] != null) return {v: t[mode], atLeast: false};
  if (mode === "transit" && t.transit_at_least != null) return {v: t.transit_at_least, atLeast: true};
  return null;
}

const limitFor = (d, mode) => d.limits[mode] ?? M.defaultLimit;

// Per destination, the mode the score uses: the first listed mode that has a time.
function scoreModeValue(row, d) {
  for (const mode of d.modes) {
    const tv = travelValue(row, d.name, mode);
    if (tv) return {...tv, limit: limitFor(d, mode)};
  }
  return null;
}

// Combined views work in percent of each destination's own limit.
const STORE_KEY = "househunt.combineExcluded";
let excluded = new Set();
try { excluded = new Set(JSON.parse(localStorage.getItem(STORE_KEY) || "[]")); } catch (e) {}

function combined(row, reduce) {
  const parts = DATA.dests.filter(d => !excluded.has(d.name)).map(d => ({d, tv: scoreModeValue(row, d)}));
  if (!parts.length || parts.some(p => !p.tv)) return null;
  const pct = parts.map(p => ({pct: 100 * p.tv.v / p.tv.limit, w: p.d.weight ?? 1, atLeast: p.tv.atLeast}));
  return {v: Math.round(reduce(pct)), atLeast: pct.some(p => p.atLeast)};
}
const worst = ps => Math.max(...ps.map(p => p.pct));
const average = ps => ps.reduce((a, p) => a + p.pct * p.w, 0) / ps.reduce((a, p) => a + p.w, 0);

const options = [];
if (DATA.dests.length > 1) {
  options.push({combined: true, label: "All destinations · worst", unit: "% of limit", limit: 100, value: r => combined(r, worst)});
  options.push({combined: true, label: "All destinations · weighted average", unit: "% of limit", limit: 100, value: r => combined(r, average)});
}
DATA.dests.forEach(d => d.modes.forEach(mode => {
  if (DATA.rows.some(r => travelValue(r, d.name, mode))) {
    const limit = limitFor(d, mode);
    options.push({label: `${d.name} · ${mode} (limit ${limit} min)`, unit: "min", limit, value: r => travelValue(r, d.name, mode)});
  }
}));
if (DATA.dests.length > 1 && !DATA.rows.some(r => DATA.dests.every(d => scoreModeValue(r, d)))) options.splice(0, 2);
const select = document.getElementById("colour-by");
select.innerHTML = options.map((o, i) => `<option value="${i}">${esc(o.label)}</option>`).join("");
if (!options.length) select.closest("label").hidden = true;
const combineEl = document.getElementById("combine-dests");
combineEl.innerHTML = "Include:" + DATA.dests.map((d, i) =>
  `<label><input type="checkbox" data-dest="${i}"${excluded.has(d.name) ? "" : " checked"}> ${esc(d.name)}</label>`).join("");
combineEl.addEventListener("change", e => {
  const d = DATA.dests[e.target.dataset.dest];
  if (e.target.checked) excluded.delete(d.name); else excluded.add(d.name);
  try { localStorage.setItem(STORE_KEY, JSON.stringify([...excluded])); } catch (err) {}
  recolour();
});
const current = () => options[+select.value];

// Inverse-distance-weighted surface from the houses' own travel times, faded out away from any house.
const TravelLayer = L.Layer.extend({
  onAdd(map) {
    this._canvas = L.DomUtil.create("canvas", "leaflet-zoom-hide");
    map.getPane("travel").appendChild(this._canvas);
    map.on("moveend zoomend resize", this.redraw, this);
    this.redraw();
  },
  onRemove(map) {
    map.off("moveend zoomend resize", this.redraw, this);
    this._canvas.remove();
  },
  setData(points, limit) { this._points = points; this._limit = limit; if (this._map) this.redraw(); },
  redraw() {
    const map = this._map, size = map.getSize(), c = this._canvas;
    L.DomUtil.setPosition(c, map.containerPointToLayerPoint([0, 0]));
    c.width = size.x; c.height = size.y;
    const ctx = c.getContext("2d");
    const points = this._points || [];
    if (!points.length) return;
    const step = 6, gw = Math.ceil(size.x / step), gh = Math.ceil(size.y / step);
    const kx = 111.32 * Math.cos(map.getCenter().lat * Math.PI / 180), ky = 110.57;
    const pts = points.map(p => [p.lon * kx, p.lat * ky, p.v]);
    const off = document.createElement("canvas");
    off.width = gw; off.height = gh;
    const octx = off.getContext("2d"), img = octx.createImageData(gw, gh);
    const half = M.fadeKm / 2, pow = M.idwPower / 2;
    for (let gy = 0; gy < gh; gy++) {
      for (let gx = 0; gx < gw; gx++) {
        const ll = map.containerPointToLatLng([gx * step + step / 2, gy * step + step / 2]);
        const x = ll.lng * kx, y = ll.lat * ky;
        let num = 0, den = 0, dmin2 = Infinity, exact = null;
        for (const [px, py, v] of pts) {
          const d2 = (px - x) ** 2 + (py - y) ** 2;
          if (d2 < 1e-6) { exact = v; dmin2 = 0; break; }
          if (d2 < dmin2) dmin2 = d2;
          const w = 1 / d2 ** pow;
          num += w * v; den += w;
        }
        const dmin = Math.sqrt(dmin2);
        const alpha = dmin <= half ? 1 : Math.max(0, 1 - (dmin - half) / half);
        if (alpha === 0) continue;
        const rgb = ttColor(exact ?? num / den, this._limit, M.greenFactor, M.redFactor);
        const i = (gy * gw + gx) * 4;
        img.data[i] = rgb[0]; img.data[i + 1] = rgb[1]; img.data[i + 2] = rgb[2]; img.data[i + 3] = Math.round(alpha * 255);
      }
    }
    octx.putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(off, 0, 0, gw * step, gh * step);
  },
});
const travelLayer = new TravelLayer().addTo(map);

const legend = L.control({position: "bottomright"});
legend.onAdd = () => L.DomUtil.create("div", "legend");
legend.addTo(map);
function renderLegend() {
  const el = legend.getContainer();
  const o = current();
  if (!o) { el.hidden = true; return; }
  const at = t => o.limit * (M.greenFactor + t * (M.redFactor - M.greenFactor));
  const stops = [0, .25, .5, .75, 1].map(t => css(ttColor(at(t), o.limit, M.greenFactor, M.redFactor)));
  el.innerHTML = `<b>${esc(o.label)}</b>
    <div class="bar" style="background: linear-gradient(to right, ${stops.join(",")})"></div>
    <div class="ticks"><span>≤ ${Math.round(at(0))}</span><span>${Math.round(at(.5))}</span><span>≥ ${Math.round(at(1))} ${o.unit}</span></div>
    <div><span class="swatch" style="background:#9e9e9e"></span>no route</div>`;
}

const bounds = [];
DATA.dests.forEach(d => {
  L.circleMarker([d.lat, d.lon], {radius: 9, color: "#fff", weight: 2, fillColor: "#1d1d1f", fillOpacity: 1}).addTo(map)
    .bindTooltip(esc(d.name), {permanent: true, direction: "right"});
});
const fmt = (row, dest, mode) => {
  const tv = travelValue(row, dest, mode);
  return tv ? (tv.atLeast ? "> " : "") + tv.v : "–";
};
const markers = DATA.rows.map(r => {
  const img = r.image ? `<img src="${esc(r.image)}" width="220"><br>` : "";
  const times = DATA.dests.map(d => `${esc(d.name)}: ${fmt(r, d.name, "transit")} min transit / ${fmt(r, d.name, "car")} min car`).join("<br>");
  bounds.push([r.lat, r.lon]);
  return L.circleMarker([r.lat, r.lon], {radius: 7, color: "#1d1d1f", weight: 1.5, fillOpacity: .95})
    .bindPopup(`${img}<b><a href="${esc(r.url)}" target="_blank">${esc(r.address)}, ${esc(r.municipality)}</a></b><br>${esc(r.house_type)} · ${r.rooms ?? "?"} h · ${r.size_m2 ?? "?"} m² · ${r.price_eur?.toLocaleString("fi-FI") ?? "?"} €<br>${times}`);
});
if (bounds.length) map.fitBounds(bounds, {padding: [20, 20]});

const showFar = document.getElementById("show-far");
const visible = r => showFar.checked || r.within_limits;

function recolour() {
  const o = current();
  combineEl.hidden = !o?.combined;
  const points = [];
  DATA.rows.forEach((r, i) => {
    const tv = o && o.value(r);
    if (tv) points.push({lat: r.lat, lon: r.lon, v: tv.v});
    markers[i].setStyle({fillColor: css(tv ? ttColor(tv.v, o.limit, M.greenFactor, M.redFactor) : null)});
    if (visible(r)) markers[i].addTo(map); else markers[i].remove();
  });
  travelLayer.setData(points, o ? o.limit : M.defaultLimit);
  renderLegend();
}

function render() {
  const rows = DATA.rows.map((r, i) => [r, i]).filter(([r]) => visible(r)).sort(([a], [b]) => {
    const x = a[sortCol], y = b[sortCol];
    if (x == null) return 1; if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
  });
  document.querySelector("thead").innerHTML = "<tr>" + cols.map(c => `<th data-c="${esc(c)}">${esc(c)}${c === sortCol ? (asc ? " ▲" : " ▼") : ""}</th>`).join("") + "<th>link</th></tr>";
  document.querySelector("tbody").innerHTML = rows.map(([r, i]) => `<tr data-i="${i}"${r.within_limits ? "" : ' class="far"'}>` + cols.map(c => {
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
select.addEventListener("change", recolour);
showFar.addEventListener("change", () => { recolour(); render(); });
recolour();
render();
</script></body></html>
"""
