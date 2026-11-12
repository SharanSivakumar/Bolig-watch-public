"""Central configuration for BoligWatch — a Copenhagen rental finder.

Everything here is a default. A JSON profile can override the values a normal
user cares about (destination, dates, budget, rooms, areas, weights, ratings)
without editing Python. See ``profile.example.json`` and ``profile.py``.

Three independent axes describe each match:
- Rank (S to D): derived from the 0-100 score.
- Commute band (A/B/C): door-to-door time to the destination.
- Budget: a hard all-inclusive monthly rent cap.

The scoring model is affordability (35), rapid-transit access (20), commute
(16), location (10), personal fit (19). Every weight and threshold below is a
named constant, and the profile loader can rebuild the derived curves when one
of them changes.
"""
import datetime as _datetime
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
METRO_JSON = os.path.join(BASE_DIR, "metro_stations.json")
S_TOG_JSON = os.path.join(BASE_DIR, "s_tog_stations.json")
DB_PATH = os.path.join(BASE_DIR, "boligwatch.db")
LOG_FILE = os.path.join(BASE_DIR, "boligwatch.log")
GEO_CACHE = os.path.join(BASE_DIR, "geo_cache.json")

# ==============================================================================
# 1. Target Dates & Window
# ==============================================================================
# Defaults target the first day of the month three months from now, with a
# one-month window around it. A profile sets these explicitly.
_DEFAULT_TARGET = (_datetime.date.today() + _datetime.timedelta(days=90)).replace(day=1)
TARGET_MOVE_DATE = _DEFAULT_TARGET.isoformat()
PREFERRED_WINDOW_START = (_DEFAULT_TARGET - _datetime.timedelta(days=15)).isoformat()
PREFERRED_WINDOW_END = (_DEFAULT_TARGET + _datetime.timedelta(days=15)).isoformat()
DATE_TOLERANCE_DAYS = 120.0
# A lease that starts before TARGET_MOVE_DATE can overlap an existing lease.
# EARLY_START_* turns that overlap into a monthly cost in the score.
EARLY_START_MAX_DAYS = 62
EARLY_START_DAYS_PER_MONTH = 30.0
EARLY_START_AMORTISE_MONTHS = 12.0

# ==============================================================================
# 2. Cost Ceilings
# ==============================================================================
# Maximum all-inclusive move-in cost ceiling (inclusive: 60,000 passes, 60,001 fails).
# Tier rent ceilings and every scoring weight live in section 6.
MOVE_IN_COST_MAX = 60000
# Room count the search admits (inclusive). The default is a studio or one-bed.
ROOMS_MIN = 1
ROOMS_MAX = 2

# Reference city centre coordinates: Radhuspladsen (Copenhagen City Hall)
RADHUSPLADSEN_COORDS = (55.6764406, 12.5682006)

# Pedestrian and road routing model & Google Maps API configuration
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
WALK_ROUTING_FACTOR = float(os.environ.get("WALK_ROUTING_FACTOR", "1.35"))
WALK_DISTANCE_PENALTY_M = float(os.environ.get("WALK_DISTANCE_PENALTY_M", "150"))
WALK_SPEED_M_PER_MIN = float(os.environ.get("WALK_SPEED_M_PER_MIN", "80"))  # 4.8 km/h
ROAD_ROUTE_PROFILE = "driving"
OSRM_DRIVING_URL = os.environ.get("OSRM_DRIVING_URL", "https://router.project-osrm.org")

# Lifecycle & tracking thresholds
PRICE_CHANGE_THRESHOLD_DKK = 200
MISSING_SCANS_THRESHOLD = 1
EXPIRED_SCANS_THRESHOLD = 3
# v3.5 (spec 06 section 6): only a scan whose coverage_status proves it saw the
# whole site may count as a miss, and three such misses expire a listing.
# EXPIRED_SCANS_THRESHOLD is kept as the legacy name and stays equal to it.
EXPIRED_COMPLETE_MISSES = 3
# An expired listing stays in local processing this long, labelled "last seen N
# days ago". It never holds a building's representative row and never alerts.
EXPIRED_VISIBLE_DAYS = 14
CONSECUTIVE_FAILURE_THRESHOLD = 2
FAILURE_COVERAGE_HOURS = 24

# ==============================================================================
# 3. Portal & Scraper Settings
# ==============================================================================
SITES = ["lejebolig", "boligportal"]

# --- v3.5 (spec 06) discovery envelope ----------------------------------------
# These are the ONLY numbers a portal scan is allowed to read. The eligibility
# constants (RENT_CAP_MAX and the whole v3.3 block in section 6) are read by
# local evaluation and by nothing in the fetch path, so a rule change inside the
# envelope is answered from stored data with no portal request.
#
# The envelope is deliberately wider than the rules: discovery stores every card
# whatever its rent, rooms or type, and only decides which cards deserve a
# detail page in this run.
DISCOVERY_RENT_MAX = 11000          # portal query ceiling and detail-fetch gate
DETAIL_REFRESH_DAYS = 7             # a stored detail page is fresh for this long
DECISION_DETAIL_REFRESH_HOURS = 24  # decision-relevant detail revalidation window (spec 09 §8.1)
# A complete scan must return at least this fraction of the cards the previous
# complete scan of the same site returned; below it the scan is 'partial' and
# may not expire anything.
SCAN_COMPLETE_MIN_RATIO = 0.5
# Bumped whenever the extraction changes shape, so a snapshot records which
# parser produced it.
PARSER_VERSION = "v3.6-2"
LEJEBOLIG_MAX_PAGES = 30

