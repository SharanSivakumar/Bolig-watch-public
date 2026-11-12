#!/usr/bin/env python3
"""Generate transit_lines.json and station_commute.json for BoligWatch v3.0.

transit_lines.json     ordered station sequences for every metro and S-tog line
station_commute.json   offline door-to-platform ride time from every station to
                       the office anchor (Noerreport), including transfers but
                       NOT the initial wait

Usage:
  python generate_transit.py                 # fetch from Overpass, fall back to curated
  python generate_transit.py --curated       # skip Overpass, use the curated tables only
  python generate_transit.py --calibrate     # optional Rejseplanen ride-time calibration
"""
import argparse
import datetime
import heapq
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from profile import prescan_profile
prescan_profile()

import config
import geo

_USER_AGENT = "BoligWatch/3.12 (open-source rental research tool)"
_WANTED_REFS = ["M1", "M2", "M3", "M4", "A", "B", "Bx", "C", "E", "F", "H"]
_METRO_REFS = {"M1", "M2", "M3", "M4"}

# Consecutive stations further apart than this mean the member order is unreliable.
SANITY_MAX_HOP_M = 4000.0
RESOLVE_MAX_M = 250.0


# ==============================================================================
# Curated fallback line sequences (official line maps, September 2026)
# ==============================================================================
CURATED_LINES = {
    "M1": ["Vanløse", "Flintholm", "Lindevang", "Fasanvej", "Frederiksberg", "Forum",
           "Nørreport", "Kongens Nytorv", "Christianshavn", "Islands Brygge", "DR Byen",
           "Sundby", "Bella Center", "Ørestad", "Vestamager"],
    "M2": ["Vanløse", "Flintholm", "Lindevang", "Fasanvej", "Frederiksberg", "Forum",
           "Nørreport", "Kongens Nytorv", "Christianshavn", "Amagerbro", "Lergravsparken",
           "Øresund", "Amager Strand", "Femøren", "Kastrup", "Lufthavnen (Metro)"],
    "M3": ["Kongens Nytorv", "Gammel Strand", "Rådhuspladsen", "Københavns Hovedbanegård",
           "Enghave Plads", "Frederiksberg Allé", "Frederiksberg", "Aksel Møllers Have",
           "Nuuks Plads", "Nørrebro", "Nørrebros Runddel", "Skjolds Plads",
           "Vibenhus Runddel", "Poul Henningsens Plads", "Trianglen", "Østerport",
           "Marmorkirken", "Kongens Nytorv"],
    "M4": ["Orientkaj", "Nordhavn", "Østerport", "Marmorkirken", "Kongens Nytorv",
           "Gammel Strand", "Rådhuspladsen", "Københavns Hovedbanegård", "Havneholmen",
           "Enghave Brygge", "Sluseholmen", "Mozarts Plads", "København Syd"],
    "A": ["Hillerød", "Allerød", "Birkerød", "Holte", "Virum", "Sorgenfri", "Lyngby",
          "Jægersborg", "Gentofte", "Bernstorffsvej", "Hellerup", "Svanemøllen",
          "Nordhavn", "Østerport", "Nørreport", "Vesterport", "Københavns Hovedbanegård",
          "Dybbølsbro", "Sydhavn", "Sjælør", "København Syd", "Åmarken", "Friheden",
          "Avedøre", "Brøndby Strand", "Vallensbæk", "Ishøj", "Hundige", "Greve",
          "Karlslunde", "Solrød Strand", "Jersie", "Køge Nord", "Ølby", "Køge"],
    "B": ["Holte", "Virum", "Sorgenfri", "Lyngby", "Jægersborg", "Gentofte",
          "Bernstorffsvej", "Hellerup", "Svanemøllen", "Nordhavn", "Østerport",
          "Nørreport", "Vesterport", "Københavns Hovedbanegård", "Dybbølsbro",
          "Carlsberg", "Valby", "Danshøj", "Hvidovre", "Rødovre", "Brøndbyøster",
          "Glostrup", "Albertslund", "Taastrup", "Høje Taastrup"],
    "Bx": ["Københavns Hovedbanegård", "Dybbølsbro", "Carlsberg", "Valby", "Danshøj",
           "Hvidovre", "Rødovre", "Brøndbyøster", "Glostrup", "Albertslund", "Taastrup",
           "Høje Taastrup"],
    "C": ["Klampenborg", "Charlottenlund", "Ordrup", "Hellerup", "Svanemøllen",
          "Nordhavn", "Østerport", "Nørreport", "Vesterport", "Københavns Hovedbanegård",
          "Dybbølsbro", "Carlsberg", "Valby", "Langgade", "Danshøj", "Vanløse",
          "Jyllingevej", "Islev", "Husum", "Herlev", "Skovlunde", "Ballerup",
          "Malmparken", "Måløv",
          "Kildedal", "Veksø", "Stenløse", "Ølstykke", "Egedal", "Frederikssund"],
    "E": ["Køge", "Ølby", "Køge Nord", "Jersie", "Solrød Strand", "Karlslunde", "Greve", "Hundige",
          "Ishøj", "Vallensbæk", "Brøndby Strand", "Avedøre", "Friheden", "Åmarken",
          "København Syd", "Sjælør", "Sydhavn", "Dybbølsbro",
          "Københavns Hovedbanegård", "Vesterport", "Nørreport", "Østerport",
          "Nordhavn", "Svanemøllen", "Hellerup", "Bernstorffsvej", "Gentofte",
          "Jægersborg", "Lyngby", "Sorgenfri", "Virum", "Holte"],
    "F": ["København Syd", "Vigerslev Allé", "Danshøj", "Ålholm", "KB Hallen",
          "Peter Bangs Vej", "Flintholm", "Grøndal", "Fuglebakken", "Nørrebro",
          "Bispebjerg", "Emdrup", "Ryparken", "Hellerup"],
    "H": ["Farum", "Værløse", "Hareskov", "Skovbrynet", "Bagsværd", "Stengården",
          "Buddinge", "Kildebakke", "Vangede", "Dyssegård", "Emdrup", "Ryparken",
          "Svanemøllen", "Nordhavn", "Østerport", "Nørreport", "Vesterport",
          "Københavns Hovedbanegård", "Dybbølsbro", "Carlsberg", "Valby", "Langgade",
          "Danshøj", "Vanløse", "Jyllingevej", "Islev", "Husum", "Herlev",
          "Skovlunde", "Ballerup",
          "Malmparken", "Måløv", "Kildedal", "Veksø", "Stenløse", "Ølstykke", "Egedal",
          "Frederikssund"],
}


