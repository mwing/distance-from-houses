"use strict";

const view = document.getElementById("view");
const logoutBtn = document.getElementById("logout");
let meta = null;
let cleanup = () => {};

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? {"Content-Type": "application/json"} : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && path !== "/api/login") {
    location.hash = "#/login";
    throw new ApiError(401, "Not signed in");
  }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok && res.status !== 409) {
    const detail = data?.detail;
    throw new ApiError(res.status, typeof detail === "string" ? detail : `Request failed (${res.status})`);
  }
  return {status: res.status, data};
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "checked" || k === "value" || k === "disabled" || k === "hidden") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const titleCase = s => s.charAt(0).toUpperCase() + s.slice(1);
const fmtTime = iso => iso ? new Date(iso).toLocaleString("fi-FI", {dateStyle: "short", timeStyle: "short"}) : "";
const numOrNull = v => v === "" || v == null ? null : Number(v);

function show(...nodes) {
  cleanup();
  cleanup = () => {};
  view.replaceChildren(...nodes);
  window.scrollTo(0, 0);
}

function errorBox(message) {
  return h("div", {class: "error", role: "alert"}, message);
}

// Routing

async function route() {
  const hash = location.hash || "#/";
  try {
    if (!meta && hash !== "#/login") {
      const s = (await api("GET", "/api/session")).data;
      logoutBtn.hidden = !s.auth_required;
      if (s.auth_required && !s.authenticated) { location.hash = "#/login"; return; }
      meta = (await api("GET", "/api/meta")).data;
    }
    let m;
    if (hash === "#/login") return viewLogin();
    if (hash === "#/" || hash === "#") return viewProfiles();
    if (hash === "#/new") return viewSettings(null);
    if ((m = hash.match(/^#\/p\/(\d+)$/))) return viewResults(+m[1]);
    if ((m = hash.match(/^#\/p\/(\d+)\/edit$/))) return viewSettings(+m[1]);
    location.hash = "#/";
  } catch (e) {
    if (e.status !== 401) show(errorBox(e.message));
  }
}
window.addEventListener("hashchange", route);
logoutBtn.addEventListener("click", async () => {
  await api("POST", "/api/logout").catch(() => {});
  meta = null;
  location.hash = "#/login";
});

// Login

function viewLogin() {
  const pw = h("input", {type: "password", autocomplete: "current-password", required: true, "aria-label": "Password"});
  const err = h("div");
  const form = h("form", {class: "card login", onsubmit: async e => {
    e.preventDefault();
    err.replaceChildren();
    try {
      await api("POST", "/api/login", {password: pw.value});
      meta = null;
      location.hash = "#/";
    } catch (ex) {
      err.replaceChildren(errorBox(ex.message));
    }
  }},
    h("h1", {}, "Sign in"),
    err,
    h("label", {class: "field"}, h("span", {}, "Password"), pw),
    h("button", {class: "primary", type: "submit"}, "Sign in"),
  );
  show(form);
  pw.focus();
}

// Profiles

const STATUS_LABEL = {queued: "Queued", running: "Running", done: "Updated", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted"};

function runStatus(run) {
  if (!run) return h("span", {class: "status muted"}, "Not run yet");
  const when = fmtTime(run.finished_at || run.started_at || run.created_at);
  const text = `${STATUS_LABEL[run.status] || run.status} ${when}` + (run.summary ? ` · ${run.summary}` : "");
  return h("span", {class: `status status-${run.status}`}, text);
}

async function viewProfiles() {
  const profiles = (await api("GET", "/api/profiles")).data;
  const head = h("div", {class: "page-head"}, h("h1", {}, "Searches"), h("a", {class: "button primary", href: "#/new"}, "New search"));
  if (!profiles.length) {
    return show(head, h("div", {class: "card empty"},
      h("p", {}, "No searches yet. Create one with your filters and the places you travel to."),
      h("a", {class: "button primary", href: "#/new"}, "Create a search")));
  }
  const cards = profiles.map(p => {
    const del = h("button", {type: "button", class: "danger"}, "Delete");
    let armed = false;
    del.addEventListener("click", async () => {
      if (!armed) { armed = true; del.textContent = "Confirm delete"; setTimeout(() => { armed = false; del.textContent = "Delete"; }, 4000); return; }
      await api("DELETE", `/api/profiles/${p.id}`);
      viewProfiles();
    });
    const dests = (p.settings.destinations || []).map(d => d.name).join(", ");
    return h("div", {class: "card profile-card"},
      h("h2", {}, h("a", {href: `#/p/${p.id}`}, p.name)),
      dests ? h("div", {class: "muted"}, "To: ", dests) : null,
      runStatus(p.last_run),
      p.refresh_daily ? h("div", {class: "muted status"}, `Refreshes daily at ${p.refresh_at}`) : null,
      h("div", {class: "actions"},
        h("a", {class: "button primary", href: `#/p/${p.id}`}, "Open"),
        h("a", {class: "button", href: `#/p/${p.id}/edit`}, "Edit"),
        del),
    );
  });
  show(head, h("div", {class: "cards"}, cards));
}

// Settings form

function chipGroup(name, options, selected, labelFn = titleCase) {
  const set = new Set(selected);
  return h("div", {class: "chips", role: "group"}, options.map(o =>
    h("label", {class: "chip"}, h("input", {type: "checkbox", name, value: String(o), checked: set.has(o)}), labelFn(o))));
}
const checkedValues = (root, name) => [...root.querySelectorAll(`input[name="${name}"]:checked`)].map(i => i.value);

function numberField(label, value, attrs = {}) {
  const input = h("input", {type: "number", value: value ?? "", inputmode: "decimal", ...attrs});
  return {input, el: h("label", {class: "field"}, h("span", {}, label), input)};
}

function destinationEditor(d, onRemove) {
  const state = {lat: d.lat ?? null, lon: d.lon ?? null};
  const name = h("input", {type: "text", value: d.name || "", required: true, placeholder: "e.g. Work"});
  const address = h("input", {type: "text", value: d.address || "", placeholder: "Street, city"});
  const pin = h("div", {class: "pin"});
  const results = h("div", {class: "geo-results"});
  const setPin = (lat, lon, label) => {
    state.lat = lat; state.lon = lon;
    pin.textContent = lat != null ? `Location set: ${label || `${lat.toFixed(5)}, ${lon.toFixed(5)}`}` : "";
  };
  setPin(state.lat, state.lon, d.address);
  address.addEventListener("input", () => setPin(null, null));

  const find = h("button", {type: "button"}, "Find");
  find.addEventListener("click", async () => {
    results.replaceChildren();
    const q = address.value.trim();
    if (q.length < 3) { results.append(h("div", {class: "muted"}, "Type at least 3 characters.")); return; }
    find.disabled = true;
    try {
      const hits = (await api("GET", `/api/geocode?q=${encodeURIComponent(q)}`)).data;
      if (!hits.length) results.append(h("div", {class: "muted"}, "No matches. Try adding the city."));
      hits.forEach(hit => results.append(h("button", {type: "button", onclick: () => {
        address.value = hit.label.split(",").slice(0, 3).join(",").trim();
        setPin(hit.lat, hit.lon, hit.label);
        results.replaceChildren();
      }}, hit.label, hit.uusimaa ? "" : " (outside Uusimaa: car only)")));
    } catch (e) {
      results.append(errorBox(e.message));
    } finally {
      find.disabled = false;
    }
  });

  const modes = chipGroup("modes", ["transit", "car"], d.modes || ["transit", "car"], m => m === "transit" ? "Public transport" : "Car");
  const weight = numberField("Weight", d.weight ?? 1, {min: 0, step: "0.1"});
  const maxTransit = numberField("Max min, public transport", d.max_minutes?.transit, {min: 1, step: 1});
  const maxCar = numberField("Max min, car", d.max_minutes?.car, {min: 1, step: 1});
  const el = h("div", {class: "dest"},
    h("div", {class: "dest-head"},
      h("label", {class: "field"}, h("span", {}, "Name"), name),
      h("label", {class: "field grow"}, h("span", {}, "Address"), address),
      find,
      h("button", {type: "button", class: "danger", onclick: onRemove}, "Remove")),
    pin, results,
    h("div", {class: "row"}, modes, weight.el, maxTransit.el, maxCar.el),
  );
  el.read = () => {
    const out = {name: name.value.trim(), modes: checkedValues(modes, "modes"), weight: Number(weight.input.value || 1)};
    if (address.value.trim()) out.address = address.value.trim();
    if (state.lat != null) { out.lat = state.lat; out.lon = state.lon; }
    const mm = {};
    if (maxTransit.input.value) mm.transit = Number(maxTransit.input.value);
    if (maxCar.input.value) mm.car = Number(maxCar.input.value);
    if (Object.keys(mm).length) out.max_minutes = mm;
    return out;
  };
  return el;
}

async function viewSettings(profileId) {
  const profile = profileId ? (await api("GET", `/api/profiles/${profileId}`)).data : null;
  const s = structuredClone(profile?.settings || meta.defaults);
  const f = s.filters || {};
  const t = s.transit || {};
  const mp = {...meta.defaults.map, ...(s.map || {})};
  const err = h("div");

  const name = h("input", {type: "text", value: profile?.name || "", required: true, maxlength: 100, placeholder: "e.g. Family house"});
  const sources = chipGroup("sources", ["oikotie", "etuovi"], s.sources || ["oikotie", "etuovi"]);
  const maxListings = numberField("Max listings per site", s.max_listings ?? 300, {min: 1, max: 5000, step: 1});

  const houseTypes = chipGroup("house_types", meta.house_types, f.house_types || []);
  const rooms = chipGroup("rooms", [1, 2, 3, 4, 5], f.rooms || [], r => r === 5 ? "5+" : String(r));
  const plot = chipGroup("plot_ownership", meta.plot_ownership, f.plot_ownership || [], p => p === "own" ? "Own plot" : "Rented plot");
  const priceMin = numberField("Price min €", f.price_min, {min: 0, step: 1000});
  const priceMax = numberField("Price max €", f.price_max, {min: 0, step: 1000});
  const sizeMin = numberField("Size min m²", f.size_min, {min: 0});
  const sizeMax = numberField("Size max m²", f.size_max, {min: 0});
  const selectedMunis = new Set((f.municipalities || []).map(m => m.toLowerCase()));
  const munis = h("div", {class: "chips"}, meta.municipalities.map(m =>
    h("label", {class: "chip"}, h("input", {type: "checkbox", name: "municipalities", value: m, checked: selectedMunis.has(m.toLowerCase())}), m)));

  const destList = h("div");
  const addDest = d => {
    const ed = destinationEditor(d, () => ed.remove());
    destList.append(ed);
  };
  (s.destinations || []).forEach(addDest);
  if (!(s.destinations || []).length) addDest({});

  const timeMode = t.depart_at ? "depart" : "arrive";
  const arrive = h("input", {type: "radio", name: "time_mode", value: "arrive", checked: timeMode === "arrive"});
  const depart = h("input", {type: "radio", name: "time_mode", value: "depart", checked: timeMode === "depart"});
  const timeInput = h("input", {type: "time", value: t.depart_at || t.arrive_by || "09:00", required: true});
  const day = h("select", {}, meta.weekdays.map(w => h("option", {value: w, selected: (t.day || "tuesday") === w}, titleCase(w))));
  const router = h("select", {}, meta.routers.map(r => h("option", {value: r, selected: (t.router || "hsl") === r}, r === "hsl" ? "HSL (Helsinki region)" : "Finland (nationwide)")));

  const green = numberField("Green up to × limit", mp.green_factor, {min: 0, step: "0.05"});
  const red = numberField("Red from × limit", mp.red_factor, {min: 0.05, step: "0.05"});
  const defLimit = numberField("Default limit, min", mp.default_max_minutes, {min: 1, step: 1});
  const fade = numberField("Fade distance, km", mp.fade_km, {min: 0.5, step: "0.5"});
  const idw = numberField("Interpolation power", mp.idw_power, {min: 0.5, step: "0.5"});

  const refresh = h("input", {type: "checkbox", checked: !!profile?.refresh_daily});
  const refreshAt = h("input", {type: "time", value: profile?.refresh_at || "06:00"});

  const save = h("button", {class: "primary", type: "submit"}, profile ? "Save" : "Create search");
  const form = h("form", {class: "settings", novalidate: true},
    h("div", {class: "page-head"}, h("h1", {}, profile ? `Edit “${profile.name}”` : "New search")),
    err,
    h("fieldset", {}, h("legend", {}, "Search"),
      h("div", {class: "row"}, h("label", {class: "field"}, h("span", {}, "Name"), name)),
      h("div", {class: "row"}, h("div", {class: "field"}, h("span", {}, "Sites"), sources), maxListings.el)),
    h("fieldset", {}, h("legend", {}, "Home"),
      h("div", {class: "field"}, h("span", {}, "House type (none = all)"), houseTypes),
      h("div", {class: "row"},
        h("div", {class: "field"}, h("span", {}, "Rooms"), rooms),
        h("div", {class: "field"}, h("span", {}, "Plot"), plot)),
      h("div", {class: "row"}, priceMin.el, priceMax.el, sizeMin.el, sizeMax.el),
      h("div", {class: "field", style: "margin-top:12px"}, h("span", {}, "Municipalities (none = all of Uusimaa)"), munis)),
    h("fieldset", {}, h("legend", {}, "Destinations"),
      h("p", {class: "hint"}, "Places you travel to. Use Find to pin the address. The weight sets how much a destination counts in the ranking."),
      destList,
      h("div", {class: "row"}, h("button", {type: "button", onclick: () => addDest({})}, "Add destination"))),
    h("fieldset", {}, h("legend", {}, "Public transport"),
      meta.transit_available ? null : h("div", {class: "notice"}, "The server has no Digitransit API key, so only car times are calculated."),
      h("div", {class: "row"},
        h("label", {}, arrive, "Arrive by"), h("label", {}, depart, "Depart at"), timeInput,
        h("label", {class: "field"}, h("span", {}, "Day"), day),
        h("label", {class: "field"}, h("span", {}, "Journey planner"), router))),
    h("fieldset", {}, h("legend", {}, "Map colours"),
      h("div", {class: "row"}, green.el, red.el, defLimit.el, fade.el, idw.el),
      h("p", {class: "hint"}, "Green up to limit × green factor, red from limit × red factor. The default limit applies to destinations without a max.")),
    h("fieldset", {}, h("legend", {}, "Automatic refresh"),
      h("div", {class: "row"}, h("label", {}, refresh, "Refresh daily at"), refreshAt),
      h("p", {class: "hint"}, "Fetches new listings once a day. Routes are cached, so only new homes are looked up.")),
    h("div", {class: "sticky-save"}, save, h("a", {class: "button", href: profile ? `#/p/${profile.id}` : "#/"}, "Cancel")),
  );

  form.addEventListener("submit", async e => {
    e.preventDefault();
    err.replaceChildren();
    const settings = {
      sources: checkedValues(sources, "sources"),
      max_listings: Number(maxListings.input.value || 300),
      filters: {
        house_types: checkedValues(houseTypes, "house_types"),
        rooms: checkedValues(rooms, "rooms").map(Number),
        plot_ownership: checkedValues(plot, "plot_ownership"),
        price_min: numOrNull(priceMin.input.value), price_max: numOrNull(priceMax.input.value),
        size_min: numOrNull(sizeMin.input.value), size_max: numOrNull(sizeMax.input.value),
        municipalities: checkedValues(munis, "municipalities"),
      },
      destinations: [...destList.children].map(el => el.read()),
      transit: {
        router: router.value, day: day.value,
        [depart.checked ? "depart_at" : "arrive_by"]: timeInput.value,
      },
      map: {
        green_factor: Number(green.input.value), red_factor: Number(red.input.value),
        default_max_minutes: Number(defLimit.input.value), fade_km: Number(fade.input.value), idw_power: Number(idw.input.value),
      },
    };
    const body = {name: name.value.trim(), settings, refresh_daily: refresh.checked, refresh_at: refreshAt.value || "06:00"};
    save.disabled = true;
    try {
      const res = profile
        ? await api("PUT", `/api/profiles/${profile.id}`, body)
        : await api("POST", "/api/profiles", body);
      location.hash = `#/p/${res.data.id}`;
    } catch (ex) {
      err.replaceChildren(errorBox(ex.message));
      err.scrollIntoView({behavior: "smooth", block: "center"});
    } finally {
      save.disabled = false;
    }
  });
  show(form);
}

// Results

const PHASES = {
  "destinations": "Locating destinations",
  "fetch oikotie": "Fetching listings from Oikotie",
  "fetch etuovi": "Fetching listings from Etuovi",
  "car": "Car travel times",
  "transit": "Public transport times",
  "done": "Finishing",
};

function progressView(run) {
  const p = run.progress || {};
  const label = run.status === "queued" ? "Waiting for another search to finish" : (PHASES[p.phase] || "Starting");
  const pct = p.total ? Math.round(100 * p.done / p.total) : null;
  const bits = [];
  if (p.total) bits.push(`${p.done}/${p.total}`);
  if (p.cached) bits.push(`${p.cached} cached`);
  if (p.eta_seconds) bits.push(`about ${Math.max(1, Math.round(p.eta_seconds / 60))} min left`);
  if (!p.total && p.message) bits.push(p.message);
  return h("div", {class: "card progress", role: "status"},
    h("div", {class: "progress-line"}, h("b", {}, label), h("span", {class: "muted"}, bits.join(" · "))),
    pct != null ? h("div", {class: "bar"}, h("div", {style: `width:${pct}%`})) : null);
}

async function viewResults(profileId) {
  const profile = (await api("GET", `/api/profiles/${profileId}`)).data;
  const status = h("div");
  const resultsEl = h("div");
  const runBtn = h("button", {type: "button", class: "primary"}, "Run now");
  const cancelBtn = h("button", {type: "button", hidden: true}, "Cancel run");
  const csv = h("a", {class: "button", href: `/api/profiles/${profileId}/results.csv`, hidden: true}, "Download CSV");
  const summaryEl = h("div", {class: "muted"});
  let handle = null, timer = null, activeRun = null, alive = true;

  show(
    h("div", {class: "page-head"},
      h("div", {}, h("h1", {}, profile.name), summaryEl),
      h("div", {class: "actions"}, runBtn, cancelBtn, h("a", {class: "button", href: `#/p/${profileId}/edit`}, "Edit"), csv)),
    status, resultsEl,
  );
  cleanup = () => { alive = false; clearTimeout(timer); handle?.destroy(); };

  async function loadResults() {
    try {
      const {run, payload} = (await api("GET", `/api/profiles/${profileId}/results`)).data;
      if (!alive) return;
      handle?.destroy();
      summaryEl.textContent = `${run.summary || ""} · updated ${fmtTime(run.finished_at)}`;
      csv.hidden = false;
      handle = renderResults(resultsEl, payload, {storageId: profileId});
    } catch (e) {
      if (e.status === 404) {
        resultsEl.replaceChildren(h("div", {class: "card empty"}, h("p", {}, "No results yet. Press Run now to fetch listings and travel times.")));
      } else {
        resultsEl.replaceChildren(errorBox(e.message));
      }
    }
  }

  function setRunning(run) {
    activeRun = run;
    const running = run && (run.status === "queued" || run.status === "running");
    runBtn.disabled = !!running;
    cancelBtn.hidden = !running;
    if (running) status.replaceChildren(progressView(run));
    return running;
  }

  async function poll() {
    if (!alive || !activeRun) return;
    try {
      const run = (await api("GET", `/api/runs/${activeRun.id}`)).data;
      if (!alive) return;
      if (setRunning(run)) { timer = setTimeout(poll, 1500); return; }
      if (run.status === "done") { status.replaceChildren(); await loadResults(); }
      else status.replaceChildren(errorBox(`Run ${STATUS_LABEL[run.status]?.toLowerCase() || run.status}${run.error ? `: ${run.error}` : ""}`));
    } catch (e) {
      if (alive && e.status !== 401) timer = setTimeout(poll, 5000);
    }
  }

  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    try {
      const {data} = await api("POST", `/api/profiles/${profileId}/runs`);
      setRunning(data);
      poll();
    } catch (e) {
      runBtn.disabled = false;
      status.replaceChildren(errorBox(e.message));
    }
  });
  cancelBtn.addEventListener("click", async () => {
    if (!activeRun) return;
    cancelBtn.disabled = true;
    await api("POST", `/api/runs/${activeRun.id}/cancel`).catch(() => {});
    cancelBtn.disabled = false;
  });

  await loadResults();
  const last = profile.last_run;
  if (setRunning(last)) poll();
  else if (last && ["failed", "interrupted"].includes(last.status)) {
    status.replaceChildren(errorBox(`Last run ${STATUS_LABEL[last.status].toLowerCase()} ${fmtTime(last.finished_at)}${last.error ? `: ${last.error}` : ""}`));
  }
}

route();
