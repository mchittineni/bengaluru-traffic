"""Download the drivable road network around the ORR from OpenStreetMap.

Tries several public Overpass servers in turn, because the main one is often busy.
Saves data/orr_network.graphml for routing_sim.py.

Usage:
    python fetch_network.py
"""

import sys
import time

import osmnx as ox

# west, south, east, north: Silk Board / HSR / Koramangala to KR Puram / Whitefield edge
BBOX = (77.61, 12.905, 77.72, 13.01)

# Arterials down to residential streets. Residential streets matter here: they are
# where "creep" shows up when navigation apps push drivers off the main roads.
ROAD_FILTER = (
    '["highway"~"motorway|trunk|primary|secondary|tertiary|unclassified|residential"]'
    '["highway"!~"_link$"]["access"!~"private|no"]["motor_vehicle"!~"no"]'
)
LINK_FILTER = '["highway"~"_link$"]'

MIRRORS = [
    "https://overpass.kumi.systems/api",
    "https://overpass-api.de/api",
    "https://maps.mail.ru/osm/tools/overpass/api",
    "https://overpass.private.coffee/api",
]
OUT = "data/orr_network.graphml"


def main():
    ox.settings.requests_timeout = 240
    ox.settings.overpass_rate_limit = False
    for attempt, url in enumerate(MIRRORS * 2):
        ox.settings.overpass_url = url
        print(f"Trying {url} ...", flush=True)
        try:
            main_roads = ox.graph_from_bbox(BBOX, custom_filter=ROAD_FILTER, simplify=False)
            links = ox.graph_from_bbox(BBOX, custom_filter=LINK_FILTER, simplify=False)
        except Exception as e:
            print(f"  failed: {str(e).splitlines()[0][:150]}", flush=True)
            time.sleep(min(60, 10 * (attempt + 1)))
            continue
        import networkx as nx
        G = ox.simplify_graph(nx.compose(main_roads, links))
        G = ox.truncate.largest_component(G, strongly=True)
        ox.save_graphml(G, OUT)
        print(f"Saved {len(G)} nodes, {G.number_of_edges()} edges to {OUT}")
        return
    sys.exit("All Overpass servers failed. Try again later.")


if __name__ == "__main__":
    main()
