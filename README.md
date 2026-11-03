# Bengaluru ORR bottleneck finder

Step 1 of the Bengaluru traffic model: measure where and when congestion starts on the
Outer Ring Road (Silk Board -> KR Puram) using live travel times, before building a simulation.

## Setup

```bash
cd ~/bengaluru-traffic
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Try it without an API key

```bash
.venv/bin/python demo_data.py
.venv/bin/python analyze.py data/demo_travel_times.csv
open output/bottleneck_report.md output/heatmap_N.png
```

The demo data is synthetic and only shows what the output looks like.

## Collect real data on AWS (recommended)

See [aws/README.md](aws/README.md): a scheduled Lambda collects from Amazon Location Service into S3,
so nothing runs on your laptop.

## Collect real data locally

Set `PROVIDER` in `config.py` to `"aws"` (uses your AWS credentials) or `"google"`.
For Google:

1. In the Google Cloud console, create a project, enable the **Routes API**, set up billing
   and create an API key (restrict it to the Routes API).
2. `export GOOGLE_MAPS_API_KEY=your-key`
3. `.venv/bin/python collector.py check`: confirms each segment follows the ORR. Fix any flagged
   coordinates in `config.py`.
4. `.venv/bin/python collector.py loop`: samples 14 segments every 15 min from 06:00-23:00 IST
   (about 950 calls/day). Leave it running for at least one full week (Mon-Sun).
   Traffic-aware routing is billed at a higher tier than basic routing, so check current pricing and
   your free monthly quota before running. To cut cost, set `INTERVAL_MINUTES = 30`, collect only
   peak hours, or use one direction.
5. `.venv/bin/python analyze.py`

## Files

| File | Purpose |
|---|---|
| `config.py` | Corridor nodes, collection window, congestion threshold |
| `providers.py` | Travel-time lookup (Amazon Location or Google) |
| `collector.py` | Local sampler (`check`, `once`, `loop`) |
| `lambda_function.py`, `aws/main.tf` | AWS Lambda collector + Terraform |
| `fetch_s3.py` | Merge S3 samples into `data/travel_times.csv` |
| `fetch_weather.py` | Backfill observed rain for older data |
| `analyze.py` | Heatmaps + ranked bottleneck heads -> `output/` |
| `demo_data.py` | Synthetic data for testing (`--days 56` for forecasting) |
| `forecast.py` | 15/30/60-min travel-time forecast vs live-traffic and profile baselines -> `output/forecast/` |
| `fetch_network.py`, `routing_sim.py` | OSM road network; Waze-style vs coordinated routing simulation -> `output/routing/` |

## Forecast

```bash
.venv/bin/python demo_data.py --days 56 --out data/demo_8weeks.csv
.venv/bin/python forecast.py data/demo_8weeks.csv     # or: forecast.py (real data, needs 3+ weeks)
```

The collector logs rain (Open-Meteo) and the provider's own estimate for departures 30 and 60
minutes ahead, so the report compares our model with the provider directly. For data collected
without rain columns: `fetch_weather.py` then `forecast.py --weather data/weather.csv`.

## Next steps

- Extend the corridor past Silk Board and KR Puram when a head appears at either end of the corridor.
- Field visit or Street View check at each ranked head: lane drop, U-turn, bus stop, merge, signal.
- Build the SUMO model (OSM network + sublane mixed traffic) and calibrate it against this data.
