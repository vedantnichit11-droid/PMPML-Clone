"""
One script that replaces analyze.py, analyze2.py and analyze5.py.

Usage:
    python analyze_all.py                         # reads fleet_log.csv
    python analyze_all.py fleet_log_sun_morning.csv
"""
import sys
import numpy as np
import pandas as pd

PATH = sys.argv[1] if len(sys.argv) > 1 else "fleet_log.csv"


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = (np.sin((p2 - p1) / 2) ** 2
         + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * np.arcsin(np.sqrt(a))


df = pd.read_csv(PATH, dtype={"id": str})
df["lat"] = pd.to_numeric(df.lat, errors="coerce")
df["lon"] = pd.to_numeric(df.lon, errors="coerce")
df = df.dropna(subset=["ts", "lat", "lon"]).copy()
df["row"] = np.arange(len(df))                 # file order = arrival order
df["lag"] = df.recv - df.ts

print(f"File: {PATH}")
print(f"Duration: {(df.recv.max() - df.recv.min()) / 60:.1f} min | "
      f"{df.id.nunique()} buses | {len(df)} records")

# ---------- 1. lag (first poll excluded: it is the whole existing snapshot) ----------
first = df.recv == df.recv.min()
print("\n[1] Lag in seconds (receive time minus record timestamp), first poll excluded:")
print(df.loc[~first, "lag"].describe(percentiles=[.5, .9, .95, .99]).round(1))

# ---------- 2. arrival order ----------
arr = df.sort_values(["id", "row"])
print("\n[2] Records that arrived OLDER than the previous one for that bus:",
      int((arr.groupby("id").ts.diff() < 0).sum()))

# ---------- 3. intervals, distances, speeds ----------
df = df.sort_values(["id", "ts", "recv"]).reset_index(drop=True)
g = df.groupby("id")
df["dt"] = g.ts.diff()
df["dist"] = haversine(g.lat.shift(), g.lon.shift(), df.lat, df.lon)
df["kmh"] = np.where(df.dt > 0, df.dist / df.dt * 3.6, np.nan)

print("\n[3] Update interval per bus (seconds), most common values:")
print(df.dt.value_counts().head(10))
print("Same timestamp, different position:", int((df.dt == 0).sum()))

med = g.dt.median().dropna()
print("\nBuses by their median reporting interval (s):")
print(pd.cut(med, [0, 6, 12, 25, 45, 90, 1e9],
             labels=["<=6", "7-12", "13-25", "26-45", "46-90", ">90"])
      .value_counts().sort_index())

# ---------- 4. implausible speeds ----------
d = df[["id", "ts", "lat", "lon"]].sort_values("ts")
past = d.rename(columns={"ts": "ts0", "lat": "lat0", "lon": "lon0"}).copy()
past["ts"] = past.ts0 + 30
w = pd.merge_asof(d, past, on="ts", by="id", direction="backward").dropna(subset=["ts0"])
w["kmh30"] = haversine(w.lat0, w.lon0, w.lat, w.lon) / (w.ts - w.ts0) * 3.6

print(f"\n[4] Speed above 90 km/h, between consecutive records: "
      f"{int((df.kmh > 90).sum())} of {int(df.kmh.notna().sum())}")
print(f"    Speed above 90 km/h, over a window of at least 30 s: "
      f"{int((w.kmh30 > 90).sum())} of {len(w)}")
print("    30 s-window speed percentiles (km/h):")
print(w.kmh30.describe(percentiles=[.5, .9, .99, .999]).round(1))

# ---------- 5. do longer gaps mean the bus was parked? ----------
df["bucket"] = pd.cut(df.dt, [0, 10, 30, 60, 1e9],
                      labels=["<=10s", "11-30s", "31-60s", ">60s"])
tab = df.dropna(subset=["dt"]).groupby("bucket", observed=True).agg(
    n=("dt", "size"),
    pct_moving=("dist", lambda s: round((s >= 10).mean() * 100, 1)),
    median_dist_m=("dist", lambda s: round(s.median(), 1)),
)
print("\n[5] Longer gaps between records: were the buses still moving? (moving = 10 m or more)")
print(tab)

# ---------- 6. fleet classes ----------
s = g.agg(n=("ts", "size"), t0=("ts", "min"), t1=("ts", "max"),
          lat=("lat", "median"), lon=("lon", "median"),
          lat_min=("lat", "min"), lat_max=("lat", "max"),
          lon_min=("lon", "min"), lon_max=("lon", "max"))
s["spread_m"] = haversine(s.lat_min, s.lon_min, s.lat_max, s.lon_max)
s["span_s"] = s.t1 - s.t0
s["med_dt"] = g.ts.apply(lambda x: x.diff().median())

s["cls"] = "unclear"
s.loc[s.n == 1, "cls"] = "single_record"
s.loc[(s.n >= 2) & (s.spread_m < 50) & (s.span_s >= 300), "cls"] = "stationary"
s.loc[s.spread_m >= 300, "cls"] = "moving"

print("\n[6] Bus classes (moving = travelled 300 m or more; stationary = under 50 m "
      "over at least 5 min):")
print(s.cls.value_counts())
print(f"Share of fleet clearly moving: {(s.cls == 'moving').mean():.0%}")
print("Median reporting interval (s) by class:")
print(s.groupby("cls").med_dt.describe(percentiles=[.5, .9]).round(1))

st = s[s.cls == "stationary"].copy()
if len(st):
    st["cell"] = st.lat.round(3).astype(str) + "," + st.lon.round(3).astype(str)
    print("\nWhere stationary buses cluster (possible depots or terminals):")
    print(st.cell.value_counts().head(8))

# ---------- 7. how old is each bus's newest record when the log ends? ----------
age = df.recv.max() - g.ts.max()
print("\n[7] Age of each bus's newest record at the end of the log (s):")
print(age.describe(percentiles=[.5, .75, .9]).round(0))
for thr in (60, 120, 300, 900, 3600):
    print(f"  older than {thr:>4}s: {int((age > thr).sum())} buses")
print("Buses with only ONE record in the whole log:", int((s.n == 1).sum()))