# Site-specific "no results" markers. A zero-card page carrying one of these is
# empty-confirmed (real evidence of an empty market); a zero-card page without
# one is empty-unconfirmed and expires nothing. Spec 06 section 4.4.
NO_RESULTS_MARKERS = {
    "lejebolig": ["ingen boliger", "ingen resultater", "0 boliger",
                  "vi fandt ingen", "ingen ledige boliger"],
    "boligportal": ["ingen boliger", "ingen resultater", "0 boliger til leje",
                    "vi har desv\u00e6rre ingen"],
}

GONE_MARKERS = (
    "siden findes ikke",
    "annoncen er udløbet",
    "boligen er udlejet",
    "annoncen findes ikke",
    "page not found",
    "404 not found",
    "markeret som reserveret af udlejeren",
    "lejemålet er blevet markeret som reserveret",
    "markeret som reserveret",
    "lejemålet er reserveret",
    "boligen er reserveret",
    "ikke længere ledig",
    "denne bolig er ikke længere ledig",
    "desværre udlejet",
    "denne bolig er desværre udlejet",
    "bolig er desværre udlejet",
    "bolig er ikke længere ledig",
)

HEADLESS = True
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
LOCALE = "da-DK"
PAGE_TIMEOUT_MS = 60000

# Lejebolig search areas across Greater Copenhagen (preserved scope)
LEJEBOLIG_AREAS = [
    "koebenhavn-sv",   # Sydhavnen
    "koebenhavn-s",    # Amager
    "koebenhavn-k",    # Indre By
    "koebenhavn-v",    # Vesterbro
    "koebenhavn-oe",   # Osterbro
    "koebenhavn-n",    # Norrebro
    "koebenhavn-nv",   # Nordvest
    "valby",
    "vanloese",
    "frederiksberg",
    "frederiksberg-c",
    "broenshoej",
    "dyssegaard",
    "bagsvaerd",
    "nordhavn",
    "hellerup",
    "taastrup",
    "broendby",
    "hvidovre",
    "roedovre",
    "kongens-lyngby",
    "glostrup",
    "albertslund",
    "ishoej",
    "kastrup",
    "dragoer",
    "soeborg",
    "ballerup",
    "herlev",
    "gentofte",
    "charlottenlund",
]

# ==============================================================================
# 4. Telegram & Alert Settings
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("BOLIGWATCH_TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("BOLIGWATCH_TG_CHAT_ID", "")

# The bot is text-only: no pictures, no link previews, no emoji.
SEND_TELEGRAM_PHOTOS = False
BOT_CONFIRMED = True

# --- Alert volume policy ---
# The dashboard is for browsing; Telegram is for the few things worth
# interrupting a day for. Everything not listed here is stored and shown, never
# sent. What still sends:
#   1. the daily digest (digest.py),
#   2. a price drop on a shortlisted property,
#   3. a portal that stopped returning results,
#   4. a new S- or A-rank flat, whatever its commute band or timeline relevance.
ALERT_INSTANT_RANK_TIERS = ("S", "A")  # rank letters (0-100 score) that interrupt
ALERT_INSTANT_COMMUTE_TIERS = ("A",)   # legacy: commute bands, no longer read by the gate
ALERT_INSTANT_STRETCH = False          # a stretch flat waits for the digest
ALERT_PRICE_DROP_SHORTLIST_ONLY = True
# One address can rotate many listings as tenants move out, so a per-property
# cooldown stops the same building pinging every day.
ALERT_PROPERTY_COOLDOWN_DAYS = 7

# ==============================================================================
# 5. Local Web Viewer Settings
# ==============================================================================
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("WEB_PORT", 58240))
# Informational: keep in step with the schedule you install (deploy/).
SCAN_TIMES = ("10:00", "15:00")
DIGEST_TIME = "10:30"
VERBOSE = os.environ.get("BOLIGWATCH_VERBOSE", "1") == "1"

# ==============================================================================
# 6. v3.0 — Nørreport-commute, value-first model
# ==============================================================================
# --- Anchor points and geography ---
OFFICE_NAME = "Nørreport"
OFFICE_COORDS = (55.6838, 12.5712)              # Nørreport metro entrance; office is 30 m away
GREATER_CPH_BBOX = (55.55, 12.35, 55.80, 12.75)  # south, west, north, east

# --- Reference data files generated by generate_transit.py / generate_water.py ---
TRANSIT_LINES_JSON = os.path.join(BASE_DIR, "transit_lines.json")
STATION_COMMUTE_JSON = os.path.join(BASE_DIR, "station_commute.json")
WATER_EDGES_JSON = os.path.join(BASE_DIR, "water_edges.json")

# --- External routing / data services ---
ORS_API_KEY = os.environ.get("ORS_API_KEY", "")
OSRM_FOOT_URL = os.environ.get("OSRM_FOOT_URL", "https://routing.openstreetmap.de/routed-foot")
OVERPASS_URLS = [
    os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
    "https://overpass.kumi.systems/api/interpreter",
]
REJSEPLANEN_ACCESS_TOKEN = os.environ.get("REJSEPLANEN_ACCESS_TOKEN", "")

# --- Offline transit ride-time model ---
METRO_HOP_MIN = 1.8      # minutes between adjacent metro stations
STOG_HOP_MIN = 2.3       # minutes between adjacent S-tog stations
METRO_WAIT_MIN = 2.0     # initial wait, metro
STOG_WAIT_MIN = 4.0      # initial wait, S-tog
TRANSFER_MIN = 5.0       # line or network change at a shared station
EGRESS_MIN = 2.0         # Nørreport platform to office door

# --- Commute tiers (replace the v2.1 metro categories) ---
TIER_A_MAX_MIN = 20
TIER_B_MAX_MIN = 35
TIER_C_MAX_MIN = 50
# Legacy stretch-band reference used by the over-budget label. The hard cap is
# RENT_CAP_MAX below.
TIER_AB_RENT_MAX = 9500
TIER_C_RENT_MAX = 10000
DIRECT_WALK_OK_MIN = 25
TIER_NAMES = {"A": "Close", "B": "Commutable", "C": "Far"}

