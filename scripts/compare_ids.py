"""
Do the live feed's route_id values match the route_id values in a GTFS feed?

Usage (run from the PROJECT ROOT, the folder that contains backend.py):
    py scripts\\compare_ids.py
    py scripts\\compare_ids.py some\\folder some\\file.zip

Each argument can be a folder of GTFS .txt files or a .zip. With no arguments it
tests the three feeds in your current layout. It makes one request to the live API.
"""
import os
import sys
import zipfile

import httpx
import pandas as pd

URL = "https://prod-pmpml-live-data-api.chartr.in/all-buses"
BASE = "pmpml-gtfs-main/pmpml-gtfs-main"
DEFAULTS = [
    f"{BASE}/pmpml_gtfs",          # API data as GTFS (IDs used by the API, per its README)
    f"{BASE}/pmpml_gtfs_compat",   # directions merged, uses direction_id
    "data/gtfs",                   # the MobilityDatabase feed you use today
]
paths = sys.argv[1:] or DEFAULTS


def opener(path):
    """Return a function read(filename) -> DataFrame or None, for a folder or a zip."""
    if os.path.isdir(path):
        def read(fname):
            p = os.path.join(path, fname)
            return pd.read_csv(p, dtype=str) if os.path.exists(p) else None
        return read
    z = zipfile.ZipFile(path)
    members = z.namelist()

    def read(fname):
        hit = [n for n in members if n == fname or n.endswith("/" + fname)]
        if not hit:
            return None
        with z.open(hit[0]) as fh:
            return pd.read_csv(fh, dtype=str)
    return read


live = pd.DataFrame(httpx.get(URL, timeout=15).json()["data"])
live = live.reindex(columns=["id", "route", "route_desc", "route_id"]).fillna("").astype(str)
live["route_id"] = live.route_id.str.strip()
named = live[live.route_id != ""]
live_ids = set(named.route_id)
print(f"Live feed: {len(live)} buses, {len(named)} with a route_id, {len(live_ids)} distinct route_ids")

summary = []
for p in paths:
    print("\n" + "=" * 72)
    print(p)
    print("=" * 72)
    try:
        read = opener(p)
    except Exception as e:
        print("Cannot open this one:", e)
        continue

    routes = read("routes.txt")
    if routes is None:
        print("No routes.txt found here.")
        continue

    cal = read("calendar.txt")
    if cal is not None and {"start_date", "end_date"} <= set(cal.columns):
        print(f"calendar dates: {cal.start_date.min()} to {cal.end_date.max()}")
    trips = read("trips.txt")
    short = routes.route_short_name.nunique() if "route_short_name" in routes else "n/a"
    print(f"routes.txt: {len(routes)} rows | distinct route_short_name: {short}")
    if trips is not None:
        print("trips.txt columns:", list(trips.columns))

    gtfs_ids = set(routes.route_id.str.strip())
    common = live_ids & gtfs_ids
    covered = int(named.route_id.isin(common).sum())
    pct_ids = len(common) / max(len(live_ids), 1)
    pct_bus = covered / max(len(named), 1)
    print(f"\nroute_id match: {len(common)} of {len(live_ids)} live route_ids ({pct_ids:.0%})")
    print(f"Buses whose route_id is in this feed: {covered} of {len(named)} ({pct_bus:.0%})")
    summary.append((p, pct_ids, pct_bus))

    # sanity check: if the IDs really match, the names should agree too
    if common:
        r = routes.assign(route_id=routes.route_id.str.strip()).set_index("route_id")
        sample = named[named.route_id.isin(common)].drop_duplicates("route_id").head(10)
        print("\nSample matches (live route -> names in this feed for the same route_id):")
        for row in sample.itertuples():
            g = r.loc[row.route_id]
            print(f"  {row.route_id:>6}  live: {row.route:<12} {row.route_desc[:36]:<36}"
                  f" | feed: {g.get('route_short_name', '')} | {str(g.get('route_long_name', ''))[:36]}")
    print("\nLive route_ids not found here (sample):", sorted(live_ids - gtfs_ids)[:15])

print("\n" + "=" * 72)
print("SUMMARY (share of live route_ids found / share of buses covered)")
for p, a, b in summary:
    print(f"  {a:>4.0%} / {b:>4.0%}   {p}")