def network_for_ref(ref):
    return "Metro" if ref in _METRO_REFS else "s-tog"


# ==============================================================================
# Overpass fetch
# ==============================================================================
OVERPASS_QUERY = """[out:json][timeout:120];
(
  relation["route"="subway"](55.55,12.35,55.80,12.75);
  relation["route"="train"]["ref"~"^(A|B|Bx|C|E|F|H)$"](55.40,11.80,56.05,12.85);
);
out body;
>;
out skel qt;
"""


def _overpass(query, timeout=180, attempts=2):
    """POST an Overpass query, retrying each configured mirror in order."""
    last_err = None
    for attempt in range(attempts):
        for url in config.OVERPASS_URLS:
            try:
                data = urllib.parse.urlencode({"data": query}).encode()
                req = urllib.request.Request(url, data=data)
                req.add_header("User-Agent", _USER_AGENT)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.load(r)
            except Exception as e:  # noqa: BLE001 - mirror rotation is the point
                last_err = e
                print(f"  [overpass] {url} failed: {e}")
                time.sleep(10)
    raise RuntimeError(f"all Overpass mirrors failed: {last_err}")


def _clean_station_name(name):
    if not name:
        return None
    n = " ".join(str(name).split())
    for suffix in (" Station", " station", " St.", " st."):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n.strip() or None


def _build_station_index():
    """Return {'Metro': [...], 's-tog': [...]} of reference stations."""
    return {
        "Metro": geo.load_metro(),
        "s-tog": geo.load_s_tog(),
    }


def _resolve(lat, lon, name, stations):
    """Snap an OSM stop to a reference station name, else keep its own name."""
    best, best_d = None, None
    for s in stations:
        d = geo.hav(lat, lon, s["lat"], s["lon"])
        if best_d is None or d < best_d:
            best, best_d = s, d
    if best is not None and best_d <= RESOLVE_MAX_M:
        return best["name"], True
    return _clean_station_name(name), False


