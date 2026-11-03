"""Collection path: config, providers, local collector and the Lambda handler. No network calls."""

import csv
import io
import json
from datetime import datetime

import pytest

import collector
import config
import lambda_function
import providers


def fake_urlopen(payload, seen=None):
    def urlopen(req, timeout=None):
        if seen is not None:
            seen.append(req)
        return io.BytesIO(json.dumps(payload).encode())
    return urlopen


def test_segments_cover_the_corridor_both_ways():
    north, south = config.segments("N"), config.segments("S")
    assert len(north) == len(south) == len(config.NODES) - 1
    assert [o for o, *_ in north] == list(range(1, len(config.NODES)))
    assert north[0][2] == south[-1][3] and north[-1][3] == south[0][2]
    assert north[0][1].startswith("Silk Board")


def test_haversine():
    assert collector.haversine_m((0, 0), (0, 1)) == pytest.approx(111_195, rel=1e-3)
    assert collector.haversine_m((12.9, 77.6), (12.9, 77.6)) == 0


def test_route_calls_per_day_counts_benchmark_calls(monkeypatch):
    monkeypatch.setattr(config, "BENCHMARK_HORIZONS_MIN", [30, 60])
    assert collector.route_calls_per_day() == 14 * 3 * 68
    monkeypatch.setattr(config, "BENCHMARK_HORIZONS_MIN", [])
    assert collector.route_calls_per_day() == 14 * 68


def test_collection_window():
    assert collector.in_window(datetime(2026, 11, 2, 6, 0))
    assert not collector.in_window(datetime(2026, 11, 2, 23, 0))


def test_route_dispatches_on_provider(monkeypatch):
    monkeypatch.setattr(providers, "aws_route", lambda *a: ("aws", a))
    monkeypatch.setattr(providers, "google_route", lambda *a: ("google", a))
    monkeypatch.setattr(config, "PROVIDER", "google")
    assert providers.route((1, 2), (3, 4))[0] == "google"
    monkeypatch.setattr(config, "PROVIDER", "aws")
    assert providers.route((1, 2), (3, 4))[0] == "aws"


def test_google_route_parses_durations_and_requests_departure(monkeypatch):
    seen = []
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen(
        {"routes": [{"distanceMeters": 2100, "duration": "420s", "staticDuration": "300s"}]}, seen))
    assert providers.google_route((12.9, 77.6), (12.95, 77.65)) == (2100, 420, 300)
    depart = datetime(2026, 11, 2, 9, 0, tzinfo=providers.IST)
    providers.google_route((12.9, 77.6), (12.95, 77.65), depart)
    body = json.loads(seen[-1].data)
    assert body["departureTime"] == "2026-11-02T03:30:00Z"
    assert seen[-1].headers["X-goog-api-key"] == "test-key"


def test_google_route_needs_a_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        providers.google_route((0, 0), (0, 1))


def test_rain_reads_current_and_forecast_slots(monkeypatch):
    times = ["2026-11-02T08:00", "2026-11-02T08:15", "2026-11-02T08:30", "2026-11-02T09:00"]
    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen(
        {"minutely_15": {"time": times, "precipitation": [0.0, 1.2, 0.4, 2.0]}}))
    monkeypatch.setattr(config, "RAIN_FORECAST_MIN", [15, 45])
    now = datetime(2026, 11, 2, 8, 7, tzinfo=providers.IST)   # slot 08:00
    assert providers.rain(now) == [0.0, 1.2, None]


@pytest.fixture
def stub_providers(monkeypatch):
    def route(a, b, depart=None):
        if a == config.NODES[1][1:]:
            raise RuntimeError("segment down")
        if depart is not None:
            raise RuntimeError("no forecast")
        return 1500, 240, None
    monkeypatch.setattr(providers, "route", route)
    monkeypatch.setattr(providers, "rain", lambda now: (_ for _ in ()).throw(OSError("offline")))


def test_sample_rows_survives_partial_failures(stub_providers):
    _, rows = providers.sample_rows()
    n_seg = 2 * (len(config.NODES) - 1)
    assert len(rows) == n_seg - 2                     # the failing node starts one segment per direction
    assert all(len(r) == len(providers.CSV_FIELDS) for r in rows)
    r = dict(zip(providers.CSV_FIELDS, rows[0]))
    assert r["duration_s"] == 240
    assert r["precip_mm"] is None and r["provider_eta_30_s"] is None


