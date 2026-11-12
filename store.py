"""Dedup store, SQLite database, lifecycle tracking, and durable logger for BoligWatch v2.1."""
import datetime
import hashlib
import json
import os
import re
import sqlite3
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import config
import tiers
from geo import hav, strip_floor_door


def format_bw_id(prop_id: int) -> str:
    """Format integer property ID into stable BoligWatch ID, e.g. BW-0001842."""
    return f"BW-{prop_id:07d}"


def parse_bw_id(text: Any) -> Optional[int]:
    """Parse stable ID with or without prefix and leading zeroes, e.g. 'BW-1842', 'BW-0001842', '1842'."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text if text > 0 else None
    s = str(text).strip().upper()
    m = re.fullmatch(r"(?:BW-?)?(\d+)", s)
    if m:
        try:
            val = int(m.group(1))
            return val if val > 0 else None
        except ValueError:
            return None
    return None


# ==============================================================================
# v3.3 — Building / complex consolidation helpers
# ==============================================================================
def _fold_complex_text(s: Optional[str]) -> str:
    """Lowercase, Unicode-insensitive fold for address matching.

    Danish ø/æ/å are folded to their ASCII digraphs so a portal that writes
    "Viola Noerloevs Gade" merges with "Viola Nørløvs Gade". Whitespace and
    punctuation collapse to single spaces.
    """
    if not s:
        return ""
    s = str(s).lower()
    for a, b in (("ø", "oe"), ("æ", "ae"), ("å", "aa"),
                 ("Ø", "oe"), ("Æ", "ae"), ("Å", "aa")):
        s = s.replace(a, b)
    s = re.sub(r"[\s,.;:]+", " ", s).strip()
    return s


def _split_street_number(head: str) -> Tuple[str, Optional[str]]:
    """Split a street/address head into (street_name, house_number).\n
    'Viola Nørløvs Gade 1' -> ('Viola Nørløvs Gade', '1');
    'Viola Nørløvs Gade' -> ('Viola Nørløvs Gade', None).
    """
    head = head.strip()
    # Strip a trailing house-number token (digits, optional letter / range).
    m2 = re.match(r"^(.*?)\s+(\d+[A-Za-z]?(?:[-\/]\d+[A-Za-z]?)?)$", head)
    if m2:
        return m2.group(1), m2.group(2)
    m3 = re.search(r"^(.*?)\s+(\d+[A-Za-z]?(?:[-\/]\d+[A-Za-z]?)?)(?:\s+.*)?$", head)
    if m3 and re.search(r"\d", head.split()[0] if head else "", ) is None:
        # Only split when the number is not the very first token (a bare number
        # address is ambiguous) and a trailing number actually exists.
        if re.search(r"\d", m3.group(2)):
            return m3.group(1), m3.group(2)
    return head, None


def resolve_complex_key_for(address: Optional[str],
                            postcode: Optional[str]) -> Optional[str]:
    """Map an address+postcode to a configured complex_key, or None.

    Alias matching is folded and Unicode-insensitive. An alias carrying a house
    number matches that exact address head; an alias without a number explicitly
    merges the whole street (see COMPLEX_GROUPS). The group postcode must match
    when the property postcode is known.
    """
    if not address:
        return None
    head = address.split(",")[0].strip()
    folded_head = _fold_complex_text(head)
    street, _num = _split_street_number(head)
    folded_street = _fold_complex_text(street)
    inferred_pc = re.search(r",\s*(\d{4})\b", address)
    pc = _fold_complex_text(str(postcode or (inferred_pc.group(1) if inferred_pc else "")))

    for key, group in config.COMPLEX_GROUPS.items():
        gps = _fold_complex_text(str(group.get("postcode", "") or ""))
        if gps and pc and pc != gps:
            continue
        # Bare-street adverts are explicitly curated; this does not grant a
        # wildcard over unknown numbered buildings on that street.
        if _num is None and folded_head in {
                _fold_complex_text(a) for a in group.get("street_only_aliases", [])}:
            if pc == gps:
                return key
        for alias in group.get("aliases", []):
            a_head = alias.split(",")[0].strip()
            a_street, a_num = _split_street_number(a_head)
            if a_num is not None:
                if (_fold_complex_text(a_street) == folded_street and
                        _fold_complex_text(a_num) == _fold_complex_text(_num)):
                    return key
            else:
                if _fold_complex_text(a_street) == folded_street:
                    return key
    return None


def _days_ago(timestamp: Optional[str],
              today: Optional[datetime.date] = None) -> Optional[int]:
    """Whole days between an ISO timestamp and today, or None if unparseable."""
    if not timestamp:
        return None
    try:
        seen = datetime.datetime.fromisoformat(str(timestamp)).date()
    except ValueError:
        return None
    return max(0, ((today or datetime.date.today()) - seen).days)


def _tristate(value: Any) -> Optional[str]:
    """Serialise a true / false / unknown signal without collapsing it to a bool."""
    if value is None:
        return "unknown"
    if isinstance(value, str):
        return value
    return "true" if value else "false"


def _parse_soft_flags(value: Any) -> List[Dict[str, Any]]:
    """Parse a stored soft_flags JSON column into a list of flag dicts.

    v3.11 (spec 10 section 5.3). Malformed or empty values yield an empty list,
    so an older row never breaks a read.
    """
    if not value:
        return []
    if isinstance(value, list):
        return [f for f in value if isinstance(f, dict)]
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return []
    return [f for f in parsed if isinstance(f, dict)] if isinstance(parsed, list) else []


# Spec 07 section 5.1: a stored evidence excerpt is bounded so one long portal
# page can never bloat a read model row.
EVIDENCE_EXCERPT_MAX = 200


def _excerpt(value: Optional[str]) -> Optional[str]:
    """Bound an evidence excerpt to EVIDENCE_EXCERPT_MAX characters."""
    if value is None:
        return None
    return str(value)[:EVIDENCE_EXCERPT_MAX]


def source_record_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild a trust-classifier input from one stored `source_listings` row.

    Mirrors the merge the local pass performs on a stored row (`run.py`): the
    record's own `raw_data` JSON plus the row's columns, with the record's own
    stored value winning wherever it has one. A sibling counted during a
    building-form decision therefore reads exactly the fields it would be
    evaluated with.

    The columns carried include all nine spec 07 section 5.1 trust facts -
    the unit-type trio, the tenancy state and the landlord triple, and the
    building state with its `building_profile_id` - so a stored landlord or
    building fact reaches `filters.classify_tenancy_channel` /
    `filters.classify_building_form` on the next pass instead of decorate-only
    columns being invisible to the classifier.
    """
    raw = row.get("raw_data")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            rec = json.loads(raw) or {}
        except Exception:
            rec = {}
    else:
        rec = dict(raw or {})
    rec.pop("eval_result", None)
    for field in ("site", "listing_id", "property_id", "lat", "lon",
                  "normalized_address", "postcode", "geo_precision",
                  "eligibility_text", "eligibility_source_url",
                  "eligibility_evidence_state", "eligibility_checked_at",
                  "card_class", "detail_state", "card_text", "card_price",
                  "unit_type_state", "unit_type_evidence_source",
                  "unit_type_evidence_excerpt",
                  "tenancy_channel_state", "landlord_name",
                  "landlord_evidence_source", "landlord_evidence_excerpt",
                  "building_form_state", "building_profile_id",
                  "student_requirement_state", "age_requirement_state",
                  "age_min", "age_max", "profile_age_fit",
                  "eligibility_fact_excerpt", "explicit_gone_at",
                  "explicit_gone_marker"):
        if rec.get(field) is None and row.get(field) is not None:
            rec[field] = row[field]
    rec["url"] = rec.get("url") or row.get("url")
    return rec


# ==============================================================================
# v3.6 (spec 07 section 5.2) — the frozen building-profile manifest
# ==============================================================================
# The reviewed profiles are tracked as `building_profiles_v36.json`: the nine
# proposals v36m0 read out of stored, source-scoped operator evidence, frozen
# verbatim under a header that carries the proposal pack's path and SHA-256 and
# the freeze date. That file is the only source of reviewed profiles and
# `Store.load_building_profiles_manifest` is the only path that writes them, so
# the database cannot drift from what was frozen. Everything here is local and
# deterministic: the manifest is read from disk and applied; no URL is fetched.
BUILDING_PROFILE_MANIFEST_VERSION = "building-profiles-v36/1"
BUILDING_PROFILE_MANIFEST_PATH = "building_profiles_v36.json"

# Spec 07 section 5.2: `management_state` is one of these three. The other two
# vocabularies live in `config` because the classifier reads them through it
# (`config.BUILDING_PROFILE_EVIDENCE_KINDS`, `config.TRUST_BUILDING_FORM_STATES`).
BUILDING_PROFILE_MANAGEMENT_STATES = ("professional", "private", "unknown")

# The per-entry fields of one manifest entry (section 5.2 minus `id`, the
# database's own AUTOINCREMENT surrogate key, which no entry may invent), plus
# `state`, which the manifest carries explicitly.
BUILDING_PROFILE_ENTRY_KEYS = (
    "profile_key", "display_name", "normalized_address", "complex_key",
    "building_form_state", "management_state", "operator_name", "evidence_url",
    "evidence_excerpt", "evidence_kind", "verified_at", "state",
)

# Header fields every manifest must carry: the frozen version, the proposal pack
# it was copied from and that pack's SHA-256, the freeze date, the entry count.
BUILDING_PROFILE_MANIFEST_HEADER_KEYS = (
    "manifest_version", "frozen_at", "profile_count", "source_pack",
    "source_pack_sha256",
)


def _manifest_entry_problems(entry: Any, index: int) -> List[str]:
    """Every reason one manifest entry cannot be loaded into `building_profiles`."""
    where = f"profiles[{index}]"
    if not isinstance(entry, dict):
        return [f"{where}: not an object"]
    problems: List[str] = []
    for key in BUILDING_PROFILE_ENTRY_KEYS:
        if key not in entry:
            problems.append(f"{where}: missing {key}")
    if problems:
        return problems
    key = entry.get("profile_key")
    if not isinstance(key, str) or not key.strip():
        problems.append(f"{where}: profile_key is empty")
    address = entry.get("normalized_address")
    complex_key = entry.get("complex_key")
    if not address and not complex_key:
        problems.append(f"{where}: no scope (normalized_address and complex_key "
                        f"are both empty)")
    if address:
        if not isinstance(address, str):
            problems.append(f"{where}: normalized_address is not a string")
        else:
            if not Store.address_is_one_building(address):
                problems.append(f"{where}: scope {address!r} has no house number")
            folded = Store.normalize_exact_address(address)
            if isinstance(key, str) and key.strip() and folded != key:
                problems.append(f"{where}: profile_key {key!r} is not the folded "
                                f"scope {folded!r}")
    if entry.get("state") not in ("active", "superseded"):
        problems.append(f"{where}: state {entry.get('state')!r} is not "
                        f"active/superseded")
    if entry.get("building_form_state") not in config.TRUST_BUILDING_FORM_STATES:
        problems.append(f"{where}: building_form_state "
                        f"{entry.get('building_form_state')!r} is outside "
                        f"spec 07 section 4.3's vocabulary")
    if entry.get("management_state") not in BUILDING_PROFILE_MANAGEMENT_STATES:
        problems.append(f"{where}: management_state "
                        f"{entry.get('management_state')!r} is outside "
                        f"spec 07 section 5.2's vocabulary")
    if entry.get("evidence_kind") not in config.BUILDING_PROFILE_EVIDENCE_KINDS:
        problems.append(f"{where}: evidence_kind {entry.get('evidence_kind')!r} is "
                        f"outside spec 07 section 5.2's vocabulary")
    excerpt = entry.get("evidence_excerpt")
    if excerpt is not None and len(str(excerpt)) > EVIDENCE_EXCERPT_MAX:
        problems.append(f"{where}: evidence_excerpt is longer than "
                        f"{EVIDENCE_EXCERPT_MAX} characters")
    # A profile only becomes positive building evidence through its evidence URL
    # and verified timestamp (`filters.classify_building_form`), so an entry
    # without them is not loadable as a reviewed profile.
    for field in ("operator_name", "evidence_url", "evidence_excerpt", "verified_at"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: {field} is empty")
    return problems


def validate_building_profile_manifest(manifest: Any) -> List[str]:
    """Every reason a manifest cannot be loaded. An empty list means loadable.

    Spec 07 section 5.2: one entry per reviewed exact building (or configured
    complex), each with an exact house-numbered scope, the state and evidence
    vocabularies, its operator, its evidence URL, a bounded excerpt and the
    timestamp the evidence was read. `profile_key` must be the folded scope —
    the form `upsert_building_profile` stores and every exact-address lookup
    compares — so a duplicate address cannot hide behind a different key.
    Purely local: writes nothing, opens nothing, fetches nothing.
    """
    if not isinstance(manifest, dict):
        return ["manifest is not a JSON object"]
    problems: List[str] = []
    for header in BUILDING_PROFILE_MANIFEST_HEADER_KEYS:
        if header not in manifest:
            problems.append(f"header: missing {header}")
    if manifest.get("manifest_version") not in (None, BUILDING_PROFILE_MANIFEST_VERSION):
        problems.append(f"header: manifest_version "
                        f"{manifest.get('manifest_version')!r} is not "
                        f"{BUILDING_PROFILE_MANIFEST_VERSION!r}")
    digest = manifest.get("source_pack_sha256")
    if digest is not None and not (isinstance(digest, str) and len(digest) == 64
                                   and all(c in "0123456789abcdef" for c in digest)):
        problems.append("header: source_pack_sha256 is not a sha256 hex digest")
    for header in ("frozen_at", "source_pack"):
        value = manifest.get(header)
        if header in manifest and (not isinstance(value, str) or not value.strip()):
            problems.append(f"header: {header} is empty")
    entries = manifest.get("profiles")
    if not isinstance(entries, list):
        problems.append("header: profiles is not a list")
        return problems
    count = manifest.get("profile_count")
    if count is not None and count != len(entries):
        problems.append(f"header: profile_count {count!r} does not match "
                        f"{len(entries)} profile(s)")
    for index, entry in enumerate(entries):
        problems.extend(_manifest_entry_problems(entry, index))
    keys = [e.get("profile_key") for e in entries if isinstance(e, dict)]
    if len(set(keys)) != len(keys):
        duplicated = sorted({k for k in keys
                             if isinstance(k, str) and keys.count(k) > 1})
        problems.append(f"profiles: duplicate profile_key {duplicated}")
    scopes = [e.get("normalized_address") for e in entries
              if isinstance(e, dict) and e.get("normalized_address")]
    folded = [Store.normalize_exact_address(s) for s in scopes]
    if len(set(folded)) != len(folded):
        problems.append("profiles: two entries share one exact address")
    return problems


def read_building_profile_manifest(path: Optional[str] = None) -> Dict[str, Any]:
    """Parse and validate the tracked manifest, or raise `ValueError`.

    The default path is the tracked file beside this module, so a local
    migration does not depend on the caller's working directory. The error lists
    every validation problem, so a malformed manifest fails loudly instead of
    silently seeding a partial set of profiles.
    """
    target = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  BUILDING_PROFILE_MANIFEST_PATH)
    with open(target, encoding="utf-8") as handle:
        manifest = json.load(handle)
    problems = validate_building_profile_manifest(manifest)
    if problems:
        raise ValueError(f"{target} is not a loadable profile manifest: "
                         + "; ".join(problems))
    return manifest


# ==============================================================================
# v3.6 (spec 07 sections 7, 7.1) — the four-view read model's vocabulary
# ==============================================================================
# The dashboard reads only `matches`, which makes quarantine, rejection,
# orphaning and expiry all look like disappearance (section 7). The four views
# below are answered from the source row plus its CURRENT evaluation, and a
# source is in at most one of them. Trusted blocks stays the existing grouped
# match path (section 7: the trusted count remains grouped
# properties/buildings); the other three are source-level.

# The view keys, in the dashboard's own order (Trusted blocks is the default).
VIEW_TRUSTED_BLOCKS = "trusted_blocks"
VIEW_NEEDS_VERIFICATION = "needs_verification"
VIEW_RECENTLY_GONE = "recently_gone"
VIEW_EXCLUDED = "excluded"
VIEW_KEYS = (VIEW_TRUSTED_BLOCKS, VIEW_NEEDS_VERIFICATION, VIEW_RECENTLY_GONE,
             VIEW_EXCLUDED)

# The one explained absence: a source expired beyond `config.EXPIRED_VISIBLE_DAYS`
# is in no view at all, and no reason is invented for it beyond its lifecycle.
VIEW_OUT_OF_WINDOW = "out_of_window"

# The lifecycle states a current source may hold while it is in scope
# (`run.update_lifecycle_after_scan`); an `expired` row is in scope only inside
# the visible window.
VIEW_IN_SCOPE_LIFECYCLES = ("active", "missing", "relisted")

# Section 7.1 rank 7's own wording. "property identity unresolved" is the reason
# the section names for an unresolved property (section 6.1 item 6, section 7.1
# item 7); the lifecycle one is what keeps a Recently gone or out-of-window
# row's disclosure from being empty when its last decision states no reason.
REASON_IDENTITY_UNRESOLVED = "property identity unresolved"
REASON_LIFECYCLE_EXPIRED = "lifecycle expired"

# The frozen accounting set: the unambiguous superset of section 3.3's 27 named
# former options (all 53 former match sources of the v3.3 backup), with the
# backup's sha256 in its provenance block. Resolved relative to this module, so
# the default path is cwd-independent.
FORMER_OPTIONS_FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tests", "fixtures", "former_options_v33.json")

# The accounting helper's view names (section 3.3's reconciliation appendix).
ACCOUNTING_VIEW_FOR_VIEW = {
    VIEW_TRUSTED_BLOCKS: "trusted",
    VIEW_NEEDS_VERIFICATION: "review",
    VIEW_RECENTLY_GONE: "gone",
    VIEW_EXCLUDED: "excluded",
    VIEW_OUT_OF_WINDOW: VIEW_OUT_OF_WINDOW,
}
# Present in the corpus but carrying no current verdict is not a view either.
ACCOUNTING_VIEW_MISSING = "missing_source"

# Section 8's forbidden trust states in `matches`. Declared here and asserted
# against the `config` vocabularies in the tests, so a vocabulary rename cannot
# leave the predicate silently matching nothing.
FORBIDDEN_UNIT_TYPE_STATES = ("room_rental",)
# v3.11 (spec 10 call 4): only a sublet still rejects. A private landlord ranks
# with a badge, so it is no longer a forbidden match state.
FORBIDDEN_TENANCY_CHANNEL_STATES = ("sublet",)
# Spec 08 section 6.2's third clause: an `eligible` evaluation carrying explicit
# rejection evidence is a critical failure. The unit and tenancy dimensions are
# forbidding lists; the building dimension is checked against the one definite
# negative state, `small_or_private_property`. Same guard as above: asserted
# against `config.TRUST_BUILDING_FORM_STATES` in the tests.
FORBIDDEN_BUILDING_FORM_STATES = ("small_or_private_property",)

# Section 11's (v3.6) positive-evidence triple, kept as the state names the
# audit's `eligible` vocabulary is read against. Spec 08 section 6.2 replaced the
# triple invariant with the explicit-rejection one below: an `eligible` row must
# be a self-contained apartment, must not be a sublet or private-landlord row and
# must not be a reviewed small/private property. `ELIGIBLE_TENANCY_CHANNEL_STATE`
# and `ELIGIBLE_BUILDING_FORM_STATE` are no longer required of an eligible row -
# a present verdict earns a badge, not a gate (spec 08 section 4.3). Each value is
# asserted against its `config` vocabulary in the tests.
ELIGIBLE_TRUST_OUTCOME = "eligible"
ELIGIBLE_UNIT_TYPE_STATE = "self_contained_apartment"
ELIGIBLE_TENANCY_CHANNEL_STATE = "professional_direct"
ELIGIBLE_BUILDING_FORM_STATE = "managed_multi_unit_block"

# Section 8's critical invariants, in report order. A non-zero value for any of
# them is the "critical invariant violation" the command exits non-zero for.
# Normal building consolidation is deliberately NOT here: section 8 reports it
# and states that it is not an error.
AUDIT_CRITICAL_INVARIANTS = ("null_linked_current_matches",
                             "forbidden_in_matches",
                             "eligible_invariant_failures",
                             "explicit_gone_in_matches",
                             "duplicate_dataset_placement")

# The bucket the audit reports for a stored value it groups by that is NULL (an
# evaluation with no outcome, a review row carrying no reason). It labels
# "nothing recorded"; it is never a verdict.
AUDIT_UNSPECIFIED = "unspecified"


def _reason_matcher(*alternatives: str) -> "re.Pattern[str]":
    """One case-insensitive matcher for a rank's alternative phrasings."""
    return re.compile("|".join(re.escape(a) for a in alternatives), re.IGNORECASE)


# Spec 07 section 7.1's seven ranks, lowest rank = primary reason. Each entry is
# a (rank, matcher) pair; a reason is ranked by the FIRST rank whose matcher it
# matches, so a string belonging to two families takes the stronger one (the
# v3.5 "shared housing / room rental" composite is rank 1, not rank 3).
#
# Where the section's own wording is a config constant, the matcher is built
# from the constant rather than a retyped literal (ranks 1, 2 and 4), so a
# rename in `config` cannot silently unrank a reason.
REASON_PRECEDENCE: List[Tuple[int, "re.Pattern[str]"]] = [
    # 1. room rental / not self-contained
    (1, _reason_matcher(config.TRUST_REASON_ROOM, config.TRUST_REASON_UNIT_UNVERIFIED,
                        "room rental", "not self-contained")),
    # 2. sublet or private landlord. Spec 08 section 6.2 removed the
    #    `landlord / direct lease unverified` string from this matcher: no code
    #    path emits it any more, and the config constant that named it is gone.
    (2, _reason_matcher("sublet", "private landlord")),
    # 3. student, youth, senior, care or shared housing. The v3.4 housing-evidence
    #    quarantine ("housing eligibility unverified" / "... fetch failed") belongs
    #    to this axis: it is the evidence for the student/youth/senior/care/shared
    #    housing question that could not be read.
    (3, _reason_matcher("student housing", "student", "kollegie", "not a studio",
                        "elderly", "senior housing", "care housing",
                        "shared housing", "ungdomsbolig", "youth housing", "andelsbolig",
                        "housing eligibility")),
    # 4. professional management or apartment-block evidence missing. Spec 08
    #    section 6.2 drops the removed `managed apartment block unverified`
    #    string; the rank stays in the list for ordering stability and matches
    #    only the section 7.2 lifecycle and conflict wording this release still
    #    emits as badge-side notes.
    (4, _reason_matcher(config.TRUST_REASON_STALE, config.TRUST_REASON_CONFLICT,
                        "block unverified", "building profile", "building evidence")),
    # 5. private kitchen, bathroom or unlimited lease unverified
    (5, _reason_matcher("kitchen", "bathroom", "unverified lease",
                        "lease duration", "unlimited lease", "time-limited")),
    # 6. rent, move-in, centrality, transit or commute gate. Also the fallback for
    #    an unmatched string (`reason_rank`), which is a gate by construction.
    (6, _reason_matcher("rent", "move-in", "commute", "metro", "transit",
                        "centrality", "boundary", "distance", "geocod")),
    # 7. lifecycle state or unresolved property identity. This matcher IS the
    #    "except lifecycle or identity wording, which ranks 7" clause.
    (7, _reason_matcher("property identity", "identity", "lifecycle", "expired",
                        "relisted", "last seen", "missing")),
]
#: An unmatched reason is a gate.
REASON_RANK_GATE = 6
#: Section 7.1 item 7 (the same rank as REASON_PRECEDENCE's last entry).
REASON_RANK_LIFECYCLE = 7


def reason_rank(text: Optional[str]) -> int:
    """Section 7.1's rank for one reason string (1 = primary)."""
    if not text:
        return REASON_RANK_GATE
    for rank, matcher in REASON_PRECEDENCE:
        if matcher.search(str(text)):
            return rank
    return REASON_RANK_GATE


def ordered_reasons(reasons: Any) -> List[Tuple[int, str]]:
    """(rank, reason) pairs in section 7.1 order: rank, then original order.

    `sorted` is stable, so two reasons of the same rank keep the order they were
    collected in, which is the order the evaluation stored them.
    """
    texts = [str(r) for r in (reasons or []) if r]
    return sorted(((reason_rank(t), t) for t in texts), key=lambda pair: pair[0])


def _link_state_block(rows: Any, dimension: str, label: str) -> Dict[str, Any]:
    """One dimension of a link-state cross-tab, from grouped audit rows.

    `rows` are `(dimension, linked, n)` count rows off one `GROUP BY`; the block
    is `{total, linked, unlinked, <label>: {key: {total, linked, unlinked}}}`.
    Sources and evaluations are summarised with the same shape so their readers
    (and the tests) need one form, not two. A NULL dimension value is reported
    under `AUDIT_UNSPECIFIED` rather than dropped, so the per-key totals always
    add up to the block's own total.
    """
    pairs: Dict[str, List[int]] = {}
    for row in rows:
        key = row[dimension]
        slot = pairs.setdefault(AUDIT_UNSPECIFIED if key is None else str(key),
                                [0, 0])
        slot[0 if row["linked"] else 1] += int(row["n"])
    by_key = {key: {"total": counts[0] + counts[1], "linked": counts[0],
                    "unlinked": counts[1]}
              for key, counts in sorted(pairs.items())}
    return {"total": sum(v["total"] for v in by_key.values()),
            "linked": sum(v["linked"] for v in by_key.values()),
            "unlinked": sum(v["unlinked"] for v in by_key.values()),
            label: by_key}


def _ts_desc(ts: Optional[str]) -> float:
    if not ts:
        return float("inf")
    try:
        return -datetime.datetime.fromisoformat(str(ts)).timestamp()
    except Exception:
        return float("inf")


