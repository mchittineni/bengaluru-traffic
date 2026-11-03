"""Forecast ORR segment travel times 15, 30 and 60 minutes ahead.

Forecasters, scored on the last --test-days days (never seen in training):
  now      travel time right now, assumed to stay the same (what a live-traffic
           ETA does at the moment you leave)
  profile  typical travel time for that segment, weekday/weekend and time of day
  model    gradient-boosted trees using recent travel times on this segment and
           its neighbours (queues spill back upstream), the profile, time of day and rain
  provider the routing provider's own estimate for that departure time, logged by the
           collector (config.BENCHMARK_HORIZONS_MIN); only when the data has it

Rain comes from the precip_* columns the collector logs. For older data without them,
run fetch_weather.py and pass --weather data/weather.csv; observed rain then stands in
for the rain forecast, which flatters the model (the report says so).

Usage:
    python forecast.py [path/to/travel_times.csv] [--test-days 14] [--weather data/weather.csv]

Outputs (in output/forecast/):
    report.md       error per method and horizon
    metrics.csv     the same numbers
    example.png     one held-out day: actual vs each forecast
    models.joblib   trained models, free-flow times and profile, for serving later
"""

import argparse
import os

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

import config

OUT = "output/forecast"
STEP = pd.Timedelta(minutes=config.INTERVAL_MINUTES)
HORIZONS_MIN = [15, 30, 60]   # horizons that are not a multiple of INTERVAL_MINUTES are skipped
EXAMPLE_HORIZON_MIN = 30
PEAK_HOURS = [(7, 11), (17, 21)]
CONGESTED_RATIO = 2.0   # travel time at least twice free-flow
FEATURES = ["seg", "r_now", "r_lag1", "r_lag2", "r_lag4", "r_up", "r_down", "r_down2",
            "r_dir_mean", "prof_now", "prof_target", "hour_sin", "hour_cos", "weekday",
            "rain_now", "rain_last_hour", "rain_target"]


def load_wide(path):
    """Travel time in seconds: one row per timestamp, one column per (direction, segment)."""
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    W = df.pivot_table(index="timestamp", columns=["direction", "seg_order"],
                       values="duration_s", aggfunc="mean").sort_index()
    names = df.drop_duplicates(["direction", "seg_order"]).set_index(["direction", "seg_order"])["segment"]
    return W, names, df


def load_rain(df, weather_path):
    """Rain per timestamp: observed precip_mm plus any logged forecasts (precip_fc_<h>_mm)."""
    cols = [c for c in df.columns if c.startswith("precip_")]
    if cols and df["precip_mm"].notna().any():
        return df.groupby("timestamp")[cols].mean(), "logged with each sample"
    if weather_path:
        w = pd.read_csv(weather_path)
        w["timestamp"] = pd.to_datetime(w["timestamp"])
        return w.set_index("timestamp")[["precip_mm"]], f"backfilled from {weather_path}"
    return pd.DataFrame({"precip_mm": []}, index=pd.DatetimeIndex([], tz=df.timestamp.dt.tz)), "none"


def load_provider(df, h_min):
    """Provider estimate (s) made at t for a departure h_min later, per (direction, segment)."""
    col = f"provider_eta_{h_min}_s"
    if col not in df.columns or df[col].isna().all():
        return None
    return df.pivot_table(index="timestamp", columns=["direction", "seg_order"], values=col, aggfunc="mean")


def horizons():
    return [h for h in HORIZONS_MIN if h % config.INTERVAL_MINUTES == 0]


def shifted(R, steps):
    """Values at t + steps*STEP, aligned to t (NaN where that sample doesn't exist)."""
    return pd.DataFrame(R.reindex(R.index + steps * STEP).values, index=R.index, columns=R.columns)


def profile_keys(times):
    return pd.MultiIndex.from_arrays([times.day_of_week < 5, times.strftime("%H:%M")])


def profile_table(R):
    return R.groupby(profile_keys(R.index)).median()


def profile_at(P, times):
    return pd.DataFrame(P.reindex(profile_keys(times)).values, index=times, columns=P.columns)


