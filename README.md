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
   (about 2,860 route calls/day: the live time plus the provider's estimates 30 and 60 minutes
   ahead; ~950/day with `BENCHMARK_HORIZONS_MIN = []`). Leave it running for at least one full
   week (Mon-Sun).
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
| `sim3d.py`, `viewer/` | Vehicle-level simulation of the morning peak + interactive 3D viewer -> `output/sim3d/` |
| `.github/workflows/sim3d.yml` | Runs `sim3d.py` on GitHub Actions; publishes the viewer as an artifact or on GitHub Pages |
| `tests/` | pytest suite (Python), `tests/viewer/` node:test suite (viewer data code) |
| `.github/workflows/ci.yml` | Lint, tests with coverage, viewer tests, Terraform checks on every PR |

## Forecast

```bash
.venv/bin/python demo_data.py --days 56 --out data/demo_8weeks.csv
.venv/bin/python forecast.py data/demo_8weeks.csv     # or: forecast.py (real data, needs 3+ weeks)
```

The collector logs rain (Open-Meteo) and the provider's own estimate for departures 30 and 60
minutes ahead, so the report compares our model with the provider directly. For data collected
without rain columns: `fetch_weather.py` then `forecast.py --weather data/weather.csv`.

## 3D simulation

`sim3d.py` simulates individual vehicles through the 07:00-11:30 morning peak on the OSM network
(the same commuters under three routing rules: no app, live navigation, coordinated) and writes an
interactive 3D viewer: congestion-coloured roads, queue columns, 3D buildings, animated vehicles,
and a ride-along camera that follows one commuter and compares their trip across the rules.

```bash
.venv/bin/python fetch_network.py              # once
.venv/bin/python sim3d.py                      # about 30 s
python3 -m http.server -d output/sim3d 8000    # open http://localhost:8000
```

Controls: space play/pause, `1`-`3` switch rule, `F` or click a vehicle to ride along, `Esc` back to
the overview. The basemap (OpenFreeMap) and libraries load from public CDNs; no API key is needed.

On GitHub Actions the workflow `3D traffic simulation` runs on pushes to `main` that touch the
simulation, on pull requests, and on demand (Actions -> 3D traffic simulation -> Run workflow, with
demand and seed inputs). Each run writes the scenario table to the run summary and uploads the
viewer as the `orr-3d-viewer` artifact: download it, unzip, and serve it as above. To publish it on
GitHub Pages instead, set Settings -> Pages -> Source to "GitHub Actions" and add the repository
variable `DEPLOY_PAGES=true`. (Pages on a private repository needs a paid GitHub plan.)

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check .                         # lint
.venv/bin/pytest                               # tests (about 10 s; no network or AWS calls)
.venv/bin/pytest --cov                         # with coverage; fails below 80%
node --test tests/viewer/*.test.js             # viewer data code
terraform -chdir=aws fmt -check && terraform -chdir=aws validate
```

The browser smoke test is opt-in: `pip install playwright && playwright install chromium`, run
`sim3d.py`, then `pytest -m browser`. CI (`.github/workflows/ci.yml`) runs everything above on every
pull request; the 3D simulation workflow also runs the browser test before publishing the viewer.
Dependabot opens weekly update PRs for Python packages and GitHub Actions.

The viewer (`viewer/`) is plain static files with no build step: `core.js` holds the data decoding
and is unit tested, `app.js` the map and controls. External scripts are version-pinned with
Subresource Integrity hashes, and a Content-Security-Policy restricts the page to those CDNs and the
OpenFreeMap basemap. When bumping a library version, update its `integrity` hash
(`curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A`).

## Next steps

- Extend the corridor past Silk Board and KR Puram when a head appears at either end of the corridor.
- Field visit or Street View check at each ranked head: lane drop, U-turn, bus stop, merge, signal.
- Build the SUMO model (OSM network + sublane mixed traffic) and calibrate it against this data.
