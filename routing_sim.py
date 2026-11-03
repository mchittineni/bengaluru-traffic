"""Phase 1 proof: Waze-style routing vs coordinated routing on the real ORR road network.

Static morning-peak traffic assignment (one hour) on the OpenStreetMap network from
fetch_network.py. Every scenario uses the same trips; only the routing rule changes:

  habitual      everyone drives the route that is fastest on empty roads (no app)
  waze          every driver takes the fastest route given current traffic
                (user equilibrium: what selfish real-time navigation converges to)
  coordinated   routes minimise total delay for everyone (system optimum)
  coord+protect coordinated, and residential streets are kept well below capacity
  mixed N%      N% of drivers use coord+protect, the rest are Waze-style

Travel time per road: BPR curve t = t0 * (1 + 0.15 (v/c)^4).
Coordinated drivers are routed on marginal cost t + v * dt/dv: their own time plus
the delay they add to everyone already on that road.

Usage:
    python routing_sim.py [--trips 60000] [--seed 7]

Outputs go to output/routing/.
"""

import argparse
import os
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import osmnx as ox
from matplotlib.collections import LineCollection
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

NETWORK = "data/orr_network.graphml"
OUT = "output/routing"

# highway type: (free-flow km/h, capacity PCU/h per lane, default lanes per direction).
# Speeds and capacities are rough values for Bengaluru mixed traffic, not measurements.
ROAD = {
    "motorway": (60, 2000, 3),
    "trunk": (45, 1600, 3),
    "primary": (40, 1400, 2),
    "secondary": (32, 1200, 2),
    "tertiary": (28, 1000, 1),
    "unclassified": (22, 700, 1),
    "residential": (20, 500, 1),
}
LINK = (30, 1200, 1)
NEIGHBOURHOOD = {"residential", "unclassified"}
ALPHA, BETA = 0.15, 4.0

# Residential streets are routed as if they had this share of their capacity
# when the "protect" rule is on, so coordinated routing keeps them lightly loaded.
PROTECT_CAPACITY_SHARE = 0.5

# Approximate morning-peak destinations (lat, lng, relative weight).
JOB_CENTRES = [
    ("Ecospace / RMZ Ecoworld, Bellandur", 12.9265, 77.6810, 3),
    ("Prestige Tech Park, Kadubeesanahalli", 12.9400, 77.6970, 3),
    ("Embassy Golf Links, Domlur", 12.9540, 77.6420, 2),
    ("Bagmane Tech Park, CV Raman Nagar", 12.9810, 77.6615, 2),
    ("Mahadevapura", 12.9900, 77.7000, 2),
    ("Koramangala", 12.9350, 77.6240, 1),
]
JOB_SHARE = 0.7          # remaining trips go to random destinations
N_ORIGINS = 80
N_RANDOM_DESTS = 30
NODES_PER_CENTRE = 4


def first_token(v):
    """OSM tags may be lists, or lists saved as strings: return the first value."""
    if isinstance(v, list):
        v = v[0]
    m = re.search(r"[A-Za-z0-9_.]+", str(v))
    return m.group(0) if m else ""