def _needs_verification_sort_key(row: Dict[str, Any]) -> Tuple:
    """Spec 09 Section 9.1:
    lifecycle bucket: active/relisted (0), missing once (1), missing twice (2), other (3)
    decision relevance: monthly card price <= cap or unknown (0) before over-cap (1)
    last_seen_at descending
    provisional_score descending, NULL last
    source_listing_id ascending
    """
    lifecycle = row.get("lifecycle_state")
    misses = int(row.get("consecutive_misses") or 0)
    if lifecycle in ("active", "relisted"):
        bucket = 0
    elif lifecycle == "missing":
        bucket = 1 if misses <= 1 else 2
    else:
        bucket = 3

    card_price = row.get("card_price")
    rent_cap = getattr(config, "RENT_CAP_MAX", 9500)
    relevance = 1 if (card_price is not None and card_price > rent_cap) else 0

    last_seen = _ts_desc(row.get("last_seen_at"))

    score = row.get("provisional_score") if row.get("provisional_score") is not None else row.get("score")
    score_val = -float(score) if score is not None else float("inf")

    src_id = int(row.get("source_listing_id") or 0)

    return (
        bucket,
        relevance,
        last_seen,
        score_val,
        src_id,
    )


def _recently_gone_sort_key(row: Dict[str, Any]) -> Tuple:
    """Spec 09 Section 9.2:
    explicit_gone_at / expired_at newest first
    last_seen_at newest first
    source_listing_id ascending
    """
    gone_stamp = row.get("explicit_gone_at") or row.get("expired_at")
    return (
        _ts_desc(gone_stamp),
        _ts_desc(row.get("last_seen_at")),
        int(row.get("source_listing_id") or 0),
    )


def _excluded_sort_key(row: Dict[str, Any]) -> Tuple:
    """Spec 09 Section 9.2:
    latest observation / last seen descending
    source_listing_id ascending
    """
    obs_stamp = row.get("observed_at") or row.get("last_seen_at")
    return (
        _ts_desc(obs_stamp),
        int(row.get("source_listing_id") or 0),
    )


