"""Collect live travel times for each ORR segment (run locally).

The travel-time source is config.PROVIDER: "aws" uses your AWS credentials,
"google" needs GOOGLE_MAPS_API_KEY. For unattended collection, deploy the AWS
Lambda version instead (see aws/README.md).

Usage:
    python collector.py check     # verify every segment follows the ORR (1 call per segment)
    python collector.py once      # one sample of all segments (use from cron)
    python collector.py loop      # sample every INTERVAL_MINUTES inside the collection window
"""

import csv
import math
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from providers import CSV_FIELDS, route, sample_rows

TZ = ZoneInfo(config.TIMEZONE)


def haversine_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def check():
    ok = True
    print(f"Provider: {config.PROVIDER}")
    for direction in config.DIRECTIONS:
        print(f"\nDirection {direction}")
        for order, name, a, b in config.segments(direction):
            dist, dur, _ = route(a, b)
            ratio = dist / haversine_m(a, b)
            flag = "OK " if ratio <= config.MAX_DETOUR_RATIO else "BAD"
            ok &= flag == "OK "
            print(f"  [{flag}] {order}. {name:45s} {dist/1000:5.2f} km  "
                  f"detour x{ratio:.2f}  {dur/60:5.1f} min now")
            if flag == "BAD":
                print(f"        check: https://www.google.com/maps/dir/{a[0]},{a[1]}/{b[0]},{b[1]}")
    print("\nAll segments follow the corridor." if ok else
          "\nFix the flagged node coordinates in config.py, then re-run check.")


def sample_once():
    now, rows = sample_rows()
    os.makedirs(os.path.dirname(config.DATA_FILE), exist_ok=True)
    new_file = not os.path.exists(config.DATA_FILE)
    with open(config.DATA_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_FIELDS)
        w.writerows(rows)
    print(f"{now} wrote {len(rows)} segment samples")


def in_window(t):
    return config.COLLECT_START_HOUR <= t.hour < config.COLLECT_END_HOUR


def route_calls_per_day():
    """Routing calls per day: one per segment per sample, plus one per benchmark horizon."""
    n_seg = len(config.NODES) - 1
    samples = (config.COLLECT_END_HOUR - config.COLLECT_START_HOUR) * 60 // config.INTERVAL_MINUTES
    return n_seg * len(config.DIRECTIONS) * (1 + len(config.BENCHMARK_HORIZONS_MIN)) * samples


def loop():
    calls_per_day = route_calls_per_day()
    print(f"About {calls_per_day} route calls/day (~{calls_per_day * 7} per week). "
          f"Check {config.PROVIDER} routing pricing and free quota before a long run.")
    interval = config.INTERVAL_MINUTES * 60
    while True:
        if in_window(datetime.now(TZ)):
            sample_once()
        # Sleep to the next interval boundary so samples line up across days.
        time.sleep(interval - time.time() % interval)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "check":
        check()
    elif cmd == "once":
        sample_once()
    elif cmd == "loop":
        loop()
    else:
        sys.exit(__doc__)
