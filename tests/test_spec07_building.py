"""Spec 07 section 9.3 — managed-block / building-form evidence.

Covers the building classifier of the v3.6 trust gate: the pure
``filters.classify_building_form(rec, store)`` (spec 07 sections 4.3, 6.3, 7.2,
9.3 cases 8 and 9, plan duty 3). Everything is local and deterministic: the
function reads the stored record and the store's reviewed profiles, fetches
nothing, and writes nothing.

Spec 08 section 4.2 (v37c1) inverted this gate into a reject-only filter: every
branch that used to be ``decision="review"`` is a ``pass`` with its reason text
kept, so an unidentified, stale, superseded or conflicting building form is a
missing badge rather than a hold. The ``small_or_private_property`` verdict is
still a rejection.

Deliberately NOT here: the final trust decision table (section 4.4, v36c3), the
wiring into ``evaluate_listing`` (section 6.4, v36i1) and the reviewed profile
manifest (section 5.2, v36m0/v36m1). No real profile is seeded by this row.
"""
import copy
import datetime
import json
import socket

import pytest

from filters import (BUILDING_EVIDENCE_MAX_DAYS, EvidenceDecision,
                     classify_building_form)
from store import Store

# The project/company advertiser block exactly as production stores it on the
# ad's own canonical record (boligportal#5813542 / Poppelstykket).
NEWBUILD_DETAILS = {
    "newbuild_id": 76666,
    "newbuild_name": "Poppelstykket",
    "company_name": "LIV Residential",
    "company_link": "https://livresidential.dk/bygninger/kobenhavn/poppelstykket",
    "units_amount": 360,
    "min_rooms": 1,
    "max_rooms": 2,
}

ADDRESS = "Jorisvej 1a, 2300 København"


def _iso(days_ago):
    when = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    return when.isoformat(timespec="seconds")


def _rec(**over):
    """A stored apartment advert at one exact house-numbered address."""
    rec = {
        "site": "boligportal",
        "listing_id": "5813542",
        "url": "https://www.boligportal.dk/lejligheder/koebenhavn/poppelstykket-76666",
        "title": "Poppelstykket",
        "listing_type": "lejlighed",
        "card_class": "apartment",
        "rooms": 1,
        "rent_total": 7800,
        "address": ADDRESS,
        "normalized_address": ADDRESS,
        "postcode": "2300",
    }
    rec.update(over)
    return rec


def _with_project(rec, details=None, is_newbuild=True, ad_id=5660253):
    """Give the advert its own canonical source_record with a project block."""
    rec = dict(rec)
    rec["source_record"] = {"id": ad_id, "is_newbuild": is_newbuild,
                            "newbuild_details": NEWBUILD_DETAILS if details is None else details}
    return rec


def _profile(store, key="block-1", normalized_address=ADDRESS,
             state="managed_multi_unit_block", kind="manual",
             url="https://operator.test/jorisvej-1a", verified_at=None,
             complex_key=None, excerpt="Jorisvej 1a administreres af Test A/S"):
    """One reviewed profile, written the way v36m1 will write it."""
    return store.upsert_building_profile(
        profile_key=key, building_form_state=state, management_state="professional",
        evidence_kind=kind, normalized_address=normalized_address, complex_key=complex_key,
        operator_name="Ejendomsselskabet Test A/S", evidence_url=url,
        evidence_excerpt=excerpt,
        verified_at=verified_at or _iso(1))


def _add_listing(store, address, listing_id, seen_days_ago=1, rec=None,
                 postcode="2300", site="lejebolig"):
    """One stored source listing linked to its own property row."""
    store.conn.execute(
        "INSERT INTO properties(normalized_address, postcode, lat, lon) VALUES(?,?,?,?)",
        (address, postcode, 55.65, 12.60))
    pid = store.conn.execute("SELECT id FROM properties ORDER BY id DESC LIMIT 1").fetchone()["id"]
    rec = rec or {"title": "1-værelses lejlighed", "listing_type": "lejlighed",
                  "card_class": "apartment", "rooms": 1, "size_m2": 34.0}
    store.conn.execute(
        "INSERT INTO source_listings(property_id, site, listing_id, url, "
        "lifecycle_state, first_seen_at, last_seen_at, raw_data, card_class) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (pid, site, str(listing_id), f"https://example.test/{listing_id}", "active",
         _iso(seen_days_ago + 1), _iso(seen_days_ago), json.dumps(rec),
         rec.get("card_class")))
    store.conn.commit()
    return pid