class Network:
    def __init__(self, source):
        """`source`: a GraphML path from fetch_network.py, or an osmnx-style MultiDiGraph."""
        G = ox.load_graphml(source) if isinstance(source, str) else source
        nodes = list(G.nodes)
        idx = {n: i for i, n in enumerate(nodes)}
        self.n = len(nodes)
        self.x = np.array([G.nodes[n]["x"] for n in nodes])
        self.y = np.array([G.nodes[n]["y"] for n in nodes])

        # Collapse parallel edges, keeping the shortest, so (u, v) identifies an edge.
        best = {}
        for u, v, d in G.edges(data=True):
            if u == v:
                continue
            key = (idx[u], idx[v])
            if key not in best or d["length"] < best[key]["length"]:
                best[key] = d

        u, v, length, speed, cap, cls, orr, geom = [], [], [], [], [], [], [], []
        for (a, b), d in best.items():
            hw = first_token(d.get("highway"))
            spd, per_lane, default_lanes = LINK if hw.endswith("_link") else ROAD.get(hw, ROAD["residential"])
            lanes = default_lanes
            if d.get("lanes") is not None:
                try:
                    total = int(float(first_token(d["lanes"])))
                    oneway = str(d.get("oneway")) in ("True", "true", "yes")
                    lanes = max(1, total if oneway else total // 2)
                except ValueError:
                    pass
            u.append(a)
            v.append(b)
            length.append(d["length"])
            speed.append(spd)
            cap.append(lanes * per_lane)
            cls.append(hw.removesuffix("_link"))
            orr.append("outer ring road" in str(d.get("name", "")).lower())
            g = d.get("geometry")
            geom.append(np.array(g.coords) if g is not None else
                        np.array([[self.x[a], self.y[a]], [self.x[b], self.y[b]]]))

        self.u, self.v = np.array(u), np.array(v)
        self.length_km = np.array(length) / 1000
        self.t0 = self.length_km / np.array(speed)          # hours
        self.cap = np.array(cap, dtype=float)
        self.cls = np.array(cls)
        self.neighbourhood = np.isin(self.cls, list(NEIGHBOURHOOD))
        self.orr = np.array(orr)
        self.geom = geom
        self.E = len(self.u)
        keys = self.u * self.n + self.v
        self._order = np.argsort(keys)
        self._keys = keys[self._order]

    def edge_id(self, a, b):
        return self._order[np.searchsorted(self._keys, a * self.n + b)]

    def nearest_nodes(self, lat, lng, k):
        d = (self.y - lat) ** 2 + ((self.x - lng) * np.cos(np.radians(lat))) ** 2
        return np.argsort(d)[:k]


class Demand:
    """Origin-destination trips (PCU in the peak hour)."""

    def __init__(self, net, total_trips, rng):
        # Origins: random nodes on neighbourhood streets, where commuters start.
        candidates = np.unique(net.u[net.neighbourhood])
        self.origins = rng.choice(candidates, N_ORIGINS, replace=False)
        origin_weight = rng.lognormal(0, 0.5, N_ORIGINS)
        origin_trips = total_trips * origin_weight / origin_weight.sum()

        dests, dest_w = [], []
        centre_total = sum(c[3] for c in JOB_CENTRES)
        for _, lat, lng, w in JOB_CENTRES:
            for node in net.nearest_nodes(lat, lng, NODES_PER_CENTRE):
                dests.append(node)
                dest_w.append(JOB_SHARE * w / centre_total / NODES_PER_CENTRE)
        for node in rng.choice(net.n, N_RANDOM_DESTS, replace=False):
            dests.append(node)
            dest_w.append((1 - JOB_SHARE) / N_RANDOM_DESTS)
        dests, dest_w = np.array(dests), np.array(dest_w)

        rows, d_nodes, trips = [], [], []
        for r, (o, t) in enumerate(zip(self.origins, origin_trips)):
            keep = dests != o
            w = dest_w[keep] / dest_w[keep].sum()
            rows += [r] * keep.sum()
            d_nodes += list(dests[keep])
            trips += list(t * w)
        self.row, self.dest, self.trips = np.array(rows), np.array(d_nodes), np.array(trips)
        self.total = self.trips.sum()

    def scaled(self, share):
        out = object.__new__(Demand)
        out.origins, out.row, out.dest = self.origins, self.row, self.dest
        out.trips = self.trips * share
        out.total = out.trips.sum()
        return out


def all_or_nothing(net, demand, cost):
    """Load every trip onto its cheapest path under `cost`; return flow per edge."""
    graph = csr_matrix((cost + 1e-9, (net.u, net.v)), shape=(net.n, net.n))
    _, pred = dijkstra(graph, directed=True, indices=demand.origins, return_predecessors=True)
    flow = np.zeros(net.E)
    rows, cur, w = demand.row, demand.dest, demand.trips
    while len(cur):
        p = pred[rows, cur]
        ok = p >= 0
        rows, cur, w, p = rows[ok], cur[ok], w[ok], p[ok]
        flow += np.bincount(net.edge_id(p, cur), weights=w, minlength=net.E)
        cur = p
        moving = cur != demand.origins[rows]
        rows, cur, w = rows[moving], cur[moving], w[moving]
    return flow


def travel_time(net, x, cap=None):
    cap = net.cap if cap is None else cap
    return net.t0 * (1 + ALPHA * (x / cap) ** BETA)


def class_cost(net, x, cls):
    """Routing cost for one driver class at total flow x."""
    t = travel_time(net, x, cls["cap"])
    if cls["coordinated"]:
        t = t + net.t0 * ALPHA * BETA * (x / cls["cap"]) ** BETA   # + v * dt/dv
    return t


def make_class(net, demand, coordinated=False, protect=False):
    cap = net.cap.copy()
    if protect:
        cap[net.neighbourhood] *= PROTECT_CAPACITY_SHARE
    return {"demand": demand, "coordinated": coordinated, "cap": cap}


def assign(net, classes, max_iter=60, tol=1e-3):
    """Multi-class Frank-Wolfe: each class's flows move toward its cheapest paths."""
    xs = [all_or_nothing(net, c["demand"], net.t0) for c in classes]
    gap = 1.0
    for it in range(1, max_iter + 1):  # noqa: B007 - `it` is returned as the iteration count
        x = sum(xs)
        costs = [class_cost(net, x, c) for c in classes]
        ys = [all_or_nothing(net, c["demand"], cost) for c, cost in zip(classes, costs)]
        ds = [y - xk for y, xk in zip(ys, xs)]
        gap = -sum(c @ d for c, d in zip(costs, ds)) / sum(c @ xk for c, xk in zip(costs, xs))
        if gap < tol:
            break
        dx = sum(ds)

        def slope(lam, x=x, dx=dx, ds=ds):
            z = x + lam * dx
            return sum(class_cost(net, z, c) @ d for c, d in zip(classes, ds))

        lo, hi = 0.0, 1.0
        if slope(1.0) > 0:
            for _ in range(30):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if slope(mid) < 0 else (lo, mid)
        lam = (lo + hi) / 2 if hi < 1.0 else 1.0
        xs = [xk + lam * d for xk, d in zip(xs, ds)]
    return xs, gap, it


def metrics(net, name, xs, classes, gap, iters):
    x = sum(xs)
    t = travel_time(net, x)
    vc = x / net.cap
    total_trips = sum(c["demand"].total for c in classes)
    nb = net.neighbourhood
    row = {
        "scenario": name,
        "avg_trip_min": 60 * (x @ t) / total_trips,
        "delay_veh_h": x @ (t - net.t0),
        "nbhd_veh_km": x[nb] @ net.length_km[nb],
        "nbhd_km_overloaded": net.length_km[nb & (vc > 0.8)].sum(),
        "orr_avg_vc": (x[net.orr] @ vc[net.orr]) / max(x[net.orr].sum(), 1e-9),
        "km_over_capacity": net.length_km[vc > 1].sum(),
        "gap": gap,
        "iterations": iters,
    }
    if len(classes) == 2:
        for label, xk, c in (("waze_users", xs[0], classes[0]), ("app_users", xs[1], classes[1])):
            row[f"{label}_trip_min"] = 60 * (xk @ t) / c["demand"].total
    return row, x


def draw(ax, net, x, title, vmax=1.2):
    vc = x / net.cap
    order = np.argsort(x)                       # busiest roads drawn on top
    lc = LineCollection([net.geom[i] for i in order], cmap="RdYlGn_r",
                        linewidths=0.3 + 3.0 * np.sqrt(x[order] / x.max()))
    lc.set_array(np.clip(vc[order], 0, vmax))
    lc.set_clim(0, vmax)
    ax.add_collection(lc)
    ax.set_xlim(net.x.min(), net.x.max())
    ax.set_ylim(net.y.min(), net.y.max())
    ax.set_aspect(1 / np.cos(np.radians(net.y.mean())))
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=10)
    return lc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trips", type=float, default=60000, help="peak-hour trips (PCU)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    net = Network(NETWORK)
    demand = Demand(net, args.trips, np.random.default_rng(args.seed))
    print(f"Network: {net.n} nodes, {net.E} edges ({net.orr.sum()} ORR edges). "
          f"Demand: {demand.total:.0f} trips, {len(demand.trips)} OD pairs.")

    results, flows = [], {}

    def run(name, classes):
        if name == "habitual":
            xs, gap, iters = [all_or_nothing(net, classes[0]["demand"], net.t0)], float("nan"), 0
        else:
            xs, gap, iters = assign(net, classes)
        row, x = metrics(net, name, xs, classes, gap, iters)
        results.append(row)
        flows[name] = x
        print(f"{name:22s} avg trip {row['avg_trip_min']:5.1f} min  delay {row['delay_veh_h']:8.0f} veh-h  "
              f"nbhd {row['nbhd_veh_km']:7.0f} veh-km  gap {gap:.4f} ({iters} it)", flush=True)

    run("habitual", [make_class(net, demand)])
    run("waze", [make_class(net, demand)])
    run("coordinated", [make_class(net, demand, coordinated=True)])
    run("coord+protect", [make_class(net, demand, coordinated=True, protect=True)])
    for p in (0.1, 0.25, 0.5):
        run(f"mixed {int(p * 100)}% app", [make_class(net, demand.scaled(1 - p)),
                                           make_class(net, demand.scaled(p), coordinated=True, protect=True)])

    import csv
    keys = list(dict.fromkeys(k for r in results for k in r))
    with open(f"{OUT}/results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        w.writerows(results)

    fig, axes = plt.subplots(1, 3, figsize=(18, 7))
    for ax, name in zip(axes, ("habitual", "waze", "coord+protect")):
        lc = draw(ax, net, flows[name], name)
    fig.colorbar(lc, ax=axes, shrink=0.6, label="volume / capacity")
    fig.suptitle("Morning peak: road load under each routing rule (line width = traffic volume)")
    fig.savefig(f"{OUT}/maps.png", dpi=120, bbox_inches="tight")
    plt.close(fig)

    write_report(results, args, net, demand)
    print(f"\nReport: {OUT}/report.md")


def write_report(results, args, net, demand):
    base = next(r for r in results if r["scenario"] == "waze")
    lines = [
        "# Waze-style vs coordinated routing: ORR area, morning peak",
        "",
        f"Network: OpenStreetMap, {net.n} junctions, {net.E} road links. "
        f"Demand: {demand.total:,.0f} synthetic peak-hour trips (seed {args.seed}).",
        "",
        "| Scenario | Avg trip (min) | Total delay (veh-h) | vs Waze | Residential veh-km | vs Waze "
        "| Residential km at >80% capacity | ORR avg v/c | Road km over capacity |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['scenario']} | {r['avg_trip_min']:.1f} | {r['delay_veh_h']:,.0f} "
            f"| {100 * (r['delay_veh_h'] / base['delay_veh_h'] - 1):+.0f}% "
            f"| {r['nbhd_veh_km']:,.0f} | {100 * (r['nbhd_veh_km'] / base['nbhd_veh_km'] - 1):+.0f}% "
            f"| {r['nbhd_km_overloaded']:.1f} | {r['orr_avg_vc']:.2f} | {r['km_over_capacity']:.1f} |"
        )
    mixed = [r for r in results if "app_users_trip_min" in r]
    if mixed:
        lines += ["", "## Mixed adoption: who benefits", "",
                  "| Scenario | Waze-style drivers (min) | App drivers (min) |", "|---|---|---|"]
        lines += [f"| {r['scenario']} | {r['waze_users_trip_min']:.1f} | {r['app_users_trip_min']:.1f} |"
                  for r in mixed]
    lines += [
        "",
        "## Caveats",
        "",
        "- Trips are synthetic (random neighbourhood origins, approximate tech-park destinations). "
        "Replace with real OD data before quoting numbers.",
        "- Static one-hour assignment: no queue spillback or junction/signal delay, so absolute "
        "delays are understated. Use the differences between scenarios, not the absolute values.",
        "- Speeds and capacities are typical values per road type, not calibrated to Bengaluru counts.",
        "",
        "![maps](maps.png)",
    ]
    with open(f"{OUT}/report.md", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
