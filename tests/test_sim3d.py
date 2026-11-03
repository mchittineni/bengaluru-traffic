import base64
import json
import shutil
import subprocess
import types
from pathlib import Path

import numpy as np
import pytest
from conftest import agents

import sim3d


def test_shortest_paths_are_connected_routes(grid):
    origins, dests = np.array([0, 5, 143]), np.array([143, 100, 0])
    for o, d, p in zip(origins, dests, sim3d.shortest_paths(grid, origins, dests, grid.t0 * 3600)):
        assert grid.u[p[0]] == o and grid.v[p[-1]] == d
        assert np.array_equal(grid.v[p[:-1]], grid.u[p[1:]])


def test_shortest_paths_prefer_fast_roads(grid):
    # Along row 0 the trunk road (45 km/h) beats every residential detour.
    (p,) = sim3d.shortest_paths(grid, np.array([0]), np.array([11]), grid.t0 * 3600)
    assert set(grid.cls[p]) == {"trunk"}


def test_lone_vehicle_drives_at_free_flow(grid):
    a = agents([0], [143], [600])
    sim = sim3d.simulate(grid, a, "habitual")
    free_flow = (grid.t0[sim["paths"][0]] * 3600).sum()
    assert sim["arrive"][0] - 600 == pytest.approx(free_flow, rel=1e-3)
    assert len(sim["times"][0]) == len(sim["paths"][0]) + 1


def test_link_releases_one_vehicle_per_headway(line):
    # 10 vehicles of 5 PCU leave together onto a 500 PCU/h link: one every 36 s.
    a = agents([0] * 10, [1] * 10, [0] * 10, pcu=5)
    sim = sim3d.simulate(line, a, "habitual")
    gaps = np.diff(np.sort(sim["arrive"]))
    assert np.allclose(gaps, 5 * 3600 / 500)
    assert sim["queue"][0].max() == 0      # queues are sampled at bin starts; none at t=0
    assert np.isfinite(sim["arrive"]).all()


@pytest.mark.parametrize("rule", ["waze", "coordinated"])
def test_dynamic_rules_avoid_a_queue(grid, rule):
    # 5,000 PCU leave at once onto a 4,800 PCU/h trunk: the queue still holds ~50 minutes of
    # traffic when 20 more drivers leave at 10 minutes, far more than the ~18 minute detour
    # along residential row 1. Habitual drivers join the queue; dynamic ones divert.
    n = 1000
    a = agents([0] * (n + 20), [11] * (n + 20), [0.0] * n + [600.0] * 20)
    habit = sim3d.simulate(grid, a, "habitual")
    smart = sim3d.simulate(grid, a, rule)
    trunk_share = lambda sim: np.mean([set(grid.cls[p]) == {"trunk"} for p in sim["paths"][n:]])  # noqa: E731
    assert trunk_share(habit) == 1
    assert trunk_share(smart) < 1


def test_agents_follow_departure_window(grid):
    a = sim3d.Agents(grid, total_pcu=2000, pcu=5, rng=np.random.default_rng(0))
    assert a.n == 400
    assert a.depart.min() >= 0 and a.depart.max() < sim3d.DEPART_WINDOW_S
    assert np.all(a.origin != a.dest)
    assert set(np.unique(a.origin)) <= set(np.unique(grid.u[grid.neighbourhood]))


@pytest.fixture
def exported(grid, tmp_path, monkeypatch):
    monkeypatch.setattr(sim3d, "OUT", str(tmp_path))
    rng = np.random.default_rng(3)
    a = sim3d.Agents(grid, total_pcu=3000, pcu=5, rng=rng)
    sims = {k: sim3d.simulate(grid, a, k) for k, _, _ in sim3d.SCENARIOS}
    summaries = {k: sim3d.summarise(grid, a, s) for k, s in sims.items()}
    args = types.SimpleNamespace(trips=3000, pcu_per_agent=5, seed=3)
    sim3d.export(grid, a, sims, summaries, 50, rng, args)
    sim3d.write_report(summaries, a, args)
    with open(tmp_path / "data.json") as f:
        return json.load(f), sims, a, tmp_path


def b64(s, dtype):
    return np.frombuffer(base64.b64decode(s), dtype=dtype)


def test_export_writes_viewer_and_report(exported):
    _, _, _, out = exported
    for name in ("index.html", "core.js", "app.js", "data.json", "report.md"):
        assert (out / name).exists(), name
    report = (out / "report.md").read_text()
    for _, label, _ in sim3d.SCENARIOS:
        assert f"| {label} |" in report


def test_export_trip_encoding_matches_documented_layout(exported):
    data, _, _, _ = exported
    n_edges = len(data["edges"]["coords"])
    n_bins = sim3d.END_S // sim3d.BIN_S
    assert [s["key"] for s in data["scenarios"]] == [k for k, _, _ in sim3d.SCENARIOS]
    for s in data["scenarios"]:
        off = b64(s["trips"]["offsets"], np.uint32)
        edges = b64(s["trips"]["edges"], np.uint16)
        times = b64(s["trips"]["times"], np.uint16)
        shown = data["meta"]["shown"]
        assert len(off) == shown + 1 and off[-1] == len(edges)
        assert len(times) == len(edges) + shown
        assert edges.max() < n_edges
        for i in range(shown):
            ts = times[off[i] + i: off[i + 1] + i + 1]
            assert np.all(np.diff(ts.astype(int)) >= 0), "a trip's node times must not go backwards"
        speed = b64(s["speed"], np.uint8)
        assert len(speed) == n_bins * len(data["congestion_edges"])
        assert len(b64(s["queues"]["pcu"], np.uint16)) == n_bins * len(s["queues"]["edges"])
        assert len(s["summary"]["timeline"]["on_road"]) == n_bins


def test_export_follows_the_same_commuters_in_every_scenario(exported):
    data, sims, a, _ = exported
    departs = []
    for s in data["scenarios"]:
        off, times = b64(s["trips"]["offsets"], np.uint32), b64(s["trips"]["times"], np.uint16)
        departs.append([times[off[i] + i] for i in range(len(off) - 1)])
    assert departs[0] == departs[1] == departs[2]


NODE_CHECK = """
const core = require(process.argv[1] + "/viewer/core.js");
const data = JSON.parse(require("fs").readFileSync(process.argv[2]));
const edges = core.prepareEdges(data.edges);
console.log(JSON.stringify(data.scenarios.map((s) => {
  const trips = core.buildTrips(core.prepareScenario(s, edges), edges, data.meta.end_s);
  return {
    trips: trips.length,
    ordered: trips.every((d) => d.ts.every((t, i) => i === 0 || t >= d.ts[i - 1])),
    aligned: trips.every((d) => d.ts.length === d.path.length),
    departs: Array.from(s.depart),
  };
})));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_viewer_core_decodes_python_export(exported):
    # Contract between sim3d.export (Python) and viewer/core.js: same binary layout.
    data, _, _, out = exported
    res = subprocess.run(["node", "-e", NODE_CHECK, str(Path.cwd()), str(out / "data.json")],
                         capture_output=True, text=True, check=True)
    for js, s in zip(json.loads(res.stdout), data["scenarios"]):
        off = b64(s["trips"]["offsets"], np.uint32)
        times = b64(s["trips"]["times"], np.uint16)
        assert js["trips"] == int((np.diff(off) > 0).sum())
        assert js["ordered"] and js["aligned"]
        assert js["departs"] == [int(times[off[i] + i]) for i in range(len(off) - 1)]
