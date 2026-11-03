"""Turn collected travel times into congestion heatmaps and a bottleneck ranking.

Usage:
    python analyze.py [path/to/travel_times.csv]

Outputs (in output/):
    heatmap_<dir>.png       typical weekday speed by segment and time of day
    daily_<dir>.png         the same, one panel per collected day
    bottleneck_report.md    ranked bottleneck heads with onset, clearance and delay

A bottleneck "head" is a congested segment whose downstream neighbour is flowing.
That is where the queue starts; congested segments upstream of it are the queue
spilling back, not separate causes.
"""

import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import config

DIR_LABEL = {"N": "Silk Board -> KR Puram", "S": "KR Puram -> Silk Board"}


def load(path):
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["date"] = df["timestamp"].dt.date
    df["weekday"] = df["timestamp"].dt.day_of_week < 5
    df["bin"] = df["timestamp"].dt.floor(f"{config.INTERVAL_MINUTES}min").dt.strftime("%H:%M")
    df["speed_kmph"] = df["distance_m"] / df["duration_s"] * 3.6
    df["congested"] = df["speed_kmph"] < config.CONGESTED_KMPH
    # Free-flow time per segment: 5th percentile of observed times (more robust
    # than the API's staticDuration, which ignores Bengaluru's side friction).
    ff = df.groupby(["direction", "seg_order"])["duration_s"].quantile(0.05).rename("ff_s")
    df = df.join(ff, on=["direction", "seg_order"])
    df["delay_s"] = (df["duration_s"] - df["ff_s"]).clip(lower=0)
    return df


def mark_heads(df):
    """Flag bottleneck heads and the length of the queue behind each one."""
    out = []
    for (_, _, _), g in df.groupby(["direction", "date", "bin"]):
        g = g.sort_values("seg_order").copy()
        cong = g["congested"].tolist()
        n = len(cong)
        heads, queue = [], []
        for i in range(n):
            is_head = cong[i] and (i == n - 1 or not cong[i + 1])
            q = 0
            if is_head:
                j = i
                while j >= 0 and cong[j]:
                    q, j = q + 1, j - 1
            heads.append(is_head)
            queue.append(q)
        g["is_head"], g["queue_segments"] = heads, queue
        out.append(g)
    return pd.concat(out)