# ==============================================================================
# v3.3 — hard eligibility (filters, never score penalties)
# ==============================================================================
# All-inclusive rent cap. Above this the home is rejected before anything else.
# v3.12 (spec 11): 9.500 -> 9.700 kr (audit requirement, 10 October 2026).
RENT_CAP_MAX = 10000
# Audited straight-line distance from the property coordinate to Nørreport. It
# is retained for diagnostics only; the eligibility gate uses the road route.
NORREPORT_RADIUS_MAX_M = 10000
# Driving road-route distance from the property coordinate to Nørreport. The
# cutoff is inclusive and is deliberately separate from transit and commute.
NORREPORT_ROAD_DISTANCE_MAX_M = 10000
# Maximum door-to-door office commute (already the tier-C ceiling).
COMMUTE_MAX_MIN = 50
# v3.11 (spec 10 section 4.1): a commute is never a rejection. When the routed
# commute cannot be computed the straight-line distance to the office is turned
# into an estimate with these constants and the badge `Route estimated`.
COMMUTE_FALLBACK_BASE_MIN = 10.0
COMMUTE_FALLBACK_MIN_PER_KM = 4.0
# v3.10 never zones become a score penalty rather than a rejection (spec 10 call
# 3 overruled). The listing stays visible, down-ranked, with a `Never zone:` badge.
NEVER_ZONE_SCORE_PENALTY = 15.0

# ==============================================================================
# v3.3 — 100-point ranking (weights decided 10 Sep 2026)
# ==============================================================================
# 3.1 Absolute affordability — 35 points.
SCORE_AFFORDABILITY_PTS = 35.0
AFFORDABILITY_FULL_RENT = 7500.0     # rent <= this -> full affordability points
AFFORDABILITY_ZERO_RENT = 10000.0    # ceiling of the linear ramp (== RENT_CAP_MAX)
AFFORDABILITY_SPAN = 2500.0          # (10000 - 7500)

# 3.2 Rapid-transit proximity — 20 points (routed walking, never straight-line).
# v3.10: 30 -> 20. Metro walk and commute time measure the same journey twice.
SCORE_TRANSIT_PTS = 20.0
TRANSIT_METRO_FULL_M = 500.0         # metro <= 500 m  -> 20
TRANSIT_METRO_24_M = 800.0           # metro 800 m     -> 16
TRANSIT_METRO_ZERO_M = 1500.0        # metro 1500 m    -> 0; above needs RP
NORMAL_METRO_WALK_MAX_M = 1500.0     # max metro walk for a normal candidate
RP_S_TRAIN_FULL_M = 400.0            # RP s-train <= 400 m -> 16
RP_S_TRAIN_18_M = 800.0              # RP s-train 800 m    -> 12
RP_S_TRAIN_MAX_M = 800.0             # above -> not an RP -> rejected
RP_RENT_MAX = 8500.0                 # Responsible Pick rent cap

# 3.3 Office commute — 16 points (v3.10: 20 -> 16).
SCORE_COMMUTE_PTS = 16.0
SCORE_COMMUTE_FULL_MIN = 15.0        # <= 15 min -> 16; 15-45 linear 16->0
SCORE_COMMUTE_ZERO_MIN = 45.0        # 45-50 min -> 0 points but still eligible

# 3.4 Location bonuses — 10 points (v3.10: water 6 -> 3, centre 6 -> 4).
SCORE_WATER_PTS = 3.0                # waterfront, existing 300-2000m + lake weight
SCORE_CENTRE_PTS = 4.0               # centrality, existing 1.5-8 km Rådhuspladsen
SCORE_BUZZ_PTS = 3.0                 # urban character, existing buzz signal

# 3.6 Personal fit — 19 points (v3.10, decided 1 Oct 2026). See section 6b.
#   neighbourhood 15 (SCORE_AREA_PTS) + floor height 4 (SCORE_FLOOR_PTS).
# Sum: 35 + 20 + 16 + 3 + 4 + 3 + 15 + 4 = 100.

# 3.5 Signals removed from rank (computed + displayed, zero ranking points).
SCORE_VALUE_PTS = 25.0               # value vs market: display only
SCORE_DATE_PTS = 10.0                # availability/date fit: display only
# (confidence is a badge, never points)

# -------------------------------
# 5. Building / complex consolidation — curated alias map.
# The configuration aliases (not numeric property ids) are the durable source of
# truth. Address matching is normalized and Unicode-insensitive, so ø/oe portal
# variants do not split a group. Door/unit/floor suffixes are ignored; an alias
# missing its house number must be explicitly present to merge a whole street.
COMPLEX_GROUPS = {}
# A profile can add curated groups. Each entry needs a display name, one or more
# house-numbered aliases, an optional street-only alias, and a postcode. Example:
#   "my-complex": {"name": "My complex", "aliases": ["Testvej 1", "Testvej 3"],
#                  "postcode": "2100"}

# --- Rank tiers (defaults; a profile can keep or adjust them) ---
# The letter a human reads is a RANK in the gaming sense: S is the top of the
# ladder, D the bottom. It is derived from the 0-100 score and says nothing
# about the commute band or the budget band, which are shown separately and in
# words. Before this, A/B/C were commute bands and S was a rent band, so a
# cheaper, better flat could display a "lower" letter than a dearer one.
# Ordered highest first; each entry is (letter, minimum score). Last floor is 0.
RANK_TIER_BANDS = (("S", 75), ("A", 60), ("B", 45), ("C", 30), ("D", 0))
RANK_TIER_LETTERS = tuple(letter for letter, _ in RANK_TIER_BANDS)
RANK_TIER_NAMES = {"S": "Exceptional", "A": "Strong", "B": "Solid",
                   "C": "Weak", "D": "Poor"}
