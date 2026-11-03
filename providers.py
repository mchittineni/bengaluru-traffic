"""Travel-time and rain lookups for the ORR corridor.

Travel times come from Amazon Location Service or Google (config.PROVIDER); rain from
Open-Meteo. Uses only the standard library plus boto3 (imported lazily), so the same
code runs locally and inside AWS Lambda without extra packages.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import config

BENCH_FIELDS = [f"provider_eta_{h}_s" for h in config.BENCHMARK_HORIZONS_MIN]
RAIN_FIELDS = ["precip_mm"] + [f"precip_fc_{h}_mm" for h in config.RAIN_FORECAST_MIN]
CSV_FIELDS = (["timestamp", "direction", "seg_order", "segment",
               "distance_m", "duration_s", "static_duration_s"] + RAIN_FIELDS + BENCH_FIELDS)

# Fixed offset: IST has no daylight saving, and Lambda images may lack tz data.
IST = timezone(timedelta(hours=5, minutes=30))

_geo = None


def aws_route(origin, dest, depart=None):
    """Amazon Location Service (geo-routes v2): live traffic now, or predicted for `depart`."""
    global _geo
    if _geo is None:
        import boto3
        _geo = boto3.client("geo-routes", region_name=os.environ.get("AWS_REGION", config.AWS_REGION))
    when = {"DepartureTime": depart.isoformat()} if depart else {"DepartNow": True}
    r = _geo.calculate_routes(
        Origin=[origin[1], origin[0]],          # Amazon Location uses [lng, lat]
        Destination=[dest[1], dest[0]],
        TravelMode="Car",
        **when,
    )["Routes"][0]["Summary"]
    return r["Distance"], r["Duration"], None  # no free-flow time; analyze.py estimates it


def google_route(origin, dest, depart=None):
    """Google Routes API: live traffic now, or predicted for `depart`. Needs GOOGLE_MAPS_API_KEY."""
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        sys.exit("Set GOOGLE_MAPS_API_KEY first (Google Cloud console, Routes API enabled).")

    def ll(p):
        return {"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}}

    body = {"origin": ll(origin), "destination": ll(dest), "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE"}
    if depart:
        body.update(routingPreference="TRAFFIC_AWARE_OPTIMAL", trafficModel="BEST_GUESS",
                    departureTime=depart.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.distanceMeters"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        r = json.load(resp)["routes"][0]
    return (r["distanceMeters"], int(r["duration"].rstrip("s")),
            int(r.get("staticDuration", r["duration"]).rstrip("s")))


def route(origin, dest, depart=None):
    """Return (distance_m, duration_s, static_duration_s or None) for one segment."""
    return {"aws": aws_route, "google": google_route}[config.PROVIDER](origin, dest, depart)


def rain(now):
    """Rain (mm per 15 min) in the current slot and forecast for each RAIN_FORECAST_MIN ahead."""
    lat, lng = config.WEATHER_POINT
    qs = urllib.parse.urlencode({
        "latitude": lat, "longitude": lng, "timezone": config.TIMEZONE,
        "minutely_15": "precipitation", "past_minutely_15": 1,
        "forecast_minutely_15": max(config.RAIN_FORECAST_MIN) // 15 + 8,  # counted from the hour
    })
    with urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{qs}", timeout=20) as resp:
        m = json.load(resp)["minutely_15"]
    by_slot = dict(zip(m["time"], m["precipitation"]))
    slot = now.replace(minute=now.minute - now.minute % 15)
    return [by_slot.get((slot + timedelta(minutes=h)).strftime("%Y-%m-%dT%H:%M"))
            for h in [0] + config.RAIN_FORECAST_MIN]


def sample_rows():
    """One sample of every segment in every direction, as CSV rows (CSV_FIELDS order)."""
    now_dt = datetime.now(IST).replace(second=0, microsecond=0)
    now = now_dt.isoformat()
    segs = [(d, o, name, a, b) for d in config.DIRECTIONS for o, name, a, b in config.segments(d)]

    def one(seg):
        d, o, name, a, b = seg
        try:
            dist, dur, static = route(a, b)
        except Exception as e:  # one failed segment shouldn't drop the whole sample
            print(f"{now} {d}{o} failed: {e}", file=sys.stderr)
            return None
        bench = []
        for h in config.BENCHMARK_HORIZONS_MIN:
            try:
                bench.append(route(a, b, now_dt + timedelta(minutes=h))[1])
            except Exception as e:
                print(f"{now} {d}{o} +{h}min estimate failed: {e}", file=sys.stderr)
                bench.append(None)
        return [d, o, name, dist, dur, static], bench

    try:
        precip = rain(now_dt)
    except Exception as e:
        print(f"{now} rain lookup failed: {e}", file=sys.stderr)
        precip = [None] * len(RAIN_FIELDS)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, segs))
    rows = [[now] + base + precip + bench for r in results if r for base, bench in [r]]
    return now, rows