def fetch_overpass_lines(station_index):
    """Return {ref: [station names]} taken from OSM route relation member order."""
    print("[transit] querying Overpass for route relations")
    data = _overpass(OVERPASS_QUERY)
    elements = data.get("elements", [])
    nodes = {e["id"]: e for e in elements if e.get("type") == "node"}
    ways = {e["id"]: e for e in elements if e.get("type") == "way"}

    def member_coords(m):
        if m["type"] == "node":
            n = nodes.get(m["ref"])
            if n and "lat" in n:
                return n["lat"], n["lon"], (n.get("tags") or {}).get("name")
        elif m["type"] == "way":
            w = ways.get(m["ref"])
            if w and w.get("nodes"):
                pts = [nodes[i] for i in w["nodes"] if i in nodes and "lat" in nodes[i]]
                if pts:
                    return (sum(p["lat"] for p in pts) / len(pts),
                            sum(p["lon"] for p in pts) / len(pts),
                            (w.get("tags") or {}).get("name"))
        return None

    best = {}
    for e in elements:
        if e.get("type") != "relation":
            continue
        tags = e.get("tags") or {}
        ref = (tags.get("ref") or "").strip()
        if ref not in _WANTED_REFS:
            continue
        net = network_for_ref(ref)
        stations = station_index[net]

        seq = []
        for m in e.get("members", []):
            role = (m.get("role") or "")
            if not (role.startswith("stop") or role.startswith("platform")):
                continue
            c = member_coords(m)
            if not c:
                continue
            name, _matched = _resolve(c[0], c[1], c[2], stations)
            if not name:
                continue
            if not seq or seq[-1] != name:
                seq.append(name)

        if len(seq) < 2:
            continue
        if ref not in best or len(seq) > len(best[ref]):
            best[ref] = seq

    print(f"[transit] Overpass returned usable sequences for: {sorted(best)}")
    return best


def sanity_problems(ref, seq, station_index):
    """Return a list of reasons the sequence looks unusable."""
    problems = []
    if len(seq) < 2:
        return ["fewer than two stations"]
    if len(set(seq)) < len(seq) - 1:
        problems.append("repeated stations in sequence")

    coords = {}
    for net_stations in station_index.values():
        for s in net_stations:
            coords.setdefault(s["name"], (s["lat"], s["lon"]))

    for a, b in zip(seq, seq[1:]):
        if a in coords and b in coords:
            d = geo.hav(coords[a][0], coords[a][1], coords[b][0], coords[b][1])
            if d > SANITY_MAX_HOP_M:
                problems.append(f"{a} -> {b} is {round(d)} m apart")
    return problems


def build_lines(use_curated_only=False):
    """Assemble the final line table, preferring sane Overpass data."""
    station_index = _build_station_index()
    overpass = {}
    if not use_curated_only:
        try:
            overpass = fetch_overpass_lines(station_index)
        except Exception as e:  # noqa: BLE001 - curated tables are the fallback
            print(f"[transit] Overpass unavailable ({e}); using curated tables")

    lines = []
    for ref in _WANTED_REFS:
        seq = overpass.get(ref)
        source = "overpass"
        if seq:
            problems = sanity_problems(ref, seq, station_index)
            if problems:
                print(f"[transit] {ref}: Overpass order rejected ({problems[0]}); using curated")
                seq, source = None, "curated"
        if not seq:
            seq = CURATED_LINES.get(ref)
            source = "curated"
        if not seq:
            print(f"[transit] {ref}: no sequence available, line omitted")
            continue
        lines.append({
            "ref": ref,
            "network": network_for_ref(ref),
            "source": source,
            "stations": list(seq),
        })
    return lines, station_index


def find_orphans(lines, station_index):
    """Reference-table stations that appear on no line."""
    on_lines = set()
    for ln in lines:
        on_lines.update(ln["stations"])
    orphans = []
    for net, stations in station_index.items():
        for s in stations:
            if s["name"] not in on_lines:
                orphans.append({
                    "name": s["name"],
                    "network": net,
                    "reason": "not a member of any generated line sequence",
                })
    return orphans