RANK_TIER_BLURBS = {
    "S": "Close, well priced and on a lively street. Book a viewing the day it appears.",
    "A": "Strong on most axes. Worth a viewing without hesitation.",
    "B": "Solid, with one clear weakness such as commute or price.",
    "C": "Weak. Only worth a look if the shortlist is empty.",
    "D": "Poor. Kept for the map and the market table, not for viewing.",
}
# Commute bands are the stored `tier` column. Shown in words, never as a letter.
COMMUTE_BAND_NAMES = {"A": "Close", "B": "Commutable", "C": "Far"}
COMMUTE_BAND_BLURBS = {
    "A": f"Door to door to {OFFICE_NAME} in {TIER_A_MAX_MIN} min or less.",
    "B": f"{TIER_A_MAX_MIN + 1} to {TIER_B_MAX_MIN} min door to door.",
    "C": f"{TIER_B_MAX_MIN + 1} to {TIER_C_MAX_MIN} min, rent at most "
         f"{TIER_C_RENT_MAX} kr.",
}
TIER_LETTERS = {1: "A", 2: "B", 3: "C"}
TIER_NUMBERS = {"A": 1, "B": 2, "C": 3}
# Straight-line cutoff above which a direct walk to the office is not even routed
DIRECT_WALK_MAX_STRAIGHT_M = 2500
# Penalty in minutes applied when the address only resolved to street level
GEO_PRECISION_PENALTY_MIN = 3
# v3.11 (spec 10 section 3.2): a postcode centroid is coarser than a street, so
# the commute estimate takes a larger penalty. Never a rejection: a flat with no
# location at all is ranked with location-derived points at 0 and badged.
GEO_PRECISION_POSTCODE_PENALTY_MIN = 6
# Guard rail: a geocode hit outside this radius of Rådhuspladsen is treated as a
# miss, never as a result.
GEO_MAX_DISTANCE_M = 40000.0

# --- Unified 100-point score (v3.3 weights; see the decision block above) ---
# Commute-eligibility curve anchors (used by both the 20-pt commute score and
# the date-fit curve anchors).
SCORE_COMMUTE_PTS = 16.0
SCORE_COMMUTE_FULL_MIN = 15.0
SCORE_COMMUTE_ZERO_MIN = 45.0

# Value vs market: computed and displayed for market intelligence under §3.5,
# but contributes zero ranking points. These curves drive the display only.
SCORE_VALUE_PTS = 25.0
SCORE_VALUE_FULL_RATIO = 0.75
SCORE_VALUE_ZERO_RATIO = 1.25
SCORE_VALUE_NO_SIZE_FACTOR = 0.6
SCORE_VALUE_NO_SIZE_RENT_MAX = 9500.0
SCORE_VALUE_NO_SIZE_DENOM = 4500.0

SCORE_BUZZ_PTS = 3.0
BUZZ_STREET_WEIGHT = 0.65
BUZZ_BUS_WEIGHT = 0.35
BUZZ_MAJOR_CLASS_M = 40.0
BUZZ_MAJOR_NEAR_M = 60.0
BUZZ_BUS_FULL_ROUTES = 4.0
# OSM tags Amagerbrogade, Torvegade and most Copenhagen high streets as
# "tertiary", so highway class alone under-rates exactly the streets a renter
# wants to live on. A named tertiary carrying at least this many bus routes is
# treated as a major street. Set to 0 to fall back to pure highway class.
BUZZ_ARTERIAL_MIN_BUSES = 2

SCORE_WATER_PTS = 3.0
SCORE_WATER_FULL_M = 300.0
SCORE_WATER_ZERO_M = 2000.0
WATER_LAKE_WEIGHT = 0.6

SCORE_CENTRE_PTS = 4.0
SCORE_CENTRE_FULL_M = 1500.0
SCORE_CENTRE_ZERO_M = 8000.0

SCORE_DATE_PTS = 10.0  # display only under §3.5; contributes zero ranking points

# --- Frozen score curves (each is a piecewise-linear anchor list, flat outside).
# The anchors ARE the frozen decisions (10 Sep 2026); interpolate linearly
# between them and clamp to 0 beyond the last anchor.
TRANSIT_METRO_CURVE = [(500, 20), (800, 16), (1500, 0)]   # metro walk m -> pts
RP_S_TRAIN_CURVE = [(400, 16), (800, 12)]                  # RP s-train walk m -> pts
COMMUTE_CURVE = [(15, 16), (45, 0)]                        # commute min -> pts
AFFORDABILITY_CURVE = [(7500, 35), (10000, 0)]           # rent_total -> pts

# --- Market benchmarks ---
BENCHMARK_WINDOW_DAYS = 90
BENCHMARK_MIN_N = 8
BENCHMARK_MIN_SIZE_M2 = 10.0

# --- Timeline relevance ---
ACTIONABLE_BEFORE_DAYS = 45
ACTIONABLE_AFTER_DAYS = 30
UNKNOWN_LIVE_BEFORE_DAYS = 75
POST_TARGET_HORIZON_DAYS = 60

# --- Price and lifecycle alerts ---
PRICE_DROP_SAVED_DKK = 200
PRICE_DROP_ACTIONABLE_DKK = 500

