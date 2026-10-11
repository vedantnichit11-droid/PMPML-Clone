"""
Tests which "is this bus live?" rule works best on a logged session.

Usage:
    python threshold_check.py fleet_log_sun_morning.csv

It replays the log as if the backend had been running: at many moments it asks
"how old is each bus's newest record right now?", then checks whether that bus
really did report again within the next 10 minutes. A good rule labels most
still-reporting buses as live and few silent buses as live.
"""
import sys
import numpy as np
import pandas as pd

PATH = sys.argv[1] if len(sys.argv) > 1 else "fleet_log.csv"
HORIZON = 600   # "still reporting" = sends another new record within 10 minutes
STEP = 30       # check every 30 seconds of the log
WARMUP = 600    # skip the first 10 minutes (buses not yet seen)

df = pd.read_csv(PATH, dtype={"id": str})
df = df.dropna(subset=["ts", "recv"]).sort_values(["id", "recv"])
t_start, t_end = df.recv.min(), df.recv.max()
times = np.arange(t_start + WARMUP, t_end - HORIZON, STEP)
if len(times) == 0:
    sys.exit("Log too short: this check needs more than about 20 minutes of data.")

rules = {
    "fixed 60 s (current)":          lambda age, med: age <= 60,
    "fixed 90 s":                    lambda age, med: age <= 90,
    "fixed 120 s":                   lambda age, med: age <= 120,
    "adaptive 2 x interval + 15 s":  lambda age, med: age <= 2 * np.clip(med, 10, 120) + 15,
    "adaptive 3 x interval + 20 s":  lambda age, med: age <= 3 * np.clip(med, 10, 120) + 20,
}

parts = []
for bid, g in df.groupby("id"):
    recv = g.recv.to_numpy()
    ts = np.maximum.accumulate(g.ts.to_numpy())      # newest timestamp seen so far
    uniq = np.unique(ts)
    med = float(np.median(np.diff(uniq))) if len(uniq) >= 3 else 30.0

    idx = np.searchsorted(recv, times, side="right") - 1   # last record received by each moment
    ok = idx >= 0
    if not ok.any():
        continue
    i, t = idx[ok], times[ok]
    age = t - ts[i]

    nxt = i + 1                                       # first record received after the moment
    alive = np.zeros(len(i), dtype=bool)
    has = nxt < len(recv)
    alive[has] = recv[nxt[has]] <= t[has] + HORIZON
    parts.append(pd.DataFrame({"age": age, "med": med, "alive": alive}))

d = pd.concat(parts, ignore_index=True)
act, sil = d[d.alive], d[~d.alive]

print(f"File: {PATH}")
print(f"Samples (bus x moment): {len(d):,} | still reporting within 10 min: {len(act):,} "
      f"| silent for 10 min: {len(sil):,}")

print("\nHow old does a healthy (still reporting) bus look at a random moment? (seconds)")
print(act.age.describe(percentiles=[.5, .75, .9, .95, .99]).round(0))

print(f"\n{'rule':<32}{'healthy buses shown live':>26}{'silent buses shown live':>26}")
for name, fn in rules.items():
    a = fn(act.age, act.med).mean()
    s = fn(sil.age, sil.med).mean() if len(sil) else float("nan")
    print(f"{name:<32}{a:>25.0%} {s:>25.0%}")

print("\nHigher on the left and lower on the right is better.")
print("Note: the adaptive rules use each bus's interval over the whole log; "
      "the real backend would use only its recent history.")
