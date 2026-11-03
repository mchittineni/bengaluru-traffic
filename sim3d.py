"""Vehicle-level simulation of the ORR morning peak, exported as an interactive 3D web view.

Every scenario simulates the same commuters (same origins, destinations and departure
times) on the OpenStreetMap network from fetch_network.py; only the routing rule changes:

  habitual      drivers take the route that is fastest on empty roads (no app)
  waze          drivers take the fastest route given traffic at the moment they leave
  coordinated   routes also count the delay a driver adds to everyone else, and residential
                streets are kept lightly loaded (routing_sim.py's coord+protect rule, live)

Each simulated vehicle stands for --pcu-per-agent passenger-car units (PCU). Every road
link is a first-in-first-out queue: a vehicle drives the link at free-flow speed (slowed
by the BPR curve up to capacity), then leaves no sooner than one capacity headway after
the vehicle in front, so queues build wherever demand exceeds capacity. Routes are chosen
at departure from the traffic state at that moment, refreshed every 5 simulated minutes.

Usage:
    python sim3d.py [--trips 60000] [--pcu-per-agent 5] [--show 3000] [--seed 7]
    python -m http.server -d output/sim3d 8000      # then open http://localhost:8000

Outputs go to output/sim3d/: index.html + data.json (the 3D viewer) and report.md.
"""

import argparse
import base64
import heapq
import json
import os
import shutil
from datetime import UTC, datetime

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from routing_sim import ALPHA, BETA, JOB_CENTRES, NETWORK, Demand, Network, class_cost, make_class

OUT = "output/sim3d"
VIEWER_DIR = "viewer"     # static viewer files (index.html, core.js, app.js), copied next to data.json

# Simulated clock starts at 07:00. Departures follow a normal curve around 08:30,
# cut to 07:00-10:00; the simulation stops at 11:30 whether or not everyone has arrived.
CLOCK_START = "07:00"
DEPART_MEAN_S = 90 * 60
DEPART_SD_S = 40 * 60
DEPART_WINDOW_S = 180 * 60
END_S = 270 * 60
BIN_S = 300               # routing refresh and recording interval
FLOW_WINDOW_BINS = 3      # recent link entries used to estimate flow (15 min)
ZONE_NODES = 40           # junctions each origin/destination zone is spread over

SCENARIOS = [
    ("habitual", "No app", "Everyone drives the route that is fastest on empty roads."),
    ("waze", "Live navigation", "Everyone takes the fastest route given traffic when they leave."),
    ("coordinated", "Coordinated", "Routes count the delay each driver adds to others; "
                                   "residential streets are kept lightly loaded."),
]

# Exported to the viewer: links with the most traffic get a time-varying congestion colour,
# links with the longest queues get a 3D queue column.
N_CONGESTION_EDGES = 6000
N_QUEUE_EDGES = 250


class Agents:
    """Discrete commuters drawn from routing_sim's origin-destination demand."""

    def __init__(self, net, total_pcu, pcu, rng):
        demand = Demand(net, total_pcu, rng)
        n = int(round(total_pcu / pcu))
        pair = rng.choice(len(demand.trips), n, p=demand.trips / demand.trips.sum())
        depart = rng.normal(DEPART_MEAN_S, DEPART_SD_S, n)
        while (bad := (depart < 0) | (depart >= DEPART_WINDOW_S)).any():
            depart[bad] = rng.normal(DEPART_MEAN_S, DEPART_SD_S, bad.sum())
        self.n, self.pcu = n, pcu
        self.depart = np.round(depart)
        # Demand puts every trip on one junction per zone; a real zone loads onto many
        # streets, so spread each commuter over the junctions near their zone's point.
        homes = np.unique(net.u[net.neighbourhood])
        zone_o, zone_d = demand.origins[demand.row[pair]], demand.dest[pair]
        self.origin = self._spread(net, zone_o, homes, rng)
        self.dest = self._spread(net, zone_d, np.arange(net.n), rng)
        same = self.origin == self.dest
        self.origin[same], self.dest[same] = zone_o[same], zone_d[same]

    @staticmethod
    def _spread(net, nodes, candidates, rng):
        out = nodes.copy()
        for node in np.unique(nodes):
            d = (net.y[candidates] - net.y[node]) ** 2 + \
                ((net.x[candidates] - net.x[node]) * np.cos(np.radians(net.y[node]))) ** 2
            near = candidates[np.argsort(d)[:ZONE_NODES]]
            mask = nodes == node
            out[mask] = rng.choice(near, mask.sum())
        return out


