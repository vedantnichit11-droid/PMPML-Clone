// The page is served by the backend itself, so API calls use relative URLs.
// If you ever open index.html straight from disk (file://), fall back to localhost.
const API = location.protocol === "file:" ? "http://127.0.0.1:8000" : "";
const POLL_MS = 5000;

// ---------------- map ----------------
const map = L.map("map", { preferCanvas: true }).setView([18.5204, 73.8567], 12);

// The CARTO basemap key is NOT stored in this file. The backend reads it from the
// CARTO_KEY setting in .env and hands it out at /config.
async function initTiles() {
  let key = "";
  try {
    const r = await fetch(`${API}/config`);
    key = (await r.json()).carto_key || "";
  } catch (e) {
    console.error("Could not load /config:", e);
  }
  if (!key) console.warn("No CARTO_KEY set: the basemap will show a watermark.");

  L.tileLayer(`https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=${key}`, {
    subdomains: "abcd",
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>, ' +
                 '&copy; <a href="https://carto.com/attributions">CARTO</a>'
  }).addTo(map);
}
initTiles();

// ---------------- helpers ----------------
const markers = new Map();   // bus id -> circleMarker
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function styleFor(status) {
  if (status === "live") {
    return { color: css("--live"), fillColor: css("--live"), fillOpacity: 0.9, weight: 1, radius: 6 };
  }
  if (status === "delayed") {
    return { color: css("--delayed"), fillColor: css("--delayed"), fillOpacity: 0.9, weight: 1, radius: 6 };
  }
  return { color: css("--stale"), fillColor: css("--stale"), fillOpacity: 0.12, weight: 2, radius: 5 };
}

// Route text comes from a third-party feed, so escape it before putting it in HTML.
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtAge(s) {
  if (s < 90) return Math.round(s) + " s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  return (s / 3600).toFixed(1) + " h ago";
}

function popupHtml(b) {
  const dir = b.direction_id === 0 ? "DOWN" : b.direction_id === 1 ? "UP" : "";
  const speed = b.speed_kmh_30s == null ? "not enough data" : b.speed_kmh_30s + " km/h";
  let next = "";
  if (b.next_stop) {
    next = `<div>Next stop: <b>${esc(b.next_stop.name)}</b> (${b.next_stop.distance_m} m, stop ${b.next_stop.index} of ${b.next_stop.of})</div>`;
  } else if (b.off_route_m != null && b.off_route_m > 150) {
    next = `<div class="sub">${b.off_route_m} m away from its route line</div>`;
  }
  return `<div class="popup">
    <b>${esc(b.route || "No route")}</b>
    <div class="sub">${esc(b.route_desc || "")}</div>
    <div>Bus ${esc(b.id)}</div>
    <div>Status: ${esc(b.status)} (position ${fmtAge(b.age_s)})</div>
    <div>Speed over last ~30 s: ${speed}</div>
    ${next}
    <div class="sub">${b.route_matched ? "Route found in GTFS" : "Route not found in GTFS"}${dir ? " · " + dir : ""}</div>
  </div>`;
}

// ---------------- route display ----------------
const routeLayer = L.layerGroup().addTo(map);
const routeCache = new Map();
let routeReq = 0;

async function showRoute(b) {
  const req = ++routeReq;
  routeLayer.clearLayers();
  if (!b || !b.route_matched) return;

  const key = `${b.route_base}/${b.direction_id}`;
  try {
    let r = routeCache.get(key);
    if (!r) {
      const res = await fetch(`${API}/routes/${encodeURIComponent(b.route_base)}/${b.direction_id}`);
      if (!res.ok) throw new Error("route lookup returned " + res.status);
      r = await res.json();
      routeCache.set(key, r);
    }
    if (req !== routeReq) return;   // user already clicked another bus

    L.polyline(r.shape, { color: css("--live"), weight: 4, opacity: 0.7, interactive: false })
      .addTo(routeLayer);
    for (const s of r.stops) {
      L.circleMarker([s.lat, s.lon], {
        radius: 3, weight: 1, color: css("--fg"),
        fillColor: css("--bg"), fillOpacity: 1
      }).bindTooltip(esc(s.name)).addTo(routeLayer);
    }
  } catch (e) {
    console.error("showRoute failed:", e);   // a failure here must never break the popup
  }
}

// ---------------- polling ----------------
let inFlight = false;
let timer = null;

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  const statusEl = document.getElementById("status");

  try {
    const q = new URLSearchParams({
      max_age_s: document.getElementById("showStale").checked ? 900 : 300
    });
    const route = document.getElementById("route").value.trim();
    if (route) q.set("route", route);

    const r = await fetch(`${API}/buses?${q}`);
    if (!r.ok) throw new Error("backend returned " + r.status);
    const data = await r.json();

    const seen = new Set();
    const counts = { live: 0, delayed: 0, stale: 0 };

    for (const b of data.buses) {
      seen.add(b.id);
      counts[b.status] = (counts[b.status] || 0) + 1;

      let m = markers.get(b.id);
      if (!m) {
        m = L.circleMarker([b.lat, b.lon], styleFor(b.status)).addTo(map);
        m.bindPopup(() => popupHtml(m.bus));
        m.on("popupopen", () => showRoute(m.bus));
        m.on("popupclose", () => { routeReq++; routeLayer.clearLayers(); });
        markers.set(b.id, m);
      } else {
        m.setLatLng([b.lat, b.lon]);
        m.setStyle(styleFor(b.status));
      }
      m.bus = b;
    }

    // remove buses that are no longer in the response
    for (const [id, m] of markers) {
      if (!seen.has(id)) {
        m.remove();
        markers.delete(id);
      }
    }

    for (const k of ["live", "delayed", "stale"]) {
      document.getElementById("n-" + k).textContent = counts[k] || 0;
    }
    statusEl.className = "";
    statusEl.textContent = `${data.count} buses shown · updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    statusEl.className = "err";
    statusEl.textContent = "Can't reach the backend. Is uvicorn running?";
    console.error(e);
  } finally {
    inFlight = false;
  }
}

function start() {
  if (!timer) {
    refresh();
    timer = setInterval(refresh, POLL_MS);
  }
}

function stop() {
  clearInterval(timer);
  timer = null;
}

// pause polling while the tab is hidden
document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));

// controls
let debounce;
document.getElementById("route").addEventListener("input", () => {
  clearTimeout(debounce);
  debounce = setTimeout(refresh, 300);
});
document.getElementById("showStale").addEventListener("change", refresh);

start();