def plot_heatmap(ax, pivot, names, title):
    im = ax.imshow(pivot.values, aspect="auto", origin="lower", cmap="RdYlGn",
                   vmin=0, vmax=45, interpolation="nearest")
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels(names, fontsize=7)
    step = max(1, len(pivot.columns) // 12)
    ax.set_xticks(range(0, len(pivot.columns), step))
    ax.set_xticklabels(pivot.columns[::step], rotation=45, fontsize=7)
    ax.set_title(title, fontsize=9)
    return im


def segment_names(df, direction):
    s = df[df.direction == direction].drop_duplicates("seg_order").sort_values("seg_order")
    return [f"{o}. {n}" for o, n in zip(s.seg_order, s.segment)]


def heatmaps(df):
    paths = {}
    for d in sorted(df.direction.unique()):
        dd = df[df.direction == d]
        names = segment_names(df, d)
        base = dd[dd.weekday] if dd.weekday.any() else dd
        typical = base.pivot_table(index="seg_order", columns="bin",
                                   values="speed_kmph", aggfunc="median")
        fig, ax = plt.subplots(figsize=(12, 4))
        im = plot_heatmap(ax, typical, names,
                          f"Typical weekday speed (median), {DIR_LABEL.get(d, d)}  "
                          "(travel direction: bottom -> top)")
        fig.colorbar(im, ax=ax, label="km/h")
        fig.tight_layout()
        paths[f"heatmap_{d}"] = os.path.join(config.OUTPUT_DIR, f"heatmap_{d}.png")
        fig.savefig(paths[f"heatmap_{d}"], dpi=130)
        plt.close(fig)

        dates = sorted(dd.date.unique())
        fig, axes = plt.subplots(len(dates), 1, figsize=(12, 2.2 * len(dates)), squeeze=False)
        for ax, day in zip(axes[:, 0], dates):
            p = dd[dd.date == day].pivot_table(index="seg_order", columns="bin",
                                               values="speed_kmph", aggfunc="median")
            plot_heatmap(ax, p, names, f"{day:%a %d %b}")
        fig.tight_layout()
        paths[f"daily_{d}"] = os.path.join(config.OUTPUT_DIR, f"daily_{d}.png")
        fig.savefig(paths[f"daily_{d}"], dpi=110)
        plt.close(fig)
    return paths


def ranking(df):
    rows = []
    weekdays = df[df.weekday] if df.weekday.any() else df
    n_days = max(1, weekdays.date.nunique())
    n_seg = df.groupby("direction").seg_order.max()
    for (d, order, name), g in weekdays.groupby(["direction", "seg_order", "segment"]):
        heads = g[g.is_head]
        if heads.empty:
            continue
        onsets = heads.groupby("date").timestamp.min().dt.strftime("%H:%M")
        ends = heads.groupby("date").timestamp.max().dt.strftime("%H:%M")
        typical_delay = g.groupby("bin").delay_s.median()
        rows.append({
            "direction": d,
            "segment": f"{order}. {name}",
            "head_min_per_day": len(heads) * config.INTERVAL_MINUTES / n_days,
            "days_active": f"{heads.date.nunique()}/{n_days}",
            "typical_onset": sorted(onsets)[len(onsets) // 2],
            "typical_clearance": sorted(ends)[len(ends) // 2],
            "max_queue_segments": int(heads.queue_segments.max()),
            "peak_delay_min": typical_delay.max() / 60,
            "peak_at": typical_delay.idxmax(),
            "at_corridor_end": order == n_seg[d],
        })
    return pd.DataFrame(rows).sort_values("head_min_per_day", ascending=False)


def report(df, rank, paths):
    days = sorted(df.date.unique())
    lines = [
        "# ORR bottleneck report (Silk Board - KR Puram)",
        "",
        f"Data: {len(days)} days ({days[0]} to {days[-1]}), "
        f"{df.weekday.groupby(df.date).first().sum()} weekdays, "
        f"{config.INTERVAL_MINUTES}-minute samples. Congested = below {config.CONGESTED_KMPH:.0f} km/h.",
        "",
        "## Ranked bottleneck heads (weekdays)",
        "",
        "A head is where the queue starts: the segment is congested but the next one downstream is not.",
        "",
        "| Rank | Direction | Segment | Active as head (min/day) | Days | Onset | Clearance "
        "| Longest queue (segments) | Peak delay per traveller | Peak at |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rank.itertuples(), 1):
        seg = r.segment + (" *" if r.at_corridor_end else "")
        lines.append(
            f"| {i} | {DIR_LABEL.get(r.direction, r.direction)} | {seg} | {r.head_min_per_day:.0f} "
            f"| {r.days_active} | {r.typical_onset} | {r.typical_clearance} "
            f"| {r.max_queue_segments} | {r.peak_delay_min:.1f} min | {r.peak_at} |"
        )
    if rank.empty:
        lines.append("| - | - | No segment fell below the congestion threshold | | | | | | | |")
    lines += [
        "",
        "\\* Last segment of the corridor. The real cause may be just beyond the study area "
        "(e.g. the junction itself); extend the corridor or add the cross streets to confirm.",
        "",
        "## How to read this",
        "",
        "- **Long head time + long queue**: a recurring capacity bottleneck. Look at what changes "
        "at that location: lane drop, merge, U-turn, bus stop, signal.",
        "- **Head on only some days**: a non-recurring cause (incident, rain, event, roadwork). "
        "Check the daily heatmaps for those dates.",
        "- **Queue reaching an upstream junction**: spillback. Fixing the head clears the whole queue.",
        "",
        "## Charts",
        "",
    ]
    lines += [f"![{k}]({os.path.basename(v)})" for k, v in sorted(paths.items())]
    path = os.path.join(config.OUTPUT_DIR, "bottleneck_report.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else config.DATA_FILE
    if not os.path.exists(src):
        sys.exit(f"No data at {src}. Run collector.py or demo_data.py first.")
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    df = mark_heads(load(src))
    paths = heatmaps(df)
    rank = ranking(df)
    path = report(df, rank, paths)
    print(rank.to_string(index=False) if not rank.empty else "No bottleneck heads found.")
    print(f"\nReport: {path}")


if __name__ == "__main__":
    main()
