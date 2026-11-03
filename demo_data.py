"""Generate SYNTHETIC travel-time data so the analysis and forecast can be tried without an API key.

The patterns are invented to resemble well-known ORR behaviour; they are not
measurements. Scenario:
  - N (Silk Board -> KR Puram), evening: queue starts at Kadubeesanahalli ->
    Marathahalli and spills back towards Silk Board.
  - N, morning: a smaller bottleneck on the last segment into KR Puram.
  - S (KR Puram -> Silk Board), morning: queue starts on the approach to
    Silk Board and spills back towards Marathahalli.
  - Day to day, onset and clearance times shift and jam strength varies.
  - Random rain spells (about 1 day in 5) slow everything and lengthen the evening queue.
  - Random incidents create short-lived queues at any segment.
  - Weekends: light traffic.
  - Rain columns (observed and an imperfect forecast) and a SIMULATED provider
    estimate for later departures, only so the forecast/benchmark code can be tested.

Usage:
    python demo_data.py                     # one week -> data/demo_travel_times.csv
    python demo_data.py --days 56           # eight weeks, e.g. for forecast.py
    python analyze.py data/demo_travel_times.csv
"""

import argparse
import csv
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from collector import haversine_m
from providers import CSV_FIELDS

FREE_FLOW_KMPH = 40.0
JAM_KMPH = 9.0
OUT = "data/demo_travel_times.csv"

# (direction, head segment order, onset hour, end hour)
BOTTLENECKS = [
    ("N", 4, 17.5, 20.75),
    ("N", 7, 9.0, 10.5),
    ("S", 7, 8.5, 11.0),
]
SPREAD_MIN_PER_SEGMENT = 25   # queue tail moves one segment upstream every 25 min
SHRINK_MIN_PER_SEGMENT = 20   # and clears from the tail inward at the end
RAIN_PROB = 0.2
INCIDENTS_PER_DAY = 0.8       # mean, across both directions


def ramp(x, width=0.25):
    """0 -> 1 over `width` hours, for smooth onset and recovery."""
    return max(0.0, min(1.0, x / width))


def day_events(rng, weekday, rain):
    """Queues for one day: (direction, head, start, end, strength)."""
    events = []
    if weekday:
        for d, head, start, end in BOTTLENECKS:
            s = start + rng.gauss(0, 0.25)
            e = end + rng.gauss(0, 0.3)
            if rain and start > 12:
                e += 0.75
            events.append((d, head, s, e, rng.uniform(0.75, 1.05)))
    n_incidents = sum(rng.random() < INCIDENTS_PER_DAY / 3 for _ in range(3))
    n_seg = len(config.NODES) - 1
    for _ in range(n_incidents):
        start = rng.uniform(7, 21)
        events.append((rng.choice(config.DIRECTIONS), rng.randint(1, n_seg),
                       start, start + rng.uniform(0.5, 1.5), rng.uniform(0.7, 1.0)))
    return events


def intensity(events, direction, order, hour):
    c = 0.0
    for d, head, start, end, strength in events:
        if d != direction or order > head:
            continue
        k = head - order  # segments upstream of the head
        s = start + k * SPREAD_MIN_PER_SEGMENT / 60
        e = end - k * SHRINK_MIN_PER_SEGMENT / 60
        if s < e:
            c = max(c, strength * min(ramp(hour - s), ramp(e - hour)))
    return min(c, 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    tz = ZoneInfo(config.TIMEZONE)
    monday = datetime(2026, 9, 7, tzinfo=tz) if args.days > 7 else datetime(2026, 10, 26, tzinfo=tz)
    step = timedelta(minutes=config.INTERVAL_MINUTES)
    samples, precip, forecast_shift = {}, {}, {}
    for day in range(args.days):
        date = monday + timedelta(days=day)
        weekday = date.weekday() < 5
        rain = rng.random() < RAIN_PROB
        rain_start = rng.uniform(15, 19)
        rain_mm = rng.uniform(1.5, 6)
        # Rain forecasts are imperfect: timing off by up to 30 min, and 1 rainy day in 5 missed.
        forecast_shift[date.date()] = None if rain and rng.random() < 0.2 else rng.uniform(-0.5, 0.5)
        events = day_events(rng, weekday, rain)
        t = date.replace(hour=config.COLLECT_START_HOUR)
        while t.hour < config.COLLECT_END_HOUR:
            hour = t.hour + t.minute / 60
            raining = rain and rain_start <= hour < rain_start + 2.5
            precip[t] = (rain, rain_start, rain_mm)
            for direction in config.DIRECTIONS:
                for order, name, a, b in config.segments(direction):
                    dist = haversine_m(a, b) * 1.15
                    c = intensity(events, direction, order, hour) * rng.uniform(0.85, 1.0)
                    speed = FREE_FLOW_KMPH * (1 - c) + JAM_KMPH * c
                    if raining:
                        speed *= 0.7
                    speed *= rng.uniform(0.92, 1.05)
                    samples[(t, direction, order)] = (name, dist, dist / (speed / 3.6))
            t += step

    def rain_at(t, shift=0.0):
        rain, start, mm = precip.get(t, (False, 0, 0))
        hour = t.hour + t.minute / 60 + shift
        return round(mm, 1) if rain and start <= hour < start + 2.5 else 0.0

    rows = []
    for (t, direction, order), (name, dist, dur) in samples.items():
        shift = forecast_shift[t.date()]
        rain_fc = [None if t + timedelta(minutes=h) not in precip else
                   0.0 if shift is None else rain_at(t + timedelta(minutes=h), shift)
                   for h in config.RAIN_FORECAST_MIN]
        # SIMULATED provider estimate for a later departure: part live, part future, with noise.
        # Only exercises the benchmark code; says nothing about any real provider's accuracy.
        bench = []
        for h in config.BENCHMARK_HORIZONS_MIN:
            future = samples.get((t + timedelta(minutes=h), direction, order))
            bench.append(None if future is None else
                         round(0.4 * dur + 0.6 * future[2] * rng.lognormvariate(0, 0.15)))
        rows.append([t.isoformat(), direction, order, name, round(dist), round(dur),
                     round(dist / (FREE_FLOW_KMPH / 3.6)), rain_at(t)] + rain_fc + bench)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        w.writerows(rows)
    print(f"Wrote {len(rows)} synthetic samples ({args.days} days) to {args.out}")


if __name__ == "__main__":
    main()