# --- Coverage ---
BP_MAX_PAGES = 30
BP_PAGE_SIZE_FALLBACK = 18          # only used if the store JSON omits "limit"
# Verified in the browser against the script#store JSON on 6 September 2026:
#   - "offset" is still the paging parameter, page size comes from page_props.limit
#     (18 today) and page_props.result_count carries the total.
#   - The city taxonomy has no landing pages for Frederiksberg, Lyngby, Hvidovre
#     and the rest: /lejligheder/frederiksberg/ and /lejligheder/koebenhavns-omegn/
#     both resolve but return result_count 0. The map bbox filter
#     (min_lat/min_lng/max_lat/max_lng) is what actually covers the surrounding
#     municipalities, so the second URL spans GREATER_CPH_BBOX.
#   - There is no rooms query parameter. Rooms are a path segment
#     (/1-vaerelser/, /2-vaerelser/), which the bbox URL cannot carry, so rooms
#     above 2 are dropped client-side before geocoding instead.
_BP_BBOX = GREATER_CPH_BBOX
# v3.5: the query ceiling is the DISCOVERY ceiling, not the rent cap. Anything
# the portal returns below 11,000 kr is stored; the 9,500 kr cap is applied
# locally afterwards, so raising it never needs a re-scan.
BOLIGPORTAL_SEARCH_URLS = [
    f"https://www.boligportal.dk/lejligheder/k%C3%B8benhavn/?max_monthly_rent={DISCOVERY_RENT_MAX}",
    (f"https://www.boligportal.dk/lejligheder/?max_monthly_rent={DISCOVERY_RENT_MAX}"
     f"&min_lat={_BP_BBOX[0]}&min_lng={_BP_BBOX[1]}"
     f"&max_lat={_BP_BBOX[2]}&max_lng={_BP_BBOX[3]}"),
]
BP_MAX_ROOMS = 2


# ==============================================================================
# 6b. Personal fit
# ==============================================================================
# Distance alone does not say whether a neighbourhood suits you, so a profile
# can rate stations 0..1 and mark places to avoid. Unrated stations score
# AREA_DEFAULT_RATING, which makes the feature neutral out of the box.

# 6b.1 Neighbourhood — 15 points, rated 0..1 by the nearest station of any
# network (geo.nearest_transit). Empty means every station is neutral.
SCORE_AREA_PTS = 15.0
AREA_DEFAULT_RATING = 0.5
AREA_STATION_RATINGS = {}

# 6b.2 Never zones — down-ranked with a badge, checked right after geocoding.
# A profile supplies postcodes and optional radius zones.
NEVER_POSTCODES = {}
# (name, lat, lon, radius_m). Straight-line radius around the point.
NEVER_ZONES = ()

# 6b.3 Floor height — 4 points. Privacy, skyline and distance from street noise.
# Read from the listing's "Etage" field or the address floor ("1. 9", "st. tv").
SCORE_FLOOR_PTS = 4.0
FLOOR_CURVE = [(0, 0.0), (4, 4.0)]   # ground floor 0 .. 4th floor and up 4
FLOOR_UNKNOWN_PTS = 2.0


# ==============================================================================
# 7. v3.6 (spec 07) — trust gate rule inputs
# ==============================================================================
# Spec 07 section 6.3: every input the trust classifiers read lives here and is
# named in ELIGIBILITY_CONSTANTS below, because eligibility_rules_version()
# hashes config globals only. A phrase list, a state vocabulary, a reason or a
# threshold kept inside filters.py would let a wording change reuse verdicts
# produced by the old text. filters.py binds these names; no trust rule is
# written inline there.

# --- Section 4.4 — the trust gate's own vocabulary -----------------------------
# The three outcomes of section 5.3's `trust_outcome` column. `eligible` is
# section 4.4's "continue through existing gates"; `review` maps to the existing
# `quarantined` outcome and `reject` to `rejected` (section 5.3).
TRUST_OUTCOMES = ("eligible", "review", "reject")

# The three independent decision vocabularies (sections 4.1, 4.2, 4.3,
# verbatim). A value outside its vocabulary is never a verdict.
TRUST_UNIT_TYPE_STATES = ("self_contained_apartment", "room_rental",
                          "house_or_other", "unknown")
TRUST_TENANCY_CHANNEL_STATES = ("professional_direct", "private_landlord",
                                "sublet", "unknown")
TRUST_BUILDING_FORM_STATES = ("managed_multi_unit_block",
                              "small_or_private_property", "unknown")

# Section 5's reason strings the trust gate states. The first is section 4.4's
# own words (`room rental / not self-contained` is also section 6.2's rejection
# reason); the last two are section 7.2's. v3.7 (section 4.3) removed
# `landlord / direct lease unverified` and `managed apartment block unverified`:
# no code path may emit them, and the only remaining review reason is
# `self-contained apartment unverified`.
TRUST_REASON_ROOM = "room rental / not self-contained"
TRUST_REASON_UNIT_UNVERIFIED = "self-contained apartment unverified"
TRUST_REASON_STALE = "verification stale"
TRUST_REASON_CONFLICT = "conflicting building evidence"

# Section 4.3's (v3.7) decision table, in precedence order, one row per line:
#   (unit-type states, tenancy states, building states, outcome, primary reason)
# `"any"` matches every state in that column. A reason of `None` means the row's
# own component supplies its accurate reason (section 4.2's `sublet` /
# `private landlord`, section 6.2's room reason, `house / other`). The last row
# is the catch-all and never fires while any component is an explicit rejection.
# v3.7 inverted the two evidence gates into reject-only filters, so the two
# review rows that held a listing on missing landlord or building evidence are
# gone and the apartment row continues through the existing gates with either
# evidence present or absent.
TRUST_DECISION_TABLE = (
    (("room_rental", "house_or_other"), ("any",), ("any",), "reject", None),
    (("self_contained_apartment",), ("sublet", "private_landlord"), ("any",),
     "reject", None),
    (("self_contained_apartment",), ("professional_direct", "unknown"),
     ("managed_multi_unit_block", "unknown"), "eligible", None),
    (("unknown",), ("any",), ("any",), "review", TRUST_REASON_UNIT_UNVERIFIED),
)

