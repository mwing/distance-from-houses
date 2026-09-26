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

const hhEsc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const hhCss = rgb => rgb ? `rgb(${rgb.join(",")})` : "#9e9e9e";
const hhSafeUrl = u => /^https?:\/\//i.test(u || "") ? u : "#";

function renderResults(container, DATA, opts = {}) {
  const M = DATA.map;
  const cols = DATA.columns;
  const storeKey = `househunt.combineExcluded.${opts.storageId || "report"}`;
  let sortCol = "score", asc = true;

  container.innerHTML = `
    <div class="hh-controls">
      <label>Colour by <select class="hh-colour-by"></select></label>
      <span class="hh-combine" hidden></span>
      <label><input type="checkbox" class="hh-show-far"> Show houses over the limit</label>
    </div>
    <div class="hh-map"></div>
    <div class="hh-table-wrap"><table><thead></thead><tbody></tbody></table></div>`;
  const $ = sel => container.querySelector(sel);

  const map = L.map($(".hh-map"));
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  map.createPane("travel");
  Object.assign(map.getPane("travel").style, {zIndex: 350, opacity: 0.5, pointerEvents: "none"});

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

  let excluded = new Set();
  try { excluded = new Set(JSON.parse(localStorage.getItem(storeKey) || "[]")); } catch (e) {}

  // Combined views work in percent of each destination's own limit.
  function combined(row, reduce) {
    const parts = DATA.dests.filter(d => !excluded.has(d.name)).map(d => ({d, tv: scoreModeValue(row, d)}));
    if (!parts.length || parts.some(p => !p.tv)) return null;
    const pct = parts.map(p => ({pct: 100 * p.tv.v / p.tv.limit, w: p.d.weight ?? 1, atLeast: p.tv.atLeast}));
    return {v: Math.round(reduce(pct)), atLeast: pct.some(p => p.atLeast)};
  }
  const worst = ps => Math.max(...ps.map(p => p.pct));
  const average = ps => ps.reduce((a, p) => a + p.pct * p.w, 0) / ps.reduce((a, p) => a + p.w, 0);

  const options = [];
  if (DATA.dests.length > 1 && DATA.rows.some(r => DATA.dests.every(d => scoreModeValue(r, d)))) {
    options.push({combined: true, label: "All destinations · worst", unit: "% of limit", limit: 100, value: r => combined(r, worst)});
    options.push({combined: true, label: "All destinations · weighted average", unit: "% of limit", limit: 100, value: r => combined(r, average)});
  }
  DATA.dests.forEach(d => d.modes.forEach(mode => {
    if (DATA.rows.some(r => travelValue(r, d.name, mode))) {
      const limit = limitFor(d, mode);
      options.push({label: `${d.name} · ${mode} (limit ${limit} min)`, unit: "min", limit, value: r => travelValue(r, d.name, mode)});
    }
  }));
  const select = $(".hh-colour-by");
  select.innerHTML = options.map((o, i) => `<option value="${i}">${hhEsc(o.label)}</option>`).join("");
  if (!options.length) select.closest("label").hidden = true;
  const current = () => options[+select.value];

  const combineEl = $(".hh-combine");
  combineEl.innerHTML = "Include:" + DATA.dests.map((d, i) =>
    `<label><input type="checkbox" data-dest="${i}"${excluded.has(d.name) ? "" : " checked"}> ${hhEsc(d.name)}</label>`).join("");
  combineEl.addEventListener("change", e => {
    const d = DATA.dests[e.target.dataset.dest];
    if (e.target.checked) excluded.delete(d.name); else excluded.add(d.name);
    try { localStorage.setItem(storeKey, JSON.stringify([...excluded])); } catch (err) {}
    recolour();
  });

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
      if (!points.length || !size.x || !size.y) return;
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
  legend.onAdd = () => L.DomUtil.create("div", "hh-legend");
  legend.addTo(map);
  function renderLegend() {
    const el = legend.getContainer();
    const o = current();
    el.hidden = !o;
    if (!o) return;
    const at = t => o.limit * (M.greenFactor + t * (M.redFactor - M.greenFactor));
    const stops = [0, .25, .5, .75, 1].map(t => hhCss(ttColor(at(t), o.limit, M.greenFactor, M.redFactor)));
    el.innerHTML = `<b>${hhEsc(o.label)}</b>
      <div class="bar" style="background: linear-gradient(to right, ${stops.join(",")})"></div>
      <div class="ticks"><span>≤ ${Math.round(at(0))}</span><span>${Math.round(at(.5))}</span><span>≥ ${Math.round(at(1))} ${hhEsc(o.unit)}</span></div>
      <div><span class="swatch" style="background:#9e9e9e"></span>no route</div>`;
  }

  DATA.dests.forEach(d => {
    L.circleMarker([d.lat, d.lon], {radius: 9, color: "#fff", weight: 2, fillColor: "#1d1d1f", fillOpacity: 1}).addTo(map)
      .bindTooltip(hhEsc(d.name), {permanent: true, direction: "right"});
  });
  const fmt = (row, dest, mode) => {
    const tv = travelValue(row, dest, mode);
    return tv ? (tv.atLeast ? "> " : "") + tv.v : "–";
  };
  const badgeHtml = r => (r.badges || []).map(b => `<span class="hh-badge hh-badge-${hhEsc(b.kind)}">${hhEsc(b.label)}</span>`).join("");
  const bounds = [];
  const markers = DATA.rows.map(r => {
    const img = r.image ? `<img src="${hhEsc(hhSafeUrl(r.image))}" width="220" alt=""><br>` : "";
    const times = DATA.dests.map(d => `${hhEsc(d.name)}: ${fmt(r, d.name, "transit")} min transit / ${fmt(r, d.name, "car")} min car`).join("<br>");
    bounds.push([r.lat, r.lon]);
    return L.circleMarker([r.lat, r.lon], {radius: 7, color: "#1d1d1f", weight: 1.5, fillOpacity: .95})
      .bindPopup(`${img}${badgeHtml(r)}<b><a href="${hhEsc(hhSafeUrl(r.url))}" target="_blank" rel="noopener">${hhEsc(r.address)}, ${hhEsc(r.municipality)}</a></b><br>${hhEsc(r.house_type)} · ${hhEsc(r.rooms ?? "?")} h · ${hhEsc(r.size_m2 ?? "?")} m² · ${hhEsc(typeof r.price_eur === "number" ? r.price_eur.toLocaleString("fi-FI") : "?")} €<br>${times}`);
  });
  if (bounds.length) map.fitBounds(bounds, {padding: [20, 20]});
  else map.setView([60.25, 24.9], 9);

  const showFar = $(".hh-show-far");
  const visible = r => showFar.checked || r.within_limits;

  function recolour() {
    const o = current();
    combineEl.hidden = !o?.combined;
    const points = [];
    DATA.rows.forEach((r, i) => {
      const tv = o && o.value(r);
      if (tv) points.push({lat: r.lat, lon: r.lon, v: tv.v});
      markers[i].setStyle({fillColor: hhCss(tv ? ttColor(tv.v, o.limit, M.greenFactor, M.redFactor) : null)});
      if (visible(r)) markers[i].addTo(map); else markers[i].remove();
    });
    travelLayer.setData(points, o ? o.limit : M.defaultLimit);
    renderLegend();
  }

  function renderTable() {
    const rows = DATA.rows.map((r, i) => [r, i]).filter(([r]) => visible(r)).sort(([a], [b]) => {
      const x = a[sortCol], y = b[sortCol];
      if (x == null) return 1; if (y == null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
    });
    $("thead").innerHTML = "<tr>" + cols.map(c => `<th data-c="${hhEsc(c)}">${hhEsc(c)}${c === sortCol ? (asc ? " ▲" : " ▼") : ""}</th>`).join("") + "<th>link</th></tr>";
    $("tbody").innerHTML = rows.map(([r, i]) => `<tr data-i="${i}"${r.within_limits ? "" : ' class="far"'}>` + cols.map(c => {
      const v = r[c];
      if (c === "address") return `<td>${badgeHtml(r)}${hhEsc(v)}</td>`;
      if (c === "build_year") return `<td class="num">${hhEsc(v)}</td>`;
      return typeof v === "number" ? `<td class="num">${v.toLocaleString("fi-FI")}</td>` : `<td>${hhEsc(v)}</td>`;
    }).join("") + `<td><a href="${hhEsc(hhSafeUrl(r.url))}" target="_blank" rel="noopener">${hhEsc(r.source)}</a>${r.other_urls ? ` <a href="${hhEsc(hhSafeUrl(r.other_urls.split(" ")[0]))}" target="_blank" rel="noopener">+1</a>` : ""}</td></tr>`).join("");
  }

  $("thead").addEventListener("click", e => {
    const c = e.target.closest("th")?.dataset.c; if (!c) return;
    asc = c === sortCol ? !asc : true; sortCol = c; renderTable();
  });
  $("tbody").addEventListener("click", e => {
    const tr = e.target.closest("tr"); if (!tr || e.target.tagName === "A") return;
    container.querySelectorAll("tr.sel").forEach(t => t.classList.remove("sel")); tr.classList.add("sel");
    const m = markers[tr.dataset.i];
    if (!map.hasLayer(m)) m.addTo(map);
    map.setView(m.getLatLng(), 14); m.openPopup();
    $(".hh-map").scrollIntoView({behavior: "smooth", block: "nearest"});
  });
  select.addEventListener("change", recolour);
  showFar.addEventListener("change", () => { recolour(); renderTable(); });
  recolour();
  renderTable();
  return {destroy: () => map.remove()};
}
