#!/usr/bin/env python3
"""Generate water_edges.json for BoligWatch v3.0.

Collects coastline, harbour, canal and lake geometry across Greater Copenhagen
from Overpass and stores it as flat polylines. Distances are computed at scan
time by features.water_distance(); this script only refreshes the reference
file, which is git-tracked.

Usage:
  python generate_water.py
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from profile import prescan_profile
prescan_profile()

import config
from generate_transit import _overpass

# The bbox-wide union of coastline plus every water polygon times out on the
# public Overpass instances, so each feature class is fetched separately.
WATER_QUERIES = [
    ("coastline ways", '[out:json][timeout:180];\nway["natural"="coastline"](%s,%s,%s,%s);\nout geom;\n'
     % config.GREATER_CPH_BBOX),
    ("harbour/canal/lake ways", '[out:json][timeout:180];\nway["natural"="water"]["water"~"^(harbour|canal|lake)$"](%s,%s,%s,%s);\nout geom;\n'
     % config.GREATER_CPH_BBOX),
    ("harbour/canal/lake relations", '[out:json][timeout:180];\nrelation["natural"="water"]["water"~"^(harbour|canal|lake)$"](%s,%s,%s,%s);\nout geom;\n'
     % config.GREATER_CPH_BBOX),
]

_KIND_BY_WATER_TAG = {"harbour": "harbour", "canal": "canal", "lake": "lake"}


def _kind_for(tags):
    if tags.get("natural") == "coastline":
        return "coast"
    return _KIND_BY_WATER_TAG.get(tags.get("water"), "lake")


def _polylines_from(element):
    """Yield [[lat, lon], ...] rings/lines from an 'out geom' element."""
    if element.get("type") == "way":
        geom = element.get("geometry") or []
        if len(geom) >= 2:
            yield [[p["lat"], p["lon"]] for p in geom]
    elif element.get("type") == "relation":
        for m in element.get("members", []):
            geom = m.get("geometry") or []
            if len(geom) >= 2:
                yield [[p["lat"], p["lon"]] for p in geom]


def build_water_edges():
    print("[water] querying Overpass for coastline, harbour, canal and lake geometry")
    edges = []
    for label, query in WATER_QUERIES:
        print(f"  fetching {label}")
        data = _overpass(query, timeout=240)
        before = len(edges)
        for e in data.get("elements", []):
            tags = e.get("tags") or {}
            kind = _kind_for(tags)
            name = tags.get("name") or tags.get("name:da") or {
                "coast": "Øresund", "harbour": "Havnen", "canal": "Kanal", "lake": "Sø",
            }[kind]
            for pts in _polylines_from(e):
                edges.append({"name": name, "kind": kind, "points": pts})
        print(f"    {len(edges) - before} polylines")
    return edges


def main():
    ap = argparse.ArgumentParser(description="Generate BoligWatch water reference data")
    ap.parse_args()

    edges = build_water_edges()
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with open(config.WATER_EDGES_JSON, "w", encoding="utf-8") as f:
        json.dump({"generated_at": now, "bbox": list(config.GREATER_CPH_BBOX),
                   "count": len(edges), "edges": edges}, f, ensure_ascii=False)

    by_kind = {}
    pts = 0
    for e in edges:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        pts += len(e["points"])
    size_kb = round(os.path.getsize(config.WATER_EDGES_JSON) / 1024)
    print(f"[water] wrote {config.WATER_EDGES_JSON}: {len(edges)} polylines, "
          f"{pts} points, {size_kb} KB")
    for kind, n in sorted(by_kind.items()):
        print(f"  {kind:<9} {n:>5} polylines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