def build(R, P, h, rain, provider):
    """One row per (time t, segment): features known at t, target = ratio at t + h steps."""
    cols = list(R.columns)
    precip = rain["precip_mm"]
    rain_now = precip.reindex(R.index).values
    rain_last_hour = sum(np.nan_to_num(precip.reindex(R.index - k * STEP).values) for k in range(4))
    rain_last_hour = np.where(np.isnan(rain_now), np.nan, rain_last_hour)
    # Rain at the target time: the logged forecast if there is one, else what was observed.
    fc_col = f"precip_fc_{h * config.INTERVAL_MINUTES}_mm"
    observed_target = precip.reindex(R.index + h * STEP).values
    forecast = rain[fc_col].reindex(R.index).values if fc_col in rain else np.full(len(R), np.nan)
    rain_target = np.where(np.isnan(forecast), observed_target, forecast)
    build.observed_share = float(np.mean(np.isnan(forecast) & ~np.isnan(observed_target)))
    lags = {k: shifted(R, -k) for k in (1, 2, 4)}
    target = shifted(R, h)
    prof_now = profile_at(P, R.index)
    prof_tgt = profile_at(P, R.index + h * STEP)
    tt = R.index + h * STEP
    hour = tt.hour + tt.minute / 60
    frames = []
    for i, (d, o) in enumerate(cols):
        f = pd.DataFrame(index=R.index)
        f["seg"] = i
        f["r_now"] = R[(d, o)]
        for k, lag in lags.items():
            f[f"r_lag{k}"] = lag[(d, o)].values
        for name, off in (("r_up", -1), ("r_down", 1), ("r_down2", 2)):
            f[name] = R[(d, o + off)] if (d, o + off) in R.columns else np.nan
        f["r_dir_mean"] = R[[c for c in cols if c[0] == d]].mean(axis=1)
        f["prof_now"] = prof_now[(d, o)].values
        f["prof_target"] = prof_tgt[(d, o)].values
        f["hour_sin"] = np.sin(2 * np.pi * hour / 24)
        f["hour_cos"] = np.cos(2 * np.pi * hour / 24)
        f["weekday"] = (tt.day_of_week < 5).astype(int)
        f["rain_now"], f["rain_last_hour"], f["rain_target"] = rain_now, rain_last_hour, rain_target
        f["provider_s"] = (provider[(d, o)].reindex(R.index).values
                           if provider is not None and (d, o) in provider.columns else np.nan)
        f["target_hour"] = hour
        f["direction"], f["seg_order"] = d, o
        f["y"] = target[(d, o)].values
        frames.append(f)
    X = pd.concat(frames)
    return X[X.y.notna() & X.r_now.notna()]


def errors(actual, pred):
    err = np.abs(pred - actual)
    return err.mean(), 100 * (err / actual).mean()


def evaluate(test, preds, h_min):
    rows = []
    peak = (test.weekday == 1) & np.any([(test.target_hour >= a) & (test.target_hour < b)
                                         for a, b in PEAK_HOURS], axis=0)
    congested = test.y >= CONGESTED_RATIO
    actual_s = test.y * test.ff
    for method, pred_r in preds.items():
        pred_s = pred_r * test.ff
        mae, mape = errors(actual_s, pred_s)
        _, mape_peak = errors(actual_s[peak], pred_s[peak])
        _, mape_cong = errors(actual_s[congested], pred_s[congested])
        # Whole corridor: sum the segments for each direction and time.
        trip = pd.DataFrame({"a": actual_s, "p": pred_s, "d": test.direction, "t": test.index})
        trip = trip.groupby(["d", "t"]).agg(a=("a", "sum"), p=("p", "sum"), n=("a", "size"))
        trip = trip[trip.n == trip.n.max()]
        _, mape_trip = errors(trip.a, trip.p)
        rows.append({"horizon_min": h_min, "method": method, "mae_s": mae, "mape": mape,
                     "mape_peak": mape_peak, "mape_congested": mape_cong, "mape_corridor": mape_trip})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?", default=config.DATA_FILE)
    ap.add_argument("--test-days", type=int, default=14)
    ap.add_argument("--weather", help="rain backfill CSV from fetch_weather.py")
    args = ap.parse_args()

    W, names, df = load_wide(args.data)
    rain, rain_source = load_rain(df, args.weather)
    dates = sorted(set(W.index.date))
    if len(dates) <= args.test_days + 7:
        raise SystemExit(f"Only {len(dates)} days of data; need at least {args.test_days + 8} "
                         f"(or lower --test-days).")
    test_dates = set(dates[-args.test_days:])
    is_test = np.array([d in test_dates for d in W.index.date])
    print(f"{len(dates)} days: training on {len(dates) - args.test_days}, testing on {args.test_days}")

    # Ratios to free-flow time, so all segments share one scale. Free-flow time and the
    # profile come from training days only.
    ff = W[~is_test].quantile(0.05)
    R = W / ff
    P = profile_table(R[~is_test])

    skipped = sorted(set(HORIZONS_MIN) - set(horizons()))
    if skipped:
        print(f"Skipping horizons {skipped} min: not a multiple of the {config.INTERVAL_MINUTES}-min sample interval")
    if not horizons():
        raise SystemExit("No forecast horizon fits the sample interval; adjust HORIZONS_MIN.")

    os.makedirs(OUT, exist_ok=True)
    metrics, models, example, rain_notes = [], {}, {}, {}
    for h_min in horizons():
        h = h_min // config.INTERVAL_MINUTES
        X = build(R, P, h, rain, load_provider(df, h_min))
        rain_notes[h_min] = build.observed_share
        X["ff"] = [ff[(d, o)] for d, o in zip(X.direction, X.seg_order)]
        test_mask = np.array([d in test_dates for d in X.index.date])
        train, test = X[~test_mask], X[test_mask]

        model = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05,
                                              categorical_features=[0], random_state=0)
        model.fit(train[FEATURES], np.log(train.y))
        if test.provider_s.notna().any():
            # Score every method on the same rows: those with a provider estimate.
            test = test[test.provider_s.notna()]
        preds = {
            "now": test.r_now,
            "profile": test.prof_target.fillna(test.r_now),
            "model": pd.Series(np.exp(model.predict(test[FEATURES])), index=test.index),
        }
        if test.provider_s.notna().any():
            preds["provider"] = test.provider_s / test.ff
        metrics += evaluate(test, preds, h_min)
        models[h_min] = model
        if h_min == EXAMPLE_HORIZON_MIN or not example:
            example = {"test": test, "preds": preds, "h_min": h_min}
        print(f"  {h_min:2d} min ahead: trained on {len(train):,} rows, tested on {len(test):,}")

    m = pd.DataFrame(metrics)
    m.to_csv(f"{OUT}/metrics.csv", index=False)
    joblib.dump({"models": models, "free_flow_s": ff, "profile": P, "features": FEATURES,
                 "columns": list(R.columns)}, f"{OUT}/models.joblib")
    plot_example(example, names)
    write_report(m, len(dates), args.test_days, args.data, rain_source, rain_notes)
    print(m.round(1).to_string(index=False))
    print(f"\nReport: {OUT}/report.md")