class Store:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
        self._migrate_schema()

    def _init_tables(self):
        # Legacy compatibility tables
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                site TEXT, listing_id TEXT, first_seen_at TEXT,
                PRIMARY KEY (site, listing_id)
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT, listing_id TEXT, tier TEXT, badge TEXT,
                title TEXT, address TEXT, rent_base REAL, utilities_amount REAL,
                rent_total REAL, move_in_price REAL, nearest_station TEXT,
                straight_dist_m REAL, distance_m REAL, walk_min INTEGER, bike_min INTEGER,
                available_from TEXT, rental_period TEXT, url TEXT, photo_url TEXT,
                kitchen_status TEXT, is_sydhavn INTEGER DEFAULT 0,
                issues TEXT, alert_text TEXT, first_seen_at TEXT
            )""")

        # v2.1 Normalized tables
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bw_id TEXT UNIQUE,
                normalized_address TEXT,
                lat REAL,
                lon REAL,
                rooms INTEGER,
                size_m2 REAL,
                merge_confidence TEXT,
                first_seen_at TEXT,
                last_seen_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS source_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id INTEGER REFERENCES properties(id),
                site TEXT,
                listing_id TEXT,
                url TEXT,
                lifecycle_state TEXT DEFAULT 'active',
                first_seen_at TEXT,
                last_seen_at TEXT,
                consecutive_misses INTEGER DEFAULT 0,
                raw_data TEXT,
                eligibility_text TEXT,
                eligibility_source_url TEXT,
                eligibility_evidence_state TEXT,
                eligibility_checked_at TEXT,
                UNIQUE(site, listing_id)
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_listing_id INTEGER REFERENCES source_listings(id),
                property_id INTEGER REFERENCES properties(id),
                scan_run_id INTEGER,
                observed_at TEXT,
                rent_base INTEGER,
                utilities_amount INTEGER,
                mandatory_monthly_fees INTEGER,
                rent_total INTEGER,
                deposit INTEGER,
                prepaid_rent INTEGER,
                first_month_charge INTEGER,
                mandatory_onetime_fees INTEGER,
                move_in_cost INTEGER,
                move_in_cost_verified INTEGER,
                available_from TEXT,
                available_from_parsed TEXT,
                rental_period TEXT,
                lease_verified INTEGER,
                content_hash TEXT,
                evidence_confidence INTEGER
            )""")

        # Per-source history lookups run for each discovered card and each
        # exact-unit comparison. Avoid scanning the entire observation history.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_observations_source_id "
            "ON observations(source_listing_id, id)")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT,
                started_at TEXT,
                completed_at TEXT,
                outcome TEXT,
                listings_observed INTEGER DEFAULT 0,
                new_matches INTEGER DEFAULT 0,
                duration_sec REAL,
                failure_detail TEXT
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT,
                first_failed_at TEXT,
                last_failed_at TEXT,
                failure_count INTEGER DEFAULT 0,
                error_message TEXT,
                resolved_at TEXT,
                notified INTEGER DEFAULT 0
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS quarantine (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id INTEGER,
                source_listing_id INTEGER,
                site TEXT,
                listing_id TEXT,
                reasons TEXT,
                first_evaluated_at TEXT,
                last_evaluated_at TEXT,
                resolution_state TEXT DEFAULT 'quarantined'
            )""")

        # v3.0 tables
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS place_features (
                coord_key TEXT PRIMARY KEY,
                commute_min INTEGER,
                commute_route TEXT,
                buzz_score REAL,
                buzz_detail TEXT,
                major_street TEXT,
                major_street_dist_m REAL,
                bus_routes INTEGER,
                water_dist_m REAL,
                water_name TEXT,
                water_kind TEXT,
                computed_at TEXT,
                provider TEXT
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS market_benchmarks (
                segment_key TEXT,
                computed_at TEXT,
                n INTEGER,
                median_kr_m2 REAL,
                p25_kr_m2 REAL,
                p75_kr_m2 REAL,
                PRIMARY KEY (segment_key, computed_at)
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id INTEGER,
                action TEXT,
                note TEXT,
                created_at TEXT
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS possible_duplicates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id_1 INTEGER,
                property_id_2 INTEGER,
                confidence TEXT,
                reason TEXT,
                created_at TEXT
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS building_disqualifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_address TEXT UNIQUE,
                reason TEXT NOT NULL,
                evidence_url TEXT,
                matched_text TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS road_routes (
                coord_key TEXT NOT NULL,
                destination TEXT NOT NULL,
                profile TEXT NOT NULL,
                provider TEXT,
                distance_m REAL,
                calculated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (coord_key, destination, profile)
            )""")

        # ======================================================================
        # v3.5 (spec 06) — coverage, versioned raw evidence, local evaluation
        # ======================================================================
        # One row per area or query a scan tried. A per-area exception is
        # recorded here as status 'failed' instead of being logged and skipped
        # as if the area were empty, which is what expired 68 live listings on
        # 12-14 September.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_areas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_run_id INTEGER REFERENCES scan_runs(id),
                site TEXT,
                area_or_query TEXT,
                status TEXT,
                cards_seen INTEGER DEFAULT 0,
                expected_cards INTEGER,
                pages_loaded INTEGER DEFAULT 0,
                error TEXT,
                started_at TEXT,
                finished_at TEXT
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_areas_run ON scan_areas(scan_run_id)")

        # Versioned raw evidence. Payloads are immutable and deduplicated by
        # content hash, so an unchanged page costs one row however often it is
        # observed, and nothing a portal said is ever overwritten.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS source_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_listing_id INTEGER REFERENCES source_listings(id),
                kind TEXT,
                content_hash TEXT,
                captured_at TEXT,
                scan_run_id INTEGER,
                parser_version TEXT,
                url TEXT,
                payload TEXT,
                UNIQUE(source_listing_id, kind, content_hash)
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_listing "
            "ON source_snapshots(source_listing_id, kind, id)")

        # One row per listing per local evaluation. rejection_history and
        # quarantine are still written from these, so existing readers keep
        # working while the history of verdicts becomes inspectable.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_listing_id INTEGER,
                property_id INTEGER,
                observation_id INTEGER,
                evaluated_at TEXT,
                rules_version TEXT,
                parser_version TEXT,
                outcome TEXT,
                reason TEXT,
                quarantine_reasons TEXT,
                score REAL,
                tier TEXT,
                rank TEXT,
                budget_band TEXT,
                private_kitchen_present TEXT,
                shared_kitchen_present TEXT,
                shared_kitchen_is_the_only_kitchen TEXT,
                kitchen_state TEXT,
                kitchen_evidence_source TEXT,
                kitchen_evidence_excerpt TEXT,
                bathroom_state TEXT,
                is_current INTEGER DEFAULT 1
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evaluations_current "
            "ON evaluations(source_listing_id, is_current)")
        if "unit_facts" not in {r[1] for r in self.conn.execute("PRAGMA table_info(evaluations)")}:
            self.conn.execute("ALTER TABLE evaluations ADD COLUMN unit_facts TEXT")

        # A local refresh records one of these instead of a scan run, so a
        # rescore can never be mistaken for a fresh look at the market.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS local_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                completed_at TEXT,
                rules_version TEXT,
                parser_version TEXT,
                listings_evaluated INTEGER DEFAULT 0,
                listings_skipped INTEGER DEFAULT 0,
                matched INTEGER DEFAULT 0,
                rejected INTEGER DEFAULT 0,
                quarantined INTEGER DEFAULT 0,
                enrich INTEGER DEFAULT 0,
                published INTEGER DEFAULT 0,
                duration_sec REAL,
                detail TEXT
            )""")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS rejection_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id INTEGER,
                source_listing_id INTEGER,
                site TEXT,
                listing_id TEXT,
                reason TEXT NOT NULL,
                evidence_url TEXT,
                evaluated_at TEXT NOT NULL
            )""")

        # ======================================================================
        # v3.6 (spec 07 section 5.2) — reviewed building profiles
        # ======================================================================
        # One row per reviewed exact building (or configured complex). A profile
        # is the only thing that can make a managed-block verdict positive, so
        # it carries its evidence, when it was verified, and whether it still
        # stands. Superseding marks a profile; it never deletes it.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS building_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_key TEXT UNIQUE NOT NULL,
                display_name TEXT,
                normalized_address TEXT,
                complex_key TEXT,
                building_form_state TEXT NOT NULL,
                management_state TEXT NOT NULL,
                operator_name TEXT,
                evidence_url TEXT,
                evidence_excerpt TEXT,
                evidence_kind TEXT NOT NULL,
                verified_at TEXT,
                state TEXT NOT NULL DEFAULT 'active',
                superseded_at TEXT,
                superseded_reason TEXT,
                CHECK (normalized_address IS NOT NULL OR complex_key IS NOT NULL)
            )""")
        # Exact-address and configured-complex lookup only (spec 07 section 5.2).
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_building_profiles_address "
            "ON building_profiles(normalized_address)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_building_profiles_complex "
            "ON building_profiles(complex_key)")

        # ======================================================================
        # v3.8 (spec 09 section 6.1) — search profile
        # ======================================================================
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS search_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                age_years INTEGER,
                updated_at TEXT NOT NULL,
                evidence_source TEXT NOT NULL
            )""")

        self.conn.commit()

    def _migrate_schema(self):
        """Add any missing columns to tables gracefully."""
        # Check matches columns
        cur = self.conn.execute("PRAGMA table_info(matches)")
        matches_cols = {row[1] for row in cur.fetchall()}
        needed_matches_cols = {
            "property_id": "INTEGER",
            "bw_id": "TEXT",
            "category": "INTEGER",
            "score": "REAL",
            "score_breakdown": "TEXT",
            "risk_state": "TEXT",
            "risk_evidence": "TEXT",
            "alert_status": "TEXT",
            "city_distance_m": "REAL",
            "move_in_cost": "INTEGER",
            "move_in_cost_verified": "INTEGER",
            "lease_verified": "INTEGER",
            "kitchen_verified": "INTEGER",
            "bathroom_verified": "INTEGER",
            "rooms_verified": "INTEGER",
            "quarantined": "INTEGER DEFAULT 0",
            "quarantine_reasons": "TEXT",
            # v3.0 additive columns
            "commute_min": "INTEGER",
            "commute_route": "TEXT",
            "size_m2": "REAL",
            "kr_per_m2": "REAL",
            "value_ratio": "REAL",
            "benchmark_median": "REAL",
            "benchmark_segment": "TEXT",
            "buzz_score": "REAL",
            "buzz_detail": "TEXT",
            "major_street": "TEXT",
            "bus_routes": "INTEGER",
            "water_dist_m": "REAL",
            "water_name": "TEXT",
            "relevance": "TEXT",
            "alerted_actionable": "INTEGER DEFAULT 0",
            "geo_precision": "TEXT",
            "features_pending": "INTEGER DEFAULT 0",
            # v3.1: 'within' or 'stretch' (rent above the tier ceiling but inside
            # config.STRETCH_RENT_MAX). Older rows read NULL and are treated as
            # 'within' by every consumer.
            "budget_band": "TEXT",
            # v3.1: ISO timestamp of the last instant alert for this property,
            # so a building that rotates listings cannot ping every day.
            "last_alert_at": "TEXT",
            # v3.3 additive columns
            "transit_walk_m": "REAL",            # routed walk to nearest S-train
            "transit_station": "TEXT",           # nearest S-train name
            "norreport_distance_m": "REAL",      # straight-line to Nørreport
            "norreport_road_distance_m": "REAL",
            "norreport_road_provider": "TEXT",
            "norreport_road_status": "TEXT",
            "used_transit_walk_m": "REAL",       # metro walk (normal) or S-train walk (RP)
            "is_responsible_pick": "INTEGER DEFAULT 0",
            "complex_key": "TEXT",               # building/complex group id (None = standalone)
        }
        for col, col_type in needed_matches_cols.items():
            if col not in matches_cols:
                try:
                    self.conn.execute(f"ALTER TABLE matches ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur = self.conn.execute("PRAGMA table_info(properties)")
        prop_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {"postcode": "TEXT", "geo_precision": "TEXT"}.items():
            if col not in prop_cols:
                try:
                    self.conn.execute(f"ALTER TABLE properties ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur = self.conn.execute("PRAGMA table_info(source_listings)")
        source_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            "eligibility_text": "TEXT",
            "eligibility_source_url": "TEXT",
            "eligibility_evidence_state": "TEXT",
            "eligibility_checked_at": "TEXT",
        }.items():
            if col not in source_cols:
                try:
                    self.conn.execute(f"ALTER TABLE source_listings ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        # ---- v3.5 (spec 06). Every change here is ADD COLUMN, never a rewrite.
        cur = self.conn.execute("PRAGMA table_info(scan_runs)")
        scan_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            # 'scheduled' (a coverage scan) or 'targeted' (an explicit detail
            # fetch). Only a scheduled run may move a lifecycle state.
            "kind": "TEXT",
            "parser_version": "TEXT",
            "coverage_status": "TEXT",
            "cards_seen": "INTEGER",
            "cards_new": "INTEGER",
            "details_attempted": "INTEGER",
            "details_succeeded": "INTEGER",
            "details_failed": "INTEGER",
            "details_skipped": "INTEGER",
            "areas_total": "INTEGER",
            "areas_ok": "INTEGER",
            "areas_failed": "INTEGER",
            "selection": "TEXT",
        }.items():
            if col not in scan_cols:
                try:
                    self.conn.execute(f"ALTER TABLE scan_runs ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur = self.conn.execute("PRAGMA table_info(source_listings)")
        src_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            "card_class": "TEXT",
            "detail_state": "TEXT",
            "detail_fetched_at": "TEXT",
            "card_hash": "TEXT",
            "detail_hash": "TEXT",
            "last_complete_scan_id": "INTEGER",
            "expired_at": "TEXT",
            "card_price": "INTEGER",
            "card_text": "TEXT",
        }.items():
            if col not in src_cols:
                try:
                    self.conn.execute(f"ALTER TABLE source_listings ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        # ---- v3.6 (spec 07 section 5.1). Additive only: these are the latest
        # parsed trust facts for the read model, never a rewrite of a table.
        # Immutable source evidence continues to live in source_snapshots.
        cur = self.conn.execute("PRAGMA table_info(source_listings)")
        trust_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            "unit_type_state": "TEXT",
            "unit_type_evidence_source": "TEXT",
            "unit_type_evidence_excerpt": "TEXT",
            "tenancy_channel_state": "TEXT",
            "landlord_name": "TEXT",
            "landlord_evidence_source": "TEXT",
            "landlord_evidence_excerpt": "TEXT",
            "building_form_state": "TEXT",
            "building_profile_id": "INTEGER",
        }.items():
            if col not in trust_cols:
                try:
                    self.conn.execute(f"ALTER TABLE source_listings ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        # ---- v3.6 (spec 07 section 5.3). The three state fields plus the trust
        # verdict. `outcome` keeps its match | quarantined | rejected values for
        # compatibility; trust_outcome is the trust gate's own answer.
        cur = self.conn.execute("PRAGMA table_info(evaluations)")
        eval_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            "unit_type_state": "TEXT",
            "tenancy_channel_state": "TEXT",
            "building_form_state": "TEXT",
            "trust_outcome": "TEXT",
            "trust_reason": "TEXT",
        }.items():
            if col not in eval_cols:
                try:
                    self.conn.execute(f"ALTER TABLE evaluations ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur = self.conn.execute("PRAGMA table_info(building_disqualifications)")
        bd_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            # A building verdict is durable because its evidence was a portal
            # page, but the RULES that read that page can change. A record the
            # current rules no longer support is marked superseded, never
            # deleted, so the history of the verdict survives (spec 06 7.2).
            "state": "TEXT",
            "superseded_at": "TEXT",
            "superseded_reason": "TEXT",
        }.items():
            if col not in bd_cols:
                try:
                    self.conn.execute(
                        f"ALTER TABLE building_disqualifications ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        # Rows created before the state column existed are current verdicts.
        # Backfill every legacy row, not only records encountered again during
        # a later scan: otherwise unverifiable legacy addresses remain NULL
        # indefinitely and the dashboard cannot distinguish active history
        # from superseded history.
        self.conn.execute(
            "UPDATE building_disqualifications SET state='active' WHERE state IS NULL")

        cur = self.conn.execute("PRAGMA table_info(observations)")
        obs_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            "card_snapshot_id": "INTEGER",
            "detail_snapshot_id": "INTEGER",
            "parser_version": "TEXT",
            "detail_state": "TEXT",
        }.items():
            if col not in obs_cols:
                try:
                    self.conn.execute(f"ALTER TABLE observations ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur = self.conn.execute("PRAGMA table_info(matches)")
        match_cols2 = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            # Read-model freshness and evidence fields (spec 06 section 9).
            "detail_state": "TEXT",
            "card_class": "TEXT",
            "kitchen_state": "TEXT",
            "private_kitchen_present": "TEXT",
            "shared_kitchen_present": "TEXT",
            "shared_kitchen_is_the_only_kitchen": "TEXT",
            "kitchen_evidence_source": "TEXT",
            "kitchen_evidence_excerpt": "TEXT",
            "bathroom_state": "TEXT",
            "last_local_evaluated_at": "TEXT",
            "evaluation_rules_version": "TEXT",
            "parser_version": "TEXT",
            "observed_at": "TEXT",
            "detail_fetched_at": "TEXT",
            "expired_at": "TEXT",
            "lifecycle_state": "TEXT",
            "last_seen_days_ago": "INTEGER",
            "portal_coverage_status": "TEXT",
            "scan_run_id": "INTEGER",
            "card_snapshot_id": "INTEGER",
            "detail_snapshot_id": "INTEGER",
        }.items():
            if col not in match_cols2:
                try:
                    self.conn.execute(f"ALTER TABLE matches ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        # ---- v3.8 (spec 09 section 5.1, 6.1, 6.2, 8.1, 8.2). Additive schema
        # migrations: personal priority, search profile, structured demographic
        # facts, explicit-gone lifecycle, and decision-revalidation counters.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS search_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                age_years INTEGER,
                updated_at TEXT NOT NULL,
                evidence_source TEXT NOT NULL
            )""")

        cur = self.conn.execute("PRAGMA table_info(properties)")
        prop_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            "rank_priority": "INTEGER NOT NULL DEFAULT 0",
            "rank_priority_kind": "TEXT",
            "rank_priority_note": "TEXT",
            "rank_priority_at": "TEXT",
        }.items():
            if col not in prop_cols:
                try:
                    self.conn.execute(f"ALTER TABLE properties ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur = self.conn.execute("PRAGMA table_info(source_listings)")
        src_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            "student_requirement_state": "TEXT",
            "age_requirement_state": "TEXT",
            "age_min": "INTEGER",
            "age_max": "INTEGER",
            "profile_age_fit": "TEXT",
            "eligibility_fact_excerpt": "TEXT",
            "explicit_gone_at": "TEXT",
            "explicit_gone_marker": "TEXT",
        }.items():
            if col not in src_cols:
                try:
                    self.conn.execute(f"ALTER TABLE source_listings ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur = self.conn.execute("PRAGMA table_info(evaluations)")
        eval_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            "student_requirement_state": "TEXT",
            "age_requirement_state": "TEXT",
            "age_min": "INTEGER",
            "age_max": "INTEGER",
            "profile_age_fit": "TEXT",
            "eligibility_fact_excerpt": "TEXT",
            # v3.11 (spec 10 section 5.3): soft flags / badges as a JSON array.
            "soft_flags": "TEXT",
        }.items():
            if col not in eval_cols:
                try:
                    self.conn.execute(f"ALTER TABLE evaluations ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        # v3.11 (spec 10 section 5.3): trusted cards are read from `matches`, so
        # the current soft flags are denormalised there too.
        cur = self.conn.execute("PRAGMA table_info(matches)")
        match_cols = {row[1] for row in cur.fetchall()}
        if "soft_flags" not in match_cols:
            try:
                self.conn.execute("ALTER TABLE matches ADD COLUMN soft_flags TEXT")
            except Exception:
                pass

        # Decision-revalidation counters on scan_runs (v38f1):
        # decision_revalidation_selected: number of eligible decision-relevant detail pages selected
        # decision_revalidation_succeeded: number of successfully fetched decision detail pages
        # decision_revalidation_failed: number of failed detail fetches during revalidation
        # decision_revalidation_gone: number of detail pages returning an explicit gone status/marker
        cur = self.conn.execute("PRAGMA table_info(scan_runs)")
        scan_cols = {row[1] for row in cur.fetchall()}
        for col, col_type in {
            "decision_revalidation_selected": "INTEGER DEFAULT 0",
            "decision_revalidation_succeeded": "INTEGER DEFAULT 0",
            "decision_revalidation_failed": "INTEGER DEFAULT 0",
            "decision_revalidation_gone": "INTEGER DEFAULT 0",
        }.items():
            if col not in scan_cols:
                try:
                    self.conn.execute(f"ALTER TABLE scan_runs ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        self.conn.commit()
        self._backfill_source_evidence_columns()
        self._backfill_legacy_records()

        # Keep cooldowns when a representative disappears or stops qualifying.
        # Scope is resolved from property addresses at lookup time, so curated
        # complex membership applies equally to older confirmed deliveries.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_delivery_history (
                property_id INTEGER NOT NULL,
                alerted_at TEXT NOT NULL,
                PRIMARY KEY (property_id, alerted_at)
            )
        """)
        self.conn.execute("""
            INSERT OR IGNORE INTO alert_delivery_history (property_id, alerted_at)
            SELECT property_id, last_alert_at FROM matches
            WHERE property_id IS NOT NULL AND last_alert_at IS NOT NULL
        """)

        # Guard the identity link (spec 07 section 5.4). An ordinary update
        # must never clear a non-null property_id to NULL. Early runs before
        # this trigger and the record_source_listing COALESCE existed wiped
        # links; this turns a repeat of that bug into a hard, loud error.
        self.conn.execute("""
            CREATE TRIGGER IF NOT EXISTS source_listings_no_clear_link
            BEFORE UPDATE OF property_id ON source_listings
            FOR EACH ROW
            WHEN OLD.property_id IS NOT NULL AND NEW.property_id IS NULL
            BEGIN
                SELECT RAISE(ABORT,
                    'cannot clear a non-null source_listings.property_id');
            END
        """)
        self.conn.commit()

    def _backfill_source_evidence_columns(self) -> None:
        """Copy v3.4 evidence fields from preserved raw records into columns."""
        cur = self.conn.execute(
            "SELECT id, raw_data FROM source_listings WHERE eligibility_evidence_state IS NULL")
        for row in cur.fetchall():
            try:
                raw = json.loads(row["raw_data"] or "{}") or {}
            except Exception:
                raw = {}
            if not any(raw.get(k) is not None for k in (
                    "eligibility_text", "eligibility_source_url",
                    "eligibility_evidence_state", "eligibility_checked_at")):
                continue
            self.conn.execute("""
                UPDATE source_listings
                SET eligibility_text=?, eligibility_source_url=?,
                    eligibility_evidence_state=?, eligibility_checked_at=?
                WHERE id=?
            """, (raw.get("eligibility_text"), raw.get("eligibility_source_url"),
                   raw.get("eligibility_evidence_state"), raw.get("eligibility_checked_at"), row["id"]))
        self.conn.commit()

    def _backfill_legacy_records(self):
        """Backfill existing legacy rows in matches/seen into properties and source_listings if needed."""
        cur = self.conn.execute("SELECT * FROM matches WHERE bw_id IS NULL OR property_id IS NULL")
        legacy_rows = cur.fetchall()
        now = datetime.datetime.now().isoformat(timespec="seconds")

        for r in legacy_rows:
            site = r["site"]
            lid = r["listing_id"]
            addr = r["address"] or ""
            lat = r["straight_dist_m"]  # If straight dist was lat placeholder or None
            # Create a property entry if none exists
            prop_id = self._find_or_create_property_direct(
                normalized_address=addr,
                lat=None,
                lon=None,
                rooms=1,
                size_m2=None,
                merge_confidence="legacy_backfill",
                seen_time=r["first_seen_at"] or now,
            )
            bw_id = format_bw_id(prop_id)
            # Link source listing
            self.conn.execute("""
                INSERT OR IGNORE INTO source_listings(property_id, site, listing_id, url, lifecycle_state, first_seen_at, last_seen_at)
                VALUES(?,?,?,?,?,?,?)
            """, (prop_id, site, str(lid), r["url"], "active", r["first_seen_at"] or now, r["first_seen_at"] or now))

            # Determine category from tier
            tier = r["tier"]
            cat = 1 if tier == "cat1" else (2 if tier == "cat2" else (3 if tier == "cat3" else 1))
            self.conn.execute("""
                UPDATE matches SET property_id=?, bw_id=?, category=? WHERE id=?
            """, (prop_id, bw_id, cat, r["id"]))

        self.conn.commit()

    def _find_or_create_property_direct(self, normalized_address: str, lat: Optional[float],
                                        lon: Optional[float], rooms: Optional[int],
                                        size_m2: Optional[float], merge_confidence: str,
                                        seen_time: str) -> int:
        cur = self.conn.execute("SELECT id FROM properties WHERE normalized_address=? AND normalized_address != ''", (normalized_address,))
        row = cur.fetchone()
        if row:
            return row["id"]

        cur = self.conn.execute("""
            INSERT INTO properties(normalized_address, lat, lon, rooms, size_m2, merge_confidence, first_seen_at, last_seen_at, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (normalized_address, lat, lon, rooms, size_m2, merge_confidence, seen_time, seen_time, seen_time, seen_time))
        prop_id = cur.lastrowid
        bw_id = format_bw_id(prop_id)
        self.conn.execute("UPDATE properties SET bw_id=? WHERE id=?", (bw_id, prop_id))
        self.conn.commit()
        return prop_id

    # ==========================================================================
    # Cross-Portal Deduplication & Property Resolution
    # ==========================================================================
    def resolve_property(self, addr: str, lat: Optional[float], lon: Optional[float],
                         rooms: Optional[int], size_m2: Optional[float],
                         postcode: Optional[str] = None,
                         geo_precision: Optional[str] = None) -> Tuple[int, str, str]:
        """Resolve listing to an existing or new property record.
        Returns: (property_id: int, bw_id: str, merge_status: 'exact_address'|'geo_merged'|'new')
        """
        now = datetime.datetime.now().isoformat(timespec="seconds")
        clean_addr = " ".join(addr.lower().split()) if addr else ""

        # 1. Exact DAWA address match
        if clean_addr:
            cur = self.conn.execute(
                "SELECT id, bw_id, normalized_address FROM properties WHERE LOWER(normalized_address)=?",
                (clean_addr,)
            )
            row = cur.fetchone()
            if row:
                self.conn.execute("UPDATE properties SET last_seen_at=?, updated_at=? WHERE id=?",
                                  (now, now, row["id"]))
                self._fill_property_gaps(row["id"], lat, lon, rooms, size_m2, postcode, geo_precision)
                return row["id"], row["bw_id"], "exact_address"

        # 2. Conservative Geo + Rooms + Size match (within 25m, equal rooms, size within 2m2)
        if lat is not None and lon is not None:
            cur = self.conn.execute("SELECT id, bw_id, lat, lon, rooms, size_m2 FROM properties WHERE lat IS NOT NULL AND lon IS NOT NULL")
            candidates = cur.fetchall()
            for cand in candidates:
                c_lat, c_lon = cand["lat"], cand["lon"]
                dist = hav(lat, lon, c_lat, c_lon)
                if dist <= 25.0:
                    cand_rooms = cand["rooms"]
                    cand_size = cand["size_m2"]
                    # Equal rooms
                    rooms_match = (rooms is not None and cand_rooms is not None and rooms == cand_rooms)
                    # Size within 2 m2
                    size_match = False
                    if size_m2 is not None and cand_size is not None:
                        size_match = abs(size_m2 - cand_size) <= 2.0
                    elif size_m2 is None and cand_size is None:
                        size_match = True

                    if rooms_match and size_match:
                        # High confidence auto-merge
                        self.conn.execute("UPDATE properties SET last_seen_at=?, updated_at=? WHERE id=?",
                                          (now, now, cand["id"]))
                        self._fill_property_gaps(cand["id"], lat, lon, rooms, size_m2,
                                                 postcode, geo_precision)
                        return cand["id"], cand["bw_id"], "geo_merged"
                    else:
                        # Medium confidence candidate duplicate: record for review, do NOT auto merge
                        self._record_possible_duplicate(cand["id"], None, "medium",
                                                        f"Within {round(dist)}m but rooms/size mismatch")

        # 3. Create new property
        cur = self.conn.execute("""
            INSERT INTO properties(normalized_address, lat, lon, rooms, size_m2, postcode,
                                   geo_precision, merge_confidence, first_seen_at, last_seen_at,
                                   created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """, (addr, lat, lon, rooms, size_m2, postcode, geo_precision, "new", now, now, now, now))
        prop_id = cur.lastrowid
        bw_id = format_bw_id(prop_id)
        self.conn.execute("UPDATE properties SET bw_id=? WHERE id=?", (bw_id, prop_id))
        self.conn.commit()
        return prop_id, bw_id, "new"

    def _fill_property_gaps(self, prop_id: int, lat, lon, rooms, size_m2,
                            postcode, geo_precision) -> None:
        """Backfill facts we have just learned, never overwriting existing ones.

        Benchmarks need size_m2 and postcode on every property we have seen, and
        older rows predate both columns.
        """
        updates, params = [], []
        for col, val in (("lat", lat), ("lon", lon), ("rooms", rooms), ("size_m2", size_m2),
                         ("postcode", postcode), ("geo_precision", geo_precision)):
            if val is not None:
                updates.append(f"{col} = COALESCE({col}, ?)")
                params.append(val)
        if not updates:
            self.conn.commit()
            return
        params.append(prop_id)
        self.conn.execute(f"UPDATE properties SET {', '.join(updates)} WHERE id=?", tuple(params))
        self.conn.commit()

    def _record_possible_duplicate(self, prop_id_1: int, prop_id_2: Optional[int], confidence: str, reason: str):
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            INSERT INTO possible_duplicates(property_id_1, property_id_2, confidence, reason, created_at)
            VALUES(?,?,?,?,?)
        """, (prop_id_1, prop_id_2, confidence, reason, now))
        self.conn.commit()

    # ==========================================================================
    # Source Listings & Observations
    # ==========================================================================
    # --- v3.5 raw-history helpers ------------------------------------------
    @staticmethod
    def _merge_raw(old: Optional[Dict[str, Any]],
                   new: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Latest merged record for the read model (spec 06 section 5.1).

        raw_data used to be replaced outright on every scan. Under the wide net
        a discovery-only pass writes a card record with no detail fields, so a
        straight replace would wipe the rent, description and eligibility
        evidence a detail fetch had already established. Merge instead: new
        values win, but a key the new record does not carry keeps its old value.
        History itself lives in source_snapshots, never here.
        """
        merged = dict(old or {})
        for key, value in (new or {}).items():
            if value is None and key in merged and merged[key] is not None:
                continue
            merged[key] = value
        return merged

    @staticmethod
    def snapshot_hash(payload: Any) -> str:
        """Stable content hash for a card or detail payload."""
        if isinstance(payload, (dict, list)):
            blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        else:
            blob = str(payload or "")
        blob = " ".join(blob.split())
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def save_snapshot(self, source_listing_id: int, kind: str, payload: Any,
                      url: Optional[str] = None, scan_run_id: Optional[int] = None,
                      parser_version: Optional[str] = None,
                      captured_at: Optional[str] = None) -> Tuple[Optional[int], bool]:
        """Store one immutable card/detail payload, deduplicated by hash.

        Returns (snapshot_id, is_new). A payload identical to the latest
        snapshot of the same kind reuses that row, so two scans of an unchanged
        page produce one snapshot and two observations pointing at it.
        """
        if source_listing_id is None or payload in (None, ""):
            return None, False
        content_hash = self.snapshot_hash(payload)
        latest = self.latest_snapshot(source_listing_id, kind)
        if latest and latest.get("content_hash") == content_hash:
            return latest["id"], False
        cur = self.conn.execute(
            "SELECT id FROM source_snapshots WHERE source_listing_id=? AND kind=? "
            "AND content_hash=?", (source_listing_id, kind, content_hash))
        existing = cur.fetchone()
        if existing:
            # The page reverted to a state we already hold. Payloads are
            # immutable, so the stored row is reused rather than rewritten.
            return existing["id"], False
        now = captured_at or datetime.datetime.now().isoformat(timespec="seconds")
        text = (payload if isinstance(payload, str)
                else json.dumps(payload, ensure_ascii=False, default=str))
        cur = self.conn.execute("""
            INSERT INTO source_snapshots(
                source_listing_id, kind, content_hash, captured_at, scan_run_id,
                parser_version, url, payload)
            VALUES(?,?,?,?,?,?,?,?)
        """, (source_listing_id, kind, content_hash, now, scan_run_id,
               parser_version or getattr(config, "PARSER_VERSION", None), url, text))
        self.conn.commit()
        return cur.lastrowid, True

    def latest_snapshot(self, source_listing_id: int,
                        kind: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM source_snapshots WHERE source_listing_id=? AND kind=? "
            "ORDER BY id DESC LIMIT 1", (source_listing_id, kind))
        row = cur.fetchone()
        return dict(row) if row else None

    def latest_detail_hydrations(self) -> Dict[int, Dict[str, Any]]:
        """Return a mapping of source_listing_id -> hydration dict from latest detail snapshots."""
        cur = self.conn.execute("""
            SELECT ss.source_listing_id, ss.payload
            FROM source_snapshots ss
            INNER JOIN (
                SELECT source_listing_id, MAX(id) AS max_id
                FROM source_snapshots
                WHERE kind = 'detail'
                GROUP BY source_listing_id
            ) latest ON ss.id = latest.max_id
        """)
        out: Dict[int, Dict[str, Any]] = {}
        for sid, payload in cur.fetchall():
            try:
                data = json.loads(payload)
                if isinstance(data, dict) and isinstance(data.get("hydration"), dict):
                    out[sid] = data["hydration"]
            except Exception:
                pass
        return out

    def snapshot_count(self, source_listing_id: int, kind: Optional[str] = None) -> int:
        if kind:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM source_snapshots WHERE source_listing_id=? AND kind=?",
                (source_listing_id, kind))
        else:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM source_snapshots WHERE source_listing_id=?",
                (source_listing_id,))
        return cur.fetchone()[0]

    def record_source_listing(self, property_id: int, site: str, listing_id: str, url: str,
                              raw_data: Optional[Dict[str, Any]] = None,
                              card_class: Optional[str] = None,
                              detail_state: Optional[str] = None,
                              detail_fetched_at: Optional[str] = None,
                              card_hash: Optional[str] = None,
                              detail_hash: Optional[str] = None,
                              card_price: Optional[int] = None,
                              card_text: Optional[str] = None) -> int:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            "SELECT id, lifecycle_state, raw_data, card_class, detail_state, "
            "detail_fetched_at, card_hash, detail_hash, card_price, card_text "
            "FROM source_listings WHERE site=? AND listing_id=?",
            (site, str(listing_id)))
        row = cur.fetchone()

        if row:
            try:
                previous = json.loads(row["raw_data"] or "{}") or {}
            except Exception:
                previous = {}
            merged = self._merge_raw(previous, raw_data)
            raw_str = json.dumps(merged, ensure_ascii=False) if merged else ""
            source_id = row["id"]
            new_state = "relisted" if row["lifecycle_state"] == "expired" else "active"
            # property_id=None means "this card adds no new identity
            # information", never "erase the existing link". A discovery-only
            # card pass must not wipe the property link a detail fetch already
            # established (spec 07 section 6.1). COALESCE keeps the existing
            # non-null id; an explicit non-null id still performs a merge.
            self.conn.execute("""
                UPDATE source_listings
                SET property_id=COALESCE(?, property_id), url=?, lifecycle_state=?, last_seen_at=?,
                    consecutive_misses=0, expired_at=NULL, raw_data=?,
                    eligibility_text=?, eligibility_source_url=?,
                    eligibility_evidence_state=?, eligibility_checked_at=?,
                    card_class=?, detail_state=?, detail_fetched_at=?,
                    card_hash=?, detail_hash=?, card_price=?, card_text=?
                WHERE id=?
            """, (property_id, url, new_state, now, raw_str,
                   merged.get("eligibility_text"),
                   merged.get("eligibility_source_url") or url,
                   merged.get("eligibility_evidence_state"),
                   merged.get("eligibility_checked_at"),
                   card_class if card_class is not None else row["card_class"],
                   detail_state if detail_state is not None else row["detail_state"],
                   detail_fetched_at if detail_fetched_at is not None else row["detail_fetched_at"],
                   card_hash if card_hash is not None else row["card_hash"],
                   detail_hash if detail_hash is not None else row["detail_hash"],
                   card_price if card_price is not None else row["card_price"],
                   card_text if card_text is not None else row["card_text"],
                   source_id))
            self.conn.commit()
            return source_id

        raw_str = json.dumps(raw_data, ensure_ascii=False) if raw_data else ""
        raw_data = raw_data or {}
        cur = self.conn.execute("""
            INSERT INTO source_listings(
                property_id, site, listing_id, url, lifecycle_state, first_seen_at,
                last_seen_at, consecutive_misses, raw_data, eligibility_text,
                eligibility_source_url, eligibility_evidence_state, eligibility_checked_at,
                card_class, detail_state, detail_fetched_at, card_hash, detail_hash,
                card_price, card_text)
            VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (property_id, site, str(listing_id), url, "active", now, now, raw_str,
               raw_data.get("eligibility_text"),
               raw_data.get("eligibility_source_url") or url,
               raw_data.get("eligibility_evidence_state"),
               raw_data.get("eligibility_checked_at"),
               card_class, detail_state, detail_fetched_at, card_hash, detail_hash,
               card_price, card_text))
        self.conn.commit()
        return cur.lastrowid

    def get_source_listing(self, site: str, listing_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM source_listings WHERE site=? AND listing_id=?",
            (site, str(listing_id)))
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Spec 07 section 5.1 — latest source trust facts                     #
    # ------------------------------------------------------------------ #
    def record_source_trust(self, site: str, listing_id: str,
                            unit_type_state: Optional[str] = None,
                            unit_type_evidence_source: Optional[str] = None,
                            unit_type_evidence_excerpt: Optional[str] = None,
                            tenancy_channel_state: Optional[str] = None,
                            landlord_name: Optional[str] = None,
                            landlord_evidence_source: Optional[str] = None,
                            landlord_evidence_excerpt: Optional[str] = None,
                            building_form_state: Optional[str] = None,
                            building_profile_id: Optional[int] = None
                            ) -> Optional[int]:
        """Store the latest parsed trust facts for one source listing.

        A None value means "this pass established nothing new about that fact",
        never "erase the fact": the unit-type, tenancy and building classifiers
        each write their own group, so one of them cannot blank out another's
        evidence. Evidence excerpts are bounded to 200 characters (section
        5.1). Returns the source listing id, or None when the listing is not
        stored.
        """
        row = self.conn.execute(
            "SELECT id FROM source_listings WHERE site=? AND listing_id=?",
            (site, str(listing_id))).fetchone()
        if not row:
            return None
        self.conn.execute("""
            UPDATE source_listings SET
                unit_type_state=COALESCE(?, unit_type_state),
                unit_type_evidence_source=COALESCE(?, unit_type_evidence_source),
                unit_type_evidence_excerpt=COALESCE(?, unit_type_evidence_excerpt),
                tenancy_channel_state=COALESCE(?, tenancy_channel_state),
                landlord_name=COALESCE(?, landlord_name),
                landlord_evidence_source=COALESCE(?, landlord_evidence_source),
                landlord_evidence_excerpt=COALESCE(?, landlord_evidence_excerpt),
                building_form_state=COALESCE(?, building_form_state),
                building_profile_id=COALESCE(?, building_profile_id)
            WHERE id=?
        """, (unit_type_state, unit_type_evidence_source,
              _excerpt(unit_type_evidence_excerpt), tenancy_channel_state,
              landlord_name, landlord_evidence_source,
              _excerpt(landlord_evidence_excerpt), building_form_state,
              building_profile_id, row["id"]))
        self.conn.commit()
        return row["id"]

    # ------------------------------------------------------------------ #
    # Spec 09 section 6.2 — latest source demographic facts               #
    # ------------------------------------------------------------------ #
    def record_source_eligibility(self, site: str, listing_id: str,
                                  student_requirement_state: Optional[str] = None,
                                  age_requirement_state: Optional[str] = None,
                                  age_min: Optional[int] = None,
                                  age_max: Optional[int] = None,
                                  profile_age_fit: Optional[str] = None,
                                  eligibility_fact_excerpt: Optional[str] = None
                                  ) -> Optional[int]:
        """Store the latest parsed demographic facts for one source listing.

        A None value means "this pass established nothing new about that fact",
        never "erase the fact". Evidence excerpts are bounded to 200 characters.
        Returns the source listing id, or None when the listing is not stored.
        """
        row = self.conn.execute(
            "SELECT id FROM source_listings WHERE site=? AND listing_id=?",
            (site, str(listing_id))).fetchone()
        if not row:
            return None
        self.conn.execute("""
            UPDATE source_listings SET
                student_requirement_state=COALESCE(?, student_requirement_state),
                age_requirement_state=COALESCE(?, age_requirement_state),
                age_min=COALESCE(?, age_min),
                age_max=COALESCE(?, age_max),
                profile_age_fit=COALESCE(?, profile_age_fit),
                eligibility_fact_excerpt=COALESCE(?, eligibility_fact_excerpt)
            WHERE id=?
        """, (student_requirement_state, age_requirement_state,
              age_min, age_max, profile_age_fit,
              _excerpt(eligibility_fact_excerpt), row["id"]))
        self.conn.commit()
        return row["id"]

    # ------------------------------------------------------------------ #
    # Spec 09 section 8.2 — explicit gone lifecycle                      #
    # ------------------------------------------------------------------ #
    def record_source_explicit_gone(self, site: str, listing_id: str,
                                    marker: Optional[str] = None,
                                    timestamp: Optional[str] = None) -> Optional[int]:
        """Mark a source listing explicitly gone from own-detail check (Spec 09 section 8.2)."""
        now = timestamp or datetime.datetime.now().isoformat(timespec="seconds")
        marker_safe = str(marker)[:100] if marker else "gone"
        row = self.conn.execute(
            "SELECT id FROM source_listings WHERE site=? AND listing_id=?",
            (site, str(listing_id))).fetchone()
        if not row:
            return None
        self.conn.execute("""
            UPDATE source_listings SET
                detail_state='failed:gone',
                explicit_gone_at=COALESCE(explicit_gone_at, ?),
                explicit_gone_marker=?
            WHERE id=?
        """, (now, marker_safe, row["id"]))
        self.conn.commit()
        return row["id"]

    def clear_source_explicit_gone(self, site: str, listing_id: str) -> Optional[int]:
        """Clear explicit-gone only on a successful recovered own-detail page (Spec 09 section 8.2)."""
        row = self.conn.execute(
            "SELECT id FROM source_listings WHERE site=? AND listing_id=?",
            (site, str(listing_id))).fetchone()
        if not row:
            return None
        self.conn.execute("""
            UPDATE source_listings SET
                explicit_gone_at=NULL,
                explicit_gone_marker=NULL,
                lifecycle_state=CASE WHEN lifecycle_state='expired' THEN 'relisted' ELSE lifecycle_state END
            WHERE id=?
        """, (row["id"],))
        self.conn.commit()
        return row["id"]

    # ------------------------------------------------------------------ #
    # Spec 07 section 6.1 — source-link repair                            #
    # ------------------------------------------------------------------ #
    def _peek_resolve(self, addr: Optional[str], lat: Optional[float],
                      lon: Optional[float], rooms, size_m2) -> Tuple[Optional[int], str]:
        """Read-only resolve_property mirror for a --dry-run repair.

        Returns (property_id_or_None, merge_status). 'new' means a real
        repair would create a property; 'exact_address'/'geo_merged' mean it
        would relink to an existing one. Nothing is written.
        """
        clean_addr = " ".join((addr or "").lower().split())
        if clean_addr:
            cur = self.conn.execute(
                "SELECT id FROM properties WHERE LOWER(normalized_address)=?",
                (clean_addr,))
            row = cur.fetchone()
            if row:
                return row["id"], "exact_address"
        if lat is not None and lon is not None:
            for cand in self.conn.execute(
                    "SELECT id, lat, lon, rooms, size_m2 FROM properties "
                    "WHERE lat IS NOT NULL AND lon IS NOT NULL"):
                if hav(lat, lon, cand["lat"], cand["lon"]) <= 25.0:
                    rooms_match = (rooms is not None and cand["rooms"] is not None
                                   and rooms == cand["rooms"])
                    if size_m2 is not None and cand["size_m2"] is not None:
                        size_match = abs(size_m2 - cand["size_m2"]) <= 2.0
                    elif size_m2 is None and cand["size_m2"] is None:
                        size_match = True
                    else:
                        size_match = False
                    if rooms_match and size_match:
                        return cand["id"], "geo_merged"
        return None, "new"

    def repair_source_links(self, dry_run: bool = False) -> Dict[str, Any]:
        """Relink orphaned source listings to their properties (spec 07 6.1).

        Reconstructs each orphan's latest record from stored raw_data and the
        latest card/detail snapshot — no network call and no alert — then
        relinks it through the existing resolve_property rules, preferring an
        existing exact property over a duplicate. Ambiguous or address-less
        rows are left unlinked with reason 'property identity unresolved';
        never guessed from street proximity. Returns a migration report.
        """
        cutoff = (datetime.datetime.now() - datetime.timedelta(
            days=int(getattr(config, "EXPIRED_VISIBLE_DAYS", 14))
        )).isoformat(timespec="seconds")
        cur = self.conn.execute("""
            SELECT s.id, s.site, s.listing_id, s.lifecycle_state, s.raw_data
            FROM source_listings s
            WHERE s.property_id IS NULL
              AND (s.lifecycle_state IN ('active','missing','relisted')
                   OR (s.lifecycle_state='expired' AND s.expired_at IS NOT NULL
                       AND s.expired_at >= ?))
            ORDER BY s.id
        """, (cutoff,))
        rows = cur.fetchall()

        report = {
            "scanned": 0, "repaired": 0, "unchanged": 0, "unresolved": 0,
            "mapping": [], "unresolved_rows": [], "dry_run": dry_run,
        }
        for row in rows:
            report["scanned"] += 1
            sid = row["id"]
            try:
                rec = json.loads(row["raw_data"] or "{}") or {}
            except Exception:
                rec = {}
            snap = self.latest_snapshot(sid, "detail") or self.latest_snapshot(sid, "card")
            if snap:
                try:
                    payload = json.loads(snap["payload"] or "{}") or {}
                    for k, v in payload.items():
                        rec.setdefault(k, v)
                except Exception:
                    pass

            addr = rec.get("normalized_address") or rec.get("address")
            lat, lon = rec.get("lat"), rec.get("lon")
            has_number = bool(addr and any(ch.isdigit() for ch in addr))
            if not (has_number or (lat is not None and lon is not None)):
                report["unresolved"] += 1
                report["unresolved_rows"].append({
                    "source_listing_id": sid, "site": row["site"],
                    "listing_id": row["listing_id"],
                    "reason": "property identity unresolved"})
                continue

            rooms, size = rec.get("rooms"), rec.get("size_m2")
            if dry_run:
                _pid, status = self._peek_resolve(addr, lat, lon, rooms, size)
                entry = {
                    "source_listing_id": sid, "site": row["site"],
                    "listing_id": row["listing_id"],
                    "old_property_id": None,
                    "new_property_id": _pid,
                    "merge_status": status}
                report["mapping"].append(entry)
                report["unchanged"] += 1  # dry-run writes nothing
                continue

            try:
                new_prop_id, _bw, status = self.resolve_property(
                    addr, lat, lon, rooms, size,
                    rec.get("postcode"), rec.get("geo_precision"))
            except Exception:
                report["unresolved"] += 1
                report["unresolved_rows"].append({
                    "source_listing_id": sid, "site": row["site"],
                    "listing_id": row["listing_id"],
                    "reason": "property identity unresolved"})
                continue
            self.conn.execute(
                "UPDATE source_listings SET property_id=? WHERE id=?",
                (new_prop_id, sid))
            report["repaired"] += 1
            report["mapping"].append({
                "source_listing_id": sid, "site": row["site"],
                "listing_id": row["listing_id"], "old_property_id": None,
                "new_property_id": new_prop_id, "merge_status": status})
            self.conn.commit()
        return report

    # Ranking for spec 06 section 5.3: a card-only observation may not trigger a
    # price event against a detail-based one. Legacy rows carry no detail_state
    # and were all written by a detail fetch, so they rank as detail evidence.
    _DETAIL_STATE_RANK = {"fetched": 2, None: 2, "": 2}

    @classmethod
    def detail_state_rank(cls, detail_state: Optional[str]) -> int:
        return cls._DETAIL_STATE_RANK.get(detail_state, 1)

    def record_observation(self, source_listing_id: int, property_id: int,
                           scan_run_id: Optional[int], rec: Dict[str, Any],
                           eval_res: Any,
                           card_snapshot_id: Optional[int] = None,
                           detail_snapshot_id: Optional[int] = None,
                           detail_state: Optional[str] = None,
                           parser_version: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        """Record an observation and detect price/availability changes.
        Returns: (observation_id: int, changes_detected: dict)
        """
        now = datetime.datetime.now().isoformat(timespec="seconds")
        content_hash = hashlib.md5(json.dumps({
            "rent_total": rec.get("rent_total"),
            "move_in_cost": rec.get("move_in_cost"),
            "available_from": rec.get("available_from"),
        }, sort_keys=True).encode()).hexdigest()

        if detail_state is None:
            detail_state = rec.get("detail_state")
        if parser_version is None:
            parser_version = getattr(config, "PARSER_VERSION", None)

        # Compare against the most recent earlier observation whose evidence is
        # at least as good as this one's. A discovery-only pass reading a card
        # price must not be able to announce a "price drop" against a figure a
        # detail page established.
        own_rank = self.detail_state_rank(detail_state)
        cur = self.conn.execute("""
            SELECT * FROM observations WHERE source_listing_id=?
            ORDER BY id DESC LIMIT 60
        """, (source_listing_id,))
        prev = None
        for candidate in cur.fetchall():
            if self.detail_state_rank(candidate["detail_state"]) >= own_rank:
                prev = candidate
                break

        changes = {}
        if prev:
            prev_rent = prev["rent_total"]
            new_rent = rec.get("rent_total")
            # The selection above guarantees the base is at least as good as
            # this observation; a price event additionally requires that this
            # observation is no worse than the base, so a card price can never
            # announce a "drop" against a figure a detail page established.
            comparable = own_rank >= self.detail_state_rank(prev["detail_state"])
            if comparable and prev_rent is not None and new_rent is not None:
                diff = new_rent - prev_rent
                if abs(diff) >= config.PRICE_CHANGE_THRESHOLD_DKK:
                    changes["price_change"] = {
                        "old_rent": prev_rent,
                        "new_rent": new_rent,
                        "diff": diff,
                        "type": "drop" if diff < 0 else "increase",
                        "old_scan_run_id": prev["scan_run_id"],
                        "new_scan_run_id": scan_run_id,
                    }
            if prev["available_from"] != rec.get("available_from"):
                changes["availability_change"] = {
                    "old": prev["available_from"],
                    "new": rec.get("available_from")
                }

        cur = self.conn.execute("""
            INSERT INTO observations(
                source_listing_id, property_id, scan_run_id, observed_at,
                rent_base, utilities_amount, mandatory_monthly_fees, rent_total,
                deposit, prepaid_rent, first_month_charge, mandatory_onetime_fees,
                move_in_cost, move_in_cost_verified, available_from, available_from_parsed,
                rental_period, lease_verified, content_hash, evidence_confidence,
                card_snapshot_id, detail_snapshot_id, parser_version, detail_state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            source_listing_id, property_id, scan_run_id, now,
            rec.get("rent_base"), rec.get("utilities_amount"), rec.get("mandatory_monthly_fees"), rec.get("rent_total"),
            rec.get("deposit"), rec.get("prepaid_rent"), rec.get("first_month_charge"), rec.get("mandatory_onetime_fees"),
            rec.get("move_in_cost"), 1 if rec.get("move_in_cost_verified") else 0,
            rec.get("available_from"), getattr(eval_res, "available_date_str", None),
            rec.get("rental_period"), 1 if getattr(eval_res, "lease_verified", False) else 0,
            content_hash, getattr(eval_res, "confidence_points", 0),
            card_snapshot_id, detail_snapshot_id, parser_version, detail_state
        ))
        self.conn.commit()
        return cur.lastrowid, changes

    def latest_observation(self, source_listing_id: int) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM observations WHERE source_listing_id=? ORDER BY id DESC LIMIT 1",
            (source_listing_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def update_lifecycle_after_scan(self, site: str, observed_source_ids: List[int],
                                    coverage_status: str = "complete",
                                    scan_run_id: Optional[int] = None) -> Dict[str, int]:
        """Update consecutive misses and transitions to 'missing' or 'expired'.

        Absence needs proof (spec 06 section 3.5). Only a scan that demonstrably
        covered the site may count a listing as missed: `complete`, or
        `empty-confirmed` where the portal itself rendered a "no results" state.
        A partial, failed or unconfirmed-empty scan writes what it did observe
        and leaves every unobserved listing exactly as it found it. The four
        zero-card Lejebolig runs of 12-14 September expired 68 live listings
        precisely because that distinction did not exist.
        """
        cur = self.conn.execute(
            "SELECT id, property_id, lifecycle_state, consecutive_misses "
            "FROM source_listings WHERE site=?", (site,))
        all_sources = cur.fetchall()
        observed_set = set(observed_source_ids)
        now = datetime.datetime.now().isoformat(timespec="seconds")
        transitions = {"missing": 0, "expired": 0, "relisted": 0,
                       "coverage_status": coverage_status,
                       "misses_counted": bool(self.coverage_counts_as_miss(coverage_status))}
        transitions["expired_properties"] = []
        transitions["relisted_properties"] = []
        counts_as_miss = transitions["misses_counted"]
        expire_at = int(getattr(config, "EXPIRED_COMPLETE_MISSES",
                                config.EXPIRED_SCANS_THRESHOLD))

        for row in all_sources:
            sid = row["id"]
            state = row["lifecycle_state"]
            misses = row["consecutive_misses"] or 0

            if sid in observed_set:
                # Any observation, scheduled or targeted, revives a listing.
                if state == "expired":
                    self.conn.execute(
                        "UPDATE source_listings SET lifecycle_state='relisted', "
                        "consecutive_misses=0, expired_at=NULL WHERE id=?", (sid,))
                    transitions["relisted"] += 1
                    transitions["relisted_properties"].append(row["property_id"])
                else:
                    self.conn.execute(
                        "UPDATE source_listings SET lifecycle_state='active', "
                        "consecutive_misses=0, expired_at=NULL WHERE id=?", (sid,))
                if counts_as_miss and scan_run_id is not None:
                    self.conn.execute(
                        "UPDATE source_listings SET last_complete_scan_id=? WHERE id=?",
                        (scan_run_id, sid))
                continue

            if not counts_as_miss:
                continue

            new_misses = misses + 1
            if new_misses >= expire_at:
                if state != "expired":
                    transitions["expired"] += 1
                    transitions["expired_properties"].append(row["property_id"])
                    self.conn.execute(
                        "UPDATE source_listings SET lifecycle_state='expired', "
                        "consecutive_misses=?, expired_at=? WHERE id=?",
                        (new_misses, now, sid))
                else:
                    self.conn.execute(
                        "UPDATE source_listings SET lifecycle_state='expired', "
                        "consecutive_misses=?, expired_at=COALESCE(expired_at, ?) "
                        "WHERE id=?", (new_misses, now, sid))
            elif new_misses >= config.MISSING_SCANS_THRESHOLD:
                if state != "missing":
                    transitions["missing"] += 1
                self.conn.execute(
                    "UPDATE source_listings SET lifecycle_state='missing', "
                    "consecutive_misses=? WHERE id=?", (new_misses, sid))
            else:
                self.conn.execute(
                    "UPDATE source_listings SET consecutive_misses=? WHERE id=?",
                    (new_misses, sid))

        self.conn.commit()
        # A property that has now lost every live listing must not keep a match.
        self.clear_stale_matches()
        return transitions

    @staticmethod
    def coverage_counts_as_miss(coverage_status: Optional[str]) -> bool:
        """Only proven coverage may count a listing as missed (spec 06 4.4)."""
        return str(coverage_status or "complete") in ("complete", "empty-confirmed")

    # ==========================================================================
    # Matches and Quarantine Recording
    # ==========================================================================
    def log_match(self, property_id: int, bw_id: str, rec: Dict[str, Any],
                  eval_res: Any, alert_text: str = "", alert_status: str = "sent") -> int:
        """Insert or update the single match row for a property.

        alerted_actionable is deliberately never written here: it is owned by
        the alerting path so an update cannot silently re-arm an alert.
        """
        now = datetime.datetime.now().isoformat(timespec="seconds")
        route = getattr(eval_res, "commute_route", None)
        soft_flags = []
        for flag in (getattr(eval_res, "soft_flags", None) or []):
            if isinstance(flag, dict):
                soft_flags.append(flag)
            else:
                soft_flags.append({
                    "code": getattr(flag, "code", None),
                    "label": getattr(flag, "label", None),
                    "severity": getattr(flag, "severity", "info"),
                    "evidence": getattr(flag, "evidence", None),
                })

        cols = {
            "site": rec.get("site"),
            "listing_id": str(rec.get("listing_id")),
            "tier": getattr(eval_res, "tier", None) or config.TIER_LETTERS.get(eval_res.category),
            "badge": eval_res.category_name,
            "title": rec.get("title"),
            "address": rec.get("address"),
            "rent_base": rec.get("rent_base"),
            "utilities_amount": rec.get("utilities_amount"),
            "rent_total": eval_res.rent_total,
            "move_in_price": eval_res.move_in_cost,
            "nearest_station": eval_res.nearest_station,
            "straight_dist_m": eval_res.straight_dist_m,
            "distance_m": eval_res.metro_walk_m,
            "walk_min": eval_res.walk_min,
            "bike_min": 0,
            "available_from": eval_res.available_date_str,
            "rental_period": rec.get("rental_period"),
            "url": rec.get("url"),
            "photo_url": rec.get("photo_url"),
            "kitchen_status": "own" if eval_res.kitchen_verified else "unverified",
            "is_sydhavn": 0,
            "issues": "",
            "alert_text": alert_text,
            "category": eval_res.category,
            "score": eval_res.score,
            "score_breakdown": json.dumps(eval_res.score_breakdown, ensure_ascii=False),
            "risk_state": eval_res.risk_state,
            "risk_evidence": json.dumps(eval_res.risk_evidence, ensure_ascii=False),
            "alert_status": alert_status,
            "city_distance_m": eval_res.city_distance_m,
            "move_in_cost": eval_res.move_in_cost,
            "move_in_cost_verified": 1 if eval_res.move_in_cost_verified else 0,
            "lease_verified": 1 if eval_res.lease_verified else 0,
            "kitchen_verified": 1 if eval_res.kitchen_verified else 0,
            "bathroom_verified": 1 if eval_res.bathroom_verified else 0,
            "rooms_verified": 1 if eval_res.rooms_verified else 0,
            "quarantined": 0,
            "quarantine_reasons": "",
            # v3.0
            "commute_min": getattr(eval_res, "commute_min", None),
            "commute_route": json.dumps(route, ensure_ascii=False) if route else None,
            "size_m2": getattr(eval_res, "size_m2", None),
            "kr_per_m2": getattr(eval_res, "kr_per_m2", None),
            "value_ratio": getattr(eval_res, "value_ratio", None),
            "benchmark_median": getattr(eval_res, "benchmark_median", None),
            "benchmark_segment": getattr(eval_res, "benchmark_segment", None),
            "buzz_score": getattr(eval_res, "buzz_score", None),
            "buzz_detail": getattr(eval_res, "buzz_detail", None),
            "major_street": getattr(eval_res, "major_street", None),
            "bus_routes": getattr(eval_res, "bus_routes", None),
            "water_dist_m": getattr(eval_res, "water_dist_m", None),
            "water_name": getattr(eval_res, "water_name", None),
            "relevance": getattr(eval_res, "relevance", None),
            "budget_band": getattr(eval_res, "budget_band", None) or "within",
            "geo_precision": getattr(eval_res, "geo_precision", None),
            "features_pending": int(getattr(eval_res, "features_pending", 0) or 0),
            # v3.3
            "transit_station": getattr(eval_res, "transit_station", None),
            "transit_walk_m": getattr(eval_res, "transit_walk_m", None),
            "norreport_distance_m": getattr(eval_res, "norreport_distance_m", None),
            "norreport_road_distance_m": getattr(eval_res, "norreport_road_distance_m", None),
            "norreport_road_provider": getattr(eval_res, "norreport_road_provider", None),
            "norreport_road_status": getattr(eval_res, "norreport_road_status", None),
            "used_transit_walk_m": getattr(eval_res, "used_transit_walk_m", None),
            "is_responsible_pick": 1 if getattr(eval_res, "is_responsible_pick", False) else 0,
            "complex_key": self.resolve_complex_key(rec.get("address"), rec.get("postcode")),
            # v3.5 read model (spec 06 section 9). Freshness is explicit: when
            # the portal last saw this listing, when its detail page was last
            # read, and when the current rules were last applied are three
            # different facts and are never collapsed into one "updated" time.
            "detail_state": rec.get("detail_state"),
            "card_class": rec.get("card_class"),
            "kitchen_state": getattr(eval_res, "kitchen_state", None),
            "private_kitchen_present": _tristate(
                getattr(eval_res, "private_kitchen_present", None)),
            "shared_kitchen_present": _tristate(
                getattr(eval_res, "shared_kitchen_present", None)),
            "shared_kitchen_is_the_only_kitchen": _tristate(
                getattr(eval_res, "shared_kitchen_is_the_only_kitchen", None)),
            "kitchen_evidence_source": getattr(eval_res, "kitchen_evidence_source", None),
            "kitchen_evidence_excerpt": getattr(eval_res, "kitchen_evidence_excerpt", None),
            "bathroom_state": getattr(eval_res, "bathroom_state", None),
            "soft_flags": json.dumps(soft_flags, ensure_ascii=False),
            "last_local_evaluated_at": now,
            "evaluation_rules_version": rec.get("_rules_version"),
            "parser_version": getattr(config, "PARSER_VERSION", None),
            "observed_at": rec.get("_observed_at"),
            "detail_fetched_at": rec.get("_detail_fetched_at"),
            "expired_at": rec.get("_expired_at"),
            "lifecycle_state": rec.get("_lifecycle_state"),
            "last_seen_days_ago": rec.get("_last_seen_days_ago"),
            "portal_coverage_status": rec.get("_portal_coverage_status"),
        }

        cur = self.conn.execute("SELECT id FROM matches WHERE property_id=?", (property_id,))
        row = cur.fetchone()
        if row:
            assignments = ", ".join(f"{c}=?" for c in cols)
            self.conn.execute(f"UPDATE matches SET {assignments} WHERE id=?",
                              tuple(cols.values()) + (row["id"],))
            self.conn.commit()
            return row["id"]

        cols["property_id"] = property_id
        cols["bw_id"] = bw_id
        cols["first_seen_at"] = now
        names = ", ".join(cols)
        marks = ", ".join("?" * len(cols))
        cur = self.conn.execute(
            f"INSERT INTO matches({names}) VALUES({marks})", tuple(cols.values()))
        self.conn.commit()
        return cur.lastrowid

    def get_match_row(self, property_id: int) -> Optional[Dict[str, Any]]:
        """The current match row for a property, or None if it has never matched."""
        cur = self.conn.execute("SELECT * FROM matches WHERE property_id=?", (property_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def local_scope_candidates(self, include_expired: bool = True) -> List[Dict[str, Any]]:
        """Everything local processing may evaluate (spec 06 section 7.1).

        Active, missing and relisted listings, plus listings expired within
        EXPIRED_VISIBLE_DAYS. An expired row is carried so it can be shown as
        "last seen N days ago"; it is evaluated like any other but never holds a
        building's representative row and never alerts.

        The spec 07 section 5.1 trust facts are selected too, so the local pass
        reconstructs the same classifier inputs the stored row carries: a
        landlord fact written by an earlier pass reaches
        `filters.classify_tenancy_channel`'s stored-fact route, and a building
        state reaches `filters.classify_building_form`'s conflict rule.
        """
        states = ["active", "missing", "relisted"]
        placeholders = ",".join("?" * len(states))
        sql = f"""
            SELECT s.id AS source_listing_id, s.property_id, s.site, s.listing_id,
                   s.url, s.raw_data, s.lifecycle_state, s.expired_at,
                   s.last_seen_at, s.consecutive_misses, s.last_complete_scan_id,
                   s.card_class, s.detail_state, s.detail_fetched_at,
                   s.card_hash, s.detail_hash, s.card_price, s.card_text,
                   s.eligibility_text, s.eligibility_source_url,
                   s.eligibility_evidence_state, s.eligibility_checked_at,
                   s.unit_type_state, s.unit_type_evidence_source,
                   s.unit_type_evidence_excerpt, s.tenancy_channel_state,
                   s.landlord_name, s.landlord_evidence_source,
                   s.landlord_evidence_excerpt, s.building_form_state,
                   s.building_profile_id,
                   s.student_requirement_state, s.age_requirement_state,
                   s.age_min, s.age_max, s.profile_age_fit,
                   s.eligibility_fact_excerpt, s.explicit_gone_at,
                   s.explicit_gone_marker,
                   p.lat, p.lon,
                   p.normalized_address, p.postcode, p.geo_precision
            FROM source_listings s
            LEFT JOIN properties p ON s.property_id = p.id
            WHERE s.lifecycle_state IN ({placeholders})
        """
        params: List[Any] = list(states)
        if include_expired:
            cutoff = (datetime.datetime.now() - datetime.timedelta(
                days=int(getattr(config, "EXPIRED_VISIBLE_DAYS", 14)))
            ).isoformat(timespec="seconds")
            sql += (" OR (s.lifecycle_state='expired' AND s.expired_at IS NOT NULL "
                    "AND s.expired_at >= ?)")
            params.append(cutoff)
        sql += " ORDER BY s.id"

        cur = self.conn.execute(sql, tuple(params))
        out = []
        today = datetime.date.today()
        for r in cur.fetchall():
            d = dict(r)
            raw = d.pop("raw_data", None)
            try:
                d["raw"] = json.loads(raw) if raw else {}
            except Exception:
                d["raw"] = {}
            d["last_seen_days_ago"] = _days_ago(d.get("last_seen_at"), today)
            out.append(d)
        return out

    def rescore_candidates(self) -> List[Dict[str, Any]]:
        """Source listings eligible for a local re-evaluation.

        Kept as the historical name; the scope is spec 06 section 7.1 and now
        includes relisted listings (102 Lejebolig rows were silently outside it)
        and listings expired inside the visible window.
        """
        return self.local_scope_candidates()

    def save_road_route(self, coord_key: str, distance_m: Optional[float],
                        provider: Optional[str], calculated_at: Optional[str],
                        status: str = "failed", profile: str = "driving",
                        destination: str = "norreport") -> None:
        """Persist the audit result for the Nørreport road-distance gate."""
        self.conn.execute("""
            INSERT INTO road_routes(
                coord_key, destination, profile, provider, distance_m,
                calculated_at, status)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(coord_key, destination, profile) DO UPDATE SET
                provider=excluded.provider, distance_m=excluded.distance_m,
                calculated_at=excluded.calculated_at, status=excluded.status
        """, (coord_key, destination, profile, provider, distance_m,
               calculated_at or datetime.datetime.now().isoformat(timespec="seconds"), status))
        self.conn.commit()

    def get_road_route(self, coord_key: str, profile: str = "driving",
                       destination: str = "norreport") -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM road_routes WHERE coord_key=? AND profile=? AND destination=?",
            (coord_key, profile, destination))
        row = cur.fetchone()
        return dict(row) if row else None

    def record_rejection(self, property_id: Optional[int], source_listing_id: Optional[int],
                         site: Optional[str], listing_id: Optional[str], reason: str,
                         evidence_url: Optional[str] = None) -> int:
        """Persist a rejection so a cleared match remains explainable."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute("""
            INSERT INTO rejection_history(
                property_id, source_listing_id, site, listing_id, reason,
                evidence_url, evaluated_at)
            VALUES(?,?,?,?,?,?,?)
        """, (property_id, source_listing_id, site, str(listing_id) if listing_id is not None else None,
               reason, evidence_url, now))
        self.conn.commit()
        return cur.lastrowid

    @staticmethod
    def normalize_exact_address(address: Optional[str]) -> str:
        """Normalize an address for exact-building evidence, without dropping its number."""
        return _fold_complex_text(strip_floor_door(address or ""))

    def record_building_disqualification(self, address: Optional[str], reason: str,
                                         evidence_url: Optional[str] = None,
                                         matched_text: Optional[str] = None) -> Optional[int]:
        """Keep durable housing evidence only for a house-numbered address."""
        if not self.address_is_one_building(address):
            return None
        normalized = self.normalize_exact_address(address)
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            INSERT INTO building_disqualifications(
                normalized_address, reason, evidence_url, matched_text,
                first_seen_at, last_seen_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(normalized_address) DO UPDATE SET
                reason=excluded.reason, evidence_url=excluded.evidence_url,
                matched_text=excluded.matched_text, last_seen_at=excluded.last_seen_at,
                state='active', superseded_at=NULL, superseded_reason=NULL
        """, (normalized, reason, evidence_url, matched_text, now, now))
        self.conn.execute(
            "UPDATE building_disqualifications SET state='active' "
            "WHERE normalized_address=? AND state IS NULL", (normalized,))
        self.conn.commit()
        cur = self.conn.execute(
            "SELECT id FROM building_disqualifications WHERE normalized_address=?",
            (normalized,))
        row = cur.fetchone()
        return row["id"] if row else None

    def building_disqualification_for_property(self, property_id: int) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT normalized_address FROM properties WHERE id=?", (property_id,))
        prop = cur.fetchone()
        if not prop or not self.address_is_one_building(prop["normalized_address"]):
            return None
        cur = self.conn.execute(
            "SELECT * FROM building_disqualifications WHERE normalized_address=? "
            "AND (state IS NULL OR state='active')",
            (self.normalize_exact_address(prop["normalized_address"]),))
        row = cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    def address_is_one_building(address: Optional[str]) -> bool:
        """True when a normalized address carries a house number.

        "Jorisvej 1a, 2300" is one building; "Gammel Køge Landevej, 2500 Valby"
        is a whole street that DAWA could not narrow further, and aggregates
        several buildings that need not share a landlord or a tenancy type.
        """
        head = (address or "").split(",")[0]
        return bool(re.search(r"\d", head))

    def property_housing_disqualification(self, property_id: int) -> Optional[str]:
        """Student or elderly verdict inherited from any sibling listing.

        A building is or is not student housing; it does not depend on which of
        its dozen adverts a scan happened to rank first. Jorisvej 1a held a tier
        A match at 70/100 while two of its own four listings were titled
        "studiebolig", because the match row was held by the one whose title
        stayed quiet.

        Applied only to single-building addresses. On a street-level property
        this would reject genuine flats that merely share a street with a
        student block.
        """
        durable = self.building_disqualification_for_property(property_id)
        if durable:
            return durable["reason"]

        cur = self.conn.execute(
            "SELECT normalized_address FROM properties WHERE id=?", (property_id,))
        row = cur.fetchone()
        if not row or not self.address_is_one_building(row["normalized_address"]):
            return None

        import filters  # local: filters imports geo/features, store must stay light
        cur = self.conn.execute(
            "SELECT raw_data FROM source_listings WHERE property_id=? "
            "AND lifecycle_state IN ('active', 'missing')", (property_id,))
        for r in cur.fetchall():
            try:
                rec = json.loads(r["raw_data"] or "{}") or {}
            except Exception:
                continue
            evidence = filters.housing_disqualification_evidence(rec)
            if evidence:
                return evidence["reason"]
        return None

    def supersede_unsupported_building_disqualifications(self) -> Dict[str, int]:
        """Re-check every durable building verdict against the current rules.

        The evidence is a portal page and stays, but a rule change can mean the
        page no longer says what the record claims. 99 buildings were condemned
        as student housing because BoligPortal renders a fact table reading
        "Kun for studerende  Nej" and the classifier matched the label. Records
        the current rules no longer support become `superseded`; they are never
        deleted, and fresh evidence reinstates them.

        A building whose listings we can no longer read is left alone: we cannot
        show that the rules stopped supporting a verdict we cannot re-test.
        """
        import filters  # local: store must stay importable without the rules

        counts = {"checked": 0, "superseded": 0, "unverifiable": 0, "held": 0}
        by_address: Dict[str, List[int]] = {}
        for row in self.conn.execute(
                "SELECT id, normalized_address FROM properties "
                "WHERE normalized_address IS NOT NULL AND normalized_address != ''"):
            by_address.setdefault(
                self.normalize_exact_address(row["normalized_address"]), []).append(row["id"])

        now = datetime.datetime.now().isoformat(timespec="seconds")
        for record in self.conn.execute(
                "SELECT * FROM building_disqualifications "
                "WHERE state IS NULL OR state='active'").fetchall():
            counts["checked"] += 1
            property_ids = by_address.get(record["normalized_address"]) or []
            if not property_ids:
                counts["unverifiable"] += 1
                continue
            placeholders = ",".join("?" * len(property_ids))
            listings = self.conn.execute(
                f"SELECT raw_data, eligibility_text, eligibility_source_url, url "
                f"FROM source_listings WHERE property_id IN ({placeholders})",
                property_ids).fetchall()
            if not listings:
                counts["unverifiable"] += 1
                continue
            still_supported = False
            for listing in listings:
                try:
                    rec = json.loads(listing["raw_data"] or "{}") or {}
                except Exception:
                    rec = {}
                if listing["eligibility_text"]:
                    rec["eligibility_text"] = listing["eligibility_text"]
                if filters.housing_disqualification_evidence(rec):
                    still_supported = True
                    break
            if still_supported:
                counts["held"] += 1
                continue
            self.conn.execute(
                "UPDATE building_disqualifications SET state='superseded', "
                "superseded_at=?, superseded_reason=? WHERE id=?",
                (now, f"the current rules no longer read {record['matched_text']!r} "
                      f"as {record['reason']}", record["id"]))
            counts["superseded"] += 1
        self.conn.commit()
        return counts

    # ------------------------------------------------------------------ #
    # Spec 07 section 5.2 — reviewed building profiles                    #
    # ------------------------------------------------------------------ #
    def upsert_building_profile(self, profile_key: str, building_form_state: str,
                                management_state: str, evidence_kind: str,
                                display_name: Optional[str] = None,
                                normalized_address: Optional[str] = None,
                                complex_key: Optional[str] = None,
                                operator_name: Optional[str] = None,
                                evidence_url: Optional[str] = None,
                                evidence_excerpt: Optional[str] = None,
                                verified_at: Optional[str] = None) -> int:
        """Create or refresh one reviewed building profile, keyed by profile_key.

        An existing row that carries a standing manual decision is returned
        untouched unless this call is itself a manual write: a parser run cannot
        silently overwrite manual evidence. The caller unlocks it by superseding
        the profile explicitly first (spec 07 section 7.2). An upsert writes the
        complete profile the caller supplied; an existing row it is allowed to
        write is (re)instated active, the way fresh evidence reinstates a
        building verdict. The address is stored in the folded exact form used by
        every other exact-building lookup, so a lookup cannot miss on case or on
        a floor/door suffix.
        """
        key = str(profile_key)
        address = (self.normalize_exact_address(normalized_address)
                   if normalized_address else None)
        existing = self.get_building_profile(key)
        if (existing and existing["evidence_kind"] == "manual"
                and evidence_kind != "manual"
                and (existing["state"] or "active") == "active"):
            return existing["id"]
        fields = (str(building_form_state), str(management_state), str(evidence_kind),
                  display_name, address, complex_key, operator_name,
                  evidence_url, _excerpt(evidence_excerpt), verified_at)
        if existing:
            self.conn.execute("""
                UPDATE building_profiles SET
                    building_form_state=?, management_state=?, evidence_kind=?,
                    display_name=?, normalized_address=?, complex_key=?,
                    operator_name=?, evidence_url=?, evidence_excerpt=?,
                    verified_at=?, state='active', superseded_at=NULL,
                    superseded_reason=NULL
                WHERE profile_key=?
            """, fields + (key,))
            self.conn.commit()
            return existing["id"]
        cur = self.conn.execute("""
            INSERT INTO building_profiles(
                profile_key, building_form_state, management_state, evidence_kind,
                display_name, normalized_address, complex_key, operator_name,
                evidence_url, evidence_excerpt, verified_at, state)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'active')
        """, (key,) + fields)
        self.conn.commit()
        return cur.lastrowid

    def get_building_profile(self, profile_key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM building_profiles WHERE profile_key=?",
            (str(profile_key),)).fetchone()
        return dict(row) if row else None

    def active_building_profile_for_address(self,
                                            normalized_address: str) -> Optional[Dict[str, Any]]:
        """The active profile for one exact house-numbered address, if any."""
        row = self.conn.execute(
            "SELECT * FROM building_profiles WHERE normalized_address=? AND state='active'",
            (self.normalize_exact_address(normalized_address),)).fetchone()
        return dict(row) if row else None

    def active_building_profile_for_complex(self, complex_key: str) -> Optional[Dict[str, Any]]:
        """The active profile for one configured complex key, if any."""
        row = self.conn.execute(
            "SELECT * FROM building_profiles WHERE complex_key=? AND state='active'",
            (complex_key,)).fetchone()
        return dict(row) if row else None

    def supersede_building_profile(self, profile_key: str, reason: str) -> int:
        """Mark a profile superseded. It stays readable; it is never deleted.

        Returns the number of rows changed, so superseding twice is a no-op
        rather than a second, later timestamp.
        """
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            "UPDATE building_profiles SET state='superseded', superseded_at=?, "
            "superseded_reason=? WHERE profile_key=? AND state!='superseded'",
            (now, str(reason), str(profile_key)))
        self.conn.commit()
        return cur.rowcount

    def building_profiles_for_exact_address(self,
                                            normalized_address: str) -> List[Dict[str, Any]]:
        """Every reviewed profile for one exact folded address, in any state.

        `active_building_profile_for_address` answers the active case only. The
        section 4.3 classifier must also be able to say that the profile for this
        exact address was superseded (section 7.2: a superseded profile is
        unknown immediately), so this read keeps the retired rows visible. The
        comparison is the existing exact folded one, so another house number or a
        street-level address cannot match.
        """
        rows = self.conn.execute(
            "SELECT * FROM building_profiles WHERE normalized_address=? ORDER BY id",
            (self.normalize_exact_address(normalized_address),)).fetchall()
        return [dict(r) for r in rows]

    def building_profiles_for_complex(self, complex_key: str) -> List[Dict[str, Any]]:
        """Every reviewed profile for one configured complex key, in any state."""
        rows = self.conn.execute(
            "SELECT * FROM building_profiles WHERE complex_key=? ORDER BY id",
            (str(complex_key),)).fetchall()
        return [dict(r) for r in rows]

    def load_building_profiles_manifest(self, manifest: Dict[str, Any],
                                        dry_run: bool = False) -> Dict[str, Any]:
        """Load a validated profile manifest into `building_profiles`, locally.

        Spec 07 sections 5.2 and 7.2; row v36m1 duty 2. Deterministic and
        idempotent: entries are applied in the manifest's own (stable) order, an
        entry whose stored row already carries identical content is not
        rewritten, and loading one manifest twice changes no row — the second
        pass reports every entry `unchanged` with `rows_written = 0`.

        Nothing is overwritten silently. A standing `evidence_kind = 'manual'`
        profile is preserved (section 7.2: a parser run cannot silently overwrite
        manual evidence), a profile a reviewer superseded is not re-instated, and
        a stored row that differs from the manifest is left exactly as it is.
        Each of those comes back in `conflicts` as a review item carrying the
        differing fields and both values.

        Raises `ValueError` — writing nothing — when the manifest does not
        validate, so a malformed manifest cannot seed a partial set. No URL is
        fetched and nothing outside this database is written.
        """
        problems = validate_building_profile_manifest(manifest)
        if problems:
            raise ValueError("manifest is not loadable: " + "; ".join(problems))
        entries = manifest.get("profiles") or []
        report: Dict[str, Any] = {
            "manifest_version": manifest.get("manifest_version"),
            "source_pack_sha256": manifest.get("source_pack_sha256"),
            "profiles_in_manifest": len(entries),
            "created": [],
            "unchanged": [],
            "conflicts": [],
            "rows_written": 0,
            "dry_run": bool(dry_run),
        }
        for entry in entries:
            key = str(entry["profile_key"])
            desired = {
                "building_form_state": str(entry["building_form_state"]),
                "management_state": str(entry["management_state"]),
                "evidence_kind": str(entry["evidence_kind"]),
                "display_name": entry.get("display_name"),
                "normalized_address": (
                    self.normalize_exact_address(entry["normalized_address"])
                    if entry.get("normalized_address") else None),
                "complex_key": entry.get("complex_key"),
                "operator_name": entry.get("operator_name"),
                "evidence_url": entry.get("evidence_url"),
                "evidence_excerpt": _excerpt(entry.get("evidence_excerpt")),
                "verified_at": entry.get("verified_at"),
                "state": "active",
            }
            if entry.get("state") != "active":
                report["conflicts"].append({
                    "profile_key": key,
                    "reason": f"manifest entry state is {entry.get('state')!r}; "
                              f"only active profiles are loaded",
                    "differences": {},
                })
                continue
            stored = self.get_building_profile(key)
            if stored is None:
                if not dry_run:
                    self.upsert_building_profile(
                        profile_key=key,
                        building_form_state=desired["building_form_state"],
                        management_state=desired["management_state"],
                        evidence_kind=desired["evidence_kind"],
                        display_name=desired["display_name"],
                        normalized_address=entry.get("normalized_address"),
                        complex_key=desired["complex_key"],
                        operator_name=desired["operator_name"],
                        evidence_url=desired["evidence_url"],
                        evidence_excerpt=desired["evidence_excerpt"],
                        verified_at=desired["verified_at"])
                    report["rows_written"] += 1
                report["created"].append(key)
                continue
            differences = {field: {"stored": stored.get(field), "manifest": value}
                           for field, value in desired.items()
                           if stored.get(field) != value}
            if not differences:
                report["unchanged"].append(key)
                continue
            if (stored.get("evidence_kind") == "manual"
                    and entry.get("evidence_kind") != "manual"
                    and (stored.get("state") or "active") == "active"):
                reason = ("stored profile is a standing manual decision; the "
                          "manifest entry was not applied (spec 07 section 7.2: "
                          "a parser run cannot overwrite manual evidence)")
            elif (stored.get("state") or "active") != "active":
                reason = ("stored profile was superseded by a reviewer; "
                          "re-instating it needs review")
            else:
                reason = ("stored profile differs from the manifest entry; left "
                          "as stored for review")
            report["conflicts"].append({"profile_key": key, "reason": reason,
                                        "differences": differences})
        return report

    def seed_building_profiles(self, path: Optional[str] = None,
                               dry_run: bool = False) -> Dict[str, Any]:
        """Read the tracked manifest and load it: the local-migration entry point.

        One call for a migration pass: parse and validate
        `building_profiles_v36.json` (or an explicit `path`) and apply it through
        `load_building_profiles_manifest`, returning the same report. Local only:
        no URL fetched, no alert sent, nothing staged.
        """
        return self.load_building_profiles_manifest(
            read_building_profile_manifest(path), dry_run=dry_run)

    def self_contained_listings_at_building(self, address: Optional[str] = None,
                                            complex_key: Optional[str] = None,
                                            days: Optional[int] = None
                                            ) -> List[Dict[str, Any]]:
        """Distinct self-contained source listings at one exact building/complex.

        Spec 07 section 4.3 bullet 3. The scope is the property rows that fold to
        the exact house-numbered `address` (all of them, so a duplicated property
        row cannot split one building) plus, for a configured `complex_key`, the
        property rows whose resolved complex key equals it. `address_is_one_
        building` refuses an address without a house number and the comparison is
        exact, so another house number, a street-only property and a merely
        nearby building are outside the scope.

        Recency is the listing's own `last_seen_at` inside `days` (default:
        `filters.BUILDING_EVIDENCE_MAX_DAYS`). Each candidate is classified with
        `filters.classify_unit_type`, so a room card is not counted as one of the
        self-contained listings. A null-linked row cannot be placed in a building
        and is not counted; links are repaired before the release refresh
        (spec 07 section 6.1).
        """
        import filters  # local: store must stay importable without the rules
        window = int(days) if days is not None else int(filters.BUILDING_EVIDENCE_MAX_DAYS)
        property_ids = self._property_ids_for_building_scope(address, complex_key)
        if not property_ids:
            return []
        cutoff = (datetime.datetime.now() - datetime.timedelta(
            days=window)).isoformat(timespec="seconds")
        placeholders = ",".join("?" * len(property_ids))
        rows = self.conn.execute(f"""
            SELECT s.id, s.site, s.listing_id, s.url, s.last_seen_at,
                   s.property_id, s.raw_data, s.eligibility_text,
                   s.eligibility_source_url, s.eligibility_evidence_state,
                   s.eligibility_checked_at, s.card_class, s.detail_state,
                   s.card_text, s.card_price, s.unit_type_state,
                   s.unit_type_evidence_source, s.unit_type_evidence_excerpt,
                   p.normalized_address, p.postcode, p.lat, p.lon,
                   p.geo_precision
            FROM source_listings s LEFT JOIN properties p ON s.property_id = p.id
            WHERE s.property_id IN ({placeholders})
              AND s.last_seen_at IS NOT NULL AND s.last_seen_at >= ?
            ORDER BY s.site, s.listing_id
        """, tuple(property_ids) + (cutoff,)).fetchall()
        out: List[Dict[str, Any]] = []
        seen = set()
        for row in rows:
            listing = dict(row)
            key = (listing["site"], str(listing["listing_id"]))
            if key in seen:
                continue
            if filters.classify_unit_type(source_record_view(listing)).state != \
                    "self_contained_apartment":
                continue
            seen.add(key)
            out.append({k: listing[k] for k in
                        ("id", "site", "listing_id", "url", "last_seen_at",
                         "property_id")})
        return out

    def _property_ids_for_building_scope(self, address: Optional[str] = None,
                                         complex_key: Optional[str] = None
                                         ) -> List[int]:
        """Property rows making up one exact house-numbered building or complex."""
        target = None
        if address and self.address_is_one_building(address):
            target = self.normalize_exact_address(address)
        ids: List[int] = []
        for row in self.conn.execute(
                "SELECT id, normalized_address, postcode FROM properties "
                "WHERE normalized_address IS NOT NULL AND normalized_address != ''"):
            if target and self.normalize_exact_address(row["normalized_address"]) == target:
                if row["id"] not in ids:
                    ids.append(row["id"])
                continue
            if complex_key and self.resolve_complex_key(
                    row["normalized_address"], row["postcode"]) == complex_key:
                if row["id"] not in ids:
                    ids.append(row["id"])
        return ids

    def clear_match(self, property_id: int) -> None:
        """Drop a match row that no longer passes, so a rescore cannot leave stale rows."""
        self.conn.execute("DELETE FROM matches WHERE property_id=?", (property_id,))
        self.conn.commit()

    def clear_match_for_listing(self, property_id: int, site: str, listing_id: str) -> None:
        """Drop the match row only if it came from this same source listing.

        One property can be advertised by several listings. A rejection on one of
        them must not delete a match another listing still supports.
        """
        if property_id is None:
            return
        sibling = self.conn.execute("""
            SELECT s.site, s.listing_id, s.url, s.raw_data,
                   e.score, e.tier, e.budget_band, e.outcome, e.trust_outcome
            FROM source_listings s
            JOIN evaluations e ON e.source_listing_id = s.id AND e.is_current = 1
            WHERE s.property_id = ?
              AND (s.site != ? OR s.listing_id != ?)
              AND s.explicit_gone_at IS NULL
              AND s.lifecycle_state IN ('active', 'missing', 'relisted')
              AND e.outcome = 'match' AND e.trust_outcome = 'eligible'
            ORDER BY e.score DESC, s.id ASC
            LIMIT 1
        """, (property_id, site, str(listing_id))).fetchone()

        if sibling:
            self.conn.execute("""
                UPDATE matches
                SET site=?, listing_id=?, url=COALESCE(?, url),
                    score=COALESCE(?, score), tier=COALESCE(?, tier)
                WHERE property_id=? AND site=? AND listing_id=?
            """, (sibling["site"], str(sibling["listing_id"]), sibling["url"],
                  sibling["score"], sibling["tier"], property_id, site, str(listing_id)))
        else:
            self.conn.execute(
                "DELETE FROM matches WHERE property_id=? AND site=? AND listing_id=?",
                (property_id, site, str(listing_id)))
        self.conn.commit()

    def clear_stale_matches(self) -> int:
        """Clear match rows whose property has no live/missing/relisted listing.

        A property whose source listings have all gone expired (or that has no
        source rows at all) must not stay visible as a match: the hard 9,500 kr
        cap and the v3.3 "eligible & live first" rule apply to live units. Called
        from the scan lifecycle update and from `--rescore` so neither path can
        leave a stale row behind.

        Spec 07 section 6.4 (v36i2 duty 2): a `matches` row is a property's
        representative, and only a source whose trust outcome is `eligible` may
        support one. When a property's current evaluations exist and none of
        them is an eligible `match`, the row is stale and is cleared here - a
        review/reject sibling may not hold it. A property with no current
        evaluation at all is an identity/lifecycle state, not a trust state, and
        is left to the lifecycle branch below.

        Spec 09 section 8.3 (v38f1 duty 4): an explicit-gone representative is
        stale. A different active eligible source for the same property may
        become the new representative instead of removing the whole property.
        """
        cur = self.conn.execute(
            "SELECT DISTINCT property_id, site, listing_id FROM matches WHERE property_id IS NOT NULL")
        cleared = 0
        for m_row in cur.fetchall():
            pid = m_row["property_id"]
            cur_site = m_row["site"]
            cur_lid = m_row["listing_id"]

            evaluations = self.conn.execute(
                "SELECT outcome, trust_outcome FROM evaluations WHERE property_id=? "
                "AND is_current=1", (pid,)).fetchall()
            # v3.11 (spec 10): the trust verdict no longer gates a match row, so
            # any current `match` evaluation supports the property.
            if evaluations and not any(
                    row["outcome"] == "match" for row in evaluations):
                self.clear_match(pid)
                cleared += 1
                continue

            # Check if current representative is explicit-gone or expired
            cur_s = None
            if cur_site and cur_lid:
                cur_s = self.conn.execute(
                    "SELECT id, lifecycle_state, explicit_gone_at FROM source_listings "
                    "WHERE site=? AND listing_id=?", (cur_site, str(cur_lid))).fetchone()

            is_stale_rep = cur_s and (cur_s["explicit_gone_at"] or cur_s["lifecycle_state"] == "expired")

            if is_stale_rep:
                # Find if an active eligible sibling source exists for this property
                sibling = self.conn.execute("""
                    SELECT s.id, s.site, s.listing_id, s.url, s.raw_data,
                           e.unit_facts, e.observation_id, e.evaluated_at, e.rules_version, e.score, e.tier, e.budget_band, e.outcome, e.trust_outcome
                    FROM source_listings s
                    JOIN evaluations e ON e.source_listing_id = s.id AND e.is_current = 1
                    WHERE s.property_id = ?
                      AND s.explicit_gone_at IS NULL
                      AND s.lifecycle_state IN ('active', 'missing', 'relisted')
                      AND e.outcome = 'match' AND e.trust_outcome = 'eligible'
                    ORDER BY e.score DESC, s.id ASC
                    LIMIT 1
                """, (pid,)).fetchone()
                if sibling:
                    # Replace every unit fact, not only the link/score. The
                    # previous representative's price/date/size must never
                    # describe the sibling selected here.
                    raw = json.loads(sibling["raw_data"] or "{}")
                    facts = json.loads(sibling["unit_facts"] or "{}")
                    observation = self.conn.execute(
                        "SELECT * FROM observations WHERE id=?",
                        (sibling["observation_id"],)).fetchone()
                    defaults = {name: None for name in (
                        "category", "category_name", "score_breakdown", "risk_state", "risk_evidence",
                        "city_distance_m", "straight_dist_m", "metro_walk_m", "nearest_station", "walk_min",
                        "available_date_str", "rent_total", "move_in_cost", "move_in_cost_verified",
                        "lease_verified", "kitchen_verified", "bathroom_verified", "rooms_verified")}
                    if observation:
                        defaults.update({key: observation[key] for key in (
                            "rent_total", "move_in_cost", "move_in_cost_verified", "lease_verified")})
                        defaults["available_date_str"] = observation["available_from_parsed"]
                    defaults.update(facts)
                    defaults.update(score=sibling["score"], tier=sibling["tier"],
                                    budget_band=sibling["budget_band"])
                    defaults["size_m2"] = facts.get("size_m2", raw.get("size_m2"))
                    raw.update(site=sibling["site"], listing_id=sibling["listing_id"], url=sibling["url"],
                               _rules_version=sibling["rules_version"])
                    existing = self.get_match_row(pid)
                    self.log_match(pid, existing["bw_id"], raw, SimpleNamespace(**defaults),
                                   alert_status="not-sent")
                    continue
                else:
                    self.clear_match(pid)
                    cleared += 1
                    continue

            state = self.lifecycle_state_for(pid)
            if state in ("active", "missing", "relisted"):
                continue
            # A property whose listings have all expired holds no `matches` row.
            # The source is not hidden by clearing it: an expired source inside
            # `config.EXPIRED_VISIBLE_DAYS` is in Recently gone by its window
            # alone (`Store._view_for_source`), where it is labelled "last seen
            # N days ago" with its last decision, and a relist recreates the row
            # through the next local refresh.
            self.clear_match(pid)
            cleared += 1
        return cleared

    def mark_actionable_alerted(self, property_id: int) -> None:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE matches SET alerted_actionable=1, last_alert_at=? WHERE property_id=?",
            (now, property_id))
        self.conn.execute(
            "INSERT OR IGNORE INTO alert_delivery_history VALUES (?, ?)",
            (property_id, now))
        self.conn.commit()

    def suppress_alert_backlog(self, stamp: str) -> int:
        """Mark every existing match row already alerted (spec 08 section 6.4).

        One `UPDATE`, so the whole backlog is suppressed in a single statement:
        matches created by the migration refresh exist before the first live scan
        of the new rules, and decision 6 keeps them out of the one-shot instant
        alert. `last_alert_at` is stamped with the caller's `stamp` - the
        migration's own completed_at, per section 6.4 - and the return value is
        the number of rows this call changed, which is 0 on a second call.

        Idempotent: it only ever matches rows still marked `alerted_actionable=0`
        (or NULL, which is the same "not alerted yet" state), and it never touches
        `user_actions`. The 10:30 digest is not affected: it reads
        `grouped_matches`, not this flag.
        """
        cur = self.conn.execute(
            "UPDATE matches SET alerted_actionable=1, last_alert_at=? "
            "WHERE alerted_actionable=0 OR alerted_actionable IS NULL", (stamp,))
        self.conn.execute("""
            INSERT OR IGNORE INTO alert_delivery_history (property_id, alerted_at)
            SELECT property_id, last_alert_at FROM matches
            WHERE property_id IS NOT NULL AND last_alert_at IS NOT NULL
        """)
        self.conn.commit()
        return int(cur.rowcount)

    def unmark_actionable_alerted(self, property_id: int) -> None:
        """Give the alert back after a failed send, so the next scan retries it.

        last_alert_at is cleared with it: a send that never landed must not start
        a cooldown, or the retry it promises would be muted for a week.
        """
        self.conn.execute(
            "DELETE FROM alert_delivery_history WHERE property_id=? AND alerted_at="
            "(SELECT last_alert_at FROM matches WHERE property_id=?)",
            (property_id, property_id))
        self.conn.execute(
            "UPDATE matches SET alerted_actionable=0, last_alert_at=NULL WHERE property_id=?",
            (property_id,))
        self.conn.commit()

    def alert_cooldown_active(self, property_id: int,
                              days: Optional[int] = None) -> bool:
        """True when this property was alerted on within the cooldown window.

        A building that rotates a dozen listings as tenants move out resolves to
        one property, so without this each fresh listing id reads as a new find.
        """
        if days is None:
            days = getattr(config, "ALERT_PROPERTY_COOLDOWN_DAYS", 0)
        if not days:
            return False
        prop = self.conn.execute(
            "SELECT normalized_address, postcode FROM properties WHERE id=?", (property_id,)).fetchone()
        key = self.resolve_complex_key(prop[0], prop[1]) if prop else None
        members = self._property_ids_for_building_scope(complex_key=key) if key else []
        if property_id not in members:
            members.append(property_id)
        placeholders = ",".join("?" for _ in members)
        cur = self.conn.execute(
            "SELECT MAX(last_alert_at) AS last_alert_at FROM ("
            "SELECT last_alert_at FROM matches WHERE property_id=? "
            "OR (? IS NOT NULL AND complex_key=?) UNION ALL "
            "SELECT alerted_at FROM alert_delivery_history "
            f"WHERE property_id IN ({placeholders}))", (property_id, key, key, *members))
        row = cur.fetchone()
        if not row or not row["last_alert_at"]:
            return False
        try:
            last = datetime.datetime.fromisoformat(str(row["last_alert_at"]))
        except ValueError:
            return False
        return (datetime.datetime.now() - last).days < int(days)

    @staticmethod
    def match_rank(score: Optional[float], budget_band: Optional[str],
                   rent_total: Optional[float]) -> Tuple[float, int, float]:
        """Sort key for competing units of one property. Higher is better.

        Score leads. Units in a building are near-identical, so scores tie often:
        Viola Nørløvs Gade 3 had a 9,350 kr flat and a 9,700 kr flat both on 65,
        and a strictly-greater comparison let whichever was evaluated first hold
        the row. A tie now breaks towards staying inside budget, then towards the
        cheaper rent, so a building is never shown as a stretch when one of its
        own units is affordable.
        """
        within = 0 if (budget_band or "within") == "stretch" else 1
        return (float(score or 0), within, -float(rent_total or 0))

    def better_match_exists(self, property_id: int, site: str, listing_id: str,
                            score: Optional[float],
                            budget_band: Optional[str] = None,
                            rent_total: Optional[float] = None) -> bool:
        """True when the stored match row is a different, still-live, better unit.

        A block of flats is one property with many source listings. Without this
        the match row was simply whichever unit the scan happened to evaluate
        last, so the cheapest flat in a building could never surface.
        """
        cur = self.conn.execute("""
            SELECT m.site, m.listing_id, m.score, m.budget_band, m.rent_total,
                   s.lifecycle_state
            FROM matches m
            LEFT JOIN source_listings s
              ON s.property_id = m.property_id AND s.site = m.site
             AND s.listing_id = m.listing_id
            WHERE m.property_id=?
        """, (property_id,))
        row = cur.fetchone()
        if not row:
            return False
        if row["site"] == site and str(row["listing_id"]) == str(listing_id):
            return False                      # same unit, always allowed to update
        if row["lifecycle_state"] != "active":
            # Gone, or never recorded as a source listing at all. Either way the
            # incumbent cannot be shown to be live, so it does not get to hold
            # the row against a unit we are looking at right now.
            return False
        incumbent = self.match_rank(row["score"], row["budget_band"], row["rent_total"])
        challenger = self.match_rank(score, budget_band, rent_total)
        return incumbent >= challenger

    def property_unit_summary(self, property_id: int) -> Dict[str, Any]:
        """Live unit count and rent span for a property, for the 'N more units' line."""
        cur = self.conn.execute("""
            SELECT raw_data FROM source_listings
            WHERE property_id=? AND lifecycle_state IN ('active', 'missing')
        """, (property_id,))
        rents = []
        n = 0
        for row in cur.fetchall():
            n += 1
            try:
                rent = (json.loads(row["raw_data"] or "{}") or {}).get("rent_total")
            except Exception:
                rent = None
            if rent:
                rents.append(int(rent))
        return {"units": n,
                "rent_min": min(rents) if rents else None,
                "rent_max": max(rents) if rents else None}

    def log_quarantine(self, property_id: int, source_listing_id: int, site: str,
                       listing_id: str, reasons: List[str]) -> int:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        reasons_str = json.dumps(reasons, ensure_ascii=False)
        cur = self.conn.execute(
            "SELECT id FROM quarantine WHERE site=? AND listing_id=?", (site, str(listing_id))
        )
        row = cur.fetchone()
        if row:
            self.conn.execute("""
                UPDATE quarantine SET last_evaluated_at=?, reasons=? WHERE id=?
            """, (now, reasons_str, row["id"]))
            self.conn.commit()
            return row["id"]

        cur = self.conn.execute("""
            INSERT INTO quarantine(property_id, source_listing_id, site, listing_id, reasons, first_evaluated_at, last_evaluated_at, resolution_state)
            VALUES(?,?,?,?,?,?,?,'quarantined')
        """, (property_id, source_listing_id, site, str(listing_id), reasons_str, now, now))
        self.conn.commit()
        return cur.lastrowid

    def resolve_quarantine(self, site: str, listing_id: str) -> int:
        """Close a quarantine row once the listing no longer qualifies for one.

        Without this the backlog only ever grows: a listing that later geocodes,
        or is rejected outright, would keep inflating the digest count forever.
        """
        cur = self.conn.execute("""
            UPDATE quarantine SET resolution_state='resolved', last_evaluated_at=?
            WHERE site=? AND listing_id=? AND resolution_state='quarantined'
        """, (datetime.datetime.now().isoformat(timespec="seconds"), site, str(listing_id)))
        self.conn.commit()
        return cur.rowcount

    # ==========================================================================
    # v3.0 place features, market benchmarks and user actions
    # ==========================================================================
    def get_place_features(self, coord_key: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM place_features WHERE coord_key=?", (coord_key,))
        row = cur.fetchone()
        return dict(row) if row else None

    def save_place_features(self, coord_key: str, data: Dict[str, Any]) -> None:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            INSERT INTO place_features(
                coord_key, commute_min, commute_route, buzz_score, buzz_detail,
                major_street, major_street_dist_m, bus_routes,
                water_dist_m, water_name, water_kind, computed_at, provider
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(coord_key) DO UPDATE SET
                commute_min=excluded.commute_min, commute_route=excluded.commute_route,
                buzz_score=excluded.buzz_score, buzz_detail=excluded.buzz_detail,
                major_street=excluded.major_street,
                major_street_dist_m=excluded.major_street_dist_m,
                bus_routes=excluded.bus_routes, water_dist_m=excluded.water_dist_m,
                water_name=excluded.water_name, water_kind=excluded.water_kind,
                computed_at=excluded.computed_at, provider=excluded.provider
        """, (
            coord_key, data.get("commute_min"), data.get("commute_route"),
            data.get("buzz_score"), data.get("buzz_detail"), data.get("major_street"),
            data.get("major_street_dist_m"), data.get("bus_routes"),
            data.get("water_dist_m"), data.get("water_name"), data.get("water_kind"),
            now, data.get("provider"),
        ))
        self.conn.commit()

    def save_benchmarks(self, segments: Dict[str, Dict[str, Any]], computed_at: str) -> int:
        rows = [
            (key, computed_at, v["n"], v["median_kr_m2"], v["p25_kr_m2"], v["p75_kr_m2"])
            for key, v in segments.items()
        ]
        self.conn.executemany("""
            INSERT INTO market_benchmarks(segment_key, computed_at, n, median_kr_m2, p25_kr_m2, p75_kr_m2)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(segment_key, computed_at) DO UPDATE SET
                n=excluded.n, median_kr_m2=excluded.median_kr_m2,
                p25_kr_m2=excluded.p25_kr_m2, p75_kr_m2=excluded.p75_kr_m2
        """, rows)
        self.conn.commit()
        return len(rows)

    def get_latest_benchmarks(self) -> Dict[str, Dict[str, Any]]:
        """Most recent stored row per segment key."""
        cur = self.conn.execute("""
            SELECT b.* FROM market_benchmarks b
            JOIN (SELECT segment_key, MAX(computed_at) AS mx
                  FROM market_benchmarks GROUP BY segment_key) l
              ON b.segment_key = l.segment_key AND b.computed_at = l.mx
        """)
        return {r["segment_key"]: dict(r) for r in cur.fetchall()}

    def record_user_action(self, property_id: int, action: str, note: str = "") -> int:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute("""
            INSERT INTO user_actions(property_id, action, note, created_at) VALUES(?,?,?,?)
        """, (property_id, action, note or "", now))
        self.conn.commit()
        return cur.lastrowid

    def get_action_state(self, property_id: int) -> Optional[Dict[str, Any]]:
        """Latest action for a property, which is its current state."""
        cur = self.conn.execute("""
            SELECT * FROM user_actions WHERE property_id=? ORDER BY id DESC LIMIT 1
        """, (property_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        return None if d["action"] == "unsave" else d

    def get_action_states(self) -> Dict[int, Dict[str, Any]]:
        """Current state for every property that has ever been acted on."""
        cur = self.conn.execute("""
            SELECT u.* FROM user_actions u
            JOIN (SELECT property_id, MAX(id) AS mx FROM user_actions GROUP BY property_id) l
              ON u.property_id = l.property_id AND u.id = l.mx
        """)
        out = {}
        for r in cur.fetchall():
            d = dict(r)
            if d["action"] != "unsave":
                out[d["property_id"]] = d
        return out

    def lifecycle_state_for(self, property_id: Optional[int]) -> Optional[str]:
        """Best lifecycle state across a property's source listings.

        'active' if any listing is still live and not explicitly gone;
        otherwise the state of the most recently touched one, so an expired property reads as expired.
        """
        if property_id is None:
            return None
        cur = self.conn.execute("""
            SELECT lifecycle_state, explicit_gone_at FROM source_listings WHERE property_id=?
            ORDER BY id DESC
        """, (property_id,))
        rows = cur.fetchall()
        if not rows:
            return None
        for preferred in ("active", "relisted", "missing"):
            for r in rows:
                if r["lifecycle_state"] == preferred and not r["explicit_gone_at"]:
                    return preferred
        return rows[0]["lifecycle_state"]

    def get_dismissed_ids(self) -> set:
        return {pid for pid, d in self.get_action_states().items() if d["action"] == "dismiss"}

    def get_shortlist(self) -> List[Dict[str, Any]]:
        """Saved and applied properties, joined with their current match row."""
        states = {pid: d for pid, d in self.get_action_states().items()
                  if d["action"] in ("save", "applied")}
        if not states:
            return []
        placeholders = ",".join("?" * len(states))
        cur = self.conn.execute(
            f"SELECT * FROM matches WHERE property_id IN ({placeholders})", tuple(states))
        out = []
        for r in cur.fetchall():
            d = dict(r)
            st = states[d["property_id"]]
            d["shortlist_action"] = st["action"]
            d["shortlist_at"] = st["created_at"]
            d["shortlist_note"] = st["note"]
            out.append(d)
        for pid, st in states.items():
            if not any(d["property_id"] == pid for d in out):
                out.append({"property_id": pid, "bw_id": format_bw_id(pid),
                            "shortlist_action": st["action"], "shortlist_at": st["created_at"],
                            "shortlist_note": st["note"]})
        out.sort(key=lambda d: d.get("shortlist_at") or "")
        return out

    # ==========================================================================
    # Legacy seen methods
    # ==========================================================================
    def is_seen(self, site: str, lid: Any) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM seen WHERE site=? AND listing_id=?", (site, str(lid)))
        return cur.fetchone() is not None

    def mark_seen(self, site: str, lid: Any):
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT OR IGNORE INTO seen(site, listing_id, first_seen_at) VALUES(?,?,?)",
            (site, str(lid), now))
        self.conn.commit()

    # ==========================================================================
    # Scan Runs & Incident Tracking
    # ==========================================================================
    def start_scan_run(self, site: str, kind: str = "scheduled",
                       selection: Optional[str] = None) -> int:
        """Open a scan run.

        A run interrupted mid-site (process killed, timer stopped) leaves
        completed_at null. The next start closes it as failed first, so an
        abandoned run can never be read as coverage (spec 06 section 4.5).
        """
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE scan_runs
            SET outcome='failed', coverage_status='failed', completed_at=?,
                failure_detail=COALESCE(failure_detail, 'interrupted; closed by the next run')
            WHERE site=? AND completed_at IS NULL
        """, (now, site))
        cur = self.conn.execute(
            "INSERT INTO scan_runs(site, started_at, outcome, kind, parser_version, selection) "
            "VALUES(?,?,'running',?,?,?)",
            (site, now, kind, getattr(config, "PARSER_VERSION", None), selection))
        self.conn.commit()
        return cur.lastrowid

    # ---- per-area coverage ---------------------------------------------------
    def record_scan_area(self, scan_run_id: Optional[int], site: str,
                         area_or_query: str, status: str, cards_seen: int = 0,
                         expected_cards: Optional[int] = None,
                         pages_loaded: int = 0, error: Optional[str] = None,
                         started_at: Optional[str] = None,
                         finished_at: Optional[str] = None) -> int:
        """One row per area or query a scan tried, success or failure."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute("""
            INSERT INTO scan_areas(
                scan_run_id, site, area_or_query, status, cards_seen,
                expected_cards, pages_loaded, error, started_at, finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (scan_run_id, site, area_or_query, status, int(cards_seen or 0),
               expected_cards, int(pages_loaded or 0),
               (str(error)[:500] if error else None), started_at or now,
               finished_at or now))
        self.conn.commit()
        return cur.lastrowid

    def scan_areas_for_run(self, scan_run_id: int) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM scan_areas WHERE scan_run_id=? ORDER BY id", (scan_run_id,))
        return [dict(r) for r in cur.fetchall()]

    def scan_area_totals(self, scan_run_id: int) -> Dict[str, int]:
        rows = self.scan_areas_for_run(scan_run_id)
        # An area that loaded and returned nothing still loaded. Only an area
        # that actually failed counts against coverage; whether zero cards means
        # "empty" or "we could not see" is decided separately, by the site's
        # own no-results marker.
        failed = sum(1 for r in rows if r["status"] == "failed")
        ok = len(rows) - failed
        return {"areas_total": len(rows), "areas_ok": ok, "areas_failed": failed,
                "cards_seen": sum(int(r["cards_seen"] or 0) for r in rows)}

    def observed_source_ids_for_run(self, scan_run_id: Optional[int]) -> List[int]:
        """Source listings that carry an observation from this run.

        The authority for `listings_observed` is the database, never a list the
        runner returned. Run 52 on 14 September wrote 104 observations under its
        own id and still reported zero.
        """
        if scan_run_id is None:
            return []
        cur = self.conn.execute(
            "SELECT DISTINCT source_listing_id FROM observations WHERE scan_run_id=?",
            (scan_run_id,))
        return [r[0] for r in cur.fetchall() if r[0] is not None]

    def last_complete_cards_seen(self, site: str,
                                 before_run_id: Optional[int] = None) -> Optional[int]:
        """Cards seen by the previous complete scan of a site, if there is one."""
        sql = ("SELECT cards_seen FROM scan_runs WHERE site=? AND coverage_status='complete' "
               "AND cards_seen IS NOT NULL AND cards_seen > 0")
        params: List[Any] = [site]
        if before_run_id is not None:
            sql += " AND id < ?"
            params.append(before_run_id)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    def compute_coverage_status(self, site: str, scan_run_id: Optional[int],
                                cards_seen: int, areas_total: int, areas_ok: int,
                                areas_failed: int, runner_failed: bool = False,
                                empty_confirmed: bool = False,
                                limited: bool = False) -> str:
        """Classify what a scan actually proved about a site (spec 06 4.4)."""
        if runner_failed or areas_total == 0 or areas_ok == 0:
            return "failed"
        if limited:
            # A run capped with --limit stopped looking on purpose. It is real
            # data about what it did see and no evidence at all about what it
            # did not, so it may never expire anything. Without this a
            # `--limit 15` verification run would mark the other 266 live
            # BoligPortal listings missing.
            return "partial"
        if areas_failed > 0:
            return "partial"
        if not cards_seen:
            return "empty-confirmed" if empty_confirmed else "empty-unconfirmed"
        previous = self.last_complete_cards_seen(site, before_run_id=scan_run_id)
        ratio = float(getattr(config, "SCAN_COMPLETE_MIN_RATIO", 0.5))
        if previous and cards_seen < ratio * previous:
            return "partial"
        return "complete"

    def finish_scan_run(self, scan_run_id: int, outcome: str,
                        listings_observed: Optional[int] = None,
                        new_matches: int = 0, duration_sec: Optional[float] = None,
                        failure_detail: Optional[str] = None,
                        coverage_status: Optional[str] = None,
                        cards_seen: Optional[int] = None,
                        cards_new: Optional[int] = None,
                        details_attempted: Optional[int] = None,
                        details_succeeded: Optional[int] = None,
                        details_failed: Optional[int] = None,
                        details_skipped: Optional[int] = None,
                        areas_total: Optional[int] = None,
                        areas_ok: Optional[int] = None,
                        areas_failed: Optional[int] = None,
                        decision_revalidation_selected: Optional[int] = None,
                        decision_revalidation_succeeded: Optional[int] = None,
                        decision_revalidation_failed: Optional[int] = None,
                        decision_revalidation_gone: Optional[int] = None):
        """Close a scan run, counting observations from the database.

        `listings_observed` is always recomputed here rather than trusted from
        the caller: it is the number of distinct source listings carrying this
        run id, which is the only figure the lifecycle rules can safely use.
        """
        now = datetime.datetime.now().isoformat(timespec="seconds")
        observed = len(self.observed_source_ids_for_run(scan_run_id))
        self.conn.execute("""
            UPDATE scan_runs
            SET completed_at=?, outcome=?, listings_observed=?, new_matches=?,
                duration_sec=?, failure_detail=?,
                coverage_status=COALESCE(?, coverage_status),
                cards_seen=COALESCE(?, cards_seen),
                cards_new=COALESCE(?, cards_new),
                details_attempted=COALESCE(?, details_attempted),
                details_succeeded=COALESCE(?, details_succeeded),
                details_failed=COALESCE(?, details_failed),
                details_skipped=COALESCE(?, details_skipped),
                areas_total=COALESCE(?, areas_total),
                areas_ok=COALESCE(?, areas_ok),
                areas_failed=COALESCE(?, areas_failed),
                decision_revalidation_selected=COALESCE(?, decision_revalidation_selected),
                decision_revalidation_succeeded=COALESCE(?, decision_revalidation_succeeded),
                decision_revalidation_failed=COALESCE(?, decision_revalidation_failed),
                decision_revalidation_gone=COALESCE(?, decision_revalidation_gone)
            WHERE id=?
        """, (now, outcome, observed, new_matches, duration_sec, failure_detail,
               coverage_status, cards_seen, cards_new, details_attempted,
               details_succeeded, details_failed, details_skipped,
               areas_total, areas_ok, areas_failed,
               decision_revalidation_selected, decision_revalidation_succeeded,
               decision_revalidation_failed, decision_revalidation_gone,
               scan_run_id))
        self.conn.commit()

    def get_scan_run(self, scan_run_id: int) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM scan_runs WHERE id=?", (scan_run_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def latest_scheduled_scan(self, site: str) -> Optional[Dict[str, Any]]:
        """Most recent finished coverage scan of a site. Targeted runs excluded."""
        cur = self.conn.execute("""
            SELECT * FROM scan_runs
            WHERE site=? AND completed_at IS NOT NULL
              AND (kind IS NULL OR kind='scheduled')
            ORDER BY id DESC LIMIT 1
        """, (site,))
        row = cur.fetchone()
        return dict(row) if row else None

    def portal_coverage_summary(self) -> List[Dict[str, Any]]:
        """Per-site freshness and coverage for the dashboard and the digest."""
        out = []
        for site in config.SITES:
            run = self.latest_scheduled_scan(site)
            if not run:
                out.append({"site": site, "coverage_status": None, "completed_at": None,
                            "cards_seen": None, "listings_observed": None,
                            "areas_total": None, "areas_ok": None, "areas_failed": None})
                continue
            out.append({
                "site": site,
                "scan_run_id": run["id"],
                "coverage_status": run.get("coverage_status"),
                "completed_at": run.get("completed_at"),
                "cards_seen": run.get("cards_seen"),
                "listings_observed": run.get("listings_observed"),
                "areas_total": run.get("areas_total"),
                "areas_ok": run.get("areas_ok"),
                "areas_failed": run.get("areas_failed"),
                "details_attempted": run.get("details_attempted"),
                "details_succeeded": run.get("details_succeeded"),
                "details_skipped": run.get("details_skipped"),
                "details_failed": run.get("details_failed"),
            })
        return out

    def coverage_status_for_site(self, site: str) -> Optional[str]:
        run = self.latest_scheduled_scan(site)
        return run.get("coverage_status") if run else None

    # ---- the one-off repair of the zero-card Lejebolig expiries -------------
    def repair_zero_card_expiries(self, site: str = "lejebolig",
                                  broken_run_ids: Tuple[int, ...] = (40, 43, 46, 49),
                                  dry_run: bool = False) -> Dict[str, Any]:
        """Undo the expiries caused by scans that saw nothing and said success.

        Runs 40, 43, 46 and 49 were Lejebolig scans that recorded outcome
        'success' with zero cards because a per-area exception was caught and
        treated as an empty area. Each counted as a miss for every Lejebolig
        listing, so three of them expired everything the last working scan had
        seen. This restores those listings to the state the last complete scan
        before run 40 left them in: observed, active, zero misses.

        A listing is only touched when its evidence is unambiguous: it is
        expired, the newest scan that ever observed it is that last complete
        scan, and its miss count is no larger than the number of broken runs.
        Anything that had already been drifting out of the market is left alone.
        """
        broken = sorted(broken_run_ids)
        if not broken:
            return {"repaired": 0, "reference_scan_id": None, "listings": []}
        first_broken = broken[0]

        cur = self.conn.execute("""
            SELECT sr.id FROM scan_runs sr
            WHERE sr.site=? AND sr.id < ?
              AND EXISTS(SELECT 1 FROM observations o WHERE o.scan_run_id = sr.id)
            ORDER BY sr.id DESC LIMIT 1
        """, (site, first_broken))
        row = cur.fetchone()
        if not row:
            return {"repaired": 0, "reference_scan_id": None, "listings": []}
        reference = row[0]

        cur = self.conn.execute("""
            SELECT s.id, s.listing_id, s.lifecycle_state, s.consecutive_misses,
                   s.property_id,
                   (SELECT MAX(o.scan_run_id) FROM observations o
                     WHERE o.source_listing_id = s.id) AS last_run
            FROM source_listings s
            WHERE s.site=? AND s.lifecycle_state='expired'
        """, (site,))
        damaged = [dict(r) for r in cur.fetchall()
                   if r["last_run"] == reference
                   and (r["consecutive_misses"] or 0) <= len(broken)]

        if not dry_run and damaged:
            self.conn.executemany(
                "UPDATE source_listings SET lifecycle_state='active', "
                "consecutive_misses=0, expired_at=NULL, last_complete_scan_id=? "
                "WHERE id=?",
                [(reference, d["id"]) for d in damaged])
            self.conn.commit()

        return {
            "repaired": len(damaged),
            "reference_scan_id": reference,
            "broken_run_ids": broken,
            "listings": [d["listing_id"] for d in damaged],
            "properties": sorted({d["property_id"] for d in damaged
                                  if d["property_id"] is not None}),
        }

    # ---- local runs ---------------------------------------------------------
    def start_local_run(self, rules_version: str, enrich: bool = False) -> int:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute("""
            INSERT INTO local_runs(started_at, rules_version, parser_version, enrich)
            VALUES(?,?,?,?)
        """, (now, rules_version, getattr(config, "PARSER_VERSION", None),
               1 if enrich else 0))
        self.conn.commit()
        return cur.lastrowid

    def finish_local_run(self, local_run_id: int, evaluated: int = 0, skipped: int = 0,
                         matched: int = 0, rejected: int = 0, quarantined: int = 0,
                         published: bool = False, duration_sec: Optional[float] = None,
                         detail: Optional[str] = None) -> None:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE local_runs SET completed_at=?, listings_evaluated=?, listings_skipped=?,
                matched=?, rejected=?, quarantined=?, published=?, duration_sec=?, detail=?
            WHERE id=?
        """, (now, evaluated, skipped, matched, rejected, quarantined,
               1 if published else 0, duration_sec, detail, local_run_id))
        self.conn.commit()

    def last_local_run(self) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM local_runs WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None

    def last_local_run_detail(self) -> Dict[str, Any]:
        """The latest completed local run's mode and migration summary.

        Returns ``{"mode": ..., "migration": ...}`` when the latest completed
        run's ``detail`` parses as a JSON object carrying a ``mode`` key, and
        ``{"mode": <that raw string>, "migration": None}`` otherwise - a legacy
        run, no detail at all, or unparsable text. Readers use this helper and
        never parse ``detail`` themselves (spec 07 section 7.3).
        """
        detail = (self.last_local_run() or {}).get("detail")
        if isinstance(detail, str) and detail:
            try:
                parsed = json.loads(detail)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and "mode" in parsed:
                return {"mode": parsed.get("mode"),
                        "migration": parsed.get("migration")}
        return {"mode": detail, "migration": None}

    def last_local_run_migration(self) -> Dict[str, Any]:
        """The latest completed local run's migration summary, with its run stamp.

        ``last_local_run_detail``'s ``{"mode", "migration"}`` plus the same run's
        ``completed_at``, so a reader can tell whether the export it is rendering
        was built by the run that carried the summary (spec 07 section 7.3: the
        banner is suppressed for an old export). One method, so both facts come
        from the same row; ``local_runs.detail`` is still parsed only here.
        """
        detail = self.last_local_run_detail()
        run = self.last_local_run() or {}
        return {"mode": detail.get("mode"),
                "migration": detail.get("migration"),
                "completed_at": run.get("completed_at")}

    # ---- evaluations --------------------------------------------------------
    def record_evaluation(self, source_listing_id: Optional[int],
                          property_id: Optional[int],
                          observation_id: Optional[int], outcome: str,
                          rules_version: str, eval_res: Any = None,
                          reason: Optional[str] = None,
                          rank: Optional[str] = None) -> int:
        """Write the current verdict for a listing and retire the previous one."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        if source_listing_id is not None:
            self.conn.execute(
                "UPDATE evaluations SET is_current=0 WHERE source_listing_id=? AND is_current=1",
                (source_listing_id,))
        g = (lambda name, default=None: getattr(eval_res, name, default))
        quarantine_reasons = g("quarantine_reasons") or []
        raw_flags = g("soft_flags") or []
        soft_flags = []
        for flag in raw_flags:
            if isinstance(flag, dict):
                soft_flags.append(flag)
            else:
                soft_flags.append({
                    "code": getattr(flag, "code", None),
                    "label": getattr(flag, "label", None),
                    "severity": getattr(flag, "severity", "info"),
                    "evidence": getattr(flag, "evidence", None),
                })
        # A source's own evaluated facts; never borrow a sibling's unit facts.
        unit_fields = (
            "rent_total", "move_in_cost", "move_in_cost_verified", "size_m2",
            "available_date_str", "commute_min", "commute_route", "score_breakdown",
            "nearest_station", "metro_walk_m", "used_transit_walk_m", "walk_min",
            "kr_per_m2", "value_ratio", "major_street", "buzz_detail",
            "water_dist_m", "water_name", "is_responsible_pick", "relevance",
            "kitchen_verified", "bathroom_verified", "lease_verified", "rooms_verified",
            "category", "category_name", "score", "tier", "risk_state", "risk_evidence",
            "city_distance_m", "straight_dist_m", "benchmark_median", "benchmark_segment",
            "buzz_score", "bus_routes", "budget_band", "geo_precision", "features_pending",
            "transit_station", "transit_walk_m", "norreport_distance_m",
            "norreport_road_distance_m", "norreport_road_provider", "norreport_road_status",
            "private_kitchen_present", "shared_kitchen_present", "shared_kitchen_is_the_only_kitchen",
            "kitchen_state", "kitchen_evidence_source", "kitchen_evidence_excerpt", "bathroom_state",
        )
        unit_facts = json.dumps({name: g(name) for name in unit_fields}, ensure_ascii=False)
        cur = self.conn.execute("""
            INSERT INTO evaluations(
                source_listing_id, property_id, observation_id, evaluated_at,
                rules_version, parser_version, outcome, reason, quarantine_reasons,
                score, tier, rank, budget_band,
                private_kitchen_present, shared_kitchen_present,
                shared_kitchen_is_the_only_kitchen, kitchen_state,
                kitchen_evidence_source, kitchen_evidence_excerpt, bathroom_state,
                unit_type_state, tenancy_channel_state, building_form_state,
                trust_outcome, trust_reason,
                student_requirement_state, age_requirement_state,
                age_min, age_max, profile_age_fit, eligibility_fact_excerpt,
                soft_flags, unit_facts, is_current)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
        """, (source_listing_id, property_id, observation_id, now,
               rules_version, getattr(config, "PARSER_VERSION", None), outcome,
               reason if reason is not None else g("rejection_reason"),
               json.dumps(quarantine_reasons, ensure_ascii=False),
               g("score"), g("tier"), rank, g("budget_band"),
               _tristate(g("private_kitchen_present")),
               _tristate(g("shared_kitchen_present")),
               _tristate(g("shared_kitchen_is_the_only_kitchen")),
               g("kitchen_state"), g("kitchen_evidence_source"),
               g("kitchen_evidence_excerpt"), g("bathroom_state"),
               # Spec 07 section 5.3: the three state fields plus the trust
               # gate's own verdict. `outcome` keeps its match | quarantined |
               # rejected values for compatibility.
               g("unit_type_state"), g("tenancy_channel_state"),
               g("building_form_state"), g("trust_outcome"), g("trust_reason"),
               # Spec 09 section 6.2: demographic eligibility facts.
               g("student_requirement_state"), g("age_requirement_state"),
               g("age_min"), g("age_max"), g("profile_age_fit"),
               g("eligibility_fact_excerpt"),
               # Spec 10 section 5.3: the soft flags (badges) as JSON.
               json.dumps(soft_flags, ensure_ascii=False), unit_facts))
        self.conn.commit()
        return cur.lastrowid

    def current_evaluation(self, source_listing_id: int) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM evaluations WHERE source_listing_id=? AND is_current=1 "
            "ORDER BY id DESC LIMIT 1", (source_listing_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def evaluation_history(self, source_listing_id: int,
                           limit: int = 10) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM evaluations WHERE source_listing_id=? ORDER BY id DESC LIMIT ?",
            (source_listing_id, limit))
        return [dict(r) for r in cur.fetchall()]

    def get_last_scan_runs(self) -> List[Dict[str, Any]]:
        """Latest completed scan row for every site, newest site first."""
        cur = self.conn.execute("""
            SELECT s.site, s.completed_at, s.outcome, s.listings_observed,
                   s.coverage_status, s.cards_seen, s.areas_total,
                   s.areas_ok, s.areas_failed
            FROM scan_runs s
            JOIN (SELECT site, MAX(id) AS max_id FROM scan_runs
                  WHERE completed_at IS NOT NULL
                    AND (kind IS NULL OR kind='scheduled') GROUP BY site) latest
              ON s.site = latest.site AND s.id = latest.max_id
            ORDER BY s.site
        """)
        return [dict(row) for row in cur.fetchall()]

    def record_portal_failure(self, site: str, error_msg: str) -> Tuple[bool, Dict[str, Any]]:
        """Record portal failure. Returns (should_notify: bool, incident_data: dict)."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            "SELECT * FROM incidents WHERE site=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
            (site,)
        )
        row = cur.fetchone()
        if row:
            new_count = row["failure_count"] + 1
            self.conn.execute("""
                UPDATE incidents SET last_failed_at=?, failure_count=?, error_message=? WHERE id=?
            """, (now, new_count, error_msg, row["id"]))
            self.conn.commit()
            should_notify = (new_count == config.CONSECUTIVE_FAILURE_THRESHOLD and row["notified"] == 0)
            if should_notify:
                self.conn.execute("UPDATE incidents SET notified=1 WHERE id=?", (row["id"],))
                self.conn.commit()
            return should_notify, {"site": site, "count": new_count, "error": error_msg}
        else:
            cur = self.conn.execute("""
                INSERT INTO incidents(site, first_failed_at, last_failed_at, failure_count, error_message, notified)
                VALUES(?,?,?,1,?,0)
            """, (site, now, now, error_msg))
            self.conn.commit()
            return False, {"site": site, "count": 1, "error": error_msg}

    def record_portal_success(self, site: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Record portal success, resolving open incidents. Returns (was_recovered: bool, incident_dict)."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            "SELECT * FROM incidents WHERE site=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
            (site,)
        )
        row = cur.fetchone()
        if row:
            self.conn.execute("UPDATE incidents SET resolved_at=? WHERE id=?", (now, row["id"]))
            self.conn.commit()
            was_notified = bool(row["notified"])
            return was_notified, dict(row)
        return False, None

    # ==========================================================================
    # Query & Presentation Methods
    # ==========================================================================
    def resolve_complex_key(self, address: Optional[str],
                            postcode: Optional[str]) -> Optional[str]:
        """Map an address+postcode to a configured complex_key (or None)."""
        return resolve_complex_key_for(address, postcode)

    def complex_members(self, complex_key: Optional[str]) -> List[int]:
        """Property ids currently grouped under a complex_key, by bw id."""
        if not complex_key:
            return []
        cur = self.conn.execute(
            "SELECT property_id FROM matches WHERE complex_key=? ORDER BY property_id",
            (complex_key,))
        return [r["property_id"] for r in cur.fetchall()]

    def eligible_unit_rows(self, member_ids: List[int]) -> List[Dict[str, Any]]:
        """All eligible live/missing adverts, with source-scoped facts and links.

        Adverts with identical price/size are retained: without an exact unit
        identity those values cannot prove that two adverts are duplicates.
        """
        if not member_ids:
            return []
        marks = ",".join("?" for _ in member_ids)
        rows = self.conn.execute(f"""
            SELECT s.*, p.bw_id, e.score, e.tier, e.rank, e.unit_facts,
                   e.kitchen_state, e.bathroom_state, e.evaluated_at
            FROM source_listings s JOIN properties p ON p.id=s.property_id
            JOIN evaluations e ON e.source_listing_id=s.id AND e.is_current=1
            WHERE s.property_id IN ({marks}) AND e.outcome='match'
              AND e.trust_outcome='eligible' AND s.explicit_gone_at IS NULL
              AND s.lifecycle_state IN ('active','missing','relisted')
            ORDER BY s.id
        """, member_ids).fetchall()
        units = []
        for row in rows:
            source = dict(row)
            raw = json.loads(source.pop("raw_data") or "{}")
            facts = json.loads(source.pop("unit_facts") or "{}")
            unit = {k: raw.get(k) for k in (
                "title", "address", "rooms", "size_m2", "rent_base", "utilities_amount",
                "mandatory_monthly_fees", "deposit", "prepaid_rent", "first_month_charge",
                "mandatory_onetime_fees", "rent_total", "available_from", "photo_url")}
            unit.update({k: v for k, v in facts.items() if v is not None})
            unit.update({k: source.get(k) for k in (
                "property_id", "bw_id", "site", "listing_id", "url", "lifecycle_state",
                "eligibility_evidence_state", "eligibility_checked_at", "last_seen_at",
                "score", "tier", "kitchen_state", "bathroom_state", "evaluated_at")})
            unit["source_listing_id"] = source["id"]
            unit["display_tier"] = source.get("rank") or tiers.rank_tier_of(source.get("score"))
            if facts.get("available_date_str") is not None:
                unit["available_from"] = facts["available_date_str"]
            units.append(unit)
        return sorted(units, key=lambda u: self._representative_sort_key(
            u, 0 if u["lifecycle_state"] in ("active", "relisted") else 1))

    @staticmethod
    def _representative_sort_key(row: Dict[str, Any],
                                 lifecycle_rank: int) -> Tuple:
        """§5.2 representative selection order (ascending).

        eligible & live before missing/expired/ineligible -> lowest rent ->
        higher score -> shorter used rapid-transit walk -> shorter commute ->
        BW-ID and listing ID ascending.
        """
        return (
            lifecycle_rank,                       # 0 live, 1 otherwise
            row.get("rent_total") if row.get("rent_total") is not None else float("inf"),
            -float(row.get("score") or 0),
            row.get("used_transit_walk_m") if row.get("used_transit_walk_m") is not None
            else row.get("distance_m") or float("inf"),
            row.get("commute_min") if row.get("commute_min") is not None else float("inf"),
            str(row.get("bw_id") or ""),
            str(row.get("listing_id") or ""),
        )

    def _group_live_price_span(self, member_ids: List[int]) -> Tuple[Optional[int],
                                                                     Optional[int],
                                                                     int]:
        """Distinct live rents (<= 9,700) across a group's active source listings.

        Returns (min, max, count_of_distinct_values). Expired/quarantined/over-cap
        listings are ignored, and duplicate adverts (same normalized unit
        address + size + rent) collapse to one. count is the number of distinct
        rents, which drives the 'More in building' tag (>= 2).
        """
        if not member_ids:
            return None, None, 0
        placeholders = ",".join("?" * len(member_ids))
        cur = self.conn.execute(
            f"SELECT s.raw_data, e.unit_facts FROM source_listings s "
            f"LEFT JOIN evaluations e ON e.source_listing_id=s.id AND e.is_current=1 "
            f"WHERE s.property_id IN ({placeholders}) AND s.lifecycle_state IN ('active','relisted') "
            "AND s.explicit_gone_at IS NULL "
            "AND (e.id IS NULL OR (e.outcome='match' AND e.trust_outcome='eligible'))",
            member_ids)
        seen_units = set()
        rents = set()
        for r in cur.fetchall():
            try:
                rec = json.loads(r["raw_data"] or "{}") or {}
            except Exception:
                continue
            try:
                facts = json.loads(r["unit_facts"] or "{}")
                rent = int(facts.get("rent_total", rec.get("rent_total")) or 0)
            except (TypeError, ValueError):
                continue
            if rent <= 0 or rent > config.RENT_CAP_MAX:
                continue
            unit = _fold_complex_text(
                f"{rec.get('address') or ''} "
                f"{rec.get('size_m2') or ''} {rec.get('rooms') or ''} {rent}")
            if unit in seen_units:
                continue
            seen_units.add(unit)
            rents.add(rent)
        if not rents:
            return None, None, 0
        return min(rents), max(rents), len(rents)

    def grouped_matches(self, category: Optional[int] = None,
                        max_rent: Optional[int] = None,
                        search: Optional[str] = None, limit: int = 200,
                        tier: Optional[str] = None,
                        relevance: Optional[str] = None,
                        include_dismissed: bool = False,
                        order: str = "score") -> List[Dict[str, Any]]:
        """One representative per property/building/complex, ranked per §3.6.

        Standalone properties surface on their own; properties sharing a
        complex_key collapse to a single representative chosen by §5.2. Each
        returned row carries extra keys: complex_key, group_members, and
        more_in_building / group_price_min / group_price_max for the §6 tag.
        """
        from collections import OrderedDict
        rows = self.get_matches(category=category, max_rent=max_rent, search=search,
                                limit=limit * 4, tier=tier, relevance=relevance,
                                include_dismissed=True, order=order)
        # Sort to the §3.6 order the whole list will be served in.
        rows.sort(key=self._representative_list_key)
        grouped: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        for r in rows:
            ck = r.get("complex_key")
            grouped.setdefault(ck if ck else f"prop:{r.get('property_id')}", []).append(r)

        reps = []
        dismissed = set() if include_dismissed else self.get_dismissed_ids()
        for key, members in grouped.items():
            _rep = self._choose_representative(members)
            if _rep is None:
                continue
            rep = dict(_rep)
            rep["complex_key"] = members[0].get("complex_key")
            rep["group_members"] = sorted(m.get("property_id") for m in members)
            rep["group_name"] = config.COMPLEX_GROUPS.get(rep["complex_key"], {}).get("name")
            if rep["complex_key"]:
                pmin, pmax, n = self._group_live_price_span(rep["group_members"])
                rep["more_in_building"] = n >= 2
                rep["group_price_min"] = pmin
                rep["group_price_max"] = pmax
            else:
                rep["more_in_building"] = False
                rep["group_price_min"] = rep.get("rent_total")
                rep["group_price_max"] = rep.get("rent_total")
            if rep.get("property_id") in dismissed:
                continue
            reps.append(rep)
            if len(reps) >= limit:
                break
        reps.sort(key=self._representative_list_key)
        return reps

    def _choose_representative(self, members: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Frozen §5.2 selection across a group's live match rows."""
        def _rank(r):
            source = self.get_source_listing(r.get("site"), r.get("listing_id"))
            ls = source.get("lifecycle_state") if source else self.lifecycle_state_for(r.get("property_id"))
            return self._representative_sort_key(r, 0 if ls in ("active", "relisted") else 1)
        return min(members, key=_rank)

    def get_group_by_property(self, property_id: int) -> Optional[str]:
        """complex_key for a property's current match row, or None."""
        cur = self.conn.execute(
            "SELECT complex_key FROM matches WHERE property_id=?", (property_id,))
        row = cur.fetchone()
        return row["complex_key"] if row else None


    @staticmethod
    def _representative_list_key(r: Dict[str, Any]) -> Tuple:
        """Spec 09 Section 5.2 rank order:
        rank tier order (S, A, B, C, D)
        rank_priority descending
        score descending
        rent_total ascending
        Metro walking distance ascending, NULL last
        property id ascending
        """
        tier_letter = tiers.rank_tier_of(r.get("score"))
        tier_order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}.get(tier_letter, 4)
        priority = int(r.get("rank_priority") or 0)
        score = float(r.get("score") or 0)
        rent = r.get("rent_total")
        rent_val = float(rent) if rent is not None else float("inf")
        metro_walk = r.get("distance_m") if r.get("distance_m") is not None else r.get("metro_walk_m")
        metro_val = float(metro_walk) if metro_walk is not None else float("inf")
        prop_id = int(r.get("property_id") or r.get("id") or 0)
        return (
            tier_order,
            -priority,
            -score,
            rent_val,
            metro_val,
            prop_id,
        )

    def get_matches(self, category: Optional[int] = None, max_rent: Optional[int] = None,
                    search: Optional[str] = None, limit: int = 200,
                    tier: Optional[str] = None, relevance: Optional[str] = None,
                    include_dismissed: bool = False,
                    order: str = "tier") -> List[Dict[str, Any]]:
        """Confirmed matches, ranked within tier first.

        order 'tier'    commute band A then B then C, each by score descending
        order 'score'   by rank, which is by score descending
        order 'commute' shortest door-to-door commute first
        order 'value'   cheapest kr per m2 against the market first
        Ties break on lower rent, then lower commute.
        """
        query = """
            SELECT m.*,
                   p.rank_priority,
                   p.rank_priority_kind,
                   p.rank_priority_note,
                   p.rank_priority_at
            FROM matches m
            LEFT JOIN properties p ON p.id = m.property_id
            WHERE (m.quarantined = 0 OR m.quarantined IS NULL)
              AND (m.site IS NULL OR m.site <> 'findbolig')
        """
        params: List[Any] = []
        if category:
            query += " AND m.category = ?"
            params.append(category)
        if tier:
            query += " AND m.category = ?"
            params.append(config.TIER_NUMBERS.get(tier, 0))
        if relevance:
            query += " AND m.relevance = ?"
            params.append(relevance)
        if max_rent:
            query += " AND m.rent_total <= ?"
            params.append(float(max_rent))
        if search:
            query += " AND (m.title LIKE ? OR m.address LIKE ? OR m.nearest_station LIKE ?)"
            s_param = f"%{search}%"
            params.extend([s_param, s_param, s_param])

        # §3.6 rank order: score desc, rent asc, used rapid-transit walk asc,
        # commute asc, BW-ID asc (the stretch tiebreak was removed in v3.3).
        tie = ("m.rent_total ASC, m.used_transit_walk_m ASC, m.commute_min ASC, "
               "m.bw_id ASC")
        order_sql = {
            "tier": f"m.category ASC, m.score DESC, {tie}",
            "score": f"m.score DESC, {tie}",
            "commute": f"m.commute_min ASC, m.score DESC, {tie}",
            "value": f"m.value_ratio ASC, m.score DESC, {tie}",
        }.get(order, f"m.category ASC, m.score DESC, {tie}")
        query += f" ORDER BY {order_sql} LIMIT ?"
        params.append(limit if include_dismissed else limit * 3)

        cur = self.conn.execute(query, tuple(params))
        rows = cur.fetchall()
        dismissed = set() if include_dismissed else self.get_dismissed_ids()

        out = []
        for r in rows:
            d = dict(r)
            if d.get("property_id") in dismissed:
                continue
            for field in ("score_breakdown", "risk_evidence", "commute_route"):
                if d.get(field) and isinstance(d[field], str):
                    try:
                        d[field] = json.loads(d[field])
                    except Exception:
                        pass
            d["soft_flags"] = _parse_soft_flags(d.get("soft_flags"))
            out.append(d)
            if len(out) >= limit:
                break
        return out

    def get_property_by_bw_id(self, bw_id_input: Any) -> Optional[Dict[str, Any]]:
        prop_id = parse_bw_id(bw_id_input)
        if prop_id is None:
            return None

        # Try match table first
        cur = self.conn.execute("SELECT * FROM matches WHERE property_id=? OR id=?", (prop_id, prop_id))
        match_row = cur.fetchone()

        # Get property details
        cur = self.conn.execute("SELECT * FROM properties WHERE id=?", (prop_id,))
        prop_row = cur.fetchone()

        # Get all source URLs
        cur = self.conn.execute(
            "SELECT site, listing_id, url, lifecycle_state, eligibility_text, "
            "eligibility_source_url, eligibility_evidence_state, eligibility_checked_at "
            "FROM source_listings WHERE property_id=?", (prop_id,))
        sources = [dict(s) for s in cur.fetchall()]

        if not match_row and not prop_row:
            return None

        res = dict(match_row) if match_row else {}
        if prop_row:
            res["property"] = dict(prop_row)
        res["sources"] = sources
        if res.get("score_breakdown") and isinstance(res["score_breakdown"], str):
            try:
                res["score_breakdown"] = json.loads(res["score_breakdown"])
            except Exception:
                pass
        if res.get("risk_evidence") and isinstance(res["risk_evidence"], str):
            try:
                res["risk_evidence"] = json.loads(res["risk_evidence"])
            except Exception:
                pass
        return res

    def resolve_property_by_bw_id(self, bw_id_input: Any) -> Optional[Dict[str, Any]]:
        """Resolve a public BW-0000083-shaped id to exactly one property.

        Returns the property row dict if found uniquely, or None if unknown or ambiguous.
        """
        if bw_id_input is None:
            return None
        prop_id = parse_bw_id(bw_id_input)
        if prop_id is None or prop_id <= 0:
            return None
        formatted = format_bw_id(prop_id)
        cur = self.conn.execute(
            "SELECT * FROM properties WHERE id=? OR bw_id=?",
            (prop_id, formatted),
        )
        rows = cur.fetchall()
        if len(rows) != 1:
            return None
        return dict(rows[0])

    def get_search_profile(self) -> Optional[Dict[str, Any]]:
        """Return the singleton search profile dict or None if not set."""
        cur = self.conn.execute(
            "SELECT id, age_years, updated_at, evidence_source FROM search_profile WHERE id=1"
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)

    def set_search_profile_age(self, age_years: Any, evidence_source: str = "user") -> None:
        """Set user search profile age (Spec 09 section 6.1).

        Accepts integer 18–120. Rejects bool, missing, fractional, string and
        out-of-range values without writing.
        """
        if isinstance(age_years, bool) or not isinstance(age_years, int):
            raise ValueError(f"age_years must be an integer, got {type(age_years).__name__}")
        if age_years < 18 or age_years > 120:
            raise ValueError(f"age_years must be between 18 and 120, got {age_years}")
        if not evidence_source or not isinstance(evidence_source, str):
            raise ValueError("evidence_source must be a non-empty string")
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            INSERT INTO search_profile (id, age_years, updated_at, evidence_source)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                age_years=excluded.age_years,
                updated_at=excluded.updated_at,
                evidence_source=excluded.evidence_source
        """, (age_years, now, evidence_source))
        self.conn.commit()

    def set_rank_priority(self, property_id: int, priority: int,
                          kind: Optional[str] = None,
                          note: Optional[str] = None) -> None:
        """Set personal priority on a property (Spec 09 section 5.1).

        v3.8 allows only priority 0 (neutral, kind=None, note=None) and
        priority 1 (kind='preferred_locality'). Clearing restores all four
        fields to neutral.
        """
        if isinstance(property_id, bool) or not isinstance(property_id, int):
            raise ValueError(f"property_id must be an integer, got {type(property_id).__name__}")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError(f"priority must be an integer, got {type(priority).__name__}")

        cur = self.conn.execute("SELECT id FROM properties WHERE id=?", (property_id,))
        if not cur.fetchone():
            raise ValueError(f"Unknown property_id {property_id}")

        if priority == 0:
            if kind is not None:
                raise ValueError("kind must be None when priority is 0")
            if note is not None:
                raise ValueError("note must be None when priority is 0")
            self.clear_rank_priority(property_id)
            return

        if priority == 1:
            if kind != "preferred_locality":
                raise ValueError(f"v3.8 allows only kind='preferred_locality' for priority=1, got {kind!r}")
            now = datetime.datetime.now().isoformat(timespec="seconds")
            self.conn.execute("""
                UPDATE properties
                SET rank_priority=?, rank_priority_kind=?, rank_priority_note=?, rank_priority_at=?
                WHERE id=?
            """, (priority, kind, note, now, property_id))
            self.conn.commit()
            return

        raise ValueError(f"v3.8 allows only priority 0 or 1, got {priority}")

    def clear_rank_priority(self, property_id: int) -> None:
        """Clear personal priority on a property, restoring all four fields to neutral."""
        if isinstance(property_id, bool) or not isinstance(property_id, int):
            raise ValueError(f"property_id must be an integer, got {type(property_id).__name__}")
        cur = self.conn.execute("SELECT id FROM properties WHERE id=?", (property_id,))
        if not cur.fetchone():
            raise ValueError(f"Unknown property_id {property_id}")
        self.conn.execute("""
            UPDATE properties
            SET rank_priority=0, rank_priority_kind=NULL, rank_priority_note=NULL, rank_priority_at=NULL
            WHERE id=?
        """, (property_id,))
        self.conn.commit()

    def get_rank_priority(self, property_id: int) -> Optional[Dict[str, Any]]:
        """Return personal priority fields for a property, or None if unknown."""
        if isinstance(property_id, bool) or not isinstance(property_id, int):
            return None
        cur = self.conn.execute(
            "SELECT rank_priority, rank_priority_kind, rank_priority_note, rank_priority_at "
            "FROM properties WHERE id=?", (property_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)

    def get_quarantined_listings(self, limit: int = 50) -> List[Dict[str, Any]]:
        cur = self.conn.execute("""
            SELECT q.*, s.url, s.eligibility_source_url,
                   s.eligibility_evidence_state, s.eligibility_checked_at,
                   p.normalized_address
            FROM quarantine q
            LEFT JOIN source_listings s ON q.source_listing_id = s.id
            LEFT JOIN properties p ON q.property_id = p.id
            WHERE q.resolution_state = 'quarantined'
            ORDER BY q.id DESC LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("reasons") and isinstance(d["reasons"], str):
                try:
                    d["reasons"] = json.loads(d["reasons"])
                except Exception:
                    pass
            out.append(d)
        return out

    # ==========================================================================
    # v3.6 (spec 07 sections 7, 7.1) & v3.8 (spec 09) — the four-view read models
    # ==========================================================================
    # Everything below is read-only: no view is materialised into `matches`
    # (section 7.1: "do not insert quarantined or rejected rows into `matches`
    # merely to make them visible"), and no method writes any table. Each row is
    # answered from the source row, its CURRENT evaluation and the latest
    # observation - never from evaluation history or an aggregate of scans.
    READ_MODEL_SQL = """
        SELECT s.id AS source_listing_id, s.site, s.listing_id, s.url,
               s.property_id, s.lifecycle_state, s.expired_at, s.last_seen_at,
               s.consecutive_misses, s.card_price,
               s.raw_data, s.explicit_gone_at, s.explicit_gone_marker,
               s.unit_type_state AS stored_unit_type_state,
               s.unit_type_evidence_source, s.unit_type_evidence_excerpt,
               s.tenancy_channel_state AS stored_tenancy_channel_state,
               s.landlord_name, s.landlord_evidence_source,
               s.landlord_evidence_excerpt,
               s.building_form_state AS stored_building_form_state,
               s.building_profile_id,
               s.student_requirement_state AS stored_student_requirement_state,
               s.age_requirement_state AS stored_age_requirement_state,
               s.age_min AS stored_age_min,
               s.age_max AS stored_age_max,
               s.profile_age_fit AS stored_profile_age_fit,
               s.eligibility_fact_excerpt AS stored_eligibility_fact_excerpt,
               p.normalized_address,
               p.rank_priority, p.rank_priority_kind, p.rank_priority_note, p.rank_priority_at,
               e.outcome, e.reason AS evaluation_reason, e.quarantine_reasons,
               e.soft_flags,
               e.score, e.trust_outcome, e.trust_reason,
               e.unit_type_state AS evaluation_unit_type_state,
               e.tenancy_channel_state AS evaluation_tenancy_channel_state,
               e.building_form_state AS evaluation_building_form_state,
               e.student_requirement_state AS evaluation_student_requirement_state,
               e.age_requirement_state AS evaluation_age_requirement_state,
               e.age_min AS evaluation_age_min,
               e.age_max AS evaluation_age_max,
               e.profile_age_fit AS evaluation_profile_age_fit,
               e.eligibility_fact_excerpt AS evaluation_eligibility_fact_excerpt,
               o.observed_at, o.rent_total AS observed_rent_total,
               o.available_from AS observed_available_from,
               bp.verified_at AS building_profile_verified_at,
               EXISTS(SELECT 1 FROM matches m WHERE m.property_id = s.property_id)
                   AS has_match_row
        FROM source_listings s
        LEFT JOIN properties p ON p.id = s.property_id
        LEFT JOIN evaluations e ON e.id = (
            SELECT MAX(id) FROM evaluations
            WHERE source_listing_id = s.id AND is_current = 1)
        LEFT JOIN observations o ON o.id = (
            SELECT MAX(id) FROM observations WHERE source_listing_id = s.id)
        LEFT JOIN building_profiles bp ON bp.id = s.building_profile_id
        WHERE COALESCE(s.site, '') <> 'findbolig'
    """

    def _source_read_model_rows(self, pairs: Optional[List[Tuple[Any, Any]]] = None,
                                ) -> List[Dict[str, Any]]:
        """One read-model row per stored source, with its view (spec 07 section 7, spec 09).

        A source is placed in at most one view by ``_view_for_source``, whose
        order is the plan's pinned partition: an expired source inside
        ``config.EXPIRED_VISIBLE_DAYS`` is Recently gone whatever its last
        decision said, then a rejected outcome is Excluded, then a quarantined
        outcome is Needs verification, then a `match` outcome whose property
        holds a `matches` row is Trusted blocks (a sibling of a representative
        is counted inside its property group, never listed as its own row), and
        a `match` whose property is unresolved is Needs verification with the
        section 7.1 item 7 reason.

        `pairs` restricts the scan to a list of ``(site, listing_id)`` tuples.
        """
        sql = self.READ_MODEL_SQL
        params: List[Any] = []
        if pairs:
            sql += " AND (" + " OR ".join(
                "(s.site=? AND s.listing_id=?)" for _ in pairs) + ")"
            for site, listing_id in pairs:
                params.extend([site, str(listing_id)])
        sql += " ORDER BY s.id"

        cutoff = (datetime.datetime.now() - datetime.timedelta(
            days=int(getattr(config, "EXPIRED_VISIBLE_DAYS", 14)))
        ).isoformat(timespec="seconds")
        today = datetime.date.today()
        out: List[Dict[str, Any]] = []
        for r in self.conn.execute(sql, tuple(params)).fetchall():
            row = dict(r)
            has_match_row = bool(row.pop("has_match_row"))
            view = self._view_for_source(row, has_match_row, cutoff)
            raw = row.get("raw_data")
            try:
                raw = json.loads(raw) if raw else {}
            except Exception:
                raw = {}
            pinned = None
            if (view == VIEW_NEEDS_VERIFICATION and row.get("outcome") == "match"):
                # Plan duty 2 rule 5 pins this row's primary reason.
                pinned = REASON_IDENTITY_UNRESOLVED
            reasons = self._read_model_reasons(row, view, pinned)
            rent = row.get("observed_rent_total")
            if rent is None:
                rent = raw.get("rent_total")
            misses = int(row.get("consecutive_misses") or 0)
            missing_label = None
            if row.get("lifecycle_state") == "missing":
                missing_label = ("Not seen in 1 complete scan" if misses == 1
                                 else (f"Not seen in {misses} complete scans" if misses > 1 else None))
            out.append({
                "view": view,
                "source_listing_id": row.get("source_listing_id"),
                "site": row.get("site"),
                "listing_id": row.get("listing_id"),
                "url": row.get("url"),
                "title": raw.get("title"),
                "address": row.get("normalized_address") or raw.get("address"),
                "rent_total": rent,
                "available_from": (row.get("observed_available_from")
                                   or raw.get("available_from")),
                "lifecycle_state": row.get("lifecycle_state"),
                "expired_at": row.get("expired_at"),
                "explicit_gone_at": row.get("explicit_gone_at"),
                "explicit_gone_marker": row.get("explicit_gone_marker"),
                "last_seen_at": row.get("last_seen_at"),
                "last_seen_days_ago": _days_ago(row.get("last_seen_at"), today),
                "consecutive_misses": misses,
                "missing_label": missing_label,
                "card_price": row.get("card_price"),
                "observed_at": row.get("observed_at"),
                "outcome": row.get("outcome"),
                # Section 5.1's latest parsed facts are the read model's own
                # values; a pass that established nothing new leaves them NULL
                # (v36i1's mapping), so the evaluation's per-pass verdict is the
                # fallback rather than the other way round.
                "unit_type_state": (row.get("stored_unit_type_state")
                                    or row.get("evaluation_unit_type_state")),
                "tenancy_channel_state": (row.get("stored_tenancy_channel_state")
                                          or row.get("evaluation_tenancy_channel_state")),
                "building_form_state": (row.get("stored_building_form_state")
                                        or row.get("evaluation_building_form_state")),
                "student_requirement_state": (row.get("stored_student_requirement_state")
                                              or row.get("evaluation_student_requirement_state")),
                "age_requirement_state": (row.get("stored_age_requirement_state")
                                          or row.get("evaluation_age_requirement_state")),
                "age_min": (row.get("stored_age_min") if row.get("stored_age_min") is not None
                            else row.get("evaluation_age_min")),
                "age_max": (row.get("stored_age_max") if row.get("stored_age_max") is not None
                            else row.get("evaluation_age_max")),
                "profile_age_fit": (row.get("stored_profile_age_fit")
                                    or row.get("evaluation_profile_age_fit")),
                "eligibility_fact_excerpt": (row.get("stored_eligibility_fact_excerpt")
                                             or row.get("evaluation_eligibility_fact_excerpt")),
                "rank_priority": int(row.get("rank_priority") or 0),
                "rank_priority_kind": row.get("rank_priority_kind"),
                "rank_priority_note": row.get("rank_priority_note"),
                "rank_priority_at": row.get("rank_priority_at"),
                "trust_outcome": row.get("trust_outcome"),
                "trust_reason": row.get("trust_reason"),
                "soft_flags": _parse_soft_flags(row.get("soft_flags")),
                "property_id": row.get("property_id"),
                "property_identity_state": ("resolved" if row.get("property_id")
                                            is not None else "unresolved"),
                # Section 7 item 2: a review row keeps its score, labelled
                # provisional, and never carries an S-D rank.
                "provisional_score": row.get("score"),
                "unit_type_evidence_source": row.get("unit_type_evidence_source"),
                "unit_type_evidence_excerpt": row.get("unit_type_evidence_excerpt"),
                "landlord_name": row.get("landlord_name"),
                "landlord_evidence_source": row.get("landlord_evidence_source"),
                "landlord_evidence_excerpt": row.get("landlord_evidence_excerpt"),
                "building_profile_id": row.get("building_profile_id"),
                "building_profile_verified_at": row.get("building_profile_verified_at"),
                "reasons": [text for _rank, text in reasons],
                "reason_ranks": [rank for rank, _text in reasons],
                "primary_reason": reasons[0][1] if reasons else None,
            })
        return out

    @staticmethod
    def _view_for_source(row: Dict[str, Any], has_match_row: bool,
                         cutoff: str) -> Optional[str]:
        """The one view a source is in, or None (in no view / out of window).

        The order is the plan's pinned partition and is exclusive by
        construction: an expired source is judged by its window and nothing
        else, and the outcome rules are a chain of ifs.
        """
        if row.get("explicit_gone_at"):
            return VIEW_RECENTLY_GONE
        lifecycle = row.get("lifecycle_state")
        if lifecycle == "expired":
            expired_at = row.get("expired_at")
            if expired_at and str(expired_at) >= cutoff:
                return VIEW_RECENTLY_GONE
            return VIEW_OUT_OF_WINDOW
        if lifecycle not in VIEW_IN_SCOPE_LIFECYCLES:
            return None
        outcome = row.get("outcome")
        if outcome is None:
            # No current evaluation: not a current source, so no view (a
            # lifecycle/identity state, not a verdict).
            return None
        if outcome == "rejected":
            return VIEW_EXCLUDED
        if outcome == "quarantined":
            return VIEW_NEEDS_VERIFICATION
        if outcome == "match":
            if row.get("property_id") is None or not has_match_row:
                return VIEW_NEEDS_VERIFICATION
            return VIEW_TRUSTED_BLOCKS
        return None

    @staticmethod
    def _read_model_reasons(row: Dict[str, Any], view: Optional[str],
                            pinned: Optional[str] = None) -> List[Tuple[int, str]]:
        """Section 7.1-ordered (rank, reason) pairs for one read-model row."""
        texts: List[str] = []
        # v3.11 (spec 10): a Q1/Q2 hold is decided by its own reason. The trust
        # reason (`Unit type unconfirmed`) is a badge on ranked rows, not the
        # primary reason for a listing the system could not judge.
        fields = ("evaluation_reason",) if row.get("outcome") == "quarantined" \
            else ("trust_reason", "evaluation_reason")
        for field in fields:
            if row.get(field):
                texts.append(str(row[field]))
        raw = row.get("quarantine_reasons")
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = [raw]
            texts.extend(str(item) for item in (parsed or []) if item)
        if view in (VIEW_RECENTLY_GONE, VIEW_OUT_OF_WINDOW):
            if row.get("explicit_gone_at"):
                texts.append("advert says no longer available")
            else:
                texts.append(REASON_LIFECYCLE_EXPIRED)
        ordered = ordered_reasons(texts)
        if pinned:
            # The plan pins this row's primary reason, so it leads and the rest
            # follow in rank order.
            return ([(reason_rank(pinned), pinned)]
                    + [pair for pair in ordered if pair[1] != pinned])
        return ordered

    def source_view_rows(self, view: Optional[str] = None,
                         limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Source-level view rows (spec 07 section 7, spec 09 section 9).

        `needs_verification`, `recently_gone` and `excluded` are source-level;
        `trusted_blocks` is the existing grouped match path and is served by
        `trusted_block_rows`. With no `view`, the three source-level views are
        returned together, each sorted by its spec 09 sort rule.
        """
        if view == VIEW_TRUSTED_BLOCKS:
            return self.trusted_block_rows(limit=limit) if limit else self.trusted_block_rows()
        if view is not None and view not in (VIEW_NEEDS_VERIFICATION,
                                            VIEW_RECENTLY_GONE, VIEW_EXCLUDED):
            raise ValueError(f"unknown view: {view!r}")

        all_rows = self._source_read_model_rows()

        if view == VIEW_NEEDS_VERIFICATION:
            rows = [r for r in all_rows if r["view"] == VIEW_NEEDS_VERIFICATION]
            rows.sort(key=_needs_verification_sort_key)
            return rows[:limit] if limit else rows
        elif view == VIEW_RECENTLY_GONE:
            rows = [r for r in all_rows if r["view"] == VIEW_RECENTLY_GONE]
            rows.sort(key=_recently_gone_sort_key)
            return rows[:limit] if limit else rows
        elif view == VIEW_EXCLUDED:
            rows = [r for r in all_rows if r["view"] == VIEW_EXCLUDED]
            rows.sort(key=_excluded_sort_key)
            return rows[:limit] if limit else rows

        nv = [r for r in all_rows if r["view"] == VIEW_NEEDS_VERIFICATION]
        nv.sort(key=_needs_verification_sort_key)
        rg = [r for r in all_rows if r["view"] == VIEW_RECENTLY_GONE]
        rg.sort(key=_recently_gone_sort_key)
        ex = [r for r in all_rows if r["view"] == VIEW_EXCLUDED]
        ex.sort(key=_excluded_sort_key)
        combined = nv + rg + ex
        return combined[:limit] if limit else combined

    def needs_verification_rows(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Apartment candidates held for one or more unknown facts (section 7)."""
        return self.source_view_rows(VIEW_NEEDS_VERIFICATION, limit=limit)

    def recently_gone_rows(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Sources expired inside the visible window, with last-seen age."""
        return self.source_view_rows(VIEW_RECENTLY_GONE, limit=limit)

    def excluded_rows(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Hard rejections, source-level (section 7's collapsed view)."""
        return self.source_view_rows(VIEW_EXCLUDED, limit=limit)

    def trusted_block_rows(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """The Trusted blocks view: the existing grouped match path.

        Section 7 keeps the trusted count grouped properties/buildings, so this
        view is `grouped_matches` itself and never a second grouping of its own.
        """
        return self.grouped_matches(**kwargs)

    def view_counts(self) -> Dict[str, int]:
        """Spec 07 sections 7 and 7.3: the four view counts.

        Keyed exactly `trusted_blocks`, `needs_verification`, `recently_gone`
        and `excluded` - the source-level counts for the three non-trusted views
        and the grouped count for trusted (labelled as such by the reader).
        """
        counts = {key: 0 for key in VIEW_KEYS}
        for row in self._source_read_model_rows():
            if row["view"] in counts:
                counts[row["view"]] += 1
        # No practical cap: the trusted count has to be the view's whole list.
        counts[VIEW_TRUSTED_BLOCKS] = len(self.grouped_matches(limit=10 ** 6))
        return counts

    def integrity_counts(self) -> Dict[str, int]:
        """Spec 07 section 8 and Spec 09 section 10.3 integrity primitives, as counts.

        `null_linked_current_matches` is the section 3.4 defect: a current
        `match` evaluation with no property link. `forbidden_trust_in_matches`
        counts `matches` rows whose property's current `match` evaluation is not
        `eligible`, or is a room rental, or is a sublet/private-landlord source.
        `explicit_gone_in_matches` counts `matches` rows whose representative
        source listing is explicitly gone.
        """
        null_linked = self.conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE is_current=1 "
            "AND outcome='match' AND property_id IS NULL").fetchone()[0]
        cur = self.conn.execute("""
            SELECT m.id AS match_id, e.trust_outcome, e.unit_type_state,
                   e.tenancy_channel_state
            FROM matches m
            JOIN evaluations e ON e.property_id = m.property_id
             AND e.is_current = 1 AND e.outcome = 'match'
        """)
        forbidden = set()
        for row in cur.fetchall():
            if (row["unit_type_state"] in FORBIDDEN_UNIT_TYPE_STATES
                    or row["tenancy_channel_state"] in FORBIDDEN_TENANCY_CHANNEL_STATES):
                forbidden.add(row["match_id"])
        explicit_gone = self.conn.execute("""
            SELECT COUNT(DISTINCT m.id) FROM matches m
            JOIN source_listings s ON s.site = m.site AND s.listing_id = m.listing_id
            WHERE s.explicit_gone_at IS NOT NULL
        """).fetchone()[0]
        return {"null_linked_current_matches": int(null_linked),
                "forbidden_trust_in_matches": len(forbidden),
                "explicit_gone_in_matches": int(explicit_gone)}

    def audit_integrity(self) -> Dict[str, Any]:
        """Spec 07 section 8 and Spec 09 section 10.3 integrity audit, as one result object.

        The human output and the JSON are two renderings of THIS dict, so both
        carry the same facts. Every statement it issues is a `SELECT`: the audit
        opens no write transaction and can be pointed at a copy through
        `run.py --audit-integrity --db`.
        """
        source_rows = self.conn.execute(
            "SELECT site, lifecycle_state, (property_id IS NOT NULL) AS linked, "
            "COUNT(*) AS n FROM source_listings "
            "GROUP BY site, lifecycle_state, linked").fetchall()
        by_site = _link_state_block(source_rows, "site", "by_site")
        by_lifecycle = _link_state_block(source_rows, "lifecycle_state",
                                         "by_lifecycle")
        sources = {"total": by_site["total"], "linked": by_site["linked"],
                   "unlinked": by_site["unlinked"],
                   "by_site": by_site["by_site"],
                   "by_lifecycle": by_lifecycle["by_lifecycle"]}

        evaluation_rows = self.conn.execute(
            "SELECT outcome, (property_id IS NOT NULL) AS linked, COUNT(*) AS n "
            "FROM evaluations WHERE is_current=1 "
            "GROUP BY outcome, linked").fetchall()
        by_outcome = _link_state_block(evaluation_rows, "outcome", "by_outcome")
        evaluations = {"total": by_outcome["total"],
                       "linked": by_outcome["linked"],
                       "unlinked": by_outcome["unlinked"],
                       "by_outcome": by_outcome["by_outcome"]}

        matched_properties = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT property_id FROM matches "
            "WHERE property_id IS NOT NULL").fetchall()}
        grouped_omissions = 0
        abnormal_omissions = 0
        for row in self.conn.execute(
                "SELECT property_id, COUNT(*) AS n FROM evaluations "
                "WHERE is_current=1 AND outcome='match' "
                "AND property_id IS NOT NULL "
                "GROUP BY property_id").fetchall():
            count = int(row["n"])
            if row["property_id"] in matched_properties:
                # The property's one `matches` row is the representative; the
                # rest of its matching evaluations are consolidated into it.
                grouped_omissions += max(0, count - 1)
            else:
                abnormal_omissions += count

        eligible_failures = 0
        for row in self.conn.execute(
                "SELECT unit_type_state, tenancy_channel_state, "
                "building_form_state FROM evaluations "
                "WHERE is_current=1 AND outcome='match' AND trust_outcome=?",
                (ELIGIBLE_TRUST_OUTCOME,)).fetchall():
            # Spec 08 section 6.2: an `eligible` verdict must not carry explicit
            # rejection evidence. The positive-evidence triple is gone - a
            # missing landlord or building verdict is exactly what this release
            # stopped holding a listing for.
            if (row["unit_type_state"] != ELIGIBLE_UNIT_TYPE_STATE
                    or row["tenancy_channel_state"] in FORBIDDEN_TENANCY_CHANNEL_STATES
                    or row["building_form_state"] in FORBIDDEN_BUILDING_FORM_STATES):
                eligible_failures += 1

        review_by_reason: Dict[str, int] = {}
        for row in self.needs_verification_rows():
            reason = row.get("primary_reason") or AUDIT_UNSPECIFIED
            review_by_reason[reason] = review_by_reason.get(reason, 0) + 1

        invalid_prefs = self.conn.execute("""
            SELECT COUNT(*) FROM properties
            WHERE (rank_priority NOT IN (0, 1))
               OR (rank_priority = 0 AND rank_priority_kind IS NOT NULL)
               OR (rank_priority = 1 AND (rank_priority_kind IS NULL OR rank_priority_kind != 'preferred_locality'))
        """).fetchone()[0]

        demographic_conflicts = self.conn.execute("""
            SELECT COUNT(*) FROM evaluations
            WHERE is_current = 1
              AND (student_requirement_state = 'conflict' OR age_requirement_state = 'conflict')
        """).fetchone()[0]

        seen_datasets: Dict[Tuple[str, str], Set[str]] = {}
        for r in self.trusted_block_rows(limit=10 ** 6):
            key = (r["site"], str(r["listing_id"]))
            seen_datasets.setdefault(key, set()).add("trusted")
        for v_name, rows in [
            ("review", self.needs_verification_rows()),
            ("gone", self.recently_gone_rows()),
            ("excluded", self.excluded_rows()),
        ]:
            for r in rows:
                key = (r["site"], str(r["listing_id"]))
                seen_datasets.setdefault(key, set()).add(v_name)
        duplicate_placement = sum(1 for datasets in seen_datasets.values() if len(datasets) > 1)

        integrity = self.integrity_counts()
        result: Dict[str, Any] = {
            "sources": sources,
            "evaluations": evaluations,
            "null_linked_current_matches": integrity["null_linked_current_matches"],
            "grouped_omissions": grouped_omissions,
            "abnormal_omissions": abnormal_omissions,
            "eligible_invariant_failures": eligible_failures,
            "forbidden_in_matches": integrity["forbidden_trust_in_matches"],
            "explicit_gone_in_matches": integrity["explicit_gone_in_matches"],
            "invalid_preference_values": int(invalid_prefs),
            "demographic_fact_conflicts": int(demographic_conflicts),
            "duplicate_dataset_placement": int(duplicate_placement),
            "review_by_reason": dict(sorted(review_by_reason.items())),
        }
        result["critical"] = [name for name in AUDIT_CRITICAL_INVARIANTS
                              if result[name]]
        result["ok"] = not result["critical"]
        return result

    def former_options_accounting(self, fixture_path: Optional[str] = None,
                                  ) -> List[Dict[str, Any]]:
        """Where every frozen former option ended up (sections 3.3 and 7).

        One row per entry of `tests/fixtures/former_options_v33.json` (the
        operator-frozen superset of section 3.3's 27 named former options), in
        fixture order, with:

            site, listing_id  the fixture's own identity
            spec_03_3_named   True when the entry matches a section 3.3 name
            view              trusted | review | gone | excluded |
                              out_of_window | missing_source
            primary_reason    section 7.1's primary reason, or None
            property_id       the live link, or None
            url               the live exact listing URL

        `out_of_window` (expired beyond `config.EXPIRED_VISIBLE_DAYS`) is the one
        explained absence. `missing_source` is the unexplained one: the fixture
        ID is absent from `source_listings`, or present with no current
        evaluation, so no view can be named for it - it must be zero on
        production. When a row is absent the fixture's own frozen
        `url_at_backup` is returned so the appendix can still cite the listing;
        no frozen property id is presented as a live one.
        """
        path = fixture_path or FORMER_OPTIONS_FIXTURE_PATH
        with open(path, encoding="utf-8") as fh:
            fixture = json.load(fh)
        entries = fixture.get("former_options") or []
        pairs = [(e.get("site"), e.get("listing_id")) for e in entries]
        rows = {(r["site"], r["listing_id"]): r
                for r in self._source_read_model_rows(pairs=pairs)}
        out = []
        for entry in entries:
            key = (entry.get("site"), str(entry.get("listing_id")))
            row = rows.get(key)
            view = None
            if row is not None and row.get("view") is not None:
                view = ACCOUNTING_VIEW_FOR_VIEW.get(row["view"], row["view"])
            if view is None:
                view = ACCOUNTING_VIEW_MISSING
            out.append({
                "site": key[0],
                "listing_id": key[1],
                "spec_03_3_named": bool(entry.get("spec_03_3_named")),
                "spec_03_3_names": list(entry.get("spec_03_3_names") or []),
                "view": view,
                "primary_reason": row.get("primary_reason") if row else None,
                "property_id": row.get("property_id") if row else None,
                "url": (row.get("url") if row else None) or entry.get("url_at_backup"),
                "lifecycle_state": (row.get("lifecycle_state") if row else None),
            })
        return out

    def get_stats(self) -> Dict[str, Any]:
        cur = self.conn.execute("SELECT COUNT(*) FROM seen")
        total_seen = cur.fetchone()[0]

        cur = self.conn.execute("SELECT COUNT(*) FROM properties")
        total_properties = cur.fetchone()[0]

        cur = self.conn.execute("SELECT COUNT(*) FROM matches WHERE (quarantined=0 OR quarantined IS NULL)")
        total_matches = cur.fetchone()[0]

        cur = self.conn.execute("SELECT COUNT(*) FROM matches WHERE category=1 AND (quarantined=0 OR quarantined IS NULL)")
        cat1_count = cur.fetchone()[0]

        cur = self.conn.execute("SELECT COUNT(*) FROM matches WHERE category=2 AND (quarantined=0 OR quarantined IS NULL)")
        cat2_count = cur.fetchone()[0]

        cur = self.conn.execute("SELECT COUNT(*) FROM matches WHERE category=3 AND (quarantined=0 OR quarantined IS NULL)")
        cat3_count = cur.fetchone()[0]

        # The stretch band cuts across the commute tiers, so it is counted
        # separately rather than carved out of cat1/cat2.
        cur = self.conn.execute("""
            SELECT COUNT(*) FROM matches
            WHERE budget_band='stretch' AND (quarantined=0 OR quarantined IS NULL)
        """)
        stretch_count = cur.fetchone()[0]

        cur = self.conn.execute("SELECT COUNT(*) FROM quarantine WHERE resolution_state='quarantined'")
        quarantine_count = cur.fetchone()[0]

        cur = self.conn.execute("""
            SELECT COUNT(*) FROM quarantine
            WHERE resolution_state='quarantined' AND reasons LIKE '%ungeocodable address%'
        """)
        ungeocodable_count = cur.fetchone()[0]

        cur = self.conn.execute("""
            SELECT relevance, COUNT(*) FROM matches
            WHERE (quarantined=0 OR quarantined IS NULL) GROUP BY relevance
        """)
        by_relevance = {r[0]: r[1] for r in cur.fetchall()}

        cur = self.conn.execute("SELECT first_seen_at FROM matches ORDER BY id DESC LIMIT 1")
        last_row = cur.fetchone()
        last_match_time = last_row[0] if last_row else None

        cur = self.conn.execute("""
            SELECT score FROM matches WHERE (quarantined=0 OR quarantined IS NULL)
        """)
        rank_counts = {l: 0 for l in config.RANK_TIER_LETTERS}
        for r in cur.fetchall():
            letter = tiers.rank_tier_of(r[0])
            rank_counts[letter] = rank_counts.get(letter, 0) + 1

        cur = self.conn.execute("""
            SELECT COUNT(*) FROM matches
            WHERE features_pending=1 AND (quarantined=0 OR quarantined IS NULL)
        """)
        pending_count = cur.fetchone()[0]

        return {
            "total_seen": total_seen,
            "total_properties": total_properties,
            "total_matches": total_matches,
            "cat1_count": cat1_count,
            "cat2_count": cat2_count,
            "cat3_count": cat3_count,
            "stretch_count": stretch_count,
            "quarantine_count": quarantine_count,
            "ungeocodable_count": ungeocodable_count,
            "actionable_count": by_relevance.get("actionable", 0),
            "intel_count": by_relevance.get("intel", 0),
            "last_match_time": last_match_time,
            "rank_counts": rank_counts,
            "pending_count": pending_count,
        }

    def soft_flag_counts(self) -> Dict[str, int]:
        """Count current soft-flag labels across visible matches (spec 10 section 8).

        Returns a ``{label: count}`` mapping, most frequent first. Used by the
        cut-over summary and the daily digest.
        """
        counts: Dict[str, int] = {}
        cur = self.conn.execute(
            "SELECT soft_flags FROM matches WHERE (quarantined=0 OR quarantined IS NULL)")
        for row in cur.fetchall():
            for flag in _parse_soft_flags(row[0]):
                label = flag.get("label")
                if label:
                    counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def get_portal_health(self) -> Dict[str, Dict[str, Any]]:
        """Return status and health metrics for each portal."""
        health = {}
        for site in config.SITES:
            # Portal health is about coverage, so an explicit targeted detail
            # fetch never counts as an attempt or as a recovery.
            cur = self.conn.execute("""
                SELECT * FROM scan_runs WHERE site=? AND (kind IS NULL OR kind='scheduled')
                ORDER BY id DESC LIMIT 1
            """, (site,))
            last_run = cur.fetchone()

            cur = self.conn.execute("""
                SELECT * FROM scan_runs WHERE site=? AND outcome='success'
                  AND (kind IS NULL OR kind='scheduled')
                ORDER BY id DESC LIMIT 1
            """, (site,))
            last_success = cur.fetchone()

            cur = self.conn.execute("""
                SELECT failure_count, error_message, first_failed_at FROM incidents WHERE site=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1
            """, (site,))
            open_incident = cur.fetchone()

            health[site] = {
                "site": site,
                "last_attempt": dict(last_run) if last_run else None,
                "last_success": dict(last_success) if last_success else None,
                "consecutive_failures": open_incident["failure_count"] if open_incident else 0,
                "error_message": open_incident["error_message"] if open_incident else None,
                "incident_since": open_incident["first_failed_at"] if open_incident else None,
            }
        return health


def append_log(rec: Dict[str, Any], status: str, notes: str = ""):
    """Append durable JSONL line to boligwatch.log."""
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    line = json.dumps({
        "ts": ts,
        "status": status,
        "category": rec.get("category"),
        "bw_id": rec.get("bw_id"),
        "site": rec.get("site"),
        "listing_id": rec.get("listing_id"),
        "title": rec.get("title"),
        "address": rec.get("address"),
        "rent_total": rec.get("rent_total"),
        "move_in_cost": rec.get("move_in_cost"),
        "nearest_station": rec.get("nearest_station"),
        "metro_walk_m": rec.get("metro_walk_m"),
        "walk_min": rec.get("walk_min"),
        "available_from": rec.get("available_from"),
        "url": rec.get("url"),
        "notes": notes,
    }, ensure_ascii=False)
    with open(config.LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
