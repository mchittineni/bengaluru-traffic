"""Backfill observed rain for every day in a travel-time file, from Open-Meteo.

Use this for data collected before rain was logged with each sample. Writes
data/weather.csv (timestamp, precip_mm per 15 min), which forecast.py picks up with
--weather. Open-Meteo's free API is for non-commercial use.

Usage:
    python fetch_weather.py [path/to/travel_times.csv]
"""

import csv
import json
import os
import sys
import urllib.parse
import urllib.request

import pandas as pd

import config

OUT = "data/weather.csv"


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else config.DATA_FILE
    times = pd.to_datetime(pd.read_csv(src, usecols=["timestamp"]).timestamp)
    lat, lng = config.WEATHER_POINT
    qs = urllib.parse.urlencode({
        "latitude": lat, "longitude": lng, "timezone": config.TIMEZONE,
        "minutely_15": "precipitation",
        "start_date": times.min().strftime("%Y-%m-%d"), "end_date": times.max().strftime("%Y-%m-%d"),
    })
    url = f"https://historical-forecast-api.open-meteo.com/v1/forecast?{qs}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        m = json.load(resp)["minutely_15"]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "precip_mm"])
        w.writerows([f"{t}:00+05:30", p] for t, p in zip(m["time"], m["precipitation"]))
    rainy = sum(1 for p in m["precipitation"] if p)
    print(f"Wrote {len(m['time'])} slots ({rainy} with rain) to {OUT}")


if __name__ == "__main__":
    main()