# --- Section 4.1 — unit-type evidence inputs -----------------------------------
# Card/title phrases that mark a rented ROOM, not a flat. Deliberately avoids
# "1 værelse", "ét værelse" and "1-værelses lejlighed", which describe the room
# COUNT of a legitimate studio (section 3.1, the Jorisvej counterexample).
UNIT_ROOM_CARD_MARKERS = (
    "værelse i ", "vaerelse i ",
    "værelse på ", "vaerelse paa ",
    "værelse til leje", "vaerelse til leje",
    "værelse til udleje", "vaerelse til udleje",
    "room for rent", "room for rent in", "a room for rent",
    "room in ", "room with shared", "room rental", "rent a room",
    "room in a shared apartment",
)
# Compiled by filters.py; both card regexes are matched case-insensitively.
UNIT_SIZE_MARKED_ROOM_RE = r"\d+[.,]?\d*\s*m[²2]?\s*(?:stort\s+|stor\s+)?(?:v[æa]relse)\b"
UNIT_APARTMENT_CARD_RE = (
    r"lejlighed|lejligheder|apartment|studio|1-v[æa]relses|2-v[æa]relses|"
    r"1-vaerelses|bolig i |lejemål i ")

# v3.12 (spec 11) section 4.1: decisive room wording matched against the product
# metadata ONLY - title, card text, URL and address - and honoured before the
# portal's structured category or the detail 'Boligtype Lejlighed' label (audit
# 2026-10-09: Ebertsgade filed a klubværelse as 'Lejlighed' and ranked #1).
# Each entry is a case-insensitive regex. Deliberately avoids "1 værelse",
# "1-værelses lejlighed" and "værelser for" which describe the room COUNT of a
# legitimate studio (spec 07 section 3.1, the Jorisvej counterexample). The
# `(?<!\d )` guard on "værelse på" keeps "1 værelse på 33 m2" a room count.
UNIT_ROOM_TITLE_MARKERS = (
    r"\bklubv[æa]relse\b",
    r"\bv[æa]relse\s+til\s+(?:ud)?leje\b",
    r"\bv[æa]relse\s+til\s+1\s+person\b",
    r"\bv[æa]relse\s+i\b",
    r"(?<!\d )\bv[æa]relse\s+p[åa]\b",
    r"\bv[æa]relse\s+(?:nr\.?|no\.?)?\s*\d+",
    r"\bv[æa]relser\s+deler\b",
    r"\bdeler\s+.{0,20}badeværelse\b",
    r"\bshared\s+bathroom\b",
    r"\bdelv[æa]relse\b",
    r"\broom\s+for\s+rent\b",
    r"\broom\s+in\s+(?:a\s+)?shared\s+apartment\b",
    r"\broom\s+in\b",
    r"\brent\s+a\s+room\b",
    r"\broom\s+rental\b",
    r"\bflatshare\b",
    r"\bdelebolig\b",
    r"\b(?:v[æa]relse|klubv[æa]relse|delv[æa]relse|room)\b.{0,40}\btil\s+1\s+person\b",
)
# A "Lejlighed"-filed card whose usable area is below this many m2 and whose
# product metadata names a room is rejected as a room (audit 2026-10-09 §6.1).
UNIT_ROOM_MIN_AREA_M2 = 15.0

# --- Section 4.2 — tenancy / landlord evidence inputs --------------------------
# The advert's OWN text only. The portal's own identity, footer and CVR are not
# evidence about the advertiser (section 4.2 bullet 2, case 9.3.7), so a stored
# landlord fact carrying any of them is treated as absent, never professional.
TENANCY_PORTAL_IDENTITY_TOKENS = (
    "lejebolig.dk",
    "cvr-nr. 27258948",
    "kontakt udlejer",
    "valideret af boligportal",
    "om udlejeren",
    "mere end 1000 annoncer",
)

# Section 4.2 bullet 1, the spec's literal token list. Sublet is the first
# precedence step, so it wins over every other channel and reports its own
# reason even when the period is unlimited (case 9.3.4) instead of being
# reported merely as "time-limited". The non-sublet occurrences of "fremleje"
# seen in production ("gæsteværelser til fremleje", "må ikke fremlejes") get no
# negation carve-out here: that is a reported observation for a later row.
TENANCY_SUBLET_PATTERNS = (
    r"\bfremleje\b",
    r"\bfremlejes\b",
    r"\bsublet\b",
    r"\bsublease\b",
    r"room in shared apartment",
)

# Section 4.2 bullet 4: unambiguous first-person private rental wording.
# Production-confirmed examples: "Jeg udlejer min lejlighed", "I min lejlighed på
# Nørrebro udlejer jeg et møbleret værelse", "udlejer derfor to tilstødende
# værelser i min lejlighed" (lejebolig) and "I'm subletting my apartment"
# (boligportal). No structured private-advertiser flag could be verified in
# stored data - source_record.is_owner is false on all 471 stored boligportal
# rows - so this wording route is the implementable one, and no metadata is
# invented for it.
TENANCY_PRIVATE_LANDLORD_PATTERNS = (
    r"\bjeg\s+udlejer\b",
    r"\budlejer\s+jeg\b",
    r"\bmin\s+lejlighed\b",
    r"\bi'?m\s+subletting\b",
    r"\bi\s+am\s+subletting\b",
    r"\bmy\s+(?:apartment|flat)\b",
    # v3.12 (spec 11): the auditor's production examples (audit 2026-10-09
    # section 2B). A private couple or an owner-occupied house is a private
    # landlord, never a professional operator.
    r"\bvi\s+er\s+(?:et\s+)?ældre\s+par\b",
    r"\budlejers\s+r[æa]kkehus\b",
    r"\bvores\s+lejlighed\b",
    r"\bmin\s+egen\s+lejlighed\b",
    r"\bjeg\s+lejer\s+ud\b",
)