def shortest_paths(net, origins, dests, cost):
    """Edge-id path for each (origin, destination) pair under `cost` (seconds per edge)."""
    graph = csr_matrix((cost + 1e-6, (net.u, net.v)), shape=(net.n, net.n))
    sources, row = np.unique(origins, return_inverse=True)
    _, pred = dijkstra(graph, directed=True, indices=sources, return_predecessors=True)
    paths = []
    for r, o, node in zip(row, origins, dests):
        nodes = [node]
        while node != o:
            node = pred[r, node]
            nodes.append(node)
        nodes = np.array(nodes[::-1])
        paths.append(net.edge_id(nodes[:-1], nodes[1:]))
    return paths


def simulate(net, agents, rule):
    n_bins = END_S // BIN_S
    t0 = net.t0 * 3600
    headway = agents.pcu * 3600 / net.cap
    coord = make_class(net, None, coordinated=True, protect=True)

    next_free = np.zeros(net.E)                       # earliest time each link can release a vehicle
    entries = np.zeros((n_bins, net.E), np.float32)   # vehicles entering, by entry bin
    done = np.zeros((n_bins, net.E), np.float32)      # ... of which have left the link
    trav = np.zeros((n_bins, net.E), np.float32)      # summed traversal seconds
    queue = np.zeros((n_bins, net.E), np.float32)     # PCU queued at the start of each bin

    paths, times = [None] * agents.n, [None] * agents.n
    arrive = np.full(agents.n, np.nan)
    order = np.argsort(agents.depart, kind="stable")
    heap, nxt = [], 0

    for b in range(n_bins):
        now, end = b * BIN_S, (b + 1) * BIN_S
        lo = max(0, b - FLOW_WINDOW_BINS)
        flow = entries[lo:b].sum(0) * agents.pcu * 3600 / ((b - lo) * BIN_S) if b else np.zeros(net.E)
        run = t0 * (1 + ALPHA * np.minimum(flow / net.cap, 1) ** BETA)
        wait = np.maximum(next_free - now, 0)
        queue[b] = wait * net.cap / 3600

        first = nxt
        while nxt < agents.n and agents.depart[order[nxt]] < end:
            nxt += 1
        batch = order[first:nxt]
        if len(batch):
            if rule == "habitual":
                cost = t0
            elif rule == "waze":
                cost = run + wait
            else:
                cost = class_cost(net, flow, coord) * 3600 + wait
            for a, p in zip(batch, shortest_paths(net, agents.origin[batch], agents.dest[batch], cost)):
                paths[a], times[a] = p, [agents.depart[a]]
                entries[b, p[0]] += 1
                heapq.heappush(heap, (agents.depart[a] + run[p[0]], a, 0))

        while heap and heap[0][0] < end:
            t, a, k = heapq.heappop(heap)
            p, ts = paths[a], times[a]
            e = p[k]
            out = max(t, next_free[e])
            next_free[e] = out + headway[e]
            entered = int(ts[-1] // BIN_S)
            done[entered, e] += 1
            trav[entered, e] += out - ts[-1]
            ts.append(out)
            if k + 1 == len(p):
                arrive[a] = out
                continue
            nb = int(out // BIN_S)
            if nb < n_bins:
                entries[nb, p[k + 1]] += 1
            heapq.heappush(heap, (out + run[p[k + 1]], a, k + 1))

    return {"paths": paths, "times": times, "arrive": arrive,
            "entries": entries, "done": done, "trav": trav, "queue": queue}


def summarise(net, agents, sim):
    t0 = net.t0 * 3600
    ff = np.array([t0[p].sum() for p in sim["paths"]])
    finished = ~np.isnan(sim["arrive"])
    trip = np.where(finished, sim["arrive"], END_S) - agents.depart   # unfinished: time so far
    nb = net.neighbourhood
    nbhd_km = np.array([net.length_km[p][nb[p]].sum() for p in sim["paths"]])
    bins = np.arange(END_S // BIN_S) * BIN_S
    on_road = [(agents.depart <= t) & ~(sim["arrive"] <= t) for t in bins]
    dep_bin = (agents.depart // BIN_S).astype(int)
    delay_ratio = trip / ff
    return {
        "finished_pct": 100 * finished.mean(),
        "avg_trip_min": trip[finished].mean() / 60,
        "avg_delay_min": (trip - ff).mean() / 60,
        "delay_veh_h": (trip - ff).sum() * agents.pcu / 3600,
        "nbhd_veh_km": nbhd_km.sum() * agents.pcu,
        "peak_queue_pcu": float(sim["queue"].sum(1).max()),
        "timeline": {
            "on_road": [int(m.sum() * agents.pcu) for m in on_road],
            "arrived": [int((sim["arrive"] <= t).sum() * agents.pcu) for t in bins],
            "delay_ratio": [round(float(delay_ratio[dep_bin == i].mean()), 2) if (dep_bin == i).any() else None
                            for i in range(len(bins))],
        },
    }


def b64(arr):
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode()


def export(net, agents, sims, summaries, show, rng, args):
    t0 = net.t0 * 3600
    sample = np.sort(rng.choice(agents.n, min(show, agents.n), replace=False))

    volume = sum(s["entries"].sum(0) for s in sims.values())
    cong = np.argsort(-volume)[:N_CONGESTION_EDGES]
    cong = cong[volume[cong] > 0]
    queue_edges = {k: np.argsort(-s["queue"].max(0))[:N_QUEUE_EDGES] for k, s in sims.items()}
    used = set(cong.tolist())
    for k, s in sims.items():
        used.update(queue_edges[k].tolist())
        for a in sample:
            used.update(s["paths"][a][:len(s["times"][a]) - 1].tolist())
    used = np.array(sorted(used))
    assert len(used) < 2 ** 16, "too many edges for the viewer's uint16 edge ids; lower --show"
    idx = {e: i for i, e in enumerate(used)}

    scenarios = []
    for key, label, desc in SCENARIOS:
        s, summ = sims[key], summaries[key]
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = np.where(s["done"][:, cong] > 0, t0[cong] * s["done"][:, cong] / s["trav"][:, cong], np.nan)
        speed = np.where(np.isnan(ratio), 255, np.round(np.clip(ratio, 0, 1) * 254)).astype(np.uint8)
        qe = queue_edges[key]
        qe = qe[s["queue"][:, qe].max(0) >= 1]
        # Trip i drove edges[offsets[i]:offsets[i+1]], passing its nodes at
        # times[offsets[i] + i : offsets[i+1] + i + 1] (seconds after CLOCK_START).
        edges, times, offsets = [], [], [0]
        for a in sample:
            ts = s["times"][a]
            edges += [idx[e] for e in s["paths"][a][:len(ts) - 1]]
            times += ts
            offsets.append(len(edges))
        trips = {
            "offsets": b64(np.array(offsets, np.uint32)),
            "edges": b64(np.array(edges, np.uint16)),
            "times": b64(np.round(times).astype(np.uint16)),
            "free_flow_s": [int(round(t0[s["paths"][a]].sum())) for a in sample],
            "arrived": [int(not np.isnan(s["arrive"][a])) for a in sample],
        }
        scenarios.append({
            "key": key, "label": label, "description": desc,
            "summary": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in summ.items()},
            "speed": b64(speed),
            "queues": {"edges": [idx[e] for e in qe], "pcu": b64(np.round(s["queue"][:, qe]).astype(np.uint16))},
            "trips": trips,
        })

    data = {
        "meta": {
            "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "clock_start": CLOCK_START, "end_s": END_S, "bin_s": BIN_S,
            "trips_pcu": args.trips, "pcu_per_agent": args.pcu_per_agent,
            "agents": agents.n, "shown": len(sample), "seed": args.seed,
            "center": [float(net.x.mean()), float(net.y.mean())],
            "job_centres": [{"name": n, "lng": lng, "lat": lat} for n, lat, lng, _ in JOB_CENTRES],
        },
        "edges": {
            "coords": [np.round(net.geom[e], 5).ravel().tolist() for e in used],
            "cls": [str(net.cls[e]) for e in used],
            "orr": [int(net.orr[e]) for e in used],
            "length_m": [round(float(net.length_km[e] * 1000), 1) for e in used],
        },
        "congestion_edges": [idx[e] for e in cong],
        "scenarios": scenarios,
    }
    with open(f"{OUT}/data.json", "w") as f:
        json.dump(data, f, separators=(",", ":"))
    shutil.copytree(VIEWER_DIR, OUT, dirs_exist_ok=True)


def write_report(summaries, agents, args):
    base = summaries["waze"]
    lines = [
        "# 3D simulation: ORR morning peak",
        "",
        f"{agents.n:,} simulated vehicles x {args.pcu_per_agent} PCU = {args.trips:,.0f} PCU departing "
        f"07:00-10:00 (seed {args.seed}). Open `index.html` (served over HTTP) for the 3D view.",
        "",
        "| Scenario | Arrived by 11:30 | Avg trip (min) | Avg delay (min) | Total delay (veh-h) | vs live nav "
        "| Residential veh-km | vs live nav | Peak queued PCU |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key, label, _ in SCENARIOS:
        r = summaries[key]
        lines.append(
            f"| {label} | {r['finished_pct']:.1f}% | {r['avg_trip_min']:.1f} | {r['avg_delay_min']:.1f} "
            f"| {r['delay_veh_h']:,.0f} | {100 * (r['delay_veh_h'] / base['delay_veh_h'] - 1):+.0f}% "
            f"| {r['nbhd_veh_km']:,.0f} | {100 * (r['nbhd_veh_km'] / base['nbhd_veh_km'] - 1):+.0f}% "
            f"| {r['peak_queue_pcu']:,.0f} |"
        )
    lines += [
        "",
        "## Caveats",
        "",
        "- Trips are synthetic (routing_sim.py's demand). Replace with real OD data before quoting numbers.",
        "- Queues are point queues: they hold vehicles at the end of a link but do not spill back "
        "into upstream junctions, and there are no signals, so delays are understated.",
        "- Routes are fixed at departure; real navigation apps also reroute drivers mid-trip.",
        "- Speeds and capacities are typical values per road type, not calibrated to Bengaluru counts.",
    ]
    with open(f"{OUT}/report.md", "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trips", type=float, default=60000, help="PCU departing 07:00-10:00")
    ap.add_argument("--pcu-per-agent", type=int, default=5, help="PCU each simulated vehicle stands for")
    ap.add_argument("--show", type=int, default=3000, help="vehicles animated in the viewer")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    net = Network(NETWORK)
    agents = Agents(net, args.trips, args.pcu_per_agent, rng)
    print(f"Network: {net.n} nodes, {net.E} edges. Agents: {agents.n} x {agents.pcu} PCU.", flush=True)

    sims, summaries = {}, {}
    for key, label, _ in SCENARIOS:
        sims[key] = simulate(net, agents, key)
        summaries[key] = summarise(net, agents, sims[key])
        r = summaries[key]
        print(f"{label:16s} arrived {r['finished_pct']:5.1f}%  avg trip {r['avg_trip_min']:5.1f} min  "
              f"delay {r['delay_veh_h']:8.0f} veh-h  nbhd {r['nbhd_veh_km']:7.0f} veh-km", flush=True)

    export(net, agents, sims, summaries, args.show, rng, args)
    write_report(summaries, agents, args)
    print(f"Viewer: {OUT}/index.html (data.json {os.path.getsize(f'{OUT}/data.json') / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
