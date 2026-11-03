import numpy as np
import pytest

from routing_sim import ALPHA, Demand, all_or_nothing, assign, make_class, metrics, travel_time


def test_network_reads_road_attributes(grid):
    assert grid.n == 144
    assert grid.orr.sum() == 2 * 11            # row 0, both directions
    trunk = grid.cls == "trunk"
    assert np.all(grid.cap[trunk] == 3 * 1600)  # 6 lanes two-way -> 3 per direction
    assert np.all(grid.cap[grid.cls == "residential"] == 500)
    assert grid.neighbourhood.sum() == (grid.cls == "residential").sum()


def test_edge_id_round_trips(grid):
    e = np.arange(grid.E)
    assert np.array_equal(grid.edge_id(grid.u, grid.v), e)


def test_bpr_travel_time_at_capacity(grid):
    t = travel_time(grid, grid.cap)
    assert np.allclose(t, grid.t0 * (1 + ALPHA))


def test_all_or_nothing_loads_each_trip_once_per_link(line):
    demand = object.__new__(Demand)
    demand.origins = np.array([0])
    demand.row, demand.dest, demand.trips = np.array([0]), np.array([1]), np.array([300.0])
    flow = all_or_nothing(line, demand, line.t0)
    assert flow[line.edge_id(0, 1)] == pytest.approx(300)
    assert flow[line.edge_id(1, 0)] == 0


def test_coordinated_routing_does_not_increase_total_delay(grid):
    demand = Demand(grid, 20000, np.random.default_rng(1))
    waze_xs, waze_gap, _ = assign(grid, [make_class(grid, demand)], max_iter=40)
    so_xs, _, _ = assign(grid, [make_class(grid, demand, coordinated=True)], max_iter=40)
    waze, _ = metrics(grid, "waze", waze_xs, [make_class(grid, demand)], waze_gap, 0)
    so, _ = metrics(grid, "so", so_xs, [make_class(grid, demand)], 0, 0)
    assert waze_gap < 0.05
    # System optimum minimises total travel time, so it can't be worse than user equilibrium
    # (small tolerance for Frank-Wolfe not fully converging).
    assert so["avg_trip_min"] <= waze["avg_trip_min"] * 1.01
