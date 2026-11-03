"""Shared fixtures: a small synthetic road grid standing in for the OSM network.

The grid sits inside fetch_network.BBOX so routing_sim's job centres snap to real grid
nodes. Row 0 is a 3-lane "Outer Ring Road" trunk; every 4th column is a primary road;
everything else is residential.
"""

import os
import types

import networkx as nx
import numpy as np
import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")   # boto3 clients are created at import

from routing_sim import Network  # noqa: E402

GRID_ORIGIN = (12.91, 77.615)   # lat, lng of node (0, 0)
GRID_STEP = 0.009               # degrees between neighbouring nodes (~1 km)


def grid_graph(rows=12, cols=12):
    G = nx.MultiDiGraph(crs="epsg:4326")
    lat0, lng0 = GRID_ORIGIN
    for r in range(rows):
        for c in range(cols):
            G.add_node(r * cols + c, y=lat0 + r * GRID_STEP, x=lng0 + c * GRID_STEP)

    def link(a, b, **attrs):
        for u, v in ((a, b), (b, a)):
            G.add_edge(u, v, length=GRID_STEP * 111_000, **attrs)

    for r in range(rows):
        for c in range(cols):
            n = r * cols + c
            if c + 1 < cols:
                if r == 0:
                    link(n, n + 1, highway="trunk", lanes="6", name="Outer Ring Road")
                else:
                    link(n, n + 1, highway="residential")
            if r + 1 < rows:
                link(n, n + cols, highway="primary" if c % 4 == 0 else "residential")
    return G


@pytest.fixture(scope="session")
def grid():
    return Network(grid_graph())


@pytest.fixture
def line():
    """Two nodes joined by one 1 km residential link in each direction (500 PCU/h)."""
    G = nx.MultiDiGraph(crs="epsg:4326")
    G.add_node(0, y=12.95, x=77.65)
    G.add_node(1, y=12.95, x=77.6592)
    for u, v in ((0, 1), (1, 0)):
        G.add_edge(u, v, length=1000.0, highway="residential")
    return Network(G)


def agents(origin, dest, depart, pcu=5):
    """A hand-built population with the attributes sim3d.simulate reads."""
    origin, dest, depart = (np.asarray(a) for a in (origin, dest, depart))
    return types.SimpleNamespace(n=len(depart), pcu=pcu, origin=origin, dest=dest, depart=depart.astype(float))