def _room_rec():
    return {"title": "14m² værelse på Frederiksberg", "listing_type": "ukendt",
            "card_class": "room", "rooms": 1}


# --------------------------------------------------------------------------- #
# Route 1 — an active reviewed building profile (section 4.3 bullet 1)          #
# --------------------------------------------------------------------------- #
def test_active_reviewed_profile_is_inherited_by_the_exact_address(temp_db):
    """A fresh reviewed managed-block profile for the exact house-numbered
    address makes the advert a managed block (section 4.3 bullet 1)."""
    _profile(temp_db, kind="manual", verified_at=_iso(3))
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "managed_multi_unit_block"
    assert dec.decision == "pass"
    assert dec.source == "manual"
    assert dec.excerpt


@pytest.mark.parametrize("kind,expected", [
    ("manual", "manual"),
    ("operator_page", "operator_page"),
    ("source_detail", "detail"),
])
def test_profile_evidence_kind_maps_onto_the_decision_source(temp_db, kind, expected):
    """Section 5.2's evidence_kind vocabulary reaches the read model unchanged."""
    _profile(temp_db, kind=kind, verified_at=_iso(1))
    dec = classify_building_form(_rec(), temp_db)
    assert dec.decision == "pass"
    assert dec.source == expected


def test_small_or_private_profile_rejects(temp_db):
    """A reviewed small/private property is explicit negative evidence, not an
    unknown (section 4.3 state list, section 1)."""
    _profile(temp_db, state="small_or_private_property", verified_at=_iso(1))
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "small_or_private_property"
    assert dec.decision == "reject"
    assert dec.reason == "small or private property"


def test_profile_without_an_evidence_url_is_not_positive(temp_db):
    """Section 4.3 bullet 1 requires an evidence URL on the profile; spec 08
    section 4.2 makes the resulting unknown a pass, so the reason is stated and
    the profile simply earns no badge."""
    _profile(temp_db, url=None, verified_at=_iso(1))
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "building profile evidence URL missing"


def test_profile_without_a_verification_date_is_not_positive(temp_db):
    """An undated profile cannot be shown to be fresh, so it is a review item."""
    temp_db.upsert_building_profile(
        profile_key="undated", building_form_state="managed_multi_unit_block",
        management_state="professional", evidence_kind="manual",
        normalized_address=ADDRESS, evidence_url="https://operator.test/undated")
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "unknown"
    assert dec.reason == "verification date unknown"


def test_evidence_older_than_ninety_days_is_stale(temp_db):
    """Section 7.2: building evidence older than 90 days is `verification
    stale`. The state and the reason text are kept; spec 08 section 4.2 makes
    the verdict a pass, so the staleness is a badge-side note rather than a
    hold (section 7.2's `(verification stale)` rendering)."""
    _profile(temp_db, verified_at=_iso(BUILDING_EVIDENCE_MAX_DAYS + 5))
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "verification stale"


def test_evidence_at_the_ninety_day_boundary_is_still_fresh(temp_db):
    """The window is closed at 90 days: exactly 90 days is not yet stale."""
    _profile(temp_db, verified_at=_iso(BUILDING_EVIDENCE_MAX_DAYS))
    dec = classify_building_form(_rec(), temp_db)
    assert dec.decision == "pass"


def test_superseded_profile_is_unknown_immediately(temp_db):
    """Section 7.2: a superseded profile is unknown at once, and the reason is
    said out loud instead of the row disappearing silently."""
    _profile(temp_db, verified_at=_iso(1))
    assert temp_db.supersede_building_profile("block-1", "operator changed") == 1
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "building profile superseded"


