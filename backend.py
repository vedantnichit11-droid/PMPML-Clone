import asyncio, math, re, time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
import os
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")
import httpx
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

LIVE_URL = "https://prod-pmpml-live-data-api.chartr.in/all-buses"
GTFS_DIR = Path(__file__).parent / "data" / "gtfs"

# ---- tunables: based on one night log and one Sunday-morning log; recheck with a weekday log ----
POLL_S = 5
MAX_JUMP_KMH = 100      # reject points implying more than this vs last accepted
MIN_DT_S = 10           # short timestamp gaps are unreliable: never use less than this
SLACK_M = 30            # GPS noise allowance
CLUSTER_M = 200         # rejected points this close together count as one pending cluster
REANCHOR_N = 4          # a pending cluster needs this many records...
REANCHOR_S = 120        # ...spanning this many seconds, with no good record in between
LIVE_S, STALE_S = 120, 300   # was 60/300; threshold_check.py showed 60 s hides many healthy buses
HISTORY_N = 40
OFF_ROUTE_M = 150       # farther than this from the route line = treated as off route
DIR_MAP = {"DOWN": 0, "UP": 1}


def hav(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


ROUTE_RE = re.compile(r"^(.*?)(UP|DOWN)?$")

def parse_route(raw):
    raw = (raw or "").strip().upper()
    if not raw:
        return None, None
    m = ROUTE_RE.match(raw)
    return m.group(1).strip(), DIR_MAP.get(m.group(2))


# ---------------- GTFS ----------------
class Gtfs:
    """For each (route short name, direction_id): the most common shape and a stop list."""
    def __init__(self, d: Path):
        routes = pd.read_csv(d / "routes.txt", dtype=str)
        trips = pd.read_csv(d / "trips.txt", dtype=str)
        routes["rk"] = routes.route_short_name.str.strip().str.upper()
        t = trips.merge(routes[["route_id", "rk"]], on="route_id")
        t["direction_id"] = t.direction_id.fillna("0").astype(int)
        self.known = set(routes.rk)

        cnt = (t.groupby(["rk", "direction_id", "shape_id"]).size()
                 .reset_index(name="n").sort_values("n", ascending=False)
                 .drop_duplicates(["rk", "direction_id"]))
        rep = (t.merge(cnt[["rk", "direction_id", "shape_id"]],
                       on=["rk", "direction_id", "shape_id"])
                 .drop_duplicates(["rk", "direction_id"]))

        st = pd.read_csv(d / "stop_times.txt", dtype=str,
                         usecols=["trip_id", "stop_id", "stop_sequence"])
        st = st[st.trip_id.isin(set(rep.trip_id))].copy()
        st["seq"] = st.stop_sequence.astype(int)
        stops = pd.read_csv(d / "stops.txt", dtype=str)
        ov = Path(__file__).parent / "stop_overrides.csv"
        if ov.exists():
            fixes = pd.read_csv(ov, dtype=str, encoding="utf-8-sig").fillna("")
            fixes.columns = [c.strip().lower() for c in fixes.columns]
            if "stop_id" not in fixes.columns:
                raise ValueError("stop_overrides.csv needs a first line 'stop_id,new_name,lat,lon'; "
                                 f"found columns: {list(fixes.columns)}")
            stops = stops.set_index("stop_id")
            for _, r in fixes.iterrows():
                sid = str(r["stop_id"]).strip()
                if sid not in stops.index:
                    print(f"stop_overrides: unknown stop_id {sid}")
                    continue
                if r.get("new_name", "").strip():
                    stops.loc[sid, "stop_name"] = r["new_name"].strip()
                if r.get("lat", "").strip() and r.get("lon", "").strip():
                    stops.loc[sid, ["stop_lat", "stop_lon"]] = [r["lat"].strip(), r["lon"].strip()]
            stops = stops.reset_index()
        
        st = (st.merge(stops[["stop_id", "stop_name", "stop_lat", "stop_lon"]], on="stop_id")
                .sort_values(["trip_id", "seq"]))

        sh = pd.read_csv(d / "shapes.txt", dtype=str)
        sh = sh[sh.shape_id.isin(set(rep.shape_id))].copy()
        sh["seq"] = sh.shape_pt_sequence.astype(int)
        sh = sh.sort_values(["shape_id", "seq"])

        stops_by_trip = {
            k: [{"stop_id": r.stop_id, "name": r.stop_name,
                 "lat": float(r.stop_lat), "lon": float(r.stop_lon)} for r in g.itertuples()]
            for k, g in st.groupby("trip_id")}
        shape_by_id = {
            k: [[float(a), float(b)] for a, b in zip(g.shape_pt_lat, g.shape_pt_lon)]
            for k, g in sh.groupby("shape_id")}

        self.routes = {}
        for r in rep.itertuples():
            self.routes[(r.rk, int(r.direction_id))] = {
                "route": r.rk, "direction_id": int(r.direction_id),
                "trip_id": r.trip_id, "shape_id": r.shape_id,
                "stops": stops_by_trip.get(r.trip_id, []),
                "shape": shape_by_id.get(r.shape_id, []),
            }


# ---------------- route geometry: position along the route line ----------------
class RouteGeom:
    """Flat-earth geometry for one route direction (fine over a city-sized area)."""
    def __init__(self, key, route):
        self.key = key
        self.stops = route["stops"]
        pts = np.array(route["shape"], dtype=float).reshape(-1, 2)   # [lat, lon]
        self.ok = len(pts) >= 2
        self.stop_along = np.array([])
        self.length = 0.0
        if not self.ok:
            return
        self.lat0, self.lon0 = float(pts[:, 0].mean()), float(pts[:, 1].mean())
        self.kx = 111320.0 * math.cos(math.radians(self.lat0))
        self.ky = 110540.0
        self.x = (pts[:, 1] - self.lon0) * self.kx
        self.y = (pts[:, 0] - self.lat0) * self.ky
        self.dx, self.dy = np.diff(self.x), np.diff(self.y)
        self.len2 = self.dx ** 2 + self.dy ** 2
        self.seglen = np.sqrt(self.len2)
        self.cum = np.concatenate([[0.0], np.cumsum(self.seglen)[:-1]])  # along-distance at segment start
        self.length = float(self.seglen.sum())

        # place each stop along the line, in order (a stop never sits before the previous one)
        sa, prev_along, prev_stop = [], 0.0, None
        for stp in self.stops:
            if prev_stop is None:
                lo, hi = 0.0, 5000.0
            else:
                gap = hav(prev_stop["lat"], prev_stop["lon"], stp["lat"], stp["lon"])
                lo, hi = prev_along - 1.0, prev_along + max(3000.0, 3 * gap + 500.0)
            res = self.project(stp["lat"], stp["lon"], lo, hi) or self.project(stp["lat"], stp["lon"])
            a = max(res[0] if res else prev_along, prev_along)
            sa.append(a)
            prev_along, prev_stop = a, stp
        self.stop_along = np.array(sa)

    def project(self, lat, lon, lo=None, hi=None):
        """Nearest point on the line -> (distance along the line in m, distance off the line in m)."""
        px = (lon - self.lon0) * self.kx
        py = (lat - self.lat0) * self.ky
        with np.errstate(divide="ignore", invalid="ignore"):
            t = ((px - self.x[:-1]) * self.dx + (py - self.y[:-1]) * self.dy) / self.len2
        t = np.where(self.len2 > 0, np.clip(t, 0.0, 1.0), 0.0)
        qx = self.x[:-1] + t * self.dx
        qy = self.y[:-1] + t * self.dy
        d = np.hypot(px - qx, py - qy)
        along = self.cum + t * self.seglen
        if lo is not None:
            d = np.where((along >= lo) & (along <= hi), d, np.inf)
        i = int(np.argmin(d))
        if not np.isfinite(d[i]):
            return None
        return float(along[i]), float(d[i])


# ---------------- live state ----------------
GTFS: Optional[Gtfs] = None
GEOM: dict = {}
STATE: dict = {}
HEALTH = {"polls": 0, "last_ok": None, "last_error": None, "last_records": 0,
          "proj_errors": 0, "last_proj_error": None}


def get_geom(key):
    g = GEOM.get(key)
    if g is None:
        g = GEOM[key] = RouteGeom(key, GTFS.routes[key])
    return g


def update_projection(s):
    """Where is this bus along its route, and what is its next stop?"""
    h = s["hist"]
    if not h or GTFS is None:
        return
    cur = h[-1]
    old = s.get("proj")
    if old and old["ts"] == cur["ts"]:
        return
    base, d = parse_route(s["meta"].get("route"))
    key = (base, d)
    if not base or d is None or key not in GTFS.routes:
        s["proj"] = None
        return
    geom = get_geom(key)
    if not geom.ok:
        s["proj"] = None
        return

    prev = old if (old and old["key"] == key and cur["ts"] - old["ts"] <= 900) else None
    res = None
    if prev:                                    # search near where the bus was last time
        lo = prev["along"] - 300
        hi = prev["along"] + max(3000.0, (cur["ts"] - prev["ts"]) * 25.0)
        res = geom.project(cur["lat"], cur["lon"], lo, hi)
    if res is None or res[1] > 100:             # no good local match: search the whole line
        g = geom.project(cur["lat"], cur["lon"])
        if g and (res is None or g[1] < res[1] - 50):
            res = g
    if res is None:
        s["proj"] = None
        return

    along, off = res
    nxt = None
    if off <= OFF_ROUTE_M:
        k = int(np.searchsorted(geom.stop_along, along + 15.0, side="right"))
        if k < len(geom.stops):
            nxt = {"name": geom.stops[k]["name"], "stop_id": geom.stops[k]["stop_id"],
                   "distance_m": round(float(geom.stop_along[k] - along)),
                   "index": k + 1, "of": len(geom.stops)}
    s["proj"] = {"ts": cur["ts"], "key": key, "along": along, "off": off, "next": nxt,
                 "progress_m": None if not prev else round(along - prev["along"], 1)}


def ingest(rec):
    bid = rec.get("id")
    ts, lat = f(rec.get("timestamp")), f(rec.get("lat"))
    lon = f(rec.get("lon", rec.get("lng")))
    if not bid or ts is None or lat is None or lon is None or (lat == 0 and lon == 0):
        return
    s = STATE.setdefault(bid, {"hist": deque(maxlen=HISTORY_N), "pending": [],
                               "rejected": 0, "meta": {}})
    hist, pend = s["hist"], s["pending"]
    pt = {"ts": ts, "lat": lat, "lon": lon}
    accept = True
    if hist:
        last = hist[-1]
        if ts <= last["ts"]:
            return                      # duplicate or older version of the data: never go back
        dist = hav(last["lat"], last["lon"], lat, lon)
        allowed = MAX_JUMP_KMH / 3.6 * max(ts - last["ts"], MIN_DT_S) + SLACK_M
        if dist > allowed:
            accept = False
            s["rejected"] += 1
            if pend and hav(pend[0]["lat"], pend[0]["lon"], lat, lon) <= CLUSTER_M:
                pend.append(pt)
            else:
                pend[:] = [pt]
            if len(pend) >= REANCHOR_N and pend[-1]["ts"] - pend[0]["ts"] >= REANCHOR_S:
                hist.clear(); hist.extend(pend); pend.clear()   # the bus really went there
    if accept:
        pend.clear()                    # a good record means earlier outliers were glitches
        hist.append(pt)
    s["meta"] = {k: rec.get(k) for k in ("route", "route_desc", "agency", "ac", "orientation")}
    try:
        update_projection(s)
    except Exception as e:              # a geometry problem must never stop ingestion
        HEALTH["proj_errors"] += 1
        HEALTH["last_proj_error"] = repr(e)


def view(bid, s, now):
    h = s["hist"]
    if not h:
        return None
    cur = h[-1]
    age = max(0.0, now - cur["ts"])
    status = "live" if age <= LIVE_S else "delayed" if age <= STALE_S else "stale"
    base, d = parse_route(s["meta"].get("route"))
    matched = bool(GTFS and base and d is not None and (base, d) in GTFS.routes)
    spd = None
    for p in reversed(h):               # speed over a window of at least 30 s
        if cur["ts"] - p["ts"] >= 30:
            spd = hav(p["lat"], p["lon"], cur["lat"], cur["lon"]) / (cur["ts"] - p["ts"]) * 3.6
            break
    pj = s.get("proj")
    return {"id": bid, "lat": cur["lat"], "lon": cur["lon"], "ts": int(cur["ts"]),
            "age_s": round(age, 1), "status": status,
            "route": s["meta"].get("route"), "route_desc": s["meta"].get("route_desc"),
            "route_base": base, "direction_id": d, "route_matched": matched,
            "speed_kmh_30s": None if spd is None else round(spd, 1),
            "rejected_points": s["rejected"],
            "along_m": None if not pj else round(pj["along"]),
            "off_route_m": None if not pj else round(pj["off"]),
            "progress_m": None if not pj else pj["progress_m"],
            "next_stop": None if not pj else pj["next"]}


async def poll_loop():
    async with httpx.AsyncClient(timeout=15) as c:
        while True:
            t0 = time.time()
            try:
                r = await c.get(LIVE_URL)
                r.raise_for_status()
                data = r.json()["data"]
                for rec in data:
                    ingest(rec)
                HEALTH.update(last_ok=time.time(), last_records=len(data), last_error=None)
            except Exception as e:      # keep polling whatever happens
                HEALTH["last_error"] = repr(e)
            HEALTH["polls"] += 1
            await asyncio.sleep(max(0.5, POLL_S - (time.time() - t0)))


@asynccontextmanager
async def lifespan(app):
    global GTFS
    print("Loading GTFS...")
    GTFS = Gtfs(GTFS_DIR)
    print(f"GTFS ready: {len(GTFS.routes)} route/direction pairs")
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    now = time.time()
    return {**HEALTH, "buses_tracked": len(STATE),
            "last_ok_age_s": None if not HEALTH["last_ok"] else round(now - HEALTH["last_ok"], 1)}


@app.get("/buses")
def buses(min_lat: Optional[float] = None, max_lat: Optional[float] = None,
          min_lon: Optional[float] = None, max_lon: Optional[float] = None,
          max_age_s: float = 900, route: Optional[str] = None):
    now = time.time()
    out = []
    for bid, s in STATE.items():
        v = view(bid, s, now)
        if not v or v["age_s"] > max_age_s:
            continue
        if min_lat is not None and v["lat"] < min_lat: continue
        if max_lat is not None and v["lat"] > max_lat: continue
        if min_lon is not None and v["lon"] < min_lon: continue
        if max_lon is not None and v["lon"] > max_lon: continue
        if route and (v["route_base"] or "") != route.upper(): continue
        out.append(v)
    return {"server_time": now, "count": len(out), "buses": out}


@app.get("/buses/{bus_id}/history")
def history(bus_id: str):
    s = STATE.get(bus_id)
    if not s:
        raise HTTPException(404, "unknown bus")
    return {"accepted": list(s["hist"]), "pending": s["pending"], "rejected": s["rejected"],
            "projection": s.get("proj")}


@app.get("/routes/{route}/{direction_id}")
def route_info(route: str, direction_id: int):
    r = GTFS.routes.get((route.upper(), direction_id)) if GTFS else None
    if not r:
        raise HTTPException(404, "no such route/direction in GTFS")
    return r


@app.get("/routes/{route}/{direction_id}/geometry")
def route_geometry(route: str, direction_id: int):
    """Debug: where each stop sits along the route line."""
    key = (route.upper(), direction_id)
    if not GTFS or key not in GTFS.routes:
        raise HTTPException(404, "no such route/direction in GTFS")
    g = get_geom(key)
    if not g.ok:
        raise HTTPException(404, "route has no usable shape")
    return {"length_m": round(g.length),
            "last_stop_to_line_end_m": round(g.length - float(g.stop_along[-1])) if len(g.stop_along) else None,
            "stops": [{"i": i + 1, "name": st["name"], "along_m": round(float(a))}
                      for i, (st, a) in enumerate(zip(g.stops, g.stop_along))]}


@app.get("/debug/projection")
def debug_projection():
    """How closely do buses sit on their assigned route lines?"""
    offs = np.array([s["proj"]["off"] for s in STATE.values() if s.get("proj")])
    if len(offs) == 0:
        return {"buses_with_projection": 0}
    return {"buses_with_projection": int(len(offs)),
            "within_50m": int((offs <= 50).sum()),
            "within_150m": int((offs <= 150).sum()),
            "over_150m": int((offs > 150).sum()),
            "median_off_m": round(float(np.median(offs)), 1),
            "p90_off_m": round(float(np.percentile(offs, 90)), 1)}

@app.get("/config")
def config():
    return {"carto_key": os.environ.get("CARTO_KEY", "")}
# must stay LAST: it catches every URL the routes above didn't handle
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
