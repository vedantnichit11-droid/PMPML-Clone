"""
For one route, list the stops of each direction (as the map draws them) with the
distance from each stop to that direction's route line, and flag stops far from it.

Usage:
    py check_route_stops.py 35
    py check_route_stops.py 351

Names shown are the raw names in the GTFS feed (stop_overrides.csv is not applied here).
"""
import math
import sys

import numpy as np
import pandas as pd

G = "data/gtfs"
ROUTE = (sys.argv[1] if len(sys.argv) > 1 else "35").strip().upper()
FLAG_M = 100


def dist_to_line(lat, lon, line):
    """Shortest distance in metres from a point to a polyline of [lat, lon] rows."""
    lat0, lon0 = line[:, 0].mean(), line[:, 1].mean()
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
    x, y = (line[:, 1] - lon0) * kx, (line[:, 0] - lat0) * ky
    px, py = (lon - lon0) * kx, (lat - lat0) * ky
    dx, dy = np.diff(x), np.diff(y)
    l2 = dx ** 2 + dy ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        t = ((px - x[:-1]) * dx + (py - y[:-1]) * dy) / l2
    t = np.where(l2 > 0, np.clip(t, 0.0, 1.0), 0.0)
    return float(np.hypot(px - (x[:-1] + t * dx), py - (y[:-1] + t * dy)).min())


routes = pd.read_csv(f"{G}/routes.txt", dtype=str)
routes["rk"] = routes.route_short_name.str.strip().str.upper()
trips = pd.read_csv(f"{G}/trips.txt", dtype=str)
t = trips.merge(routes[["route_id", "rk"]], on="route_id")
t = t[t.rk == ROUTE].copy()
if t.empty:
    sys.exit(f"No route with short name '{ROUTE}' in routes.txt.")
t["direction_id"] = t.direction_id.fillna("0").astype(int)

stops = pd.read_csv(f"{G}/stops.txt", dtype=str).set_index("stop_id")
shapes = pd.read_csv(f"{G}/shapes.txt", dtype=str)
times = pd.read_csv(f"{G}/stop_times.txt", dtype=str,
                    usecols=["trip_id", "stop_id", "stop_sequence"])

for d in sorted(t.direction_id.unique()):
    td = t[t.direction_id == d]
    shape_id = td.groupby("shape_id").size().sort_values(ascending=False).index[0]
    rep = td[td.shape_id == shape_id].iloc[0]
    headsign = rep.get("trip_headsign", "")

    sh = shapes[shapes.shape_id == shape_id].copy()
    sh["seq"] = sh.shape_pt_sequence.astype(int)
    sh = sh.sort_values("seq")
    line = sh[["shape_pt_lat", "shape_pt_lon"]].astype(float).to_numpy()

    st = times[times.trip_id == rep.trip_id].copy()
    st["seq"] = st.stop_sequence.astype(int)
    st = st.sort_values("seq")

    print("=" * 78)
    print(f"Route {ROUTE}, direction_id {d} ({'DOWN' if d == 0 else 'UP'})  |  "
          f"{len(td)} trips  |  representative trip {rep.trip_id}  |  shape {shape_id}")
    print(f"headsign: {headsign}")
    print("=" * 78)

    names = []
    for i, r in enumerate(st.itertuples(), 1):
        s = stops.loc[r.stop_id]
        dist = dist_to_line(float(s.stop_lat), float(s.stop_lon), line)
        names.append(s.stop_name)
        flag = "  <-- FAR FROM LINE" if dist > FLAG_M else ""
        print(f"{i:>3}  id {r.stop_id:>5}  {s.stop_name[:48]:<48} {dist:6.0f} m  "
              f"({float(s.stop_lat):.5f}, {float(s.stop_lon):.5f}){flag}")

    dup = pd.Series(names).value_counts()
    dup = dup[dup > 1]
    if len(dup):
        print("\nNames used by more than one stop on this direction:", dict(dup))
    print()
