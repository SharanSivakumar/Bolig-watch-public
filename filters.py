"""Normalization, verification, tiering and scoring for BoligWatch v3.4.

Listings are placed in a commute tier by door-to-door time to the office anchor
at Noerreport, then scored out of 100 on a single scale: affordability 35,
rapid-transit proximity 30, commute 20, waterfront 6, centrality 6, urban
character 3. Value-vs-market and availability/date fit are computed and shown
but contribute zero ranking points (§3.5).

Hard rules:
- All-inclusive rent <= 9,700 kr (hard cap, above rejects).
- Driving road-route distance to Nørreport <= 10,000 m (10,000 m passes);
  straight-line distance is retained only as an audit diagnostic.
- 1 to 2 room self-contained home; shared, student, senior and care housing out.
- Private kitchen and private bathroom.
- Unlimited lease (time-limited rejects, unknown quarantines).
- Complete move-in cost <= 50,000 DKK.
- Door-to-door commute <= 50 min.
- A normal candidate must reach a Metro on foot within 1,500 m; otherwise it
  may still pass as a Responsible Pick with an S-train within 800 m and rent
  <= 8,500 kr.

Ordering matters: every text and money check runs before any geo or routing
call, so a listing that fails on rent never costs an external request.
"""
import dataclasses
import datetime
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import benchmarks
import config
import features
import geo

# --- Studio & Facility Verification Patterns ---
# Bare "studerende" used to live in this list and was the single worst filter in
# the system. Lejebolig appends a boilerplate paragraph to every description
# naming the schools within 5 km ("... gør området relevant for studerende"),
# which rejected 37 ordinary private flats across 11 addresses, the whole Viola
# Nørløvs Gade building among them. Only wording that actually restricts the
# tenancy to students belongs here; proximity to a campus does not.
STUDENT_NEG = [
    "kollegie", "studiebolig", "studieboliger", "kollegium",
    "studieboligen", "student housing",
]

# Matched against the title and URL only, never the description body. A title
# names the product ("Canvas Student Nordhavn" is a student-housing brand); a
# description may mention students about the neighbourhood, which is exactly the
# trap the bare "studerende" keyword fell into. Verified against all stored
# listings: "student" appears in a title or URL twice, both genuine, and in no
# description at all.
STUDENT_NAME_REGEX = [
    r"\bstudent\w*\b",
    r"\bstudie\s?bolig\w*\b",
    r"\bcampus\s+living\b",
    # v3.12 (spec 11): a kollegium/kollegie named in the title is decisive
    # student housing even when the portal's "Kun for studerende: Nej" fact
    # flips the demographic classifier to not_required (audit 2026-10-09
    # section 2C: fuglebakkekollegiet ranked on that fact). No leading \b:
    # "fuglebakkekollegiet" is one composite word with no word boundary.
    r"kollegie\w*",
    r"kollegium\w*",
]

STUDENT_REGEX = [
    r"\bforbeholdt\s+(?:\w+\s+){0,3}studerende\b",
    r"\b(?:kun|udelukkende|udelukkede)\s+(?:for|til)\s+studerende\b",
    r"\budlejes\s+(?:kun\s+)?til\s+studerende\b",
    r"\bstudieaktiv(?:e|itet)?\b",
    r"\bstudiedokumentation\b",
    r"\bindskrevet\s+(?:som\s+)?studerende\b",
    r"\bkrav\s+om\s+studie\w*\b",
    r"\bmust\s+be\s+a?\s*student\b",
    r"\bstudents\s+only\b",
    r"\bonly\s+for\s+students\b",
    r"\bfor\s+students\s+only\b",
    r"\bstudent[- ]only\b",
    # v3.11 (spec 10 section 6.2): Danish youth housing that requires education.
    r"\bskal\s+være\s+under\s+uddannelse\b",
    # Bare "under uddannelse" is scoped to a tenancy requirement: the Lejebolig
    # boilerplate ("... relevant for personer under uddannelse") describes the
    # neighbourhood and must not reject an ordinary flat.
    r"\b(?:kræver|forudsætter|krav\s+om|kun\s+for|forbeholdt)\s+(?:\w+\s+){0,2}under\s+uddannelse\b",
    r"\buddannelsessøgende\b",
    r"\bkrav\s+om\s+(?:uddannelse|studie\w*|studiekort)\b",
    r"\bkun\s+for\s+(?:unge\s+)?under\s+uddannelse\b",
    r"\bungdomsbolig\w*[^.!?]{0,80}\b(?:studerende|uddannelse)\b",
]

ELDERLY_NEG = [
    "ældrebolig", "aeldrebolig", "ældreboliger", "aeldreboliger",
    "seniorbolig", "seniorboliger", "seniorbofællesskab",
    "plejebolig", "plejeboliger", "plejehjem", "beskyttet bolig",
    "elderly housing", "senior housing",
    # v3.11 (spec 10 section 6.3).
    "aldersbolig", "aldersrentebolig", "pensionistbolig", "pensionistlejlighed",
]

ELDERLY_REGEX = [
    r"\bældrebolig(?:er)?\b",
    r"\baeldrebolig(?:er)?\b",
    r"\bseniorbolig(?:er)?\b",
    r"\bplejebolig(?:er)?\b",
    r"\bplejehjem\b",
    r"\b(?:for|kun|over)\s+(?:50|55|60|65)\s*"
    r"(?:\+|år(?:ige)?|aar(?:ige)?|years?|plus)(?![a-zæøå0-9])",
    r"\b(?:50|55|60|65)\+\s*bolig\b",
    r"\bsenior\b",
    # v3.11 (spec 10 section 6.3).
    r"\baldersbolig(?:er)?\b",
    r"\baldersrentebolig(?:er)?\b",
    r"\bpensionistbolig(?:er)?\b",
    r"\bpensionistlejlighed(?:er)?\b",
    r"\b(?:50|55|60|65)\s*\+",
]

# v3.12 (spec 11) section 6: wording in the DETAIL BODY that restricts the
# tenancy to seniors. A bare feature-list label ("Ældrebolig Elevator ...")
# is deliberately NOT here: Lejebolig renders that generic label on ordinary
# flats, and 28 of 29 "elderly" rejects were that non-event (audit 2026-10-09
# section 3.4). The decisive product names (ældrebolig, seniorbolig, plejebolig,
# "55+", ...) are matched against the title/URL only; the body gets these
# restricting phrases only.
ELDERLY_BODY_RESTRICTING_REGEX = [
    r"\b(?:for|kun|over|til)\s+(?:50|55|60|65)\s*(?:\+|år(?:ige)?|aar(?:ige)?|years?|plus)(?![a-zæøå0-9])",
    r"\b(?:50|55|60|65)\s*\+\s*bolig\b",
    r"\b(?:50|55|60|65)\s*\+",
    r"\bkun\s+(?:for\s+)?(?:ældre|seniorer|pensionister)\b",
    r"\b(?:ældre|seniorer|pensionister)\s+forbeholdt\b",
    r"\budlejes\s+(?:kun\s+)?(?:til|for)\s+(?:ældre|seniorer|pensionister)\b",
    r"\b(?:for|til)\s+(?:ældre|seniorer)\s+(?:bolig|lejlighed|beboere)\b",
]

CARE_NEG = ["care housing", "care home", "plejebolig", "plejehjem",
            "beskyttet bolig", "plejeboliger"]

# Three separate signal families, deliberately never one combined "shared"
# boolean (spec 06 section 8.2). Collapsing them is what made an extra shared
# kitchen in a building reject a flat with its own kitchen, and what made a
# shared bathroom report itself as a shared kitchen.
#
# 1. True shared housing: the home itself is not self-contained. Disqualifying
#    regardless of kitchen ownership.
TRUE_SHARED_HOUSING_NEG = [
    r"\bbofællesskab\b", r"\bdelelejlighed\b", r"\bdelebolig\b",
    r"\broommate\b", r"\broomies\b", r"\bco-living\b", r"\bcoliving\b",
    r"\bværelse\s+til\s+leje\b", r"\bflatshare\b",
    # Spec 07 section 6.2: explicit room/share wording, including English.
    r"\bværelse\s+i\b",
    r"\bshared\s+apartment\b",
    r"\broom\s+in\s+(?:a\s+)?shared\s+apartment\b",
    r"\ba\s+room\s+for\s+rent\b",
    r"\broom\s+rental\b",
]

# 2. Shared bathroom: its own hard requirement with its own reason text, so a
#    rejection never says "shared kitchen" when the bathroom is the problem.
SHARED_BATHROOM_NEG = [
    r"\bfælles\s+bad\b", r"\bdelt\s+bad\b", r"\bfælles\s*badeværelse\b",
    r"\bfællesbad\w*\b", r"\btoilet\s+på\s+gangen\b", r"\bshared\s+bathroom\b",
    # v3.12 (spec 11) section 6.4: production examples the old list missed.
    r"\bbadev[æa]relset\s+ligger\s+over\s+gangen\b",
    r"\b(?:badev[æa]relse|toilet|bruser)\w*[^.!?]{0,30}\bdeles\s+med\b",
    r"\bfælles\s+toilet\b",
    r"\bdeler\s+.{0,25}(?:toiletter|badev[æa]relser|badefaciliteter)\b",
    r"\btoilet(?:ter)?\s+og\s+badefaciliteter\b",
    r"\bdelt\s+badev[æa]relse\b",
]

# 3. Kitchen sharing: evidence that A kitchen is shared. On its own this says
#    nothing about whether the unit also has its own; that is decided by the
#    signal model in analyse_kitchen.
SHARED_KITCHEN_PATTERNS = [
    r"\bfælles\s*køkken\w*\b", r"\bfælleskøkken\w*\b",
    r"\bdeler\s+køkken\b", r"\bdele\s+køkken\b", r"\bkøkken\s+deles\b",
    r"\bshared\s+kitchen\b", r"\bcommon\s+kitchen\b",
]


def _shared_kitchen_in_amenity_list(text: str, hit: re.Match) -> bool:
    """A communal kitchen listed among building facilities is not a unit fact."""
    before = text[max(0, hit.start() - 110):hit.start()]
    return bool(re.search(
        r"(?:faciliteter\s+(?:såsom|som)|fællesfaciliteter\s+(?:såsom|som)|"
        r"beboere\s+har\s+adgang\s+til)\b[^.!?]{0,100}$", before))


def _unit_shared_kitchen_match(text: str) -> Optional[re.Match]:
    for pattern in SHARED_KITCHEN_PATTERNS:
        for hit in re.finditer(pattern, text):
            if not _shared_kitchen_in_amenity_list(text, hit):
                return hit
    return None


def _unit_shared_kitchen_match_any(text: str) -> Optional[re.Match]:
    """Any shared-kitchen mention, including one inside a building-amenity list.

    The raw signal is never dropped before it can be judged: whether an
    amenity-list mention matters is decide in analyse_kitchen, where it depends
    on whether the unit also shows a private kitchen (v3.12, spec 11 section 8).
    """
    for pattern in SHARED_KITCHEN_PATTERNS:
        hit = re.search(pattern, text)
        if hit:
            return hit
    return None

# Wording that restricts the tenancy: the kitchen you get is the shared one.
# These are the phrases that make "shared" mean "instead of", not "as well as".
ONLY_SHARED_KITCHEN_PATTERNS = [
    r"\bdu\s+deler\s+køkken\s+med\b",
    r"\bkøkken\s+deles\s+med\b",
    r"\bdeler\s+køkken\s+med\s+(?:andre|de\s+øvrige|øvrige)\b",
    r"\bshared\s+kitchen\s+with\s+other\s+tenants\b",
    r"\bshare\s+the\s+kitchen\s+with\b",
    r"\byou\s+share\s+the\s+kitchen\b",
    r"\bkun\s+adgang\s+til\s+fælleskøkken\b",
    r"\bfælleskøkken\s+only\b", r"\bshared\s+kitchen\s+only\b",
    r"\bkun\s+fælleskøkken\b",
    # v3.12 (spec 11): "Kun fælles køkken og fælles bad." restricts the unit to
    # the shared kitchen, so the kitchen gate (evaluated before the bathroom
    # gate) rejects rather than the shared bathroom taking the blame.
    r"\bkun\s+fælles\s+køkken\b",
]

# Weaker wording that only restricts the tenancy when the same evidence offers
# no private kitchen of its own. "Adgang til fælleskøkken" beside "eget køkken"
# describes an extra facility, which spec 06 section 8.1 requires to pass.
ONLY_SHARED_KITCHEN_WEAK_PATTERNS = [
    r"\badgang\s+til\s+fælleskøkken\w*\b",
    r"\badgang\s+til\s+fælles\s+køkken\w*\b",
    r"\baccess\s+to\s+(?:a\s+|the\s+)?shared\s+kitchen\b",
]

# Retained under its historical name for the housing classifier. The kitchen
# phrases that used to live here now belong to the kitchen model, so detecting
# a shared kitchen can no longer override a positive private-kitchen signal.
TRUE_SHARED_NEG = TRUE_SHARED_HOUSING_NEG + SHARED_BATHROOM_NEG

OWN_KITCHEN_POS = [
    "eget køkken", "egen køkken", "eget køkken og bad",
    "eget tekøkken", "tekøkken", "eget thekøkken", "thekøkken",
    "trinettekøkken", "privat køkken", "private køkken",
    "køkken i lejligheden", "køkken i boligen", "eigen køkken",
    "eget køkken og badeværelse", "køkken og bad i lejligheden",
    "eget badeværelse og køkken", "eget køkkenniche", "køkkenniche",
    "køkken-alrum", "køkken/alrum",
    # v3.12 (spec 11) section 7: own-kitchen wording the audit found on stored
    # listings. "et særligt køkken til boligen" (Jorisvej 1908481) and "dit eget
    # HTH-køkken" (Gyngemose Parkvej 5836720) both mean the unit has its own.
    "særligt køkken", "hth-køkken", "eget hth-køkken",
]

OWN_KITCHEN_REGEX = [
    # English wording carries the same weight as the Danish: the portals publish
    # English descriptions for international tenants.
    r"\b(?:private|own)\s+kitchen(?:ette)?\b",
    r"\bkitchen\s+in\s+the\s+(?:apartment|flat|unit)\b",
    r"(?<!shared )(?<!common )\bkitchenette\b",
    r"eget\s+køkken", r"eget\s+tekøkken", r"eget\s+thekøkken", r"tekøkken",
    r"thekøkken", r"trinettekøkken", r"køkkenniche", r"eget\s+køkkenniche",
    r"privat\s+køkken", r"køkken\s+i\s+lejligheden", r"køkken\s+i\s+boligen",
    r"køkkenet\s+(?:er|har)", r"mindre\s+køkken",
    r"køkken\s+med\s+(?:to\s+)?(?:induktions)?kogeplader",
    r"kogeplader.*(?:køle|håndvask|fryseskab)",
    r"(?:køle|fryse).*kogeplader",
    r"madlavning\s+og\s+bad",
]

OWN_BATHROOM_POS = [
    "eget bad", "eget badeværelse", "eget toilet", "privat bad",
    "privat badeværelse", "privat toilet", "badeværelse i lejligheden",
    "eget køkken og bad", "eget køkken og badeværelse", "køkken og bad i lejligheden",
    "eget badeværelse og køkken",
]

OWN_BATHROOM_REGEX = [
    r"eget\s+bad", r"eget\s+badev[æa]relse", r"eget\s+toilet", r"privat\s+bad",
    r"privat\s+badev[æa]relse", r"privat\s+toilet", r"badev[æa]relse\s+med\s+bruser",
    r"eget\s+brusebadev[æa]relse", r"eget\s+toilet\s+og\s+bad", r"eget\s+bad\s+og\s+toilet",
    r"stort\s+badev[æa]relse", r"flisebadev[æa]relse", r"nyere\s+badev[æa]relse",
    r"badev[æa]relse",
]

TIME_LIMITED_PATTERNS = [
    r"\btidsbegr[æa]nset\b",
    r"\bfremleje\b",
    r"\bfremlejes\b",
    r"\bkorttidsleje\b",
    r"\bshort[\s-]term\b",
    r"\btemporary\b",
    r"\bto\s+rent\s+for\s+\d+\s+months?\b",
    r"\bi\s+\d+\s+m[aå]neder\b",
    r"\b1-12\s+m[aå]neder\b",
    r"\b1-24\s+m[aå]neder\b",
    r"\b2\+\s*[aå]r\b",
    r"\b\d+\s+mdr\s+fremleje\b",
    r"\bbegr[æa]nset\s+lejeperiode\b",
    # v3.12 (spec 11) section 6.7: lease wording the old list missed, scoped so
    # a tenant age requirement ("under 30 år") never reads as a lease. "1 år og
    # mere" is deliberately NOT here: a 1+ year term is effectively long-term.
    r"\b(?:lejeperiode|rental\s+period|udlejes|udlejning|lejes|fremlejes)\w*\b[^.!?]{0,40}\bunder\s+\d{1,2}\s*[åa]r\b",
    r"\budlejes\s+i\s+\d{1,2}\s*[åa]r\b",
    r"\budlejes\s+(?:tidsbegr[æa]nset\s+)?\d{1,2}\s*[-–]\s*\d{1,2}\s*[åa]r\b",
    r"\b(?:lejeperiode|udlejning|udlejes)\w*\b[^.!?]{0,30}\bop\s+til\s+\d{1,2}\s*[åa]r\b",
    r"\bledig\s+i\s+\d+(?:[.,]\d+)?\s*m[aå]neder\b",
    r"\btidsbegr[æa]nset\s+\d{1,2}\s*[-–]\s*\d{1,2}\s*[åa]r\b",
]

UNLIMITED_PATTERNS = [
    r"\bubegr[æa]nset\b",
    r"\btidsubegr[æa]nset\b",
    r"\buden\s+tidsbegr[æa]nsning\b",
    r"\bunlimited\b",
]