# Section 5.1's vocabulary for landlord_evidence_source: the only channel by
# which a manual review can reach the pure, record-only tenancy classifier.
TENANCY_LANDLORD_EVIDENCE_SOURCES = ("structured", "detail", "manual")

# Section 4.1 route 3c (v3.7): the advert's OWN BoligPortal advertiser block is
# positive professional-management evidence once its portfolio reaches this many
# rentables. A smaller portfolio is not evidence of anything - a bare `privat` or
# a small portfolio is not a rejection either (section 12) - and the count is
# badge evidence only, never a landlord name.
TENANCY_PORTFOLIO_MIN_RENTABLES = 10

# Section 4.1 route 3d (v3.7) was "a portal that hosts housing associations and
# professional operators only". FindBolig was retired in v3.11 (spec 10 section
# 7), so no portal qualifies by identity any more.
TENANCY_PROFESSIONAL_PORTALS = ()

# --- Section 4.3 — building-form evidence inputs -------------------------------
# Section 7.2: professional-management or building evidence older than this many
# days is `verification stale`. It stays in Needs verification until it is
# refreshed or manually reviewed; a local refresh never fetches it.
BUILDING_EVIDENCE_MAX_DAYS = 90

# Section 4.3 bullet 3: "at least three distinct self-contained source listings
# at the exact building/configured complex in the last 90 days" - and only
# together with the same professional-operator evidence as bullet 2. Section 4.2
# bullet 5, section 9.3 case 8 and section 12 all forbid repeated inventory as
# block or landlord proof on its own.
BUILDING_FORM_MIN_LISTINGS = 3

# Section 5.2's `evidence_kind` vocabulary mapped onto EvidenceDecision.source.
# `source_detail` is a parser read of the canonical detail page, i.e. `detail`.
BUILDING_PROFILE_EVIDENCE_KINDS = {"manual": "manual", "operator_page": "operator_page",
                                   "source_detail": "detail"}

# A definite reviewed building verdict: the two states that can stand on their
# own. A stored `unknown` is not a decision to protect - fresh evidence is
# allowed to arrive - and a stored state on its own is never positive evidence.
BUILDING_PROFILE_DEFINITE_STATES = ("managed_multi_unit_block",
                                    "small_or_private_property")


# ==============================================================================
# 9. v3.8 (spec 09) — demographic and price eligibility
# ==============================================================================
STUDENT_REQUIREMENT_STATES = ("required", "not_required", "unknown", "conflict")
AGE_REQUIREMENT_STATES = ("bounded", "none", "unknown", "conflict")
PROFILE_AGE_FITS = ("eligible", "ineligible", "unknown")

ELIGIBILITY_REASON_STUDENT = "student housing"
ELIGIBILITY_REASON_YOUTH = "youth housing (students only)"
ELIGIBILITY_REASON_COOP = "cooperative housing (andelsbolig)"
ELIGIBILITY_REASON_AGE_CONFLICT = "housing eligibility evidence conflicts"
ELIGIBILITY_REASON_AGE_REQUIRED = "profile age required"
ELIGIBILITY_REASON_AGE_UNVERIFIED = "age eligibility unverified"
ELIGIBILITY_REASON_PRICE_CONFLICT = "price conflict"


# ==============================================================================
# 10. v3.5 (spec 06) — eligibility rules version
# ==============================================================================
# A local evaluation stamps the version of the rules that produced it, so the
# dashboard can say "rules applied today 14:32 (rules v3.6-1)" and a refresh can
# skip listings whose rules and snapshot are both unchanged.
#
# The version is a hash of every constant local evaluation is allowed to read,
# plus a manually bumped tag. Change a threshold and the hash moves on its own;
# change wording or a phrase list and bump the tag by hand.
#
# v3.6 (spec 07) bump, section 6.3: the tag moves and the trust gate's rule
# inputs (section 7 above) join the hashed set, so a classification change
# forces a new local evaluation instead of reusing a v3.5 verdict.
#
# v3.7 (spec 08) bump, section 5: the two evidence gates became reject-only, the
# table lost its two review rows, and the tenancy badge evidence routes 3c/3d
# joined the hashed set, so every stored listing is re-evaluated by the next
# local refresh.
#
# v3.8 (spec 09) bump, section 10.1: demographic fact vocabularies, reason strings,
# and DECISION_DETAIL_REFRESH_HOURS join the hashed set.
# v3.8-2: known location, room count, and total rent precede missing evidence;
# project landing pages are excluded and communal amenity wording is scoped.
# v3.10-1: personal fit — neighbourhood, never zones, floor, early-start cost.
# v3.11-1 (spec 10): only price and an ineligible studio reject; commute, Metro,
# Nørreport distance and the never zones became score/badges. The retired hard
# rule constants left ELIGIBILITY_CONSTANTS so every listing re-evaluates.
# v3.12 (spec 11, audit 2026-10-09): the hard rules are re-hardened - rent cap
# 9.700, private landlord and commute > 50 min reject again, a decisive room
# marker in the title/card/url overrides the portal Boligtype, a kollegium in
# the title is student housing, shared kitchen/bathroom with no private signal
# holds, time-limited leases are matched from wording, and lease durations are
# excluded from the age parser. The tag bumps so every stored listing is
# re-evaluated by the next local refresh.
RULES_VERSION_TAG = "v3.12-1"

