"""
List every GTFS stop whose name contains some text, with its coordinates,
a Google Maps link, and the routes that serve it.

Usage:
    py find_stop.py punawale
    py find_stop.py "punawale corner"
"""
import sys
import pandas as pd

G = "data/gtfs"
query = " ".join(sys.argv[1:]).strip().lower() or "punawale"

stops = pd.read_csv(f"{G}/stops.txt", dtype=str)
hit = stops[stops.stop_name.str.lower().str.contains(query, na=False, regex=False)]
if hit.empty:
    sys.exit(f"No stop name contains '{query}'.")

st = pd.read_csv(f"{G}/stop_times.txt", dtype=str, usecols=["trip_id", "stop_id"])
trips = pd.read_csv(f"{G}/trips.txt", dtype=str, usecols=["trip_id", "route_id"])
routes = pd.read_csv(f"{G}/routes.txt", dtype=str, usecols=["route_id", "route_short_name"])
st = (st[st.stop_id.isin(hit.stop_id)]
        .merge(trips, on="trip_id")
        .merge(routes, on="route_id"))

print(f"{len(hit)} stop(s) match '{query}':\n")
for r in hit.itertuples():
    mine = st[st.stop_id == r.stop_id]
    names = sorted(set(mine.route_short_name))
    shown = ", ".join(names[:12]) + (" ..." if len(names) > 12 else "")
    print(f"stop_id {r.stop_id} | {r.stop_name}")
    print(f"  lat, lon: {r.stop_lat}, {r.stop_lon}")
    print(f"  map:      https://www.google.com/maps?q={r.stop_lat},{r.stop_lon}")
    print(f"  routes:   {shown or '(none)'} | trips: {mine.trip_id.nunique()}\n")