# Scam signal patterns
SCAM_UPFRONT_PAY = [
    r"western\s+union", r"moneygram",
    r"overf[øo]r\s+depositum\s+f[øo]r\s+fremvisning",
    r"betal\s+f[øo]r\s+fremvisning",
    r"pay\s+before\s+viewing",
    r"send\s+money\s+before",
    r"deposit\s+before\s+seeing",
]

SCAM_EXTERNAL_CHAT = [
    r"kun\s+p[åa]\s+whatsapp",
    r"contact\s+me\s+on\s+whatsapp",
    r"kun\s+whatsapp",
    r"only\s+whatsapp",
    r"kontakt\s+mig\s+direkte\s+p[åa]\s+whatsapp",
]

SCAM_ABROAD_KEYS = [
    r"er\s+i\s+udlandet",
    r"currently\s+abroad",
    r"living\s+abroad",
    r"n[øo]gler\s+sendes",
    r"keys\s+will\s+be\s+posted",
    r"send\s+the\s+key",
]


@dataclasses.dataclass
class Flag:
    """A soft, non-rejecting fact about a listing (spec 10 section 5.3).

    ``severity`` is one of ``info``, ``check`` or ``over-limit``. A ``check`` or
    ``over-limit`` flag adds a line to the alert; every flag is badged on the
    card, table and compare view.
    """
    code: str
    label: str
    severity: str = "info"
    evidence: Optional[str] = None


def make_flag(code: str, label: str, severity: str = "info",
              evidence: Optional[str] = None) -> Flag:
    return Flag(code=code, label=label, severity=severity, evidence=evidence)


@dataclasses.dataclass
class EvaluationResult:
    passed: bool
    quarantined: bool
    quarantine_reasons: List[str]
    rejected: bool
    rejection_reason: Optional[str]
    category: Optional[int]
    category_name: Optional[str]
    score: float
    score_breakdown: Dict[str, float]
    risk_state: str  # 'low', 'review', 'high'
    risk_evidence: List[str]
    confidence_points: int
    confidence_details: List[str]
    city_distance_m: Optional[float]
    metro_walk_m: Optional[float]
    straight_dist_m: Optional[float]
    nearest_station: Optional[str]
    walk_min: Optional[int]
    transit_station: Optional[str]
    transit_walk_m: Optional[float]
    is_target_window: bool
    available_date_str: Optional[str]
    rent_total: Optional[int]
    move_in_cost: Optional[int]
    move_in_cost_verified: bool
    lease_verified: bool
    kitchen_verified: bool
    bathroom_verified: bool
    rooms_verified: bool
    # --- v3.0 ---
    tier: Optional[str] = None
    budget_band: str = "within"
    commute_min: Optional[int] = None
    commute_route: Optional[Dict[str, Any]] = None
    size_m2: Optional[float] = None
    kr_per_m2: Optional[float] = None
    value_ratio: Optional[float] = None
    value_label: Optional[str] = None
    benchmark_median: Optional[float] = None
    benchmark_segment: Optional[str] = None
    buzz_score: Optional[float] = None
    buzz_detail: Optional[str] = None
    major_street: Optional[str] = None
    bus_routes: Optional[int] = None
    water_dist_m: Optional[float] = None
    water_name: Optional[str] = None
    water_kind: Optional[str] = None
    relevance: str = "intel"
    geo_precision: Optional[str] = None
    features_pending: int = 0
    verified_badge: Optional[str] = None
    # --- v3.3 ---
    norreport_distance_m: Optional[float] = None
    norreport_road_distance_m: Optional[float] = None
    norreport_road_provider: Optional[str] = None
    norreport_road_status: Optional[str] = None
    is_responsible_pick: bool = False
    used_transit_walk_m: Optional[float] = None
    # --- v3.5 (spec 06 section 5.5): three separate kitchen signals, never one
    # combined "shared" boolean covering kitchen, bathroom and housing.
    private_kitchen_present: Optional[bool] = None
    shared_kitchen_present: Optional[bool] = None
    shared_kitchen_is_the_only_kitchen: Optional[bool] = None
    kitchen_state: Optional[str] = None          # verified|assumed|rejected|unverified
    kitchen_evidence_source: Optional[str] = None
    kitchen_evidence_excerpt: Optional[str] = None
    kitchen_caveat: Optional[str] = None
    bathroom_state: Optional[str] = None
    detail_state: Optional[str] = None
    card_class: Optional[str] = None
    # --- v3.6 (spec 07 sections 5.1, 5.3): the composed trust verdict, attached
    # by `evaluate_listing` on every path, eligible included. `trust_reason` is
    # the single primary reason and is None when the outcome is `eligible`
    # (section 4.4's own wording; v36c3 decision 5). `trust_decision` is the
    # composed object itself, so a writer can read the three components'
    # `state`, `source`, `excerpt`, `landlord_name` and `profile_id` without
    # classifying a second time (spec 07 section 5.1).
    unit_type_state: Optional[str] = None
    tenancy_channel_state: Optional[str] = None
    building_form_state: Optional[str] = None
    trust_outcome: Optional[str] = None
    trust_reason: Optional[str] = None
    trust_decision: Optional["TrustDecision"] = None
    # --- v3.8 (spec 09 section 6.2) ---
    student_requirement_state: Optional[str] = None
    age_requirement_state: Optional[str] = None
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    profile_age_fit: Optional[str] = None
    eligibility_fact_excerpt: Optional[str] = None
    # --- v3.11 (spec 10 section 5.3) ---
    soft_flags: List["Flag"] = dataclasses.field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _piecewise(x: Optional[float], anchors) -> float:
    """Piecewise-linear interpolation over (xv, yv) anchors, flat outside.

    x below the first anchor -> first y; x at or beyond the last anchor -> the
    last anchor's y (so an RP S-train at exactly 800 m still earns 18 pts).
    Anchors are the frozen v3.3 decisions, so the acceptance numbers fall out
    of the anchors directly.
    """
    if x is None:
        return 0.0
    pts = sorted((float(xv), float(yv)) for xv, yv in anchors)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= x <= x1:
            frac = (x - x0) / (x1 - x0) if x1 != x0 else 1.0
            return y0 + frac * (y1 - y0)
    return 0.0


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").lower().split())


def clamp(x: float) -> float:
    return min(1.0, max(0.0, float(x)))


def parse_available_date(text: Optional[str]) -> Tuple[Optional[datetime.date], Optional[str]]:
    """Parse availability string into a datetime.date object and ISO string."""
    if not text:
        return None, None
    t = text.strip().lower()
    if t in ("snarest", "straks", "nu", "omgående", "omgaaende", "asap"):
        today = datetime.date.today()
        return today, "snarest"
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m-%Y", "%d-%m-%y", "%d/%m-%y"):
        try:
            d = datetime.datetime.strptime(re.sub(r"\D", "-", t)[:10], fmt).date()
            return d, d.isoformat()
        except Exception:
            continue
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
    if m:
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            d = datetime.date(yyyy, mm, dd)
            return d, d.isoformat()
        except Exception:
            pass
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2})\b", text)
    if m:
        dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yyyy = 2000 + yy
        try:
            d = datetime.date(yyyy, mm, dd)
            return d, d.isoformat()
        except Exception:
            pass
    # Danish long form, as lejebolig.dk writes it: "7. januar 2027".
    m = re.search(r"(\d{1,2})\.?\s+([a-zæøå]+)\.?\s+(\d{4})", t)
    if m and m.group(2)[:3] in _DANISH_MONTHS:
        try:
            d = datetime.date(int(m.group(3)), _DANISH_MONTHS[m.group(2)[:3]], int(m.group(1)))
            return d, d.isoformat()
        except Exception:
            pass
    return None, None


_DANISH_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
                  "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12}


# ==============================================================================
# Kitchen signal model (spec 06 section 8)
# ==============================================================================
# The hard requirement is a PRIVATE kitchen inside the advertised unit. An extra
# shared or common kitchen elsewhere in the building is acceptable and must not
# reject the home. That needs three independent signals rather than one boolean.
@dataclasses.dataclass
class KitchenEvidence:
    private_kitchen_present: Optional[bool] = None
    shared_kitchen_present: Optional[bool] = None
    shared_kitchen_is_the_only_kitchen: Optional[bool] = None
    kitchen_state: str = "unverified"       # verified | assumed | rejected | unverified
    evidence_source: Optional[str] = None   # detail | card | structured
    evidence_excerpt: Optional[str] = None
    decision: str = "quarantine"            # pass | reject | quarantine
    reason: Optional[str] = None
    caveat: Optional[str] = None


def _excerpt(text: str, match: Optional[re.Match], limit: int = 200) -> Optional[str]:
    """Up to 200 characters of the phrase's own sentence, from the same snapshot."""
    if not text:
        return None
    if match is None:
        return text[:limit].strip() or None
    start = max(0, match.start() - 80)
    return " ".join(text[start:match.start() + limit - 80].split())[:limit] or None


def _first_match(patterns, text: str) -> Optional[re.Match]:
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            return found
    return None


def _private_kitchen_match(text: str) -> Optional[re.Match]:
    for phrase in OWN_KITCHEN_POS:
        found = re.search(re.escape(phrase), text)
        if found:
            return found
    return _first_match(OWN_KITCHEN_REGEX, text)