def test_lambda_writes_one_csv_per_sample(monkeypatch):
    puts = []
    rows = [["2026-11-02T08:15:00+05:30", "N", 1, "A -> B", 1500, 240, None] + [None] * 6]
    monkeypatch.setattr(lambda_function, "sample_rows", lambda: (rows[0][0], rows))
    monkeypatch.setattr(lambda_function.s3, "put_object", lambda **kw: puts.append(kw))
    monkeypatch.setenv("BUCKET", "test-bucket")
    out = lambda_function.handler({}, None)
    assert out == {"key": "raw/date=2026-11-02/0815.csv", "segments": 1}
    (put,) = puts
    assert put["Bucket"] == "test-bucket" and put["ContentType"] == "text/csv"
    header, row = list(csv.reader(io.StringIO(put["Body"].decode())))
    assert header == providers.CSV_FIELDS and row[1] == "N"


def test_lambda_fails_loudly_when_every_segment_fails(monkeypatch):
    monkeypatch.setattr(lambda_function, "sample_rows", lambda: ("now", []))
    with pytest.raises(RuntimeError, match="Every segment failed"):
        lambda_function.handler({}, None)


def test_sample_once_appends_with_a_single_header(tmp_path, monkeypatch):
    out = tmp_path / "data" / "tt.csv"
    monkeypatch.setattr(config, "DATA_FILE", str(out))
    row = ["2026-11-02T08:15:00+05:30", "N", 1, "A -> B", 1500, 240, None] + [None] * 6
    monkeypatch.setattr(collector, "sample_rows", lambda: (row[0], [row]))
    collector.sample_once()
    collector.sample_once()
    lines = list(csv.reader(out.open()))
    assert lines[0] == providers.CSV_FIELDS and len(lines) == 3


class FakeS3:
    """Two raw files; the older one predates the rain and benchmark columns."""

    base = "timestamp,direction,seg_order,segment,distance_m,duration_s,static_duration_s"
    files = {
        "raw/date=2026-11-02/0830.csv": f"{base},precip_mm\n2026-11-02T08:30:00+05:30,N,2,B -> C,900,120,,0.4\n",
        "raw/date=2026-11-02/0815.csv": f"{base}\n2026-11-02T08:15:00+05:30,N,10,J -> K,800,100,\n"
                                        "2026-11-02T08:15:00+05:30,N,9,I -> J,700,90,\n",
    }

    def get_paginator(self, name):
        files = self.files
        return type("P", (), {"paginate": lambda self, **kw: [{"Contents": [{"Key": k} for k in files]}]})()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.files[Key].encode())}


def test_fetch_s3_merges_old_and_new_files_in_order(tmp_path, monkeypatch):
    import fetch_s3
    out = tmp_path / "tt.csv"
    monkeypatch.setattr(config, "DATA_FILE", str(out))
    monkeypatch.setattr(fetch_s3.boto3, "client", lambda *a, **kw: FakeS3())
    monkeypatch.setattr("sys.argv", ["fetch_s3.py", "bucket"])
    fetch_s3.main()
    rows = list(csv.DictReader(out.open()))
    assert list(rows[0]) == providers.CSV_FIELDS
    assert [r["seg_order"] for r in rows] == ["9", "10", "2"]     # by time, then numeric segment order
    assert rows[0]["precip_mm"] == "" and rows[2]["precip_mm"] == "0.4"


def test_fetch_weather_writes_ist_timestamps(tmp_path, monkeypatch):
    import fetch_weather
    src = tmp_path / "tt.csv"
    src.write_text("timestamp\n2026-11-02T08:15:00+05:30\n2026-11-03T09:00:00+05:30\n")
    seen = []
    monkeypatch.setattr(fetch_weather.urllib.request, "urlopen", fake_urlopen(
        {"minutely_15": {"time": ["2026-11-02T08:15", "2026-11-02T08:30"], "precipitation": [0.0, 1.5]}}, seen))
    monkeypatch.setattr(fetch_weather, "OUT", str(tmp_path / "weather.csv"))
    monkeypatch.setattr("sys.argv", ["fetch_weather.py", str(src)])
    fetch_weather.main()
    assert "start_date=2026-11-02" in seen[0] and "end_date=2026-11-03" in seen[0]
    assert (tmp_path / "weather.csv").read_text().splitlines() == [
        "timestamp,precip_mm", "2026-11-02T08:15:00+05:30,0.0", "2026-11-02T08:30:00+05:30,1.5"]