# ==============================================================================
# Ride-time graph and Dijkstra
# ==============================================================================
def build_graph(lines):
    """Return {(station, network): {(station, network): (weight, refs)}}.

    refs is a frozenset of line refs serving the edge, or None for a
    same-name cross-network interchange edge (whose weight IS the transfer).
    """
    adj = {}

    def node(name, net):
        return (name, net)

    def add(u, v, w, refs):
        for a, b in ((u, v), (v, u)):
            slot = adj.setdefault(a, {})
            cur = slot.get(b)
            if cur is None:
                slot[b] = (w, refs)
            else:
                cur_w, cur_refs = cur
                merged = None
                if cur_refs is not None and refs is not None:
                    merged = frozenset(cur_refs | refs)
                elif cur_refs is not None:
                    merged = cur_refs
                elif refs is not None:
                    merged = refs
                slot[b] = (min(cur_w, w), merged)

    for ln in lines:
        net = ln["network"]
        hop = config.METRO_HOP_MIN if net == "Metro" else config.STOG_HOP_MIN
        refs = frozenset([ln["ref"]])
        for a, b in zip(ln["stations"], ln["stations"][1:]):
            if a == b:
                continue
            adj.setdefault(node(a, net), {})
            adj.setdefault(node(b, net), {})
            add(node(a, net), node(b, net), hop, refs)

    # Same-name interchange between networks (Noerreport, Vanloese, Flintholm, ...)
    by_name = {}
    for (name, net) in list(adj):
        by_name.setdefault(name, set()).add(net)
    for name, nets in by_name.items():
        nets = sorted(nets)
        for i in range(len(nets)):
            for j in range(i + 1, len(nets)):
                add(node(name, nets[i]), node(name, nets[j]), config.TRANSFER_MIN, None)

    return adj


def dijkstra_to_office(adj, office_name=None):
    """Ride minutes from every station node to the office anchor.

    State is (node, arriving_line_ref). A None ref means the next boarding is
    free (start of trip, or straight after an interchange edge that already
    charged the transfer). Changing to an edge that does not carry the arriving
    ref costs TRANSFER_MIN, so riding a shared trunk on another line is free.
    """
    office_name = office_name or config.OFFICE_NAME
    starts = [n for n in adj if n[0] == office_name]
    if not starts:
        return {}

    dist = {}
    pq = []
    for n in starts:
        dist[(n, None)] = 0.0
        heapq.heappush(pq, (0.0, n, None))

    while pq:
        d, node, ref = heapq.heappop(pq)
        if d > dist.get((node, ref), float("inf")):
            continue
        for nxt, (w, refs) in adj.get(node, {}).items():
            if refs is None:
                cand, nxt_refs = d + w, [None]
            else:
                penalty = 0.0 if (ref is None or ref in refs) else config.TRANSFER_MIN
                cand, nxt_refs = d + w + penalty, list(refs)
            for r in nxt_refs:
                if cand < dist.get((nxt, r), float("inf")):
                    dist[(nxt, r)] = cand
                    heapq.heappush(pq, (cand, nxt, r))

    # The ref on the cheapest arrival is the line boarded at that station when
    # travelling the other way, which is what "via" should report. Ties are
    # broken by _WANTED_REFS order so B wins over the peak-only Bx and the
    # generated file is reproducible run to run.
    def rank(ref):
        return _WANTED_REFS.index(ref) if ref in _WANTED_REFS else len(_WANTED_REFS)

    out = {}
    for (node, ref), d in dist.items():
        name, net = node
        key = (round(d, 6), rank(ref), ref or "")
        cur = out.get(name)
        if cur is None or key < cur["_key"]:
            out[name] = {"_key": key, "ride_min": int(round(d)), "via": ref, "network": net}

    for v in out.values():
        v.pop("_key", None)
        if not v.get("via"):
            v["via"] = "walk"
    return dict(sorted(out.items()))


