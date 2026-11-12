"""Generate the Copenhagen space table via Overpass (OSM).

Writes:
- metro_stations.json  — Metro stops (station=subway / network Metro)
- s_tog_stations.json  — genuine DSB S-tog stops (curated whitelist, coordinates
                         resolved from OSM railway=station|halt nodes)

Run: python generate_stations.py
"""
import json, time, urllib.parse, urllib.request

METRO_QUERY = '[out:json][timeout:25];(node["station"="subway"](55.55,12.40,55.75,12.70););out body;'
TRAIN_QUERY = '[out:json][timeout:120];(node["railway"~"^(station|halt)$"](55.40,11.80,56.05,12.85););out body;'
URL = "https://overpass-api.de/api/interpreter"

# Curated genuine DSB S-tog stations (excludes planned light-rail / local-railway stops
# that OSM also tags with railway=station, so we never green-light a nonexistent stop).
S_TOG_STATION_NAMES = [
    # Hillerød line (A/E)
    "Hillerød", "Allerød", "Birkerød", "Holte", "Virum", "Sorgenfri", "Lyngby", "Jægersborg",
    "Gentofte", "Bernstorffsvej", "Hellerup", "Svanemøllen", "Nordhavn", "Østerport", "Nørreport",
    "Vesterport", "Københavns Hovedbanegård", "Dybbølsbro", "Carlsberg", "Valby", "Langgade",
    "Peter Bangs Vej", "Flintholm", "Vanløse", "Jyllingevej", "Islev", "Husum", "Herlev",
    "Skovlunde", "Malmparken", "Ballerup", "Måløv", "Kildedal", "Veksø", "Stenløse", "Egedal",
    "Ølstykke", "Frederikssund",
    # Farum line (B)
    "Farum", "Værløse", "Hareskov", "Skovbrynet", "Bagsværd", "Stengården", "Buddinge",
    "Kildebakke", "Vangede", "Dyssegård", "Emdrup", "Ryparken",
    # Høje Taastrup line (B/C)
    "Høje Taastrup", "Taastrup", "Albertslund", "Glostrup", "Rødovre", "Brøndbyøster", "Hvidovre",
    # Køge line (E)
    "Køge", "Ølby", "Køge Nord", "Vallensbæk", "Brøndby Strand", "Avedøre", "Friheden",
    "Åmarken", "Sydhavn", "Sjælør",
    # Ring line (F) / other S-tog terminals
    "Danshøj", "Ålholm", "Vigerslev Allé", "Grøndal", "KB Hallen", "Fuglebakken",
    "Nørrebro", "Bispebjerg", "København Syd", "Klampenborg",
]


def _post(query):
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(URL, data=data)
    req.add_header("User-Agent", "BoligWatch/3.12 (open-source rental research tool)")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=150) as r:
        return json.load(r)


def fetch(query, attempts=3):
    for attempt in range(attempts):
        try:
            return _post(query)
        except Exception as e:
            print(f"overpass attempt {attempt+1} failed: {e}")
            if attempt == attempts - 1:
                raise
            time.sleep(5)


def main():
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    # --- Metro ---
    raw = fetch(METRO_QUERY)
    seen = {}
    for el in raw.get("elements", []):
        t = el.get("tags", {})
        name = t.get("name")
        if not name or name in seen:
            continue
        seen[name] = {"name": name, "lat": el.get("lat"), "lon": el.get("lon"),
                      "network": t.get("network", "")}
    metro = sorted(seen.values(), key=lambda s: s["name"])
    with open("metro_stations.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": now, "count": len(metro), "stations": metro},
                  f, ensure_ascii=False, indent=1)
    print(f"Saved {len(metro)} metro stations to metro_stations.json")

    # --- S-tog ---
    raw = fetch(TRAIN_QUERY)
    coords = {}
    for el in raw.get("elements", []):
        t = el.get("tags", {})
        if t.get("network", "").lower().startswith("metro"):
            continue
        name = t.get("name")
        if name:
            coords.setdefault(name, (el.get("lat"), el.get("lon")))
    stations = []
    missing = []
    for n in dict.fromkeys(S_TOG_STATION_NAMES):
        if n in coords:
            stations.append({"name": n, "lat": coords[n][0], "lon": coords[n][1], "network": "s-tog"})
        else:
            missing.append(n)
    stations.sort(key=lambda s: s["name"])
    with open("s_tog_stations.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": now, "count": len(stations), "stations": stations},
                  f, ensure_ascii=False, indent=1)
    print(f"Saved {len(stations)} S-tog stations to s_tog_stations.json")
    if missing:
        print("WARNING: coords not found for:", missing)


if __name__ == "__main__":
    main()
