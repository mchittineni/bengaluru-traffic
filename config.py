"""Corridor definition and collection settings for the Bengaluru ORR study.

Node coordinates are approximate points on the Outer Ring Road carriageway.
Run `python collector.py check` before collecting: it routes every segment once
and flags any whose driving distance is far longer than the straight-line
distance (a sign that Google routed it off the ORR). Adjust the coordinates
here until every segment passes.
"""

# Ordered from Silk Board to KR Puram. Segments are consecutive node pairs.
NODES = [
    ("Silk Board Jn", 12.9172, 77.6227),
    ("Agara Jn (HSR)", 12.9236, 77.6478),
    ("Bellandur (Ecospace)", 12.9265, 77.6760),
    ("Kadubeesanahalli", 12.9380, 77.6955),
    ("Marathahalli Bridge", 12.9560, 77.7010),
    ("Karthik Nagar", 12.9700, 77.6960),
    ("Mahadevapura", 12.9905, 77.6940),
    ("KR Puram", 13.0020, 77.6870),
]

# "N" = Silk Board -> KR Puram, "S" = KR Puram -> Silk Board.
DIRECTIONS = ["N", "S"]

# Collection window in local time (IST) and polling interval.
COLLECT_START_HOUR = 6
COLLECT_END_HOUR = 23
INTERVAL_MINUTES = 15

# A segment is "congested" when its average speed falls below this value.
CONGESTED_KMPH = 15.0

# Segment check: driving distance / straight-line distance above this is suspicious.
MAX_DETOUR_RATIO = 1.6

# Benchmark: with every sample, also log the provider's own travel-time estimate for a
# departure this many minutes ahead. Each horizon adds one routing call per segment
# (default [30, 60] triples the call volume); set to [] to switch off.
BENCHMARK_HORIZONS_MIN = [30, 60]

# Rain from Open-Meteo at the corridor centre: the current 15-min slot plus forecasts
# this many minutes ahead (one extra call per sample). Open-Meteo's free API is for
# non-commercial use; a commercial app needs their paid plan or another weather source.
WEATHER_POINT = (12.955, 77.68)
RAIN_FORECAST_MIN = [15, 30, 60]

# Travel-time source: "aws" (Amazon Location Service) or "google" (Google Routes API).
PROVIDER = "aws"
AWS_REGION = "ap-south-1"  # Mumbai

TIMEZONE = "Asia/Kolkata"
DATA_FILE = "data/travel_times.csv"
OUTPUT_DIR = "output"


def segments(direction):
    """Return [(order, name, (lat, lng), (lat, lng)), ...] in travel order."""
    nodes = NODES if direction == "N" else list(reversed(NODES))
    return [
        (i + 1, f"{a[0]} -> {b[0]}", (a[1], a[2]), (b[1], b[2]))
        for i, (a, b) in enumerate(zip(nodes, nodes[1:]))
    ]