# ==============================================================================
# Optional Rejseplanen calibration
# ==============================================================================
def calibrate_with_rejseplanen(commute, station_index):
    """Replace ride_min with real Tuesday 08:30 trip durations, if a token is set."""
    token = config.REJSEPLANEN_ACCESS_TOKEN
    if not token:
        print("[transit] no REJSEPLANEN_ACCESS_TOKEN set, skipping calibration")
        return commute

    coords = {}
    for net_stations in station_index.values():
        for s in net_stations:
            coords.setdefault(s["name"], (s["lat"], s["lon"]))
    o_lat, o_lon = config.OFFICE_COORDS

    # Next Tuesday, 08:30 departure.
    today = datetime.date.today()
    tuesday = today + datetime.timedelta(days=(1 - today.weekday()) % 7 or 7)
    calibrated = 0
    for name, entry in commute.items():
        if name == config.OFFICE_NAME or name not in coords:
            continue
        lat, lon = coords[name]
        params = {
            "accessId": token, "format": "json",
            "originCoordLat": lat, "originCoordLong": lon,
            "destCoordLat": o_lat, "destCoordLong": o_lon,
            "date": tuesday.isoformat(), "time": "08:30", "numF": 1,
        }
        try:
            url = "https://www.rejseplanen.dk/api/trip?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url)
            req.add_header("User-Agent", _USER_AGENT)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            dur = data["Trip"][0]["duration"]  # ISO 8601 PTxxHxxMxxS
            mins = 0
            m_h = dur.split("T")[-1]
            if "H" in m_h:
                mins += int(m_h.split("H")[0]) * 60
                m_h = m_h.split("H")[1]
            if "M" in m_h:
                mins += int(m_h.split("M")[0])
            if mins:
                entry["ride_min"] = mins
                entry["source"] = "rejseplanen"
                calibrated += 1
        except Exception as e:  # noqa: BLE001 - calibration is best-effort
            print(f"  [rejseplanen] {name}: {e}")
        time.sleep(0.5)
    print(f"[transit] calibrated {calibrated} stations from Rejseplanen")
    return commute


# ==============================================================================
# Entry point
# ==============================================================================
CALIBRATION_TARGETS = {
    "Forum": 2,
    "Kongens Nytorv": 3,
    "Vanløse": 11,
    "Femøren": 13,
    "København Syd": 16,
    "Hellerup": 9,
    "Valby": 10,
    "Taastrup": 26,
}
CALIBRATION_TOLERANCE_MIN = 3


def check_calibration(commute):
    """Return (rows, failures) comparing the model against real timetable values."""
    rows, failures = [], []
    for name, expected in CALIBRATION_TARGETS.items():
        got = commute.get(name, {}).get("ride_min")
        via = commute.get(name, {}).get("via")
        if got is None:
            rows.append((name, expected, None, None, "MISSING"))
            failures.append(name)
            continue
        ok = abs(got - expected) <= CALIBRATION_TOLERANCE_MIN
        rows.append((name, expected, got, via, "ok" if ok else "OFF"))
        if not ok:
            failures.append(name)
    return rows, failures


def main():
    ap = argparse.ArgumentParser(description="Generate BoligWatch transit reference data")
    ap.add_argument("--curated", action="store_true", help="Skip Overpass, use curated tables")
    ap.add_argument("--calibrate", action="store_true", help="Calibrate ride times via Rejseplanen")
    args = ap.parse_args()

    lines, station_index = build_lines(use_curated_only=args.curated)
    orphans = find_orphans(lines, station_index)
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    with open(config.TRANSIT_LINES_JSON, "w", encoding="utf-8") as f:
        json.dump({"generated_at": now, "lines": lines, "orphans": orphans},
                  f, ensure_ascii=False, indent=1)
    print(f"[transit] wrote {config.TRANSIT_LINES_JSON}: {len(lines)} lines, {len(orphans)} orphans")
    for ln in lines:
        print(f"  {ln['ref']:<3} {ln['network']:<6} {len(ln['stations']):>3} stations  ({ln['source']})")
    for o in orphans:
        print(f"  orphan: {o['name']} ({o['network']}) - {o['reason']}")

    adj = build_graph(lines)
    commute = dijkstra_to_office(adj)
    if args.calibrate:
        commute = calibrate_with_rejseplanen(commute, station_index)

    with open(config.STATION_COMMUTE_JSON, "w", encoding="utf-8") as f:
        json.dump({"generated_at": now, "office": config.OFFICE_NAME,
                   "stations": commute}, f, ensure_ascii=False, indent=1)
    print(f"[transit] wrote {config.STATION_COMMUTE_JSON}: {len(commute)} stations")

    rows, failures = check_calibration(commute)
    print("\nCalibration against real timetable values (tolerance +/- 3 min):")
    print(f"  {'Station':<22}{'expected':>9}{'model':>7}  {'via':<6}{'status':>8}")
    for name, exp, got, via, status in rows:
        print(f"  {name:<22}{exp:>9}{str(got):>7}  {str(via or '-'):<6}{status:>8}")
    if len(failures) > 2:
        print(f"\n  WARNING: {len(failures)} stations outside tolerance: {failures}")
        return 1
    print(f"\n  {len(rows) - len(failures)}/{len(rows)} within tolerance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