def evidence_texts(rec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Evidence for THIS listing, best source first (spec 06 section 8.4).

    The detail snapshot outranks the card snapshot, and structured hydration
    from the same page counts as detail-grade. A sibling advert, a generic
    building description or search-card boilerplate from another listing never
    appears here, so it cannot rescue the current one.
    """
    out: List[Tuple[str, str]] = []
    state = str(rec.get("eligibility_evidence_state") or "").lower()
    detail_text = rec.get("eligibility_text")
    if detail_text and state == "verified":
        out.append(("detail", own_listing_text(detail_text)))
    hydration = rec.get("detail_hydration")
    if hydration:
        try:
            out.append(("structured", _norm(json.dumps(hydration, ensure_ascii=False))))
        except Exception:
            pass
    card_text = rec.get("card_text") or rec.get("description")
    if card_text:
        out.append(("card", _norm(card_text)))
    return out


def apartment_fallback_allowed(rec: Dict[str, Any]) -> bool:
    """Whether spec 8.3's apartment fallback may be used for this listing.

    Never for a card classified as a room, student or senior home, and never
    for a listing whose type we do not actually know: an assumption is only
    reasonable where the product itself is a self-contained flat.
    """
    if (rec.get("card_class") or "") in ("room", "student", "senior"):
        return False
    listing_type = (rec.get("listing_type") or "").lower()
    if listing_type in ("lejlighed", "apartment", "studio", "1-værelses", "2-værelses",
                        "rental_apartment"):
        return True
    return (rec.get("card_class") or "") == "apartment"


def analyse_kitchen(rec: Dict[str, Any], rooms_verified: bool = False) -> KitchenEvidence:
    """Decide the kitchen gate from three separate signals.

    | private | shared | only shared | decision  | state      |
    |---------|--------|-------------|-----------|------------|
    | true    | false  | any         | pass      | verified   |
    | true    | true   | false/unk   | pass +    | verified   |
    |         |        |             | caveat    |            |
    | true    | true   | true        | quarantine| unverified |
    | false   | true   | any         | reject    | rejected   |
    | unknown | true   | unknown     | quarantine| unverified |
    | unknown | unknown| unknown     | fallback  | assumed /  |
    |         |        |             |           | unverified |
    """
    for source, text in evidence_texts(rec):
        private_hit = _private_kitchen_match(text)
        shared_hit = _unit_shared_kitchen_match(text)
        shared_any_hit = _unit_shared_kitchen_match_any(text)
        only_hit = _first_match(ONLY_SHARED_KITCHEN_PATTERNS, text)
        if only_hit is None and private_hit is None:
            only_hit = _first_match(ONLY_SHARED_KITCHEN_WEAK_PATTERNS, text)
        if not (private_hit or shared_any_hit or only_hit):
            continue        # this source is silent; fall through to the next

        private = True if private_hit else (False if only_hit else None)
        shared = True if (shared_any_hit or only_hit) else None
        only = True if only_hit else (False if (private_hit and shared_hit) else None)
        hit = private_hit or only_hit or shared_any_hit

        if private is True and only is True:
            # The page both offers a private kitchen and restricts the tenancy
            # to a shared one. That is a contradiction, not a verdict. v3.11:
            # rank with the Kitchen-unconfirmed badge rather than holding.
            return KitchenEvidence(
                private_kitchen_present=True, shared_kitchen_present=True,
                shared_kitchen_is_the_only_kitchen=True, kitchen_state="unverified",
                evidence_source=source, evidence_excerpt=_excerpt(text, hit),
                decision="pass",
                reason="contradictory kitchen evidence (private and shared-only)")
        if private is True:
            return KitchenEvidence(
                private_kitchen_present=True,
                shared_kitchen_present=shared, shared_kitchen_is_the_only_kitchen=only,
                kitchen_state="verified", evidence_source=source,
                evidence_excerpt=_excerpt(text, private_hit), decision="pass",
                caveat=("extra shared kitchen in the building" if shared else None))
        if shared is True and private is False:
            return KitchenEvidence(
                private_kitchen_present=False, shared_kitchen_present=True,
                shared_kitchen_is_the_only_kitchen=True, kitchen_state="rejected",
                evidence_source=source, evidence_excerpt=_excerpt(text, hit),
                decision="reject", reason="shared kitchen")
        # A shared kitchen is mentioned and there is no private-kitchen signal
        # at all (the amenity-list carve-out only applies when the unit has its
        # own kitchen). The ambiguity is a hard stop, not a silent badge: v3.12
        # (spec 11 section 8) holds the listing as unverified. Dortheavej
        # 1639166/5829943 "Fælleskøkken fordelt på etagerne" is this case.
        return KitchenEvidence(
            private_kitchen_present=None, shared_kitchen_present=True,
            shared_kitchen_is_the_only_kitchen=None, kitchen_state="quarantined",
            evidence_source=source, evidence_excerpt=_excerpt(text, hit),
            decision="quarantine",
            reason="shared kitchen mentioned with no private-kitchen evidence")

    # No kitchen wording anywhere in this listing's own evidence. A self-contained
    # 1-2 room unit is assumed to have both (spec 10 section 5.2).
    if rooms_verified and (rec.get("card_class") or "") not in ("room", "student", "senior"):
        return KitchenEvidence(
            private_kitchen_present=None, shared_kitchen_present=None,
            shared_kitchen_is_the_only_kitchen=None, kitchen_state="assumed",
            evidence_source=None, evidence_excerpt=None, decision="pass",
            caveat="kitchen: assumed private (self-contained 1-2 room unit)")
    return KitchenEvidence(kitchen_state="unverified", decision="pass",
                           reason="unverified kitchen")


def analyse_bathroom(rec: Dict[str, Any], rooms_verified: bool = False) -> Tuple[str, str, Optional[str]]:
    """Bathroom gate as its own signal. Returns (state, decision, excerpt).

    v3.11 (spec 10 section 5.2): only proof that the shared bathroom is the only
    one rejects. A missing verdict ranks with the Bathroom-unconfirmed badge; a
    self-contained 1-2 room unit is assumed to have its own.
    """
    for source, text in evidence_texts(rec):
        shared_hit = _first_match(SHARED_BATHROOM_NEG, text)
        if shared_hit:
            return "rejected", "reject", _excerpt(text, shared_hit)
        own = (_first_match([re.escape(p) for p in OWN_BATHROOM_POS], text)
               or _first_match(OWN_BATHROOM_REGEX, text))
        if own:
            return "verified", "pass", _excerpt(text, own)
    if rooms_verified and (rec.get("card_class") or "") not in ("room", "student", "senior"):
        return "assumed", "pass", None
    return "unverified", "pass", None


# ==============================================================================
# Spec 07 section 4 — trust evidence model
# ==============================================================================
@dataclasses.dataclass
class EvidenceDecision:
    state: Optional[str] = None        # the classified state (unit_type/tenancy/building)
    decision: str = "review"           # pass | reject | review
    reason: Optional[str] = None
    source: Optional[str] = None       # structured | detail | card | manual | operator_page
    excerpt: Optional[str] = None      # <= 200 characters
    # Spec 07 section 5.1 columns only the decision itself can establish, so a
    # caller can persist them: the source-scoped advertiser name a
    # professional_direct tenancy verdict rests on, and the reviewed profile a
    # building-form verdict read. Both stay None when the verdict rests on
    # nothing.
    landlord_name: Optional[str] = None
    profile_id: Optional[int] = None

    def summary(self) -> str:
        return f"{self.state} ({self.decision}) {self.reason or ''}"


# Card/title phrases that mark a listing as a rented ROOM, not a flat. The values
# live in config.py (spec 07 section 6.3 rule inputs: the phrase lists and
# thresholds a classification change has to move, so a wording change forces a
# new local evaluation). Deliberately avoids "1 værelse", "ét værelse" and
# "1-værelses lejlighed" which describe the room COUNT of a legitimate studio
# (spec 07 section 3.1, Jorisvej counterexample).
_ROOM_CARD_MARKERS = config.UNIT_ROOM_CARD_MARKERS
_SIZE_MARKED_ROOM_RE = re.compile(config.UNIT_SIZE_MARKED_ROOM_RE)
_APARTMENT_CARD_RE = re.compile(config.UNIT_APARTMENT_CARD_RE, re.IGNORECASE)
_ROOM_TITLE_MARKERS = [re.compile(p, re.IGNORECASE)
                       for p in config.UNIT_ROOM_TITLE_MARKERS]


def _room_meta_signal(rec: Dict[str, Any]) -> Optional[EvidenceDecision]:
    """v3.12 (spec 11) section 4.1: a decisive room signal in the product
    metadata - title, card text, URL and address - beats the portal's structured
    category and the detail 'Boligtype Lejlighed' label. The auditor's example is
    Ebertsgade (1915148), a "klubværelse til 1 person" the portal filed as
    'Lejlighed' and ranked #1. The minimum-area sanity rejects a sub-15 m2 card
    whose metadata names a room, whatever the portal's label says. The full
    description body is deliberately NOT read here: boilerplate about nearby
    student life or "værelse i 2 plan" must not decide the unit type.
    """
    meta = _norm(f"{rec.get('title', '')} {rec.get('card_text', '')} "
                 f"{rec.get('url', '')} {rec.get('address', '') or ''}")
    if not meta:
        return None
    low = meta.lower()
    for pattern in _ROOM_TITLE_MARKERS:
        m = pattern.search(low)
        if m:
            start = max(0, m.start() - 40)
            excerpt = meta[start:m.end() + 40][:200] or meta[:200]
            return EvidenceDecision(state="room_rental", decision="reject",
                                    reason="room rental / not self-contained",
                                    source="card", excerpt=excerpt)
    size = rec.get("size_m2")
    if (isinstance(size, (int, float)) and not isinstance(size, bool)
            and size < config.UNIT_ROOM_MIN_AREA_M2):
        if (_SIZE_MARKED_ROOM_RE.search(low)
                or any(marker in low for marker in _ROOM_CARD_MARKERS)):
            return EvidenceDecision(state="room_rental", decision="reject",
                                    reason="room rental / not self-contained",
                                    source="card", excerpt=meta[:200])
    return None


def _detail_boligtype(rec: Dict[str, Any]) -> Optional[str]:
    """The authoritative 'Boligtype' value from the canonical detail record."""
    evidence = rec.get("unit_type_evidence_excerpt")
    if evidence and evidence.lower().startswith("boligtype"):
        val = evidence.split(None, 1)[-1].strip().lower()
        if val:
            return val
    detail = rec.get("eligibility_text") or rec.get("description") or ""
    m = re.search(r"Boligtype\s*:?\s*([A-Za-zÆØÅæøå]+)", detail)
    if m:
        return m.group(1).strip().lower()
    return None


def classify_unit_type(rec: Dict[str, Any]) -> EvidenceDecision:
    """Self-contained apartment vs room rental vs house (spec 07 section 4.1).

    Precedence (highest first):
      1. structured portal category from the canonical detail record;
      2. the detail-page fact label, e.g. 'Boligtype Værelse';
      3. an unambiguous product title/card phrase;
      4. existing source category metadata.

    An authoritative room_rental result is never overridden by rooms=1, an
    unlimited period, or kitchen/bathroom fallback assumptions.
    """
    # Precedence 0 (v3.12, spec 11 section 4.1): a decisive room signal in the
    # product metadata overrides the stored portal category and the detail
    # 'Boligtype Lejlighed' label (Ebertsgade 1915148, a klubværelse filed as
    # 'Lejlighed'). Also rejects a sub-15 m2 card whose metadata names a room.
    room_signal = _room_meta_signal(rec)
    if room_signal:
        return room_signal

    # Precedence 1: structured state already extracted from a canonical detail.
    stored_state = rec.get("unit_type_state")
    if stored_state in ("self_contained_apartment", "room_rental", "house_or_other"):
        return EvidenceDecision(
            state=stored_state,
            decision=("reject" if stored_state in ("room_rental", "house_or_other")
                      else "pass"),
            reason=("room rental / not self-contained"
                    if stored_state in ("room_rental", "house_or_other")
                    else "self-contained apartment"),
            source=rec.get("unit_type_evidence_source") or "structured",
            excerpt=rec.get("unit_type_evidence_excerpt"))

    # Precedence 2: authoritative Boligtype fact label from the detail page.
    btype = _detail_boligtype(rec)
    if btype:
        if btype in ("værelse", "vaerelse"):
            return EvidenceDecision(state="room_rental", decision="reject",
                                    reason="room rental / not self-contained",
                                    source="detail", excerpt=f"Boligtype {btype}")
        if btype in ("lejlighed", "lejligheder", "lejemål"):
            return EvidenceDecision(state="self_contained_apartment", decision="pass",
                                    reason="self-contained apartment",
                                    source="detail", excerpt=f"Boligtype {btype}")
        if btype in ("hus", "villa", "rækkehus", "raekkehus", "landejendom"):
            return EvidenceDecision(state="house_or_other", decision="reject",
                                    reason="house / other (not an apartment)",
                                    source="detail", excerpt=f"Boligtype {btype}")

    # Precedence 3: unambiguous product title/card phrase.
    card_blob = _norm(f"{rec.get('title', '')} {rec.get('card_text', '')} "
                      f"{rec.get('description', '') or ''} {rec.get('url', '')}")
    _size_m = _SIZE_MARKED_ROOM_RE.search(card_blob)
    if _size_m:
        return EvidenceDecision(state="room_rental", decision="reject",
                                reason="room rental / not self-contained",
                                source="card", excerpt=_size_m.group(0)[:200])
    # The guarded room markers from precedence 0 also apply here: '1 værelse på
    # 33 m2' is a room COUNT of a real studio, never a rented bedroom (the
    # (?<!\d ) guard blocks it); 'room with shared' has no regex twin and is
    # kept as a literal. v3.12 (spec 11) section 4.1.
    low = card_blob.lower()
    if any(m.search(low) for m in _ROOM_TITLE_MARKERS) or "room with shared" in low:
        return EvidenceDecision(state="room_rental", decision="reject",
                                reason="room rental / not self-contained",
                                source="card", excerpt=card_blob[:200])
    if _APARTMENT_CARD_RE.search(card_blob):
        return EvidenceDecision(state="self_contained_apartment", decision="pass",
                                reason="self-contained apartment",
                                source="card", excerpt=card_blob[:200])

    # Precedence 4: existing source category metadata.
    cc = (rec.get("card_class") or "").lower()
    lt = (rec.get("listing_type") or "").lower()
    if cc == "room" or lt in ("værelse", "vaerelse", "room", "værelser"):
        return EvidenceDecision(state="room_rental", decision="reject",
                                reason="room rental / not self-contained",
                                source="card", excerpt=str(rec.get("card_text") or "")[:200])
    if cc == "apartment" or lt in ("lejlighed", "apartment", "studio"):
        return EvidenceDecision(state="self_contained_apartment", decision="pass",
                                reason="self-contained apartment",
                                source="card", excerpt=str(rec.get("card_text") or "")[:200])
    if lt in ("hus", "villa", "rækkehus", "raekkehus", "delebolig", "kollegie"):
        return EvidenceDecision(state="house_or_other", decision="reject",
                                reason="house / other (not an apartment)",
                                source="card")

    return EvidenceDecision(state="unknown", decision="review",
                            reason="self-contained apartment unverified")


# --- Spec 07 section 4.2 — tenancy channel / landlord evidence ----------------
# The advert's OWN text only. The portal's own identity, footer and CVR are not
# evidence about the advertiser (section 4.2 bullet 2, case 9.3.7), so a stored
# landlord fact carrying any of them is treated as absent, never as professional.
# The token list, the sublet/private patterns and the evidence-source vocabulary
# are section 6.3 rule inputs and live in config.py.
_PORTAL_IDENTITY_TOKENS = config.TENANCY_PORTAL_IDENTITY_TOKENS

# Spec 07 section 4.2 bullet 1, the spec's literal token list. Sublet is the
# first precedence step, so it wins over every other channel and reports its own
# reason even when the period is unlimited (case 9.3.4) instead of being
# reported merely as "time-limited". The non-sublet occurrences of "fremleje"
# seen in production ("gæsteværelser til fremleje", "må ikke fremlejes") get no
# negation carve-out here: that is a reported observation for a later row.
SUBLET_PATTERNS = config.TENANCY_SUBLET_PATTERNS

# Spec 07 section 4.2 bullet 4: unambiguous first-person private rental wording.
# Production-confirmed examples: "Jeg udlejer min lejlighed", "I min lejlighed på
# Nørrebro udlejer jeg et møbleret værelse", "udlejer derfor to tilstødende
# værelser i min lejlighed" (lejebolig) and "I'm subletting my apartment"
# (boligportal). No structured private-advertiser flag could be verified in
# stored data - source_record.is_owner is false on all 471 stored boligportal
# rows - so this wording route is the implementable one, and no metadata is
# invented for it.
PRIVATE_LANDLORD_PATTERNS = config.TENANCY_PRIVATE_LANDLORD_PATTERNS

# Spec 07 section 5.1 vocabulary for landlord_evidence_source.
_LANDLORD_EVIDENCE_SOURCES = config.TENANCY_LANDLORD_EVIDENCE_SOURCES

# Spec 08 section 4.1 route 3c: the portfolio size at which the advert's own
# BoligPortal landlord block becomes positive management evidence (badge only).
PORTFOLIO_MIN_RENTABLES = config.TENANCY_PORTFOLIO_MIN_RENTABLES

# Spec 08 section 4.1 route 3d: the portals that host professional operators and
# housing associations only, so every listing on them has that evidence.
PROFESSIONAL_PORTALS = config.TENANCY_PROFESSIONAL_PORTALS


def _own_ad_texts(rec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """This advert's own text, best source first (detail page, then card).

    Deliberately not ``evidence_texts``: that helper also returns the whole
    ``detail_hydration`` JSON, which embeds other listings' cards
    (``page_props.results`` / ``other_results`` / ``similar_ads``). Spec 07
    section 4.2 makes the tenancy verdict source-scoped, so only this advert's
    own fields are read here.
    """
    out: List[Tuple[str, str]] = []
    detail_text = rec.get("eligibility_text")
    if detail_text and str(rec.get("eligibility_evidence_state") or "").lower() == "verified":
        own = own_listing_text(detail_text)
        if own:
            out.append(("detail", own))
    head = _norm(" ".join(str(rec.get(key) or "") for key in
                          ("title", "description", "card_text", "rental_period")))
    if head:
        out.append(("card", head))
    return out


def _matches_portal_identity(*values: Optional[str]) -> bool:
    """True when a stored landlord fact is really the portal's own chrome."""
    for value in values:
        blob = _norm(value)
        if blob and any(token in blob for token in _PORTAL_IDENTITY_TOKENS):
            return True
    return False


def _bound_excerpt(value: Any, limit: int = 200) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


def _own_newbuild_block(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The advert's own project/company block, or {} when there is none.

    Reads only ``source_record.newbuild_details`` (section 4.2 bullet 3:
    source-scoped structured data identifying a company/professional
    advertiser). The same block is repeated at
    ``detail_hydration.props.page_props.serialized_new_build`` and is accepted by
    ``_own_serialized_new_build`` only when its ``ad_id`` equals
    ``source_record.id``, i.e. it belongs to this advert.
    """
    source_record = rec.get("source_record")
    if not isinstance(source_record, dict):
        return {}
    details = source_record.get("newbuild_details")
    if isinstance(details, dict) and source_record.get("is_newbuild") is True:
        return details
    return {}


def _own_serialized_new_build(rec: Dict[str, Any]) -> Dict[str, Any]:
    """This advert's own copy of the project block, matched by ``ad_id``."""
    source_record = rec.get("source_record")
    if not isinstance(source_record, dict):
        return {}
    ad_id = source_record.get("id")
    hydration = rec.get("detail_hydration")
    if not isinstance(hydration, dict):
        return {}
    page_props = (hydration.get("props") or {}).get("page_props") or {}
    if not isinstance(page_props, dict):
        return {}
    block = page_props.get("serialized_new_build")
    if isinstance(block, dict) and ad_id is not None and block.get("ad_id") == ad_id:
        return block
    return {}


def _own_landlord_information(rec: Dict[str, Any]) -> Dict[str, Any]:
    """This advert's own BoligPortal landlord block, matched by ``ad.id``.

    Section 4.1 route 3c: the block is read from this advert's own
    ``detail_hydration`` and is accepted only when ``page_props.ad.id`` equals
    ``source_record.id``, mirroring :func:`_own_serialized_new_build`. The
    ``other_results`` / ``similar_ads`` cards and the portal footer on the same
    page are never this advert's evidence.
    """
    source_record = rec.get("source_record")
    if not isinstance(source_record, dict):
        return {}
    ad_id = source_record.get("id")
    hydration = rec.get("detail_hydration")
    if not isinstance(hydration, dict):
        return {}
    page_props = (hydration.get("props") or {}).get("page_props") or {}
    if not isinstance(page_props, dict):
        return {}
    block = page_props.get("landlord_information")
    ad = page_props.get("ad")
    if not isinstance(block, dict) or not isinstance(ad, dict):
        return {}
    if ad_id is not None and ad.get("id") == ad_id:
        return block
    return {}


def classify_tenancy_channel(rec: Dict[str, Any]) -> EvidenceDecision:
    """Professional direct lease vs private landlord vs sublet (spec 07 section 4.2, spec 08 section 4.1).

    Pure and record-only: no store, no URL fetch (section 6.3 pins this
    signature). Precedence, first match wins:

      1. explicit sublet wording in the advert's own text -> ``sublet``, reject,
         reason ``sublet``;
      2. unambiguous first-person private rental wording -> ``private_landlord``,
         reject;
      3. a) a project/company advertiser on the advert's own canonical record,
         else b) an already-stored source-scoped landlord fact, else c) the
         advert's own BoligPortal ``landlord_information`` with a portfolio of
         at least ``config.TENANCY_PORTFOLIO_MIN_RENTABLES`` rentables, else
         d) the advert's site being one of
         ``config.TENANCY_PROFESSIONAL_PORTALS`` -> ``professional_direct``,
         pass;
      4. otherwise ``unknown`` with ``decision="pass"``: an unidentified
         advertiser is no longer a reason to hold a listing (spec 08 section 4.1
         step 4), and the default is *not private*.

    Never professional evidence for *naming* a landlord: the portal's own
    identity, footer, CVR or ``Kontakt udlejer`` text; advertisement counts or
    portfolio size other than route 3c's own source-scoped block; several cards
    at one address; an unlimited lease. Routes 3c and 3d are badge evidence and
    carry no landlord name (sections 4.1 and 12).
    """
    # 1. Explicit sublet, ahead of every other channel and of any generic
    #    time-limited handling (section 4.2 bullet 1, case 9.3.4).
    for source, text in _own_ad_texts(rec):
        hit = _first_match(SUBLET_PATTERNS, text)
        if hit:
            return EvidenceDecision(state="sublet", decision="reject",
                                    reason="sublet", source=source,
                                    excerpt=_excerpt(text, hit))

    # 2. Explicit first-person private rental wording (section 4.2 bullet 4).
    for source, text in _own_ad_texts(rec):
        hit = _first_match(PRIVATE_LANDLORD_PATTERNS, text)
        if hit:
            return EvidenceDecision(state="private_landlord", decision="reject",
                                    reason="private landlord", source=source,
                                    excerpt=_excerpt(text, hit))

    # 3a. Project/company advertiser on the advert's own canonical record.
    block = _own_newbuild_block(rec)
    company = str(block.get("company_name") or "").strip()
    if company:
        name = str(block.get("newbuild_name") or "").strip()
        if not name:
            name = str(_own_serialized_new_build(rec).get("newbuild_name") or "").strip()
        excerpt = _bound_excerpt(" ".join(part for part in (name, company) if part))
        return EvidenceDecision(state="professional_direct", decision="pass",
                                reason="professional advertiser",
                                source="structured", excerpt=excerpt,
                                landlord_name=company)

    # 3b. An already-stored source-scoped landlord fact (section 5.1). The only
    #     channel by which a manual review or a configured-operator verdict can
    #     reach this pure rec-only function.
    name = str(rec.get("landlord_name") or "").strip()
    source = str(rec.get("landlord_evidence_source") or "").strip().lower()
    excerpt = _bound_excerpt(rec.get("landlord_evidence_excerpt"))
    if name and source in _LANDLORD_EVIDENCE_SOURCES:
        # A landlord fact carrying the portal's own chrome is treated as absent
        # (case 9.3.7), so the record falls through to 3c rather than passing.
        if not _matches_portal_identity(name, excerpt):
            return EvidenceDecision(state="professional_direct", decision="pass",
                                    reason="professional advertiser",
                                    source=source, excerpt=excerpt,
                                    landlord_name=name)

    # 3c. The advert's own BoligPortal landlord block with a real portfolio
    #     (spec 08 section 4.1 route 3c). Badge evidence only: no landlord name.
    information = _own_landlord_information(rec)
    rentables = information.get("num_rentables")
    if isinstance(rentables, int) and not isinstance(rentables, bool) \
            and rentables >= PORTFOLIO_MIN_RENTABLES:
        return EvidenceDecision(
            state="professional_direct", decision="pass",
            reason=f"advertiser portfolio of {rentables} rentables",
            source="structured",
            excerpt=_bound_excerpt(
                f"BoligPortal advertiser portfolio: {rentables} rentables"),
            landlord_name=None)

    # 3d. A portal that lists housing associations and professional operators
    #     only (spec 08 section 4.1 route 3d). No portal qualifies after FindBolig
    #     was retired in v3.11 (spec 10 section 7), so this is now a no-op guard.
    if PROFESSIONAL_PORTALS and str(rec.get("site") or "") in PROFESSIONAL_PORTALS:
        return EvidenceDecision(
            state="professional_direct", decision="pass",
            reason="operator portal", source="structured",
            excerpt=_bound_excerpt("operator portal"), landlord_name=None)

    # 4. No source-scoped advertiser evidence exists for this advert, and that is
    #    a pass: the two evidence gates are reject-only from v3.7 (section 4.1).
    return EvidenceDecision(state="unknown", decision="pass",
                            reason="advertiser not identified")


# --- Spec 07 section 4.3 — building form / managed-block evidence -------------
# Section 4.3's three states, verbatim. A profile or a stored fact carrying any
# other string is not a verdict this classifier may act on. Every value in this
# block is a section 6.3 rule input and lives in config.py.
BUILDING_FORM_STATES = config.TRUST_BUILDING_FORM_STATES

# Section 7.2: professional-management or building evidence older than this many
# days is `verification stale`. It stays in Needs verification until it is
# refreshed or manually reviewed; a local refresh never fetches it.
BUILDING_EVIDENCE_MAX_DAYS = config.BUILDING_EVIDENCE_MAX_DAYS

# Section 4.3 bullet 3: "at least three distinct self-contained source listings
# at the exact building/configured complex in the last 90 days" — and only
# together with the same professional-operator evidence as bullet 2. Section 4.2
# bullet 5, section 9.3 case 8 and section 12 all forbid repeated inventory as
# block or landlord proof on its own.
BUILDING_FORM_MIN_LISTINGS = config.BUILDING_FORM_MIN_LISTINGS

# The reason this classifier states for an unidentified building form. Spec 08
# section 4.2 makes the fall-through `unknown` with `decision="pass"`: a missing
# building verdict is no longer a hold, and the v3.6 wording
# (`config.TRUST_REASON_BLOCK_UNVERIFIED`) is removed from the tree. The
# lifecycle reasons below keep their section 7.2 text and are now badge-side
# notes rather than hold reasons.
BUILDING_UNVERIFIED_REASON = "building form not identified"
BUILDING_STALE_REASON = config.TRUST_REASON_STALE
BUILDING_CONFLICT_REASON = config.TRUST_REASON_CONFLICT

# Section 5.2's `evidence_kind` vocabulary mapped onto EvidenceDecision.source.
# `source_detail` is a parser read of the canonical detail page, i.e. `detail`.
_PROFILE_EVIDENCE_SOURCES = config.BUILDING_PROFILE_EVIDENCE_KINDS

# A definite reviewed verdict: the two states that can stand on their own. A
# stored `unknown` is not a decision to protect — fresh evidence is allowed to
# arrive — and a stored state on its own is never positive evidence.
_DEFINITE_BUILDING_STATES = config.BUILDING_PROFILE_DEFINITE_STATES


def _days_since(timestamp: Optional[str],
                today: Optional[datetime.date] = None) -> Optional[int]:
    """Whole days between an ISO timestamp and today, or None if unparseable."""
    if not timestamp:
        return None
    try:
        seen = datetime.datetime.fromisoformat(str(timestamp)).date()
    except ValueError:
        return None
    return max(0, ((today or datetime.date.today()) - seen).days)


def _profile_source(profile: Optional[Dict[str, Any]]) -> Optional[str]:
    """A reviewed profile's evidence kind in the decision vocabulary."""
    return _PROFILE_EVIDENCE_SOURCES.get(
        str((profile or {}).get("evidence_kind") or "").strip().lower())


def _profile_excerpt(profile: Optional[Dict[str, Any]]) -> Optional[str]:
    """The reviewed excerpt, or the evidence URL, bounded to 200 characters."""
    profile = profile or {}
    return _bound_excerpt(profile.get("evidence_excerpt") or profile.get("evidence_url"))


def _profile_block_decision(profile: Dict[str, Any]) -> EvidenceDecision:
    """Route 1: one active reviewed managed-block profile, checked for evidence.

    Section 4.3's first positive-evidence bullet requires an evidence URL, and
    section 7.2 requires the evidence to be fresh: with no evidence URL, with no
    verification date, or older than the window, the profile is an unidentified
    building form. Spec 08 section 4.2 turns that into a pass - it is a badge
    that is missing, not a hold - while the reason text still names the state.
    """
    source = _profile_source(profile)
    excerpt = _profile_excerpt(profile)
    if not str(profile.get("evidence_url") or "").strip():
        return EvidenceDecision(state="unknown", decision="pass",
                                reason="building profile evidence URL missing",
                                source=source, excerpt=excerpt,
                                profile_id=profile.get("id"))
    age = _days_since(profile.get("verified_at"))
    if age is None:
        return EvidenceDecision(state="unknown", decision="pass",
                                reason="verification date unknown",
                                source=source, excerpt=excerpt,
                                profile_id=profile.get("id"))
    if age > BUILDING_EVIDENCE_MAX_DAYS:
        return EvidenceDecision(state="unknown", decision="pass",
                                reason=BUILDING_STALE_REASON,
                                source=source, excerpt=excerpt,
                                profile_id=profile.get("id"))
    return EvidenceDecision(state="managed_multi_unit_block", decision="pass",
                            reason="professionally managed multi-unit block",
                            source=source, excerpt=excerpt,
                            profile_id=profile.get("id"))


def _with_stored_conflict(rec: Dict[str, Any],
                          decision: EvidenceDecision) -> EvidenceDecision:
    """Section 7.2: contradicting stored evidence names the conflict, never a badge.

    A definite stored manual/parser building fact that disagrees with the
    definite verdict the current evidence supports is a conflict, so the record
    names the conflict instead of the parser silently overwriting a manual
    decision. A stored `unknown` is not a contradiction (fresh evidence is
    allowed to arrive), and a stored fact alone never creates a verdict. Spec 08
    section 4.2 keeps the state and the reason text and makes the verdict a pass:
    the conflict is a badge-side note (section 7.2), not a hold.
    """
    stored = str(rec.get("building_form_state") or "").strip()
    if (stored in _DEFINITE_BUILDING_STATES
            and decision.state in _DEFINITE_BUILDING_STATES
            and stored != decision.state):
        return EvidenceDecision(
            state="unknown", decision="pass", reason=BUILDING_CONFLICT_REASON,
            source=decision.source,
            excerpt=_bound_excerpt(f"stored {stored} vs evidence {decision.state}"),
            profile_id=decision.profile_id)
    return decision


def classify_building_form(rec: Dict[str, Any], store) -> EvidenceDecision:
    """Managed multi-unit block vs small/private property (spec 07 section 4.3).

    Pure over stored data: it reads the record and the store, and fetches
    nothing (section 6.3). Positive evidence is exactly one of section 4.3's
    routes, tried in this order:

      1. an active reviewed building profile for the exact house-numbered
         address, or for the configured complex key, carrying an evidence URL
         and evidence verified inside section 7.2's window;
      2. the advert's own authoritative operator/project page: its canonical
         `source_record.newbuild_details` block naming a development, a
         professional operator and that operator's page (section 4.2 bullet 3);
      3. section 4.3 bullet 3: the same named development and professional
         operator, plus at least three distinct self-contained source listings
         at the exact building/configured complex inside the 90-day window.

    Inheritance is exact-scope only. An address route needs a house number and
    an exact folded match, so another house number, a street-only property and a
    merely nearby building never inherit the verdict (duty 2, case 9.3.9). A
    bare street-level geocode, a generic word such as `ejendom`, a single portal
    card and repeated inventory without operator evidence are not block evidence
    (section 4.3).

    Everything else is `unknown` with an explicit reason and - from spec 08
    section 4.2 - `decision="pass"`: a superseded profile, evidence older than 90
    days or undated, a profile state outside section 4.3's vocabulary, stored
    evidence that contradicts the verdict, or no evidence at all. The reason
    still names the state; the missing verdict is a missing badge, never a hold.
    `store` may be None, in which case only the store-free route (2) can be
    decided.
    """
    address = rec.get("normalized_address") or rec.get("address")
    complex_key = None
    profiles: List[Dict[str, Any]] = []
    if store is not None and address:
        complex_key = store.resolve_complex_key(address, rec.get("postcode"))
        if store.address_is_one_building(address):
            profiles += list(store.building_profiles_for_exact_address(address) or [])
    if store is not None and complex_key:
        profiles += list(store.building_profiles_for_complex(complex_key) or [])

    active = [p for p in profiles
              if str(p.get("state") or "active").strip().lower() == "active"]
    if active:
        states = {str(p.get("building_form_state") or "").strip() for p in active}
        if len(states) > 1:
            # Two reviewed profiles for one exact scope disagree. Section 7.2:
            # the conflict is named, and v3.7 makes it a pass.
            return _with_stored_conflict(rec, EvidenceDecision(
                state="unknown", decision="pass", reason=BUILDING_CONFLICT_REASON,
                source=_profile_source(active[0]),
                excerpt=_bound_excerpt("profiles disagree: " + " vs ".join(sorted(states))),
                profile_id=active[0].get("id")))
        profile = active[0]
        state = str(profile.get("building_form_state") or "").strip()
        if state == "managed_multi_unit_block":
            return _with_stored_conflict(rec, _profile_block_decision(profile))
        if state == "small_or_private_property":
            return _with_stored_conflict(rec, EvidenceDecision(
                state="small_or_private_property", decision="reject",
                reason="small or private property", source=_profile_source(profile),
                excerpt=_profile_excerpt(profile), profile_id=profile.get("id")))
        if state == "unknown":
            return _with_stored_conflict(rec, EvidenceDecision(
                state="unknown", decision="pass", reason=BUILDING_UNVERIFIED_REASON,
                source=_profile_source(profile), excerpt=_profile_excerpt(profile),
                profile_id=profile.get("id")))
        return _with_stored_conflict(rec, EvidenceDecision(
            state="unknown", decision="pass",
            reason="building profile state unrecognised",
            source=_profile_source(profile), excerpt=_bound_excerpt(state),
            profile_id=profile.get("id")))
    if profiles:
        # Only superseded rows exist for this exact scope: section 7.2 makes a
        # superseded profile unknown immediately, which v3.7 makes a pass.
        return _with_stored_conflict(rec, EvidenceDecision(
            state="unknown", decision="pass", reason="building profile superseded",
            source=_profile_source(profiles[0]), excerpt=_profile_excerpt(profiles[0]),
            profile_id=profiles[0].get("id")))

    # Routes 2 and 3: the advert's own stored operator/project evidence.
    block = _own_newbuild_block(rec)
    company = str(block.get("company_name") or "").strip()
    development = str(block.get("newbuild_name") or "").strip()
    if not development:
        development = str(_own_serialized_new_build(rec).get("newbuild_name") or "").strip()
    link = str(block.get("company_link") or "").strip()
    if company and development and link:
        return _with_stored_conflict(rec, EvidenceDecision(
            state="managed_multi_unit_block", decision="pass",
            reason="professional operator project page", source="structured",
            excerpt=_bound_excerpt(" ".join((development, company, link)))))
    if company and development and store is not None:
        recent = store.self_contained_listings_at_building(
            address=address, complex_key=complex_key,
            days=BUILDING_EVIDENCE_MAX_DAYS)
        if len(recent) >= BUILDING_FORM_MIN_LISTINGS:
            return _with_stored_conflict(rec, EvidenceDecision(
                state="managed_multi_unit_block", decision="pass",
                reason=(f"{BUILDING_FORM_MIN_LISTINGS} self-contained listings "
                        f"recently at one building with operator evidence"),
                source="structured",
                excerpt=_bound_excerpt(" ".join((development, company)))))
    return _with_stored_conflict(rec, EvidenceDecision(
        state="unknown", decision="pass", reason=BUILDING_UNVERIFIED_REASON))


# --- Spec 07 section 4.4 — the final trust decision ---------------------------
@dataclasses.dataclass
class TrustDecision:
    """The composed trust verdict for one source listing (spec 07 §4.4, §6.3).

    Carries section 5.3's ``trust_outcome`` vocabulary — ``eligible`` is section
    4.4's "continue through existing gates", ``review`` is the existing
    ``quarantined`` outcome and ``reject`` the ``rejected`` one — the single
    primary ``reason`` the dashboard states first (sections 4.4 and 7.1), and
    the three independent ``EvidenceDecision``s, so a caller can retain every
    section 5.1/5.3 state and show the remaining reasons under the primary one.

    The component states are exposed under the names of the columns that will
    store them (`unit_type_state`, `tenancy_channel_state`,
    `building_form_state`).
    """

    outcome: str                       # eligible | review | reject
    reason: Optional[str]              # the primary reason, None when eligible
    unit_type: EvidenceDecision
    tenancy_channel: EvidenceDecision
    building_form: EvidenceDecision

    @property
    def unit_type_state(self) -> Optional[str]:
        return self.unit_type.state

    @property
    def tenancy_channel_state(self) -> Optional[str]:
        return self.tenancy_channel.state

    @property
    def building_form_state(self) -> Optional[str]:
        return self.building_form.state

    def components(self) -> Tuple[EvidenceDecision, ...]:
        """The three decisions in section 4.4's precedence order."""
        return (self.unit_type, self.tenancy_channel, self.building_form)

    def summary(self) -> str:
        return f"{self.outcome} ({self.reason or 'eligible'})"


def classify_listing_trust(rec: Dict[str, Any], store) -> TrustDecision:
    """Apply the trust decision table to the three decisions (section 6.3).

    Pure and deterministic from stored data: nothing is fetched and nothing is
    written. ``rec["_unit_decision"]`` is consumed when the caller already ran
    ``classify_unit_type`` (``evaluate_listing`` sets it before the apartment
    kitchen/bathroom fallbacks); otherwise the unit type is classified here from
    the same record, which yields the identical decision. ``store`` is passed to
    ``classify_building_form`` and may be ``None``.

    The rows are applied in table order, so the primary reason is stable (plan
    duty 2):

      1. room / other unit type            -> reject, the unit decision's reason
         (`room rental / not self-contained`, ships as
         ``config.TRUST_REASON_ROOM``, or `house / other (not an apartment)`);
      2. sublet or private tenancy         -> reject, the tenancy decision's
         reason (`sublet` / `private landlord`, section 4.2);
      3. small or private building form    -> reject, the building decision's
         reason (`small or private property`);
      4. self-contained apartment          -> eligible: the table's single
         "continue through existing gates" row. Spec 08 section 4.3 makes the two
         evidence gates reject-only, so tenancy `professional_direct` or
         `unknown` and building `managed_multi_unit_block` or `unknown` all
         continue; a present verdict only earns the badge;
      5. unit type unknown                 -> review
         `self-contained apartment unverified`.

    Rows 1-3 precede row 5, so a lower-precedence unknown can never hide explicit
    rejection evidence (row 5's "unless explicit rejection evidence exists"
    clause, plan duty 2). ``eligible`` is the only outcome that may receive an
    S-D rank and become a match (spec 08 section 4.3).
    """
    unit = rec.get("_unit_decision")
    if not isinstance(unit, EvidenceDecision):
        unit = classify_unit_type(rec)
    tenancy = classify_tenancy_channel(rec)
    building = classify_building_form(rec, store)

    def _decide(outcome: str, reason: Optional[str]) -> TrustDecision:
        return TrustDecision(outcome=outcome, reason=reason, unit_type=unit,
                             tenancy_channel=tenancy, building_form=building)

    # Row 1 — room / other unit type, whatever the tenancy and building say.
    if unit.decision == "reject":
        reason = (config.TRUST_REASON_ROOM if unit.state == "room_rental"
                  else unit.reason)
        return _decide("reject", reason)

    # Row 2 — an apartment that is sublet or has an explicit private landlord.
    if tenancy.decision == "reject":
        return _decide("reject", tenancy.reason)

    # Row 3 (table) — a positively small or private property does not qualify
    # (section 1, section 2.4): the building verdict is a rejection, and v36c2
    # handed this final mapping to v36c3.
    if building.decision == "reject":
        return _decide("reject", building.reason)

    if unit.state == "self_contained_apartment":
        # The table's single "continue through existing gates" row. From v3.7 the
        # two evidence gates are reject-only (spec 08 section 4.3): an absent
        # tenancy or building verdict is no longer a hold, and a present one is a
        # badge rather than a gate.
        return _decide("eligible", None)

    # Row 4 — the self-contained question is itself unanswered.
    return _decide("review", config.TRUST_REASON_UNIT_UNVERIFIED)


# The nine section 5.1 `source_listings` columns, in the order
# `Store.record_source_trust` declares them. `source_trust_values` returns
# exactly these keys, so a write site passes the dict through as keywords.
SOURCE_TRUST_COLUMNS = ("unit_type_state", "unit_type_evidence_source",
                        "unit_type_evidence_excerpt", "tenancy_channel_state",
                        "landlord_name", "landlord_evidence_source",
                        "landlord_evidence_excerpt", "building_form_state",
                        "building_profile_id")


def source_trust_values(result: "EvaluationResult") -> Dict[str, Optional[Any]]:
    """The section 5.1 facts one evaluation established (section 0b).

    Reads the composed :class:`TrustDecision` that ``evaluate_listing`` attaches
    to the result as ``trust_decision``, and nothing else: never the record,
    never the store, never a second call to :func:`classify_listing_trust`. The
    keys are exactly the nine ``Store.record_source_trust`` keywords, so the
    write site passes the returned dict straight through.

    Group rules, each with the spec section that requires it:

    - the unit-type trio only for section 4.1 precedence 1-2 evidence
      (``structured`` or ``detail``): a precedence-3 card phrase is re-derived
      from the record's own stored text on every pass, and persisting it into the
      precedence-1 slot would let it outrank a later authoritative ``Boligtype``
      (sections 3.1-3.2);
    - the tenancy state for any definite section 4.2 state;
    - the landlord evidence pair for any source-scoped ``professional_direct``
      fact whose source is in section 5.1's own vocabulary
      (``structured | detail | manual``) - from spec 08 section 5 the pair is
      written with or without a name, because routes 3c (portfolio) and 3d
      (operator portal) are badge evidence and carry no name - and
      ``landlord_name`` only when the route named one;
    - the building state for any definite section 4.3 state, and
      ``building_profile_id`` whenever a reviewed profile informed the verdict -
      including the profile whose evidence was found stale, which is what
      section 7.2's evidence date needs.

    ``None`` everywhere else means "this pass established nothing new", which is
    how section 5.1's COALESCE is written, so no pass can erase a previous
    definite fact.
    """
    trust = getattr(result, "trust_decision", None)
    if trust is None:
        # `evaluate_listing` always attaches the decision; an absent one states
        # nothing new for any group rather than inventing a value.
        return dict.fromkeys(SOURCE_TRUST_COLUMNS)
    unit, tenancy, building = (trust.unit_type, trust.tenancy_channel,
                              trust.building_form)
    values: Dict[str, Optional[Any]] = {
        "unit_type_state": None,
        "unit_type_evidence_source": None,
        "unit_type_evidence_excerpt": None,
        "tenancy_channel_state": (tenancy.state
                                  if tenancy.state and tenancy.state != "unknown"
                                  else None),
        "landlord_name": None,
        "landlord_evidence_source": None,
        "landlord_evidence_excerpt": None,
        "building_form_state": (building.state
                                if building.state and building.state != "unknown"
                                else None),
        "building_profile_id": building.profile_id,
    }
    if unit.state != "unknown" and unit.source in ("structured", "detail"):
        values["unit_type_state"] = unit.state
        values["unit_type_evidence_source"] = unit.source
        values["unit_type_evidence_excerpt"] = unit.excerpt
    landlord_name = str(tenancy.landlord_name or "").strip()
    if (tenancy.state == "professional_direct"
            and tenancy.source in _LANDLORD_EVIDENCE_SOURCES):
        # Spec 08 section 5: the pair is persisted for every source-scoped
        # professional fact, name or no name - routes 3c/3d carry no name, and
        # the badge reads the evidence, not a landlord identity.
        values["landlord_evidence_source"] = tenancy.source
        values["landlord_evidence_excerpt"] = tenancy.excerpt
        if landlord_name:
            values["landlord_name"] = landlord_name
    return values


# ==============================================================================
# Spec 09 section 6 — demographic eligibility model
# ==============================================================================
@dataclasses.dataclass
class DemographicDecision:
    student_requirement_state: str   # required | not_required | unknown | conflict
    age_requirement_state: str       # bounded | none | unknown | conflict
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    profile_age_fit: str = "unknown" # eligible | ineligible | unknown
    eligibility_fact_excerpt: Optional[str] = None
    outcome: str = "eligible"        # eligible | review | reject
    rejection_reason: Optional[str] = None
    quarantine_reason: Optional[str] = None


_STUDENT_EXPLICIT_REQUIRED_PATTERNS = [
    r"\bforbeholdt\s+(?:\w+\s+){0,3}studerende\b",
    r"\b(?:kun|udelukkende|udelukkede)\s+(?:for|til)\s+studerende\b",
    r"\budlejes\s+(?:kun\s+)?til\s+studerende\b",
    r"\bstudieaktiv(?:e|itet)?\b",
    r"\bstudiedokumentation\b",
    r"\bindskrevet\s+(?:som\s+)?studerende\b",
    r"\bkrav\s+om\s+studie\w*\b",
    r"\bmust\s+be\s+a?\s*student\b",
    r"\bstudents\s+only\b",
    r"\bonly\s+for\s+students\b",
    r"\bfor\s+students\s+only\b",
    r"\bstudent[- ]only\b",
    r"\bstudiebolig\w*\b",
    r"\bkollegie\w*\b",
    r"\bkollegium\b",
    r"\bstudent\s+housing\b",
    # v3.11 (spec 10 section 6.2).
    r"\bskal\s+være\s+under\s+uddannelse\b",
    # Bare "under uddannelse" is scoped to a tenancy requirement: the Lejebolig
    # boilerplate ("... relevant for personer under uddannelse") describes the
    # neighbourhood and must not reject an ordinary flat.
    r"\b(?:kræver|forudsætter|krav\s+om|kun\s+for|forbeholdt)\s+(?:\w+\s+){0,2}under\s+uddannelse\b",
    r"\buddannelsessøgende\b",
    r"\bkrav\s+om\s+(?:uddannelse|studie\w*|studiekort)\b",
    r"\bkun\s+for\s+(?:unge\s+)?under\s+uddannelse\b",
    r"\bungdomsbolig\w*[^.!?]{0,80}\b(?:studerende|uddannelse)\b",
]

_STUDENT_EXPLICIT_NOT_REQUIRED_PATTERNS = [
    r"\b(?:kun\s+for\s+studerende|studiebolig|studiedokumentation|krav\s+om\s+studie\w*|forbeholdt\s+studerende|studiekort)\s*:?\s*(?:nej|no|ikke|not)\b",
    r"\b(?:ingen|intet|uden|ikke)\s+(?:form\s+for\s+)?(?:krav\s+om\s+)?(?:studie\w*|studerende|studiekort|studiedokumentation)\b",
    r"\bnot\s+(?:for\s+students|student\s+housing|students\s+only)\b",
]


def _bp_student_only_feature(rec: Dict[str, Any]) -> Optional[bool]:
    hydration = rec.get("detail_hydration")
    if not isinstance(hydration, dict):
        return None
    page_props = (hydration.get("props") or {}).get("page_props") or {}
    if not isinstance(page_props, dict):
        return None
    ad = page_props.get("ad")
    if isinstance(ad, dict) and isinstance(ad.get("features"), dict):
        val = ad["features"].get("student_only")
        if isinstance(val, bool):
            return val
    return None


def _bp_ad_description(rec: Dict[str, Any]) -> str:
    hydration = rec.get("detail_hydration")
    if not isinstance(hydration, dict):
        return ""
    page_props = (hydration.get("props") or {}).get("page_props") or {}
    if not isinstance(page_props, dict):
        return ""
    ad = page_props.get("ad")
    if isinstance(ad, dict) and isinstance(ad.get("description"), str):
        return ad["description"]
    return ""


def _clean_boligportal_lifestyle_chips(text: str) -> str:
    """Strip BoligPortal's raw unselected lifestyle chip block.

    BoligPortal renders all 4 chips ('Delevenlig', 'Kun for studerende',
    'Husdyr tilladt', 'Seniorvenlig') in inner_text between 'Livsstil' and
    'Faciliteter' regardless of selection.
    """
    if not text:
        return ""
    return re.sub(
        r"\bLivsstil\s+(?:Delevenlig\s+)?(?:Kun for studerende\s+)?(?:Husdyr tilladt\s+)?(?:Seniorvenlig\s+)?Faciliteter\b",
        "Faciliteter", text, flags=re.IGNORECASE)


def parse_student_requirement(rec: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Determine student requirement state from the advert's own evidence (Spec 09 §6.2).

    Returns (student_requirement_state, matched_excerpt).
    """
    title_url = _norm(f"{rec.get('title', '')} {rec.get('url', '')}")
    eligibility = own_listing_text(rec.get("eligibility_text"))

    pos_hit: Optional[re.Match] = None
    pos_text = ""
    neg_hit: Optional[re.Match] = None
    neg_text = ""

    # 1. Check negative patterns first on eligibility / full text
    for pat in _STUDENT_EXPLICIT_NOT_REQUIRED_PATTERNS:
        m = re.search(pat, eligibility, re.IGNORECASE)
        if m:
            neg_hit = m
            neg_text = eligibility
            break

    # 2. Check title / URL for product branding / naming
    for pat in STUDENT_NAME_REGEX:
        m = re.search(pat, title_url, re.IGNORECASE)
        if m:
            pos_hit = m
            pos_text = title_url
            break
    if not pos_hit:
        for kw in STUDENT_NEG:
            m = re.search(r"\b" + re.escape(kw) + r"\b", title_url, re.IGNORECASE)
            if m:
                pos_hit = m
                pos_text = title_url
                break

    # BoligPortal structured features gate:
    # On BoligPortal, the "Livsstil" section renders all 4 chips (Delevenlig, Kun for studerende,
    # Husdyr tilladt, Seniorvenlig) in the page body regardless of selection, so "Kun for studerende"
    # appears in raw inner_text even when inactive.
    bp_student = _bp_student_only_feature(rec)
    if bp_student is True and not pos_hit:
        pos_hit = re.search(r"student_only", "student_only: true")
        pos_text = "BoligPortal feature student_only: true"
    elif bp_student is False:
        desc = _bp_ad_description(rec)
        desc_has_student = False
        for pat in _STUDENT_EXPLICIT_REQUIRED_PATTERNS:
            if re.search(pat, desc, re.IGNORECASE) or re.search(pat, title_url, re.IGNORECASE):
                desc_has_student = True
                break
        if not desc_has_student and not pos_hit:
            return "not_required", "BoligPortal feature student_only: false"

    # Sanitize BoligPortal unselected chip section in inner_text so inactive chips don't trigger required patterns
    effective_eligibility = eligibility
    if rec.get("site") == "boligportal":
        effective_eligibility = _clean_boligportal_lifestyle_chips(eligibility)

    # 3. Check eligibility text for required patterns (respecting negated values/prefixes)
    if effective_eligibility:
        for pat in _STUDENT_EXPLICIT_REQUIRED_PATTERNS:
            for found in re.finditer(pat, effective_eligibility, re.IGNORECASE):
                # Negated value following? e.g. "Kun for studerende: Nej"
                if _NEGATED_VALUE_RE.match(effective_eligibility[found.end():found.end() + 16]):
                    if not neg_hit:
                        neg_hit = found
                        neg_text = eligibility
                    continue
                # Negated prefix preceding? e.g. "Ingen krav om studiekort"
                if _NEGATED_PREFIX_RE.search(eligibility[max(0, found.start() - 24):found.start()]):
                    if not neg_hit:
                        neg_hit = found
                        neg_text = eligibility
                    continue
                if not pos_hit:
                    pos_hit = found
                    pos_text = eligibility
                break
            if pos_hit:
                break

    # 4. Check stored demographic fields on rec if text is silent
    if not pos_hit and not neg_hit:
        stored_state = rec.get("student_requirement_state")
        if stored_state in ("required", "not_required", "conflict"):
            return stored_state, _bound_excerpt(rec.get("eligibility_fact_excerpt"))

    if pos_hit and neg_hit:
        return "conflict", _excerpt(neg_text or pos_text, neg_hit or pos_hit)
    if pos_hit:
        return "required", _excerpt(pos_text, pos_hit)
    if neg_hit:
        return "not_required", _excerpt(neg_text, neg_hit)
    return "unknown", None


def parse_age_requirement(rec: Dict[str, Any], profile_age: Optional[int] = None
                          ) -> Tuple[str, Optional[int], Optional[int], str, Optional[str]]:
    """Parse age requirement facts and compute profile age fit (Spec 09 §6.2).

    Returns (age_requirement_state, age_min, age_max, profile_age_fit, excerpt).
    """
    title_url = _norm(f"{rec.get('title', '')} {rec.get('url', '')}")
    eligibility = own_listing_text(rec.get("eligibility_text"))
    text = f"{title_url} {eligibility}".strip()

    age_min: Optional[int] = None
    age_max: Optional[int] = None
    state = "none"
    excerpt: Optional[str] = None

    # A portal's "Lejeperiode Under 1 år" describes the contract, not
    # the tenant. Remove only explicitly labelled duration fields before
    # interpreting generic year bounds; genuine age wording is untouched.
    lease_duration = re.compile(
        r"\b(?:lejeperiode|rental\s+period|lease\s+duration)\s*:?\s*"
        r"(?:under|over|fra|from|up\s+to|op\s+til|mellem|between)?\s*"
        r"\d{1,3}\s*(?:(?:-|–|til|to|og|and)\s*\d{1,3}\s*)?(?:år|aar|years?)\b", re.I)
    text = lease_duration.sub(" ", text)
    # Check for age range patterns
    range_patterns = [
        r"\balder\s*:?\s*(\d{1,3})\s*(?:-|–|til|to|og|and)\s*(\d{1,3})\s*(?:år|aar|years?)?\b",
        r"\bmellem\s+(\d{1,3})\s+og\s+(\d{1,3})\s*(?:år|aar|years?)?\b",
        r"\bbetween\s+(?:ages?\s+)?(\d{1,3})\s+and\s+(\d{1,3})\s*(?:years?)?\b",
        r"\bages?\s*:?\s*(\d{1,3})\s*(?:-|–|til|to|og|and)\s*(\d{1,3})\s*(?:years?)?\b",
        r"\b(\d{1,3})\s*(?:-|–)\s*(\d{1,3})\s*(?:år|aar|years?)\b",
        r"\bfor\s+(\d{1,3})\s*(?:-|–|til)\s*(\d{1,3})\s*[- ]årige\b",
    ]
    for pat in range_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            age_min = int(m.group(1))
            age_max = int(m.group(2))
            state = "bounded"
            excerpt = _excerpt(text, m)
            break

    # Min-only patterns
    if state == "none":
        min_patterns = [
            r"\balder\s*:?\s*(?:min\.?|minimum|fra|over|fyldt|at\s+least)\s*(\d{1,3})\s*(?:år|aar|years?)?\b",
            r"\b(?:min\.?|minimum|fra|over|fyldt|at\s+least)\s+(\d{1,3})\s*(?:år|aar|years?)\b",
        ]
        for pat in min_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                age_min = int(m.group(1))
                state = "bounded"
                excerpt = _excerpt(text, m)
                break

    # Max-only patterns
    if state == "none":
        max_patterns = [
            r"\balder\s*:?\s*(?:maks\.?|maksimum|max\.?|maximum|under|op\s+til|up\s+to)\s*(\d{1,3})\s*(?:år|aar|years?)?\b",
            r"\b(?:maks\.?|maksimum|max\.?|maximum|under|op\s+til|up\s+to)\s+(\d{1,3})\s*(?:år|aar|years?)\b",
        ]
        for pat in max_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                age_max = int(m.group(1))
                state = "bounded"
                excerpt = _excerpt(text, m)
                break

    # Youth / Ungdomsbolig label without bounds
    if state == "none":
        youth_pat = r"\b(?:ungdomsbolig|ungdomsboliger|ungdomslejlighed|ungdomslejligheder|youth\s+housing)\b"
        m = re.search(youth_pat, text, re.IGNORECASE)
        if m:
            state = "unknown"
            excerpt = _excerpt(text, m)

    # If text is silent, fallback to stored fields on rec
    stored_excerpt = _bound_excerpt(rec.get("eligibility_fact_excerpt")) or ""
    false_duration_bound = bool(lease_duration.search(stored_excerpt)) and not re.search(
        r"\b(?:alder|age|årige|aarige|tenant|lejer)\b", stored_excerpt, re.I)
    if (state == "none" and not false_duration_bound and
            rec.get("age_requirement_state") in ("bounded", "unknown", "conflict")):
        state = rec["age_requirement_state"]
        age_min = rec.get("age_min")
        age_max = rec.get("age_max")
        excerpt = _bound_excerpt(rec.get("eligibility_fact_excerpt"))

    # Compute profile_age_fit
    if state == "bounded":
        if profile_age is None:
            fit = "unknown"
        else:
            if (age_min is not None and profile_age < age_min) or (age_max is not None and profile_age > age_max):
                fit = "ineligible"
            else:
                fit = "eligible"
    elif state == "none":
        fit = "eligible"
    else:
        fit = "unknown"

    return state, age_min, age_max, fit, excerpt


def classify_demographic_eligibility(rec: Dict[str, Any],
                                     profile_age: Optional[int] = None) -> DemographicDecision:
    """Apply the Spec 09 Section 6.3 decision table to demographic facts."""
    s_state, s_excerpt = parse_student_requirement(rec)
    a_state, a_min, a_max, a_fit, a_excerpt = parse_age_requirement(rec, profile_age=profile_age)
    excerpt = s_excerpt or a_excerpt

    # Spec 09 Section 6.3 Precedence:
    # 1. Conflict in student or age evidence
    if s_state == "conflict" or a_state == "conflict":
        return DemographicDecision(
            student_requirement_state=s_state, age_requirement_state=a_state,
            age_min=a_min, age_max=a_max, profile_age_fit=a_fit,
            eligibility_fact_excerpt=excerpt, outcome="review",
            quarantine_reason=config.ELIGIBILITY_REASON_AGE_CONFLICT)

    # 2. Explicit student requirement
    if s_state == "required":
        return DemographicDecision(
            student_requirement_state=s_state, age_requirement_state=a_state,
            age_min=a_min, age_max=a_max, profile_age_fit=a_fit,
            eligibility_fact_excerpt=excerpt, outcome="reject",
            rejection_reason=config.ELIGIBILITY_REASON_STUDENT)

    # 3. Age range excludes profile age
    if a_state == "bounded" and a_fit == "ineligible":
        if a_min is not None and a_max is not None:
            age_str = f"{a_min}–{a_max}"
        elif a_min is not None:
            age_str = f"min {a_min}"
        else:
            age_str = f"max {a_max}"
        return DemographicDecision(
            student_requirement_state=s_state, age_requirement_state=a_state,
            age_min=a_min, age_max=a_max, profile_age_fit=a_fit,
            eligibility_fact_excerpt=excerpt, outcome="reject",
            rejection_reason=f"age requirement {age_str}; profile age is outside range")

    # 4. Profile age missing while age range is required
    if a_state == "bounded" and a_fit == "unknown":
        return DemographicDecision(
            student_requirement_state=s_state, age_requirement_state=a_state,
            age_min=a_min, age_max=a_max, profile_age_fit=a_fit,
            eligibility_fact_excerpt=excerpt, outcome="review",
            quarantine_reason=config.ELIGIBILITY_REASON_AGE_REQUIRED)

    # 5. Youth label with no readable bounds. Youth housing is rejected outright
    # by `strict_housing_type_reason` before this table runs; this row only
    # catches a stored "unknown" age state from an earlier evaluation.
    if a_state == "unknown":
        return DemographicDecision(
            student_requirement_state=s_state, age_requirement_state=a_state,
            age_min=a_min, age_max=a_max, profile_age_fit=a_fit,
            eligibility_fact_excerpt=excerpt, outcome="reject",
            rejection_reason=config.ELIGIBILITY_REASON_YOUTH)

    # 6. Eligible (no demographic restrictions or age range satisfied)
    return DemographicDecision(
        student_requirement_state=s_state, age_requirement_state=a_state,
        age_min=a_min, age_max=a_max, profile_age_fit=a_fit,
        eligibility_fact_excerpt=excerpt, outcome="eligible")


SOURCE_ELIGIBILITY_COLUMNS = (
    "student_requirement_state", "age_requirement_state",
    "age_min", "age_max", "profile_age_fit", "eligibility_fact_excerpt"
)


def source_eligibility_values(result: "EvaluationResult") -> Dict[str, Optional[Any]]:
    """The six section 6.2 demographic facts one evaluation established (Spec 09 §6.2)."""
    if result is None:
        return dict.fromkeys(SOURCE_ELIGIBILITY_COLUMNS)
    s_state = getattr(result, "student_requirement_state", None)
    a_state = getattr(result, "age_requirement_state", None)
    fit = getattr(result, "profile_age_fit", None)
    excerpt = getattr(result, "eligibility_fact_excerpt", None)
    return {
        "student_requirement_state": s_state if s_state in ("required", "not_required", "conflict") else None,
        "age_requirement_state": a_state if a_state in ("bounded", "conflict") else (
            "unknown" if a_state == "unknown" and excerpt else None
        ),
        "age_min": getattr(result, "age_min", None),
        "age_max": getattr(result, "age_max", None),
        "profile_age_fit": fit if fit in ("eligible", "ineligible") else None,
        "eligibility_fact_excerpt": _bound_excerpt(excerpt) if excerpt else None,
    }


def verify_kitchen(description: str) -> Tuple[Optional[bool], str]:
    """Verify private kitchen status.
    Returns: (is_private_verified: bool|None, status_str: 'own'|'shared'|'unverified')
    """
    d = _norm(description)
    is_shared = any(re.search(p, d) for p in TRUE_SHARED_NEG)
    has_own = any(p in d for p in OWN_KITCHEN_POS) or any(re.search(p, d) for p in OWN_KITCHEN_REGEX)

    if "fælleskøkken" in d or "fælles køkken" in d or "shared kitchen" in d:
        if not is_shared and has_own:
            return True, "own"
        return False, "shared"

    if is_shared:
        return False, "shared"
    if has_own:
        return True, "own"
    return None, "unverified"


def verify_bathroom(description: str) -> Tuple[Optional[bool], str]:
    """Verify private bathroom status.
    Returns: (is_private_verified: bool|None, status_str: 'own'|'shared'|'unverified')
    """
    d = _norm(description)
    if any(re.search(p, d) for p in [r"\bfælles\s+bad\b", r"\bdelt\s+bad\b", r"\bfælles\s+badeværelse\b", r"\btoilet\s+på\s+gangen\b", r"\bshared\s+bathroom\b"]):
        return False, "shared"
    has_own = any(p in d for p in OWN_BATHROOM_POS) or any(re.search(p, d) for p in OWN_BATHROOM_REGEX)
    if has_own:
        return True, "own"
    return None, "unverified"


def verify_lease_duration(rec: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """Verify unlimited lease duration.
    Returns: (is_unlimited_verified: bool|None, reason: str)
    """
    rp_raw = rec.get("rental_period_raw")
    if isinstance(rp_raw, int):
        if rp_raw > 0:
            return False, f"time-limited ({rp_raw} months)"
        elif rp_raw == 0:
            return True, "unlimited"

    rp = str(rec.get("rental_period") or "").lower()
    title = str(rec.get("title") or "").lower()
    desc = str(rec.get("description") or "").lower()
    blob = f"{title} {desc} {rp}"

    for p in TIME_LIMITED_PATTERNS:
        m = re.search(p, blob)
        if m:
            return False, f"time-limited ({m.group(0)})"

    for p in UNLIMITED_PATTERNS:
        if re.search(p, blob):
            return True, "unlimited"

    return None, "unverified"


def evaluate_scam_risk(rec: Dict[str, Any], rent_total: Optional[int]) -> Tuple[str, List[str]]:
    """Evaluate scam-risk signals (Section 9).
    Returns (risk_state: 'low'|'review'|'high', evidence: list[str])
    """
    evidence = []
    blob = _norm(f"{rec.get('title', '')} {rec.get('description', '')}")

    for p in SCAM_UPFRONT_PAY:
        if re.search(p, blob):
            evidence.append("Request to pay deposit or rent before viewing")
            break

    for p in SCAM_EXTERNAL_CHAT:
        if re.search(p, blob):
            evidence.append("Pressure to communicate exclusively through WhatsApp")
            break

    for p in SCAM_ABROAD_KEYS:
        if re.search(p, blob):
            evidence.append("Landlord claims to be abroad with remote key delivery")
            break

    if rent_total and rent_total < 3500:
        evidence.append(f"Price materially below market ({rent_total} kr/md)")

    dep = rec.get("deposit")
    prep = rec.get("prepaid_rent")
    if rent_total and rent_total > 0:
        if dep and dep > 3 * rent_total:
            evidence.append(f"Deposit exceeds 3 months rent ({dep} kr vs rent {rent_total} kr)")
        if prep and prep > 3 * rent_total:
            evidence.append(f"Prepaid rent exceeds 3 months rent ({prep} kr vs rent {rent_total} kr)")

    if len(evidence) >= 2 or any("before viewing" in e or "abroad" in e for e in evidence):
        return "high", evidence
    elif len(evidence) == 1:
        return "review", evidence
    else:
        return "low", ["No obvious warning signals"]


def never_zone_reason(rec: Dict[str, Any], lat: Optional[float],
                      lon: Optional[float]) -> Optional[str]:
    """v3.10 section 6b.2: the profile's never-live areas, as a rejection reason.

    Postcodes come from the record or its address; zones are straight-line
    radii around a point. None when the home is outside every never zone.
    """
    postcode = rec.get("postcode") or benchmarks.postcode_from_address(
        rec.get("normalized_address") or rec.get("address"))
    if postcode and str(postcode) in config.NEVER_POSTCODES:
        return f"never zone: {config.NEVER_POSTCODES[str(postcode)]} ({postcode})"
    if lat is not None and lon is not None:
        for name, z_lat, z_lon, radius_m in config.NEVER_ZONES:
            if geo.hav(lat, lon, z_lat, z_lon) <= radius_m:
                return f"never zone: within {int(radius_m)} m of {name}"
    return None


def area_rating(station: Optional[str]) -> float:
    """v3.10 section 6b.1: the profile's 0..1 rating for a nearest-station name."""
    if not station:
        return config.AREA_DEFAULT_RATING
    return float(config.AREA_STATION_RATINGS.get(station, config.AREA_DEFAULT_RATING))


_FLOOR_TEXT_RE = re.compile(r"\betage\s*:?\s*(stuen|st\b|kælder|kl\b|\d{1,2})", re.IGNORECASE)
_FLOOR_ADDR_RE = re.compile(r",\s*(st|kl|\d{1,2})\.(?=\s|,|$)", re.IGNORECASE)


def floor_of(rec: Dict[str, Any]) -> Optional[int]:
    """v3.10 section 6b.3: the unit's floor (0 = ground, -1 = basement) or None.

    The listing's own "Etage" field wins; the address floor ("1. 9", "st. tv")
    is the fallback.
    """
    def _value(token: str) -> int:
        t = token.lower()
        if t in ("stuen", "st"):
            return 0
        if t in ("kælder", "kl"):
            return -1
        return int(t)

    m = _FLOOR_TEXT_RE.search(str(rec.get("eligibility_text") or ""))
    if m:
        return _value(m.group(1))
    for key in ("normalized_address", "address"):
        m = _FLOOR_ADDR_RE.search(str(rec.get(key) or ""))
        if m:
            return _value(m.group(1))
    return None


def early_start_cost(rent_total: float, avail_date: Optional[datetime.date],
                     avail_str: Optional[str], today: datetime.date) -> float:
    """v3.10 section 6b.4: one-off double-rent cost of starting before the target.

    A missing date costs nothing (unknown). "Snarest" and dates already past
    mean the landlord wants a tenant now, so they carry the full capped overlap
    rather than looking cheaper than a flat that starts in January.
    """
    target = datetime.date.fromisoformat(config.TARGET_MOVE_DATE)
    if str(avail_str or "").strip().lower() == "snarest":
        start = today
    elif avail_date is None:
        return 0.0
    else:
        start = max(avail_date, today)
    early_days = min(max((target - start).days, 0), config.EARLY_START_MAX_DAYS)
    return rent_total * early_days / config.EARLY_START_DAYS_PER_MONTH


def relevance_for(avail_date: Optional[datetime.date], avail_str: Optional[str],
                  today: Optional[datetime.date] = None) -> str:
    """Classify a listing as 'actionable' or 'intel' for the February 2027 move.

    A date we can act on is one inside the actionable window. "Snarest", a
    missing date and a date already in the past all count as unknown: they only
    become actionable once the move is close enough that a landlord would hold
    the flat. Re-evaluated every scan because it moves with the calendar.
    """
    today = today or datetime.date.today()
    target = datetime.date.fromisoformat(config.TARGET_MOVE_DATE)
    start = target - datetime.timedelta(days=config.ACTIONABLE_BEFORE_DAYS)
    end = target + datetime.timedelta(days=config.ACTIONABLE_AFTER_DAYS)
    unknown_live = target - datetime.timedelta(days=config.UNKNOWN_LIVE_BEFORE_DAYS)

    known = (avail_date is not None
             and str(avail_str or "").lower() != "snarest"
             and avail_date >= today)

    if today > target:
        horizon = today + datetime.timedelta(days=config.POST_TARGET_HORIZON_DAYS)
        actionable = (not known) or avail_date <= horizon
    elif known:
        actionable = start <= avail_date <= end
    else:
        actionable = today >= unknown_live

    return "actionable" if actionable else "intel"


def tier_for_commute(commute_min: Optional[int]) -> Optional[str]:
    """Commute tier letter for a door-to-door time, or None above the ceiling."""
    return benchmarks.tier_for_commute(commute_min)


def disqualifying_housing_reason(rec: Dict[str, Any]) -> Optional[str]:
    """Student, elderly, care, or shared-housing verdict, or None.

    Split deliberately in two. The title and URL *name the product*, so a bare
    "student" there is decisive ("Canvas Student Nordhavn" is a student-housing
    brand). The description merely *describes the area*, so it is judged only on
    wording that restricts the tenancy - the trap that bare "studerende" fell
    into when a boilerplate paragraph about nearby schools rejected 37 ordinary
    flats.
    """
    evidence = housing_disqualification_evidence(rec)
    return evidence["reason"] if evidence else None


# A detail page ends with adverts for OTHER homes ("Andre boliger", "Se andre
# lignende boliger", "Populære søgninger"). Spec 06 section 8.4: evidence must
# come from the listing's own snapshot, so that tail is not evidence about this
# home in either direction. One BoligPortal flat was rejected as student housing
# because a completely different advert for "Trørød Kollegiet" sat in its
# sidebar.
SIBLING_SECTION_MARKERS = (
    "andre boliger", "lignende boliger", "se andre lignende",
    "populære søgninger", "boliger til leje i", "lejligheder til leje i",
)
_MIN_OWN_TEXT_CHARS = 200

# A portal fact table renders a label and then its value. "Kun for studerende
# Nej" is BoligPortal stating that the home is NOT students-only, and it appears
# on essentially every BoligPortal page: matching the label alone rejected 222
# of 491 stored listings. A negative value is evidence of the opposite state.
_NEGATED_VALUE_RE = re.compile(r"\s*:?\s*(?:nej|no|ikke|not)\b")

# The same statement in the other order. "NB: ingen krav om studiekort" says
# there is NO student-card requirement, which is the opposite of the phrase it
# contains. Only the unambiguous negators are listed: Danish "ikke" attaches to
# a verb further away and would swallow genuine restrictions.
_NEGATED_PREFIX_RE = re.compile(
    r"\b(?:ingen|intet|uden|ikke|ingen\s+form\s+for)(?:\s+form\s+for)?(?:\s+krav\s+om)?\s+$",
    re.IGNORECASE
)


def own_listing_text(text: Optional[str]) -> str:
    """Normalized page text with the portal's other-listings tail removed."""
    blob = _norm(text)
    cut = len(blob)
    for marker in SIBLING_SECTION_MARKERS:
        found = blob.find(marker)
        if found >= _MIN_OWN_TEXT_CHARS and found < cut:
            cut = found
    return blob[:cut]


def housing_disqualification_evidence(rec: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return a hard housing verdict and the exact source phrase that caused it.

    ``description`` is deliberately absent from this classifier. Portal-card
    copy often talks about nearby schools or student life and is not tenancy
    evidence. The title/URL names the product; eligibility_text comes from the
    authoritative detail page.
    """
    title_url = _norm(f"{rec.get('title', '')} {rec.get('url', '')}")
    eligibility = own_listing_text(rec.get("eligibility_text"))

    def match(patterns, text):
        for pattern in patterns:
            for found in re.finditer(pattern, text):
                # A portal fact table renders the label and then the answer.
                # "Kun for studerende Nej" and "Studiedokumentation: Nej" both
                # say this home is NOT restricted, so keep looking rather than
                # treating the label itself as the verdict.
                if _NEGATED_VALUE_RE.match(text[found.end():found.end() + 16]):
                    continue
                if _NEGATED_PREFIX_RE.search(text[max(0, found.start() - 24):found.start()]):
                    continue
                return found.group(0)
        return None

    # v3.12 (spec 11) section 5: a kollegium/kollegie named in the title/URL is
    # decisive student housing even when the portal's own "Kun for studerende
    # Nej" fact flips the demographic classifier to not_required or conflict
    # (audit 2026-10-09 section 2C: fuglebakkekollegiet ranked on that fact).
    # Scoped to the kollegium family only: "studiebolig" in a title remains
    # governed by the demographic classifier, so a contradictory portal fact
    # still quarantines as a conflict rather than rejecting.
    phrase = match([r"kollegie\w*", r"kollegium\w*"], title_url)
    if phrase:
        return {"reason": "student housing", "matched_text": phrase,
                "source_url": rec.get("eligibility_source_url") or rec.get("url") or ""}

    # Spec 09 §6.3: student housing is governed by the demographic classifier.
    # If student requirement is not_required or conflict, it must not emit a
    # student rejection here (the title/URL kollegium check above still does).
    student_state, _ = parse_student_requirement(rec)
    if student_state not in ("not_required", "conflict"):
        phrase = match(STUDENT_NAME_REGEX, title_url)
        if phrase:
            return {"reason": "student housing", "matched_text": phrase,
                    "source_url": rec.get("eligibility_source_url") or rec.get("url") or ""}
        clean_eligibility = _clean_boligportal_lifestyle_chips(eligibility) if rec.get("site") == "boligportal" else eligibility
        phrase = match([*(re.escape(p) for p in STUDENT_NEG), *STUDENT_REGEX], clean_eligibility)
        if phrase:
            return {"reason": "student housing", "matched_text": phrase,
                    "source_url": rec.get("eligibility_source_url") or rec.get("url") or ""}

    # v3.12 (spec 11) section 6: two-phase senior rule. Phase 1 treats a product
    # NAME in the title/URL as decisive; phase 2 honours only wording in the
    # detail body that RESTRICTS the tenancy to seniors. A bare feature-list
    # label ("Ældrebolig Elevator ...") is a building facility, not a restriction
    # on this home, so it no longer rejects an ordinary flat (audit 2026-10-09
    # section 3.4: 28 of 29 "elderly" rejects were that label).
    phrase = match([*(re.escape(p) for p in ELDERLY_NEG), *ELDERLY_REGEX], title_url)
    if phrase:
        return {"reason": "elderly / senior housing", "matched_text": phrase,
                "source_url": rec.get("eligibility_source_url") or rec.get("url") or ""}
    phrase = match(ELDERLY_BODY_RESTRICTING_REGEX, eligibility)
    if phrase:
        return {"reason": "elderly / senior housing", "matched_text": phrase,
                "source_url": rec.get("eligibility_source_url") or rec.get("url") or ""}

    phrase = match([*(re.escape(p) for p in CARE_NEG)], f"{title_url} {eligibility}")
    if phrase:
        return {"reason": "care housing", "matched_text": phrase,
                "source_url": rec.get("eligibility_source_url") or rec.get("url") or ""}

    # Only wording that says the HOME is shared belongs here. Shared-kitchen and
    # shared-bathroom phrases moved to their own gates: a building with a common
    # kitchen is not a flatshare, and a shared bathroom needs its own reason.
    phrase = match(TRUE_SHARED_HOUSING_NEG, f"{title_url} {eligibility}")
    if phrase:
        return {"reason": "shared housing", "matched_text": phrase,
                "source_url": rec.get("eligibility_source_url") or rec.get("url") or ""}
    return None


def _eligibility_evidence_quarantine(rec: Dict[str, Any]) -> Optional[str]:
    """Return the v3.4 quarantine reason for missing/failed detail evidence."""
    state = str(rec.get("eligibility_evidence_state") or "").lower()
    if (state == "verified" and rec.get("eligibility_text")
            and rec.get("eligibility_source_url")
            and rec.get("eligibility_checked_at")):
        return None
    if state == "failed":
        return "housing eligibility fetch failed"
    return "housing eligibility unverified"


def evaluate_listing(rec: Dict[str, Any], stations: List[Dict[str, Any]],
                     store: Any = None, segments: Optional[Dict[str, Any]] = None,
                     today: Optional[datetime.date] = None,
                     profile_age: Optional[int] = None) -> EvaluationResult:
    """Evaluate a candidate against the v3.12 hard rules (spec 11).

    v3.12 hardens the spec 10 / spec 11 hard-reject set: price over
    9.700 kr, more than two rooms, not self-contained, student/senior/care/shared
    housing, an age range that excludes the profile age, a shared-only kitchen or
    bathroom, outside Greater Copenhagen, a time-limited lease, a commute over
    50 minutes (spec 11 decision 11) and a move-in cost
    over 50.000 kr. Everything else - Metro distance, Nørreport distance,
    a missing detail page, an unverified kitchen, private landlord, small private
    building and the v3.10 never zones - ranks the listing and is carried as a
    soft flag or a score penalty.

    The composed trust verdict is still attached on every path for persistence
    and disclosure. Only the sublet row of the trust table still rejects here; a
    private landlord or a small/private building is a badge (call 4).
    """
    if profile_age is None:
        rec_age = rec.get("profile_age")
        if isinstance(rec_age, int) and not isinstance(rec_age, bool):
            profile_age = rec_age
        elif store is not None:
            try:
                prof = store.get_search_profile()
                if prof and isinstance(prof.get("age_years"), int):
                    profile_age = prof["age_years"]
            except Exception:
                pass

    trust = classify_listing_trust(rec, store)
    res = _evaluate_gates(rec, stations, store=store, segments=segments,
                          today=today, profile_age=profile_age)
    res.unit_type_state = trust.unit_type_state
    res.tenancy_channel_state = trust.tenancy_channel_state
    res.building_form_state = trust.building_form_state
    res.trust_outcome = trust.outcome
    res.trust_reason = trust.reason
    res.trust_decision = trust

    if res.rejected:
        return res

    # v3.12 (spec 11) call: sublet AND a private landlord are hard rejections
    # again (spec 10 call 4 had demoted private landlord to a badge; audit
    # 2026-10-09 section 2B). A room/other unit type was already rejected by
    # the gates above. A small or private building stays a badge.
    if trust.outcome == "reject":
        if trust.reason == "sublet":
            res.rejected = True
            res.passed = False
            res.rejection_reason = "sublet"
            return res
        if trust.reason == "private landlord":
            res.rejected = True
            res.passed = False
            res.rejection_reason = "private landlord"
            return res
        if trust.reason == "small or private property":
            res.soft_flags.append(make_flag(
                "small_private_building", "Small private building", "info",
                getattr(trust.building_form, "excerpt", None)))

    if trust.tenancy_channel_state == "unknown":
        # v3.12 (spec 11): a listing with no professional evidence and no
        # private-landlord evidence must not rank as if verified. It keeps
        # ranking (the audit's re-tiering of 28 of 42 matches was declined), but
        # it is visibly flagged for review.
        res.soft_flags.append(make_flag(
            "landlord_unverified", "Advertiser not identified", "check",
            getattr(trust.tenancy_channel, "excerpt", None)))

    if trust.outcome == "review":
        # Spec 10 section 5.2: `self-contained apartment unverified` ranks with
        # the Unit-type-unconfirmed badge instead of holding the listing.
        res.soft_flags.append(make_flag(
            "unit_type_unconfirmed", "Unit type unconfirmed", "check",
            getattr(trust.unit_type, "excerpt", None)))
    return res


# Housing types that are never an option, whatever else the advert says: youth
# housing (ungdomsbolig) is a student product even when it states an age range,
# and an andelsbolig is a cooperative share, not a rental. Judged on the title,
# URL and the listing's own detail text, like the other housing verdicts. No
# stored active listing uses the word negated ("ikke en ungdomsbolig"), so no
# negation handling is applied.
YOUTH_HOUSING_REGEX = [
    r"\bungdoms[- ]?bolig\w*",
    r"\bungdomslejlighed\w*",
    r"\byouth\s+(?:housing|apartment|flat)s?\b",
]
COOP_HOUSING_REGEX = [
    r"\bandels[- ]?bolig\w*",
    r"\bandelslejlighed\w*",
    r"\bandelsbevis\w*",
    r"\bcooperative\s+(?:housing|apartment|flat)s?\b",
]


def strict_housing_type_reason(rec: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(reason, matched text) for youth or cooperative housing, else None."""
    text = _norm(f"{rec.get('title', '')} {rec.get('url', '')} "
                 f"{own_listing_text(rec.get('eligibility_text'))}")
    for pattern in YOUTH_HOUSING_REGEX:
        m = re.search(pattern, text)
        if m:
            return config.ELIGIBILITY_REASON_YOUTH, m.group(0)
    for pattern in COOP_HOUSING_REGEX:
        m = re.search(pattern, text)
        if m:
            return config.ELIGIBILITY_REASON_COOP, m.group(0)
    return None


def _demo_flags(demo: "DemographicDecision", soft_flags: List[Flag]) -> None:
    """Turn a non-rejecting demographic verdict into soft flags (spec 10 section 6)."""
    if demo.outcome != "review" or not demo.quarantine_reason:
        return
    if demo.quarantine_reason == config.ELIGIBILITY_REASON_AGE_CONFLICT:
        soft_flags.append(make_flag(
            "eligibility_conflict", "Conflicting eligibility wording", "check",
            demo.eligibility_fact_excerpt))
    elif demo.quarantine_reason == config.ELIGIBILITY_REASON_AGE_REQUIRED:
        soft_flags.append(make_flag(
            "age_required_unverified", "Age requirement unverified", "check",
            demo.eligibility_fact_excerpt))


def _evaluate_gates(rec: Dict[str, Any], stations: List[Dict[str, Any]],
                    store: Any = None, segments: Optional[Dict[str, Any]] = None,
                    today: Optional[datetime.date] = None,
                    profile_age: Optional[int] = None) -> EvaluationResult:
    """Evaluate a candidate against the v3.12 hard rules and soft flags.

    Hard rejects (spec 10 section 2 with calls 5 and 6 overruled, hardened by
    spec 11): H1 rent over
    9.700, H2a more than two rooms, H2b not self-contained, H2c student, H2d
    senior/care, H2e age range excludes the profile, H2f shared-only kitchen or
    bathroom, H3 outside Greater Copenhagen, H4 time-limited lease, H5 move-in
    cost over 50.000, private landlord (spec 11 decision 3) and commute over
    50 minutes (spec 11 decision 11). Quarantine holds only Q1 (rent unknown),
    Q2 (unit unknown) and the shared-kitchen-with-no-private case (spec 11
    decision 5). Everything else ranks and carries a soft flag.

    store, when given, supplies the place_features cache. segments is the
    market benchmark table. External failures never raise: they downgrade the
    result.
    """
    quarantine_reasons: List[str] = []
    soft_flags: List[Flag] = []
    confidence_details: List[str] = []

    # --------------------------------------------------------------------------
    # 0. Gone / reserved listings
    # --------------------------------------------------------------------------
    body = " ".join(str(rec.get("eligibility_text") or rec.get("description") or "").lower().split())
    if rec.get("explicit_gone_at") or rec.get("detail_state") == "failed:gone":
        return _reject("listing gone / no longer available", rec)
    matched_gone = next((m for m in getattr(config, "GONE_MARKERS", ()) if m in body), None)
    if matched_gone:
        return _reject(f"listing gone / reserved ({matched_gone})", rec)

    # --------------------------------------------------------------------------
    # 1. Studio type & demographic eligibility
    # --------------------------------------------------------------------------
    lt = (rec.get("listing_type") or "").lower()
    if lt in ("værelse", "room", "hus", "villa", "delebolig", "kollegie"):
        return _reject(f"not a studio ({lt})", rec)

    # A project advert describes a range of homes and prices. Its minimum rent
    # and example address cannot establish that a particular unit qualifies.
    if re.match(r"https?://(?:www\.)?boligportal\.dk/projektudlejning/",
                str(rec.get("url") or ""), re.IGNORECASE):
        return _reject("project page / no individually priced unit", rec)

    # Known hard facts must win over missing detail evidence. In particular a
    # card from Randers must not wait in the Copenhagen verification queue.
    lat, lon = rec.get("lat"), rec.get("lon")
    if lat is not None and lon is not None:
        distance = geo.distance_to_radhuspladsen(lat, lon)
        if distance is not None and distance > 40000.0:
            return _reject(f"outside Greater Copenhagen boundary "
                           f"({round(distance / 1000)}km from city centre)", rec)
    if rec.get("rooms") is not None and rec["rooms"] > 2:
        return _reject(f"apartment with {rec['rooms']} rooms (1-2 rooms required)", rec)

    title_desc = _norm(f"{rec.get('title', '')} {rec.get('description', '')} {rec.get('url', '')}")

    strict = strict_housing_type_reason(rec)
    if strict:
        return _reject(strict[0], rec)

    demo = classify_demographic_eligibility(rec, profile_age=profile_age)
    if demo.outcome == "reject":
        return _reject(demo.rejection_reason or "student housing", rec, demo=demo)
    _demo_flags(demo, soft_flags)

    disqualified = disqualifying_housing_reason(rec)
    if disqualified:
        return _reject(disqualified, rec, demo=demo)

    # The unit-type classification rejects an authoritative room rental or a
    # house/other. A review verdict is no longer a hold: it ranks with the
    # Unit-type-unconfirmed badge (spec 10 section 5.2).
    unit = classify_unit_type(rec)
    rec["_unit_decision"] = unit
    if unit.decision == "reject":
        return _reject(unit.reason or "room rental / not self-contained", rec, demo=demo)

    # --------------------------------------------------------------------------
    # 2. Monthly card price lower bound, detail evidence and shared housing
    # --------------------------------------------------------------------------
    card_price = rec.get("card_price")
    card_rent = None
    if isinstance(card_price, (int, float)) and not isinstance(card_price, bool) and card_price > 0:
        card_rent = int(card_price)

    rb = rec.get("rent_base")
    ua = rec.get("utilities_amount")
    mf = rec.get("mandatory_monthly_fees") or 0
    detail_total = int(rb + (ua or 0) + mf) if rb is not None else None

    if card_rent is not None and card_rent > config.RENT_CAP_MAX:
        if detail_total is not None and detail_total <= config.RENT_CAP_MAX:
            soft_flags.append(make_flag(
                "price_conflict", "Card and detail price differ", "info"))
        elif detail_total is not None and detail_total > config.RENT_CAP_MAX:
            return _reject(f"rent {detail_total} > {config.RENT_CAP_MAX}", rec, demo=demo)
        else:
            return _reject(f"card rent {card_rent} > {config.RENT_CAP_MAX}", rec, demo=demo)

    # Some portals store the card's base rent and aconto directly in these
    # fields without setting card_price. Their known total is still enough to
    # exclude an over-cap flat before a missing detail page can badge it.
    known_total = detail_total if detail_total is not None else rec.get("rent_total")
    if (isinstance(known_total, (int, float)) and not isinstance(known_total, bool)
            and known_total > config.RENT_CAP_MAX):
        return _reject(f"rent {int(known_total)} > {config.RENT_CAP_MAX}", rec, demo=demo)

    # A missing or failed detail page no longer holds the listing; it ranks with
    # the Not-verified-from-detail-page badge (spec 10 section 5.2).
    evidence_quarantine = _eligibility_evidence_quarantine(rec)
    if evidence_quarantine:
        soft_flags.append(make_flag(
            "detail_unverified", "Not verified from detail page", "check",
            evidence_quarantine))

    # Check for explicit flatshare / room share in authoritative housing
    # evidence. Display/card description remains out of this classifier.
    housing_blob = (_norm(f"{rec.get('title', '')} {rec.get('url', '')}") + " "
                    + own_listing_text(rec.get("eligibility_text")))
    if any(re.search(p, housing_blob) for p in TRUE_SHARED_HOUSING_NEG):
        return _reject("shared housing / room rental", rec, demo=demo)

    rooms = rec.get("rooms")
    rooms_verified = False
    room_rule = (f"{config.ROOMS_MIN}-{config.ROOMS_MAX} rooms required"
                 if config.ROOMS_MIN != config.ROOMS_MAX
                 else f"{config.ROOMS_MAX} rooms required")
    if rooms is not None:
        if rooms > config.ROOMS_MAX:
            return _reject(f"apartment with {rooms} rooms ({room_rule})", rec, demo=demo)
        elif rooms < config.ROOMS_MIN:
            return _reject(f"apartment with {rooms} rooms ({room_rule})", rec, demo=demo)
        else:
            rooms_verified = True
            confidence_details.append(f"Structured room count equals {rooms}")
    else:
        # Read the room count from the title when the portal did not supply it.
        room_words = {
            1: r"\b1-v[æa]relses\b|\b1\s+v[æa]relse\b|\bét-v[æa]relses\b|\bstudio\b",
            2: r"\b2-v[æa]relses\b|\b2\s+v[æa]relser\b|\bto-v[æa]relses\b",
            3: r"\b3-v[æa]relses\b|\b3\s+v[æa]relser\b|\btre-v[æa]relses\b",
            4: r"\b4-v[æa]relses\b|\b4\s+v[æa]relser\b|\bfire-v[æa]relses\b",
        }
        found = next((n for n in sorted(room_words) if re.search(room_words[n], title_desc)),
                     None)
        if found is not None:
            rec["rooms"] = found
            if config.ROOMS_MIN <= found <= config.ROOMS_MAX:
                rooms_verified = True
                confidence_details.append(f"{found}-værelses verified in text")
            else:
                return _reject(f"apartment with {found} rooms ({room_rule})", rec, demo=demo)
        else:
            # Q2 (spec 10 section 5.1): the unit cannot be judged at all when
            # nothing says apartment, studio or room. Otherwise it ranks with
            # the Unit-type-unconfirmed badge.
            clue = (lt in ("lejlighed", "apartment", "studio", "ukendt", "",
                           "1-værelses", "2-værelses", "rental_apartment")
                    or (rec.get("card_class") or "") == "apartment")
            if clue:
                rooms_verified = False
                soft_flags.append(make_flag(
                    "unit_unconfirmed", "Unit type unconfirmed", "check"))
            else:
                return _quarantine_only(
                    rec, ["unit unknown"], confidence_details, demo=demo,
                    soft_flags=soft_flags)

    # --------------------------------------------------------------------------
    # 3. Private Kitchen & Bathroom Verification
    # --------------------------------------------------------------------------
    is_apartment = lt in ("lejlighed", "apartment", "studio", "1-værelses", "2-værelses", "ukendt") or lt == ""

    kitchen = analyse_kitchen(rec, rooms_verified=rooms_verified)
    if kitchen.decision == "reject":
        return _reject(kitchen.reason or "shared kitchen", rec, kitchen=kitchen, demo=demo)
    if kitchen.decision == "quarantine":
        # v3.12 (spec 11) section 8: a shared kitchen with no private-kitchen
        # evidence is a hard stop for review, not a silent badge (Dortheavej
        # "Fælleskøkken fordelt på etagerne").
        return _quarantine_only(rec, [kitchen.reason or "kitchen unverified"],
                                confidence_details, demo=demo, soft_flags=soft_flags)
    kitchen_verified = kitchen.decision == "pass" and kitchen.kitchen_state == "verified"
    if kitchen.decision == "pass" and kitchen.kitchen_state == "unverified":
        soft_flags.append(make_flag(
            "kitchen_unconfirmed", "Kitchen unconfirmed", "check",
            kitchen.evidence_excerpt or kitchen.reason))
    elif kitchen.kitchen_state == "verified":
        confidence_details.append("Private kitchen verified")

    bathroom_state, bathroom_decision, _bath_excerpt = analyse_bathroom(
        rec, rooms_verified=rooms_verified)
    if bathroom_decision == "reject":
        # Its own reason text: a shared bathroom must never be reported as a
        # shared kitchen, however the two phrases sit in the same paragraph.
        return _reject("shared bathroom", rec, kitchen=kitchen,
                       bathroom_state=bathroom_state, demo=demo)
    bathroom_verified = bathroom_decision == "pass" and bathroom_state == "verified"
    if bathroom_decision == "pass" and bathroom_state == "unverified":
        soft_flags.append(make_flag(
            "bathroom_unconfirmed", "Bathroom unconfirmed", "info"))
    elif bathroom_state == "verified":
        confidence_details.append("Private bathroom verified")

    # --------------------------------------------------------------------------
    # 4. Lease Duration Verification (H4 stays hard: call 5 overruled)
    # --------------------------------------------------------------------------
    l_ok, l_reason = verify_lease_duration(rec)
    if l_ok is False:
        return _reject(f"time-limited lease ({l_reason})", rec, demo=demo)
    elif l_ok is True:
        lease_verified = True
        confidence_details.append("Unlimited lease verified")
    elif is_apartment and (rec.get("rental_period_raw") == 0 or "ubegrænset" in str(rec.get("rental_period", "")).lower() or not rec.get("rental_period")):
        lease_verified = True
    else:
        lease_verified = False
        soft_flags.append(make_flag(
            "lease_unconfirmed", "Lease duration unconfirmed", "info"))

    # --------------------------------------------------------------------------
    # 5. Total Rent (Q1) & move-in cost (H5 stays hard: call 6 overruled)
    # --------------------------------------------------------------------------
    rent_total = rec.get("rent_total")
    if rent_total is None:
        if detail_total is not None:
            rent_total = detail_total
            rec["rent_total"] = rent_total
        elif card_rent is not None:
            rent_total = card_rent
            rec["rent_total"] = rent_total

    if not rent_total or rent_total <= 0:
        # Q1 (spec 10 section 5.1): no card price, no detail rent, no aconto:
        # H1 cannot be tested. Hold, do not reject.
        return _quarantine_only(rec, ["rent unknown"], confidence_details,
                                demo=demo, soft_flags=soft_flags)

    if card_rent is not None and card_rent > config.RENT_CAP_MAX and rent_total <= config.RENT_CAP_MAX:
        if not any(f.code == "price_conflict" for f in soft_flags):
            soft_flags.append(make_flag(
                "price_conflict", "Card and detail price differ", "info"))

    if rent_total > config.RENT_CAP_MAX:
        return _reject(f"rent {rent_total} > {config.RENT_CAP_MAX}", rec, demo=demo)

    move_in_total_explicit = rec.get("move_in_total_explicit")
    deposit = rec.get("deposit")
    prepaid_rent = rec.get("prepaid_rent")
    onetime_fees = rec.get("mandatory_onetime_fees") or 0

    move_in_cost = None
    move_in_cost_verified = False

    if move_in_total_explicit is not None and move_in_total_explicit > 0:
        move_in_cost = int(move_in_total_explicit)
        move_in_cost_verified = True
    elif deposit is not None:
        prep = prepaid_rent if prepaid_rent is not None else 0
        move_in_cost = int(deposit + prep + rent_total + onetime_fees)
        move_in_cost_verified = True
    elif is_apartment:
        # Standard Danish deposit is 3 months rent
        est_deposit = (rec.get("rent_base") or rent_total) * 3
        prep = prepaid_rent if prepaid_rent is not None else 0
        move_in_cost = int(est_deposit + prep + rent_total + onetime_fees)
        move_in_cost_verified = True
    else:
        move_in_cost = None
        move_in_cost_verified = False
        soft_flags.append(make_flag(
            "move_in_unconfirmed", "Move-in cost unconfirmed", "info"))

    rec["move_in_cost"] = move_in_cost
    rec["move_in_cost_verified"] = move_in_cost_verified

    if move_in_cost_verified and move_in_cost > config.MOVE_IN_COST_MAX:
        return _reject(f"move-in cost {move_in_cost} > {config.MOVE_IN_COST_MAX}", rec, demo=demo)

    # --------------------------------------------------------------------------
    # 6. Geocoding (from here on the evaluation may touch the network). A failure
    #    never quarantines: the listing ranks with the Location-unknown badge
    #    (spec 10 section 3.2).
    # --------------------------------------------------------------------------
    lat, lon = rec.get("lat"), rec.get("lon")
    geo_precision = rec.get("geo_precision")
    location_known = lat is not None and lon is not None

    city_distance_m = 0.0
    norreport_distance_m = None
    norreport_road_distance_m = None
    road_meta: Dict[str, Any] = {}
    station_name = straight_dist_m = metro_walk_m = walk_min = None
    transit_name = transit_walk_m = used_transit_walk_m = None
    feats: Dict[str, Any] = {}
    commute_min = None
    commute_route = None
    is_responsible_pick = False
    tier = None
    tier_name = None
    category = None
    transit_curve = config.TRANSIT_METRO_CURVE

    if not location_known:
        soft_flags.append(make_flag(
            "location_unknown", "Location unknown", "check"))
    else:
        city_distance_m = geo.distance_to_radhuspladsen(lat, lon)
        if city_distance_m is None:
            city_distance_m = 0.0
        elif city_distance_m > config.GEO_MAX_DISTANCE_M:
            return _reject(f"outside Greater Copenhagen boundary "
                           f"({round(city_distance_m / 1000)}km from city centre)", rec, demo=demo)

        addr_str = str(rec.get("address") or rec.get("_geo_address") or "")
        if re.search(r"\d+", addr_str):
            confidence_details.append("Street name and number geocoded")

        # The straight-line value is retained as an audited diagnostic only.
        norreport_distance_m = geo.distance_to_norreport(lat, lon)

        norreport_road_distance_m = geo.road_route_to_norreport(lat, lon)
        road_meta = geo.road_route_metadata(lat, lon)
        if store is not None:
            store.save_road_route(
                coord_key=f"{lat!r},{lon!r}",
                distance_m=norreport_road_distance_m,
                provider=road_meta.get("provider"),
                calculated_at=road_meta.get("calculated_at"),
                status=road_meta.get("status") or ("ok" if norreport_road_distance_m is not None else "failed"),
                profile=road_meta.get("profile") or "driving",
            )
        # Spec 10 section 4.1: the Nørreport distance never rejects.
        if norreport_road_distance_m is not None and norreport_road_distance_m > config.NORREPORT_ROAD_DISTANCE_MAX_M:
            soft_flags.append(make_flag(
                "norreport_distance",
                f"{round(norreport_road_distance_m / 1000)} km from Nørreport",
                "over-limit",
                f"{round(norreport_road_distance_m):,} m road-route"))

        # v3.10 never zones are a score penalty, not a rejection (call 3).
        never = never_zone_reason(rec, lat, lon)
        if never:
            zone = never.replace("never zone:", "").strip()
            soft_flags.append(make_flag(
                "never_zone", f"Never zone: {zone}", "over-limit", never))

        # Nearest stations, kept for display and for the rapid-transit score.
        station_name, straight_dist_m, metro_walk_m, walk_min = geo.nearest_metro(lat, lon, stations)
        transit_name, _t_straight, transit_walk_m, _t_min, _t_net = geo.nearest_transit(lat, lon, stations)

        feats = features.get_place_features(lat, lon, store=store)
        commute_min = feats.get("commute_min")
        commute_route = feats.get("commute_route")
        if isinstance(commute_route, str):
            try:
                commute_route = json.loads(commute_route)
            except Exception:
                commute_route = None

        if commute_min is None:
            # Spec 10 section 4.1: fall back to an estimate from the straight-line
            # distance to the office and badge it. Never a hold.
            straight_m = norreport_distance_m or 0.0
            commute_min = int(round(config.COMMUTE_FALLBACK_BASE_MIN
                                    + config.COMMUTE_FALLBACK_MIN_PER_KM * (straight_m / 1000.0)))
            soft_flags.append(make_flag(
                "route_estimated", "Route estimated", "info"))

        if geo_precision == "postcode":
            commute_min = commute_min + config.GEO_PRECISION_POSTCODE_PENALTY_MIN
        elif geo_precision == "street":
            commute_min = commute_min + config.GEO_PRECISION_PENALTY_MIN

        if commute_min > config.COMMUTE_MAX_MIN:
            # v3.12 (spec 11): the commute over the ceiling is a hard rejection
            # again (spec 10 had demoted it to a badge; audit 2026-10-09 call).
            return _reject(f"commute {commute_min} min > {config.COMMUTE_MAX_MIN}",
                           rec, demo=demo)

        # Spec 10 section 4.1: a far commute ranks in the Far tier rather than
        # being rejected. tier_for_commute returns None above the C ceiling.
        tier = tier_for_commute(commute_min) or "C"
        tier_name = config.TIER_NAMES.get(tier)
        category = config.TIER_NUMBERS.get(tier)

        metro_ok = metro_walk_m is not None and metro_walk_m <= config.NORMAL_METRO_WALK_MAX_M
        if metro_ok:
            transit_curve = config.TRANSIT_METRO_CURVE
            used_transit_walk_m = metro_walk_m
            is_responsible_pick = False
        else:
            s_train_ok = transit_walk_m is not None and transit_walk_m <= config.RP_S_TRAIN_MAX_M
            if s_train_ok:
                transit_curve = config.RP_S_TRAIN_CURVE
                used_transit_walk_m = transit_walk_m
                is_responsible_pick = True
            else:
                # Spec 10 section 4.1: no Metro/S-train only zeroes transit points.
                soft_flags.append(make_flag(
                    "no_transit", "No Metro/S-train nearby", "over-limit"))
                transit_curve = config.TRANSIT_METRO_CURVE
                used_transit_walk_m = None
                is_responsible_pick = False

    budget_band = "within"

    # --------------------------------------------------------------------------
    # 7. Availability, target-date fit and timeline relevance
    # --------------------------------------------------------------------------
    af_raw = rec.get("available_from")
    avail_date, avail_date_str = parse_available_date(af_raw)
    target_date = datetime.date.fromisoformat(config.TARGET_MOVE_DATE)
    pref_start = datetime.date.fromisoformat(config.PREFERRED_WINDOW_START)
    pref_end = datetime.date.fromisoformat(config.PREFERRED_WINDOW_END)

    is_target_window = bool(avail_date and pref_start <= avail_date <= pref_end)
    if avail_date and avail_date_str != "snarest":
        date_points = config.SCORE_DATE_PTS * clamp(
            1.0 - abs((avail_date - target_date).days) / config.DATE_TOLERANCE_DAYS)
    else:
        date_points = 0.0

    relevance = relevance_for(avail_date, avail_date_str, today)

    # --------------------------------------------------------------------------
    # 8. Value against the market
    # --------------------------------------------------------------------------
    size_m2 = rec.get("size_m2")
    postcode = rec.get("postcode") or benchmarks.postcode_from_address(
        rec.get("normalized_address") or rec.get("address"))
    val = benchmarks.value_fields(rent_total, size_m2, segments or {}, postcode, tier)

    # --------------------------------------------------------------------------
    # 9. Unified 100-point score (v3.3 weights, v3.10 personal fit)
    # --------------------------------------------------------------------------
    early_cost = early_start_cost(rent_total, avail_date, avail_date_str,
                                  today or datetime.date.today())
    effective_rent = rent_total + early_cost / config.EARLY_START_AMORTISE_MONTHS
    affordability_pts = _piecewise(effective_rent, config.AFFORDABILITY_CURVE)

    transit_pts = _piecewise(used_transit_walk_m, transit_curve)

    commute_pts = _piecewise(commute_min, config.COMMUTE_CURVE)

    buzz_score = feats.get("buzz_score")
    buzz_pts = config.SCORE_BUZZ_PTS * (buzz_score if buzz_score is not None else 0.0)

    water_dist_m = feats.get("water_dist_m")
    if water_dist_m is None:
        water_pts = 0.0
    else:
        water_pts = (config.SCORE_WATER_PTS
                     * features.water_weight(feats.get("water_kind"))
                     * clamp((config.SCORE_WATER_ZERO_M - water_dist_m)
                             / (config.SCORE_WATER_ZERO_M - config.SCORE_WATER_FULL_M)))

    if location_known:
        centre_pts = config.SCORE_CENTRE_PTS * clamp(
            (config.SCORE_CENTRE_ZERO_M - city_distance_m)
            / (config.SCORE_CENTRE_ZERO_M - config.SCORE_CENTRE_FULL_M))
    else:
        centre_pts = 0.0

    area_pts = config.SCORE_AREA_PTS * area_rating(transit_name or station_name)

    unit_floor = floor_of(rec)
    floor_pts = (config.FLOOR_UNKNOWN_PTS if unit_floor is None
                 else _piecewise(unit_floor, config.FLOOR_CURVE))

    value_pts = val["value_pts"]
    date_pts = date_points

    score_breakdown = {
        "affordability": round(affordability_pts, 1),
        "transit": round(transit_pts, 1),
        "commute": round(commute_pts, 1),
        "water": round(water_pts, 1),
        "centre": round(centre_pts, 1),
        "buzz": round(buzz_pts, 1),
        "area": round(area_pts, 1),
        "floor": round(floor_pts, 1),
        "value": round(value_pts, 1),   # display only (zero ranking points)
        "date": round(date_pts, 1),     # display only (zero ranking points)
    }
    ranking_pts = (affordability_pts + transit_pts + commute_pts
                   + water_pts + centre_pts + buzz_pts + area_pts + floor_pts)
    # v3.11 never-zone penalty (spec 10 call 3): down-rank, never reject.
    if any(f.code == "never_zone" for f in soft_flags):
        ranking_pts = max(0.0, ranking_pts - config.NEVER_ZONE_SCORE_PENALTY)
    total_score = round(ranking_pts)

    conf_points = min(5, len(confidence_details))
    verified_badge = f"Verified {conf_points}/5"

    # --------------------------------------------------------------------------
    # 10. Scam-risk assessment
    # --------------------------------------------------------------------------
    risk_state, risk_evidence = evaluate_scam_risk(rec, rent_total)

    return EvaluationResult(
        passed=True,
        quarantined=False,
        quarantine_reasons=quarantine_reasons,
        rejected=False,
        rejection_reason=None,
        category=category,
        category_name=tier_name,
        score=total_score,
        score_breakdown=score_breakdown,
        risk_state=risk_state,
        risk_evidence=risk_evidence,
        confidence_points=conf_points,
        confidence_details=confidence_details,
        city_distance_m=city_distance_m,
        metro_walk_m=metro_walk_m,
        straight_dist_m=straight_dist_m,
        nearest_station=station_name,
        walk_min=walk_min,
        transit_station=transit_name,
        transit_walk_m=transit_walk_m,
        is_target_window=is_target_window,
        available_date_str=avail_date_str or af_raw,
        rent_total=rent_total,
        move_in_cost=move_in_cost,
        move_in_cost_verified=move_in_cost_verified,
        lease_verified=lease_verified,
        kitchen_verified=kitchen_verified,
        bathroom_verified=bathroom_verified,
        rooms_verified=rooms_verified,
        tier=tier,
        budget_band=budget_band,
        commute_min=commute_min,
        commute_route=commute_route,
        size_m2=size_m2,
        kr_per_m2=val["kr_per_m2"],
        value_ratio=val["value_ratio"],
        value_label=val["value_label"],
        benchmark_median=val["benchmark_median"],
        benchmark_segment=val["benchmark_segment"],
        buzz_score=buzz_score,
        buzz_detail=feats.get("buzz_detail"),
        major_street=feats.get("major_street"),
        bus_routes=feats.get("bus_routes"),
        water_dist_m=water_dist_m,
        water_name=feats.get("water_name"),
        water_kind=feats.get("water_kind"),
        relevance=relevance,
        geo_precision=geo_precision,
        features_pending=int(feats.get("features_pending") or 0),
        verified_badge=verified_badge,
        norreport_distance_m=norreport_distance_m,
        norreport_road_distance_m=norreport_road_distance_m,
        norreport_road_provider=road_meta.get("provider"),
        norreport_road_status=road_meta.get("status") or ("unknown" if not location_known else "ok"),
        is_responsible_pick=is_responsible_pick,
        used_transit_walk_m=used_transit_walk_m,
        private_kitchen_present=kitchen.private_kitchen_present,
        shared_kitchen_present=kitchen.shared_kitchen_present,
        shared_kitchen_is_the_only_kitchen=kitchen.shared_kitchen_is_the_only_kitchen,
        kitchen_state=kitchen.kitchen_state,
        kitchen_evidence_source=kitchen.evidence_source,
        kitchen_evidence_excerpt=kitchen.evidence_excerpt,
        kitchen_caveat=kitchen.caveat,
        bathroom_state=bathroom_state,
        detail_state=rec.get("detail_state"),
        card_class=rec.get("card_class"),
        student_requirement_state=demo.student_requirement_state,
        age_requirement_state=demo.age_requirement_state,
        age_min=demo.age_min,
        age_max=demo.age_max,
        profile_age_fit=demo.profile_age_fit,
        eligibility_fact_excerpt=demo.eligibility_fact_excerpt,
        soft_flags=soft_flags,
    )


def _reject(reason: str, rec: Dict[str, Any],
            kitchen: Optional["KitchenEvidence"] = None,
            bathroom_state: Optional[str] = None,
            demo: Optional["DemographicDecision"] = None,
            soft_flags: Optional[List["Flag"]] = None) -> EvaluationResult:
    return EvaluationResult(
        passed=False,
        quarantined=False,
        quarantine_reasons=[],
        rejected=True,
        rejection_reason=reason,
        category=None,
        category_name=None,
        score=0.0,
        score_breakdown={"affordability": 0.0, "transit": 0.0, "commute": 0.0,
                         "water": 0.0, "centre": 0.0, "buzz": 0.0,
                         "value": 0.0, "date": 0.0},
        risk_state="low",
        risk_evidence=[],
        confidence_points=0,
        confidence_details=[],
        city_distance_m=None,
        metro_walk_m=None,
        straight_dist_m=None,
        nearest_station=None,
        walk_min=None,
        transit_station=None,
        transit_walk_m=None,
        is_target_window=False,
        available_date_str=None,
        rent_total=rec.get("rent_total"),
        move_in_cost=rec.get("move_in_cost"),
        move_in_cost_verified=False,
        lease_verified=False,
        kitchen_verified=False,
        bathroom_verified=False,
        rooms_verified=False,
        private_kitchen_present=getattr(kitchen, "private_kitchen_present", None),
        shared_kitchen_present=getattr(kitchen, "shared_kitchen_present", None),
        shared_kitchen_is_the_only_kitchen=getattr(
            kitchen, "shared_kitchen_is_the_only_kitchen", None),
        kitchen_state=getattr(kitchen, "kitchen_state", None),
        kitchen_evidence_source=getattr(kitchen, "evidence_source", None),
        kitchen_evidence_excerpt=getattr(kitchen, "evidence_excerpt", None),
        bathroom_state=bathroom_state,
        detail_state=rec.get("detail_state"),
        card_class=rec.get("card_class"),
        student_requirement_state=getattr(demo, "student_requirement_state", rec.get("student_requirement_state")),
        age_requirement_state=getattr(demo, "age_requirement_state", rec.get("age_requirement_state")),
        age_min=getattr(demo, "age_min", rec.get("age_min")),
        age_max=getattr(demo, "age_max", rec.get("age_max")),
        profile_age_fit=getattr(demo, "profile_age_fit", rec.get("profile_age_fit")),
        eligibility_fact_excerpt=getattr(demo, "eligibility_fact_excerpt", rec.get("eligibility_fact_excerpt")),
        soft_flags=list(soft_flags or []),
    )


def _quarantine_only(rec: Dict[str, Any], reasons: List[str],
                     confidence_details: List[str],
                     demo: Optional["DemographicDecision"] = None,
                     soft_flags: Optional[List["Flag"]] = None) -> EvaluationResult:
    """Hold a listing the system cannot judge (Q1 rent unknown, Q2 unit unknown)."""
    return EvaluationResult(
        passed=False,
        quarantined=True,
        quarantine_reasons=reasons,
        rejected=False,
        rejection_reason=None,
        category=None,
        category_name=None,
        score=0.0,
        score_breakdown={"affordability": 0.0, "transit": 0.0, "commute": 0.0,
                         "water": 0.0, "centre": 0.0, "buzz": 0.0,
                         "value": 0.0, "date": 0.0},
        risk_state="low",
        risk_evidence=[],
        confidence_points=min(5, len(confidence_details)),
        confidence_details=confidence_details,
        city_distance_m=None,
        metro_walk_m=None,
        straight_dist_m=None,
        nearest_station=None,
        walk_min=None,
        transit_station=None,
        transit_walk_m=None,
        is_target_window=False,
        available_date_str=None,
        rent_total=rec.get("rent_total"),
        move_in_cost=rec.get("move_in_cost"),
        move_in_cost_verified=bool(rec.get("move_in_cost_verified")),
        lease_verified=False,
        kitchen_verified=False,
        bathroom_verified=False,
        rooms_verified=False,
        geo_precision=rec.get("geo_precision"),
        detail_state=rec.get("detail_state"),
        card_class=rec.get("card_class"),
        student_requirement_state=getattr(demo, "student_requirement_state", rec.get("student_requirement_state")),
        age_requirement_state=getattr(demo, "age_requirement_state", rec.get("age_requirement_state")),
        age_min=getattr(demo, "age_min", rec.get("age_min")),
        age_max=getattr(demo, "age_max", rec.get("age_max")),
        profile_age_fit=getattr(demo, "profile_age_fit", rec.get("profile_age_fit")),
        eligibility_fact_excerpt=getattr(demo, "eligibility_fact_excerpt", rec.get("eligibility_fact_excerpt")),
        soft_flags=list(soft_flags or []),
    )
