import csv, time
import httpx

URL = "https://prod-pmpml-live-data-api.chartr.in/all-buses"
POLL_S = 5            # set to 1 for the short gap test
seen = set()          # (id, ts, lat, lon) already logged

def main():
    with httpx.Client(timeout=15) as client, \
         open("fleet_log.csv", "a", newline="") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["recv", "id", "ts", "lat", "lon", "route_id", "orientation"])
        first = True
        try:
            while True:
                try:
                    r = client.get(URL)
                    r.raise_for_status()
                    recv = time.time()
                    if first:
                        print(dict(r.headers))   # look for x-cache, etag, etc.
                        first = False
                    new = 0
                    for b in r.json()["data"]:
                        ts = b.get("timestamp")
                        if ts is None:
                            continue
                        key = (b["id"], ts, b["lat"], b["lon"])
                        if key in seen:
                            continue
                        seen.add(key)
                        w.writerow([f"{recv:.2f}", b["id"], ts, b["lat"], b["lon"],
                                    b.get("route_id"), b.get("orientation")])
                        new += 1
                    f.flush()
                    print(f"{time.strftime('%H:%M:%S')} new records: {new}")
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                    print("Error:", e)
                time.sleep(POLL_S)
        except KeyboardInterrupt:
            print("Stopped.")

main()