ELIGIBILITY_CONSTANTS = (
    "RENT_CAP_MAX", "MOVE_IN_COST_MAX",
    "TIER_A_MAX_MIN", "TIER_B_MAX_MIN", "TIER_C_MAX_MIN", "TIER_C_RENT_MAX",
    "TIER_AB_RENT_MAX", "SCORE_AFFORDABILITY_PTS", "SCORE_TRANSIT_PTS",
    "SCORE_COMMUTE_PTS", "SCORE_WATER_PTS", "SCORE_CENTRE_PTS",
    "SCORE_BUZZ_PTS", "SCORE_DATE_PTS", "AFFORDABILITY_CURVE",
    "TRANSIT_METRO_CURVE", "RP_S_TRAIN_CURVE", "COMMUTE_CURVE",
    "SCORE_WATER_FULL_M", "SCORE_WATER_ZERO_M", "WATER_LAKE_WEIGHT",
    "SCORE_CENTRE_FULL_M", "SCORE_CENTRE_ZERO_M",
    "GEO_PRECISION_PENALTY_MIN", "GEO_PRECISION_POSTCODE_PENALTY_MIN",
    "RANK_TIER_BANDS",
    "ACTIONABLE_BEFORE_DAYS", "ACTIONABLE_AFTER_DAYS",
    "UNKNOWN_LIVE_BEFORE_DAYS", "POST_TARGET_HORIZON_DAYS",
    "TARGET_MOVE_DATE", "PREFERRED_WINDOW_START", "PREFERRED_WINDOW_END",
    "DATE_TOLERANCE_DAYS", "EXPIRED_VISIBLE_DAYS", "DECISION_DETAIL_REFRESH_HOURS",
    # v3.11 section 4.1: the soft-rule inputs (fallback estimate and never-zone
    # penalty) are read by local evaluation, so they join the hashed set.
    "COMMUTE_FALLBACK_BASE_MIN", "COMMUTE_FALLBACK_MIN_PER_KM",
    "NEVER_ZONE_SCORE_PENALTY", "GEO_MAX_DISTANCE_M",
    # v3.6 (spec 07) section 6.3 — the trust gate's rule inputs plus the manual
    # rule tag itself. Section 7 above holds the values; every name must resolve,
    # or a missed typo would silently weaken the version hash.
    "RULES_VERSION_TAG",
    "TRUST_OUTCOMES", "TRUST_UNIT_TYPE_STATES", "TRUST_TENANCY_CHANNEL_STATES",
    "TRUST_BUILDING_FORM_STATES", "TRUST_DECISION_TABLE",
    "TRUST_REASON_ROOM", "TRUST_REASON_UNIT_UNVERIFIED",
    "TRUST_REASON_STALE", "TRUST_REASON_CONFLICT",
    "UNIT_ROOM_CARD_MARKERS", "UNIT_SIZE_MARKED_ROOM_RE", "UNIT_APARTMENT_CARD_RE",
    "UNIT_ROOM_TITLE_MARKERS", "UNIT_ROOM_MIN_AREA_M2",
    "TENANCY_PORTAL_IDENTITY_TOKENS", "TENANCY_SUBLET_PATTERNS",
    "TENANCY_PRIVATE_LANDLORD_PATTERNS", "TENANCY_LANDLORD_EVIDENCE_SOURCES",
    "TENANCY_PORTFOLIO_MIN_RENTABLES", "TENANCY_PROFESSIONAL_PORTALS",
    "BUILDING_EVIDENCE_MAX_DAYS", "BUILDING_FORM_MIN_LISTINGS",
    "BUILDING_PROFILE_EVIDENCE_KINDS", "BUILDING_PROFILE_DEFINITE_STATES",
    # v3.8 (spec 09) section 10.1 — demographic and price eligibility constants
    "STUDENT_REQUIREMENT_STATES", "AGE_REQUIREMENT_STATES", "PROFILE_AGE_FITS",
    "ELIGIBILITY_REASON_STUDENT", "ELIGIBILITY_REASON_YOUTH", "ELIGIBILITY_REASON_COOP", "ELIGIBILITY_REASON_AGE_CONFLICT",
    "ELIGIBILITY_REASON_AGE_REQUIRED", "ELIGIBILITY_REASON_AGE_UNVERIFIED",
    "ELIGIBILITY_REASON_PRICE_CONFLICT",
    # v3.10 section 6b — personal fit
    "SCORE_AREA_PTS", "AREA_DEFAULT_RATING", "AREA_STATION_RATINGS",
    "NEVER_POSTCODES", "NEVER_ZONES", "SCORE_FLOOR_PTS", "FLOOR_CURVE",
    "FLOOR_UNKNOWN_PTS", "EARLY_START_MAX_DAYS", "EARLY_START_DAYS_PER_MONTH",
    "EARLY_START_AMORTISE_MONTHS",
    # Profile-driven structural rules
    "ROOMS_MIN", "ROOMS_MAX", "SITES", "LEJEBOLIG_AREAS",
    "GREATER_CPH_BBOX", "BP_MAX_ROOMS",
)


def eligibility_rules_version() -> str:
    """Stable identifier for the current eligibility rules.

    Deliberately computed rather than stored: a forgotten manual bump after a
    threshold change would silently let a refresh skip every listing.
    """
    import hashlib as _hashlib
    import json as _json
    payload = {name: globals().get(name) for name in ELIGIBILITY_CONSTANTS}
    blob = _json.dumps(payload, sort_keys=True, default=str)
    digest = _hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]
    return f"{RULES_VERSION_TAG}+{digest}"


# ==============================================================================
# 11. Optional JSON profile
# ==============================================================================
# `profile.json` (or $BOLIGWATCH_PROFILE, or --profile PATH) overrides the
# values a user is likely to change and rebuilds the derived curves. Invalid
# profiles fail loudly at import rather than being silently ignored.
import profile as _profile  # noqa: E402  (loaded last on purpose)

PROFILE_PATH = _profile.apply(globals())