def test_two_active_profiles_that_disagree_name_the_conflict(temp_db):
    """Section 7.2: conflicting evidence names the conflict, it is never a
    silent pick between two reviewed profiles for one exact scope. Spec 08
    section 4.2 makes the verdict a pass: the conflict is a badge-side note, and
    the reason text is kept."""
    _profile(temp_db, key="block-a", state="managed_multi_unit_block", verified_at=_iso(1))
    _profile(temp_db, key="block-b", state="small_or_private_property", verified_at=_iso(1))
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "conflicting building evidence"


def test_profile_state_outside_the_section_4_3_vocabulary_is_not_positive(temp_db):
    """Only section 4.3's three states are verdicts; any other stored string
    must never read as a managed block."""
    _profile(temp_db, state="managed_block", verified_at=_iso(1))
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "building profile state unrecognised"


def test_reviewed_unknown_profile_outranks_the_project_block(temp_db):
    """A reviewer's explicit `unknown` is the reviewed verdict for that exact
    address (sections 2.5 and 11): the advert carries no managed-block badge.
    Spec 08 section 4.2 keeps the state and the reason and makes the verdict a
    pass, so the advert is eligible on the apartment row without a badge."""
    _profile(temp_db, state="unknown", verified_at=_iso(1))
    dec = classify_building_form(_with_project(_rec()), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "building form not identified"


def test_small_or_private_profile_outranks_the_project_block(temp_db):
    """Explicit negative reviewed evidence is not upgraded by the advert's own
    marketing block."""
    _profile(temp_db, state="small_or_private_property", verified_at=_iso(1))
    dec = classify_building_form(_with_project(_rec()), temp_db)
    assert dec.state == "small_or_private_property"
    assert dec.decision == "reject"


# --------------------------------------------------------------------------- #
# Case 9.3.9 — exact scope only, no leakage                                     #
# --------------------------------------------------------------------------- #
def test_profile_does_not_leak_to_another_house_number(temp_db):
    """A profile for 1a says nothing about number 3 on the same street."""
    _profile(temp_db, normalized_address="Jorisvej 1a, 2300 København", verified_at=_iso(1))
    dec = classify_building_form(
        _rec(normalized_address="Jorisvej 3, 2300 København", address="Jorisvej 3, 2300 København"),
        temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"


def test_profile_does_not_leak_to_a_street_level_property(temp_db):
    """A street-level property aggregates several buildings, so it inherits no
    building-form evidence at all (section 4.3, duty 2)."""
    street = "Gammel Køge Landevej, 2500 Valby"
    _profile(temp_db, normalized_address=street, verified_at=_iso(1))
    dec = classify_building_form(
        _rec(normalized_address=street, address=street, postcode="2500"), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"


def test_profile_match_is_exact_on_the_stored_address_form(temp_db):
    """The comparison is the existing folded exact one
    (`Store.normalize_exact_address`), so a differently written address is not
    silently treated as a match."""
    _profile(temp_db, normalized_address="Jorisvej 1a", verified_at=_iso(1))
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"


def test_reviewed_profile_for_a_configured_complex_is_inherited(temp_db):
    """Section 4.3: building-form evidence may be inherited by configured-complex
    siblings, matched on the configured key only."""
    _profile(temp_db, key="viola-complex", normalized_address=None,
             complex_key="viola-noerloevs-gade-complex", verified_at=_iso(1))
    member = _rec(normalized_address="Viola Nørløvs Gade 3, 2450 København",
                  address="Viola Nørløvs Gade 3, 2450 København", postcode="2450")
    assert classify_building_form(member, temp_db).decision == "pass"
    stranger = _rec(normalized_address="Jorisvej 1a, 2300 København")
    assert classify_building_form(stranger, temp_db).decision == "pass"


# --------------------------------------------------------------------------- #
# Route 2 — the advert's own authoritative operator/project page                #
# --------------------------------------------------------------------------- #
def test_operator_project_page_is_a_managed_block(temp_db):
    """A stored project block naming the development, the professional operator
    and that operator's page is section 4.3's second positive-evidence bullet."""
    dec = classify_building_form(_with_project(_rec()), temp_db)
    assert dec.state == "managed_multi_unit_block"
    assert dec.decision == "pass"
    assert dec.source == "structured"
    assert "Poppelstykket" in (dec.excerpt or "")
    assert "LIV Residential" in (dec.excerpt or "")
    assert "livresidential.dk" in (dec.excerpt or "")


def test_project_block_without_the_operator_page_needs_three_listings(temp_db):
    """Section 4.3 bullet 3: a named development plus three distinct
    self-contained listings at the exact building, with the same professional
    operator evidence, is a managed block."""
    details = {k: v for k, v in NEWBUILD_DETAILS.items() if k != "company_link"}
    _with_project(_rec(), details=details)
    for n in (1, 2, 3):
        _add_listing(temp_db, ADDRESS, f"L{n}")
    dec = classify_building_form(_with_project(_rec(), details=details), temp_db)
    assert dec.state == "managed_multi_unit_block"
    assert dec.decision == "pass"
    assert dec.source == "structured"


def test_two_recent_listings_are_not_enough(temp_db):
    """Two listings are below section 4.3 bullet 3's threshold."""
    details = {k: v for k, v in NEWBUILD_DETAILS.items() if k != "company_link"}
    for n in (1, 2):
        _add_listing(temp_db, ADDRESS, f"L{n}")
    dec = classify_building_form(_with_project(_rec(), details=details), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "building form not identified"


def test_room_cards_are_not_self_contained_listings(temp_db):
    """Three room adverts at one address are not three self-contained listings."""
    details = {k: v for k, v in NEWBUILD_DETAILS.items() if k != "company_link"}
    for n in (1, 2, 3):
        _add_listing(temp_db, ADDRESS, f"R{n}", rec=_room_rec())
    dec = classify_building_form(_with_project(_rec(), details=details), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"


def test_listings_outside_the_ninety_day_window_do_not_count(temp_db):
    """Section 4.3 bullet 3 counts listings inside the last 90 days only."""
    details = {k: v for k, v in NEWBUILD_DETAILS.items() if k != "company_link"}
    for n in (1, 2, 3):
        _add_listing(temp_db, ADDRESS, f"L{n}",
                     seen_days_ago=BUILDING_EVIDENCE_MAX_DAYS + 20)
    dec = classify_building_form(_with_project(_rec(), details=details), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"


def test_three_listings_across_a_configured_complex_count_together(temp_db):
    """Section 4.3 bullet 3 names the configured complex as the scope, so one
    configured development counts its members at both house numbers."""
    details = {k: v for k, v in NEWBUILD_DETAILS.items() if k != "company_link"}
    _add_listing(temp_db, "Viola Nørløvs Gade 1, 2450 København", "V1", postcode="2450")
    _add_listing(temp_db, "Viola Nørløvs Gade 1, 2450 København", "V2", postcode="2450")
    _add_listing(temp_db, "Viola Nørløvs Gade 3, 2450 København", "V3", postcode="2450")
    rec = _rec(normalized_address="Viola Nørløvs Gade 3, 2450 København",
               address="Viola Nørløvs Gade 3, 2450 København", postcode="2450")
    dec = classify_building_form(_with_project(rec, details=details), temp_db)
    assert dec.state == "managed_multi_unit_block"
    assert dec.decision == "pass"


def test_repeated_inventory_without_operator_evidence_is_not_proof(temp_db):
    """Case 9.3.8: three source cards at one address, and no source-scoped
    operator evidence, prove no professional management and no managed block."""
    for n in (1, 2, 3, 4):
        _add_listing(temp_db, ADDRESS, f"L{n}")
    dec = classify_building_form(_rec(), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "building form not identified"


def test_a_street_level_address_cannot_use_the_three_listing_rule(temp_db):
    """Without a house number the exact building is unknown, so the aggregate
    route cannot run (duty 2)."""
    details = {k: v for k, v in NEWBUILD_DETAILS.items() if k != "company_link"}
    street = "Gammel Køge Landevej, 2500 Valby"
    for n in (1, 2, 3):
        _add_listing(temp_db, street, f"L{n}", postcode="2500")
    rec = _rec(normalized_address=street, address=street, postcode="2500")
    dec = classify_building_form(_with_project(rec, details=details), temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"


# --------------------------------------------------------------------------- #
# Stored facts: never positive on their own, and a conflict is a review item   #
# --------------------------------------------------------------------------- #
def test_a_stored_building_fact_alone_is_not_positive_evidence(temp_db):
    """Duty 1: only the three authorised evidence routes can pass. A previously
    persisted fact is an input, not a verdict."""
    rec = _rec(building_form_state="managed_multi_unit_block", building_profile_id=7)
    dec = classify_building_form(rec, temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "building form not identified"


def test_stored_fact_contradicting_the_current_verdict_names_the_conflict(temp_db):
    """Section 7.2: the parser must not silently overwrite a stored decision
    that disagrees with the evidence now available; spec 08 section 4.2 makes
    the named conflict a badge-side note, so the verdict is a pass."""
    _profile(temp_db, state="managed_multi_unit_block", kind="operator_page", verified_at=_iso(1))
    rec = _rec(building_form_state="small_or_private_property",
               building_profile_id=3)
    dec = classify_building_form(rec, temp_db)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "conflicting building evidence"


def test_stored_fact_agreeing_with_the_verdict_is_not_a_conflict(temp_db):
    """Re-running the classifier over its own stored fact is a no-op."""
    _profile(temp_db, verified_at=_iso(1))
    rec = _rec(building_form_state="managed_multi_unit_block", building_profile_id=1)
    dec = classify_building_form(rec, temp_db)
    assert dec.state == "managed_multi_unit_block"
    assert dec.decision == "pass"


def test_a_stored_unknown_is_not_a_conflict(temp_db):
    """A stored `unknown` was never a decision, so fresh evidence may still
    establish the block (unlike a definite stored verdict)."""
    _profile(temp_db, verified_at=_iso(1))
    rec = _rec(building_form_state="unknown")
    dec = classify_building_form(rec, temp_db)
    assert dec.state == "managed_multi_unit_block"
    assert dec.decision == "pass"


# --------------------------------------------------------------------------- #
# Contract: store optional, determinism, bounded excerpts, no network          #
# --------------------------------------------------------------------------- #
def test_classifier_still_decides_without_a_store():
    """The store-free route (the advert's own operator page) still works when no
    store is available; the store routes are skipped rather than guessed."""
    assert classify_building_form(_with_project(_rec()), None).decision == "pass"
    assert classify_building_form(_rec(), None).decision == "pass"


def test_classifier_is_deterministic_and_does_not_mutate_the_record(temp_db):
    _profile(temp_db, verified_at=_iso(1))
    _profile(temp_db, key="small-2", state="small_or_private_property",
             normalized_address="Jorisvej 3, 2300 København", verified_at=_iso(1))
    recs = [
        _with_project(_rec()),
        _rec(),
        _rec(normalized_address="Jorisvej 3, 2300 København",
             address="Jorisvej 3, 2300 København"),
        _rec(building_form_state="small_or_private_property"),
        _rec(normalized_address="Gammel Køge Landevej, 2500 Valby",
             address="Gammel Køge Landevej, 2500 Valby"),
    ]
    for rec in recs:
        before = copy.deepcopy(rec)
        first = classify_building_form(rec, temp_db)
        second = classify_building_form(rec, temp_db)
        assert (first.state, first.decision, first.reason, first.source,
                first.excerpt) == (second.state, second.decision, second.reason,
                                   second.source, second.excerpt)
        assert rec == before, "classifier mutated its input record"


def test_every_excerpt_is_bounded_to_two_hundred_characters(temp_db):
    long = "z" * 600
    _profile(temp_db, key="long", url=f"https://operator.test/{long}",
             excerpt=long, verified_at=_iso(1))
    _add_listing(temp_db, ADDRESS, "L1")
    recs = [
        _with_project(_rec(), details=dict(NEWBUILD_DETAILS, newbuild_name=long,
                                           company_name=long, company_link=long)),
        _rec(normalized_address="Jorisvej 1a, 2300 København"),
        _rec(),
        _with_project(_rec(), details=dict(NEWBUILD_DETAILS, company_link=None)),
    ]
    for rec in recs:
        dec = classify_building_form(rec, temp_db)
        assert isinstance(dec, EvidenceDecision)
        assert dec.excerpt is None or len(dec.excerpt) <= 200


def test_classifier_makes_no_network_call(temp_db, monkeypatch):
    """Section 12: trust classification makes zero network calls."""

    def _boom(*args, **kwargs):
        raise AssertionError("classify_building_form attempted a network call")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    _profile(temp_db, verified_at=_iso(1))
    _add_listing(temp_db, ADDRESS, "L1")
    for rec in (_with_project(_rec()), _rec(), _room_rec()):
        classify_building_form(rec, temp_db)


def test_classifier_is_json_round_trip_safe(temp_db):
    """The stored record reaches the classifier as JSON from ``raw_data``."""
    dec_a = classify_building_form(_with_project(_rec()), temp_db)
    dec_b = classify_building_form(json.loads(json.dumps(_with_project(_rec()))), temp_db)
    assert dec_a.state == dec_b.state == "managed_multi_unit_block"
    assert dec_a.excerpt == dec_b.excerpt


def test_sibling_count_read_is_exact_scope_and_unit_type_aware(temp_db):
    """The store read behind bullet 3 counts distinct self-contained listings at
    the exact house-numbered address inside the window, and nothing else."""
    _add_listing(temp_db, ADDRESS, "L1")
    _add_listing(temp_db, ADDRESS, "L2")
    _add_listing(temp_db, ADDRESS, "R1", rec=_room_rec())
    _add_listing(temp_db, "Jorisvej 3, 2300 København", "L3")
    _add_listing(temp_db, ADDRESS, "OLD1", seen_days_ago=BUILDING_EVIDENCE_MAX_DAYS + 30)
    found = temp_db.self_contained_listings_at_building(address=ADDRESS)
    assert sorted(r["listing_id"] for r in found) == ["L1", "L2"]
    assert temp_db.self_contained_listings_at_building(
        address="Gammel Køge Landevej, 2500 Valby") == []


def test_existing_active_profile_api_is_unchanged(temp_db):
    """v36c2 adds reads beside the v36s1 API; the v36s1 behaviour stands."""
    _profile(temp_db, verified_at=_iso(1))
    assert temp_db.active_building_profile_for_address(ADDRESS)["profile_key"] == "block-1"
    assert temp_db.get_building_profile("block-1")["state"] == "active"
    assert isinstance(Store.normalize_exact_address(ADDRESS), str)


def test_reads_work_on_a_migrated_legacy_database(tmp_path):
    """The new reads need only the v36s1 schema, which the additive migration
    installs on a pre-v3.6 database. Proved by stripping it first."""
    path = str(tmp_path / "legacy-building.db")
    legacy = Store(db_path=path)
    _add_listing(legacy, ADDRESS, "L1")
    _add_listing(legacy, ADDRESS, "L2")
    for col in ("unit_type_state", "unit_type_evidence_source",
                "unit_type_evidence_excerpt", "tenancy_channel_state",
                "landlord_name", "landlord_evidence_source",
                "landlord_evidence_excerpt", "building_form_state",
                "building_profile_id"):
        legacy.conn.execute(f"ALTER TABLE source_listings DROP COLUMN {col}")
    legacy.conn.execute("DROP TABLE building_profiles")
    legacy.conn.commit()
    legacy.conn.close()

    migrated = Store(db_path=path)
    assert migrated.building_profiles_for_exact_address(ADDRESS) == []
    assert len(migrated.self_contained_listings_at_building(address=ADDRESS)) == 2
    assert classify_building_form(_rec(), migrated).decision == "pass"
    migrated.conn.close()