def plot_example(example, names):
    test, preds, h_min = example["test"], example["preds"], example["h_min"]
    # The most congested held-out weekday, on its most congested segment.
    wk = test[test.weekday == 1]
    day = wk.groupby(wk.index.date).y.mean().idxmax()
    seg = wk[wk.index.date == day].groupby(["direction", "seg_order"]).y.mean().idxmax()
    sel = (test.index.date == day) & (test.direction == seg[0]) & (test.seg_order == seg[1])
    t = test.index[sel] + pd.Timedelta(minutes=h_min)   # plot each forecast at the time it is for
    ff = test.ff[sel]
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.plot(t, test.y[sel] * ff / 60, color="black", lw=2, label="actual")
    styles = {"now": ("tab:red", "--"), "profile": ("tab:gray", ":"), "model": ("tab:blue", "-"),
              "provider": ("tab:green", "-.")}
    for method, (color, ls) in styles.items():
        if method not in preds:
            continue
        ax.plot(t, preds[method][sel] * ff / 60, color=color, ls=ls, label=f"{method} ({h_min} min ahead)")
    ax.set_ylabel("travel time (min)")
    ax.set_title(f"{day:%a %d %b}: {names[seg]} ({seg[0]})  forecasts made {h_min} minutes earlier")
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(f"{OUT}/example.png", dpi=120)
    plt.close(fig)


def write_report(m, n_days, test_days, source, rain_source, rain_notes):
    lines = [
        "# ORR travel-time forecast",
        "",
        f"Data: `{source}`, {n_days} days. Trained on the first {n_days - test_days}, "
        f"scored on the last {test_days} (not used in training).",
        "",
        f"Rain: {rain_source}." + (
            " Where no rain forecast was logged, observed rain stands in for the forecast "
            f"({', '.join(f'{h} min: {100 * v:.0f}% of rows' for h, v in rain_notes.items())}), "
            "which makes the model look better than it will be live."
            if any(v > 0 for v in rain_notes.values()) else ""),
        "",
        "Error = mean absolute % error of segment travel time, unless stated. "
        "Lower is better. **now** is the live-traffic baseline a navigation app uses at departure.",
        "",
        "| Ahead | Method | MAE (s) | All | Peak hours | Congested | Whole corridor |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in m.itertuples():
        bold = "**" if r.method == "model" else ""
        lines.append(f"| {r.horizon_min} min | {bold}{r.method}{bold} | {r.mae_s:.0f} | {r.mape:.1f}% "
                     f"| {r.mape_peak:.1f}% | {r.mape_congested:.1f}% | {r.mape_corridor:.1f}% |")
    for baseline, title in (("now", "live-traffic baseline"), ("provider", "provider's own forecast")):
        if baseline not in set(m.method):
            continue
        lines += ["", f"## Model vs {title}", ""]
        for h in horizons():
            base = m[(m.horizon_min == h) & (m.method == baseline)]
            if base.empty:
                lines.append(f"- **{h} min ahead:** no {baseline} estimates logged for this horizon.")
                continue
            base, mod = base.iloc[0], m[(m.horizon_min == h) & (m.method == "model")].iloc[0]
            lines.append(f"- **{h} min ahead:** peak-hour error {base.mape_peak:.1f}% -> {mod.mape_peak:.1f}% "
                         f"({100 * (1 - mod.mape_peak / base.mape_peak):.0f}% lower); "
                         f"whole-corridor error {base.mape_corridor:.1f}% -> {mod.mape_corridor:.1f}%.")
    lines += [
        "",
        "## Notes",
        "",
        "- 'Whole corridor' sums every segment at the same moment; a real trip reaches later "
        "segments later, which the app will handle by chaining forecasts along the route.",
        "- **provider** is the routing provider's estimate for that departure time, logged in "
        "advance by the collector. Where it is present, all methods are scored on those rows only. "
        "In the synthetic demo data it is SIMULATED and says nothing about any real provider.",
        "",
        "![example](example.png)",
    ]
    with open(f"{OUT}/report.md", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
