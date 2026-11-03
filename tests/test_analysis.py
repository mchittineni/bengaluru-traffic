"""Analysis and forecast, end to end on synthetic data with known, planted bottlenecks."""

import sys

import pandas as pd
import pytest

import analyze
import config
import demo_data
import forecast


def make_demo(tmp_path, monkeypatch, days):
    path = tmp_path / f"demo_{days}.csv"
    monkeypatch.setattr(sys, "argv", ["demo_data.py", "--days", str(days), "--out", str(path)])
    demo_data.main()
    return path


def test_mark_heads_finds_where_the_queue_starts():
    # Travel order 1 -> 4: segments 2 and 3 congested, 4 flowing, so 3 is the head of a
    # 2-segment queue; segment 2 is spillback, not a separate cause.
    ts = pd.Timestamp("2026-11-02 09:00+05:30")
    df = pd.DataFrame({"direction": "N", "date": ts.date(), "bin": "09:00", "timestamp": ts,
                       "seg_order": [1, 2, 3, 4], "congested": [False, True, True, False]})
    out = analyze.mark_heads(df).set_index("seg_order")
    assert out.is_head.tolist() == [False, False, True, False]
    assert out.loc[3, "queue_segments"] == 2


def test_analyze_ranks_the_planted_evening_bottleneck_first(tmp_path, monkeypatch):
    src = make_demo(tmp_path, monkeypatch, days=7)
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(sys, "argv", ["analyze.py", str(src)])
    analyze.main()
    report = (tmp_path / "out" / "bottleneck_report.md").read_text()
    first = next(line for line in report.splitlines() if line.startswith("| 1 |"))
    assert "4. Kadubeesanahalli -> Marathahalli Bridge" in first
    assert (tmp_path / "out" / "heatmap_N.png").exists()


def test_forecast_horizons_follow_the_sample_interval(monkeypatch):
    assert forecast.horizons() == [15, 30, 60]
    monkeypatch.setattr(config, "INTERVAL_MINUTES", 30)
    assert forecast.horizons() == [30, 60]


@pytest.mark.slow
@pytest.mark.parametrize("interval", [15, 30])
def test_forecast_model_beats_live_traffic_baseline(tmp_path, monkeypatch, interval):
    # 30 min is the README's cost-saving setting; it used to break the 15-minute horizon.
    monkeypatch.setattr(config, "INTERVAL_MINUTES", interval)
    monkeypatch.setattr(forecast, "STEP", pd.Timedelta(minutes=interval))
    src = make_demo(tmp_path, monkeypatch, days=35)
    monkeypatch.setattr(forecast, "OUT", str(tmp_path / "forecast"))
    monkeypatch.setattr(sys, "argv", ["forecast.py", str(src), "--test-days", "7"])
    forecast.main()
    m = pd.read_csv(tmp_path / "forecast" / "metrics.csv").set_index(["horizon_min", "method"])
    expected = [15, 30, 60] if interval == 15 else [30, 60]
    assert sorted(set(m.index.get_level_values("horizon_min"))) == expected
    # "now" can only be exact if the target were the present: a horizon of zero steps.
    assert (m.xs("now", level="method").mape > 0).all()
    for h in (30, 60):
        assert m.loc[(h, "model"), "mape_peak"] < m.loc[(h, "now"), "mape_peak"]
    assert (tmp_path / "forecast" / "example.png").exists()
