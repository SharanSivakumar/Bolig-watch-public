"""Spec 07 trust-gate evaluation integration and section 5.1 persistence (v36i1).

Covers plan duty 4: the section 4.4 table applied inside
``filters.evaluate_listing`` (placement pinned by the plan), the section 5.3
outcome mapping on both entry paths, the section 5.1 write mapping
``filters.source_trust_values`` and the read-back repair that makes a stored
trust fact reach the classifiers again.

Everything here is local: no portal request, no DAWA/OSRM/Overpass call and no
alert. The entry paths are the two the plan names - ``run.process_candidate``
(the scan path) and ``run.refresh_local`` (the local path, which re-evaluates
stored listings without any network access).

Spec 08 (v37c1) changed what reaches this file: the trust table's only review
row is now an unknown UNIT type (section 4.3 row 5), so the review fixtures are
built from that route, and section 4.4 keeps a gate rejection instead of
converting it to a quarantine.

Deliberately NOT here: the composer's own table (v36c3, see
``test_spec07_trust_decision.py``), the dashboard views (v36q1-v36u3) and match
rebuilding (v36i2).
"""
import inspect
import json
import socket

import pytest

import config
import filters
import geo
import notify
import run
from conftest import with_detail_evidence
from store import Store, source_record_view

# --- Fixtures -----------------------------------------------------------------
# The advert's own operator/project block, the same shape the migrated shared
# fixtures carry (section 4.2 bullet 3 for the tenancy channel, section 4.3
# route 2 for the building form). Production stores it on the ad's own canonical
# record: boligportal#5813542 / Poppelstykket (v36c1 route 3a).
OPERATOR_BLOCK = {
    "id": "sb-9001",
    "url": "https://example.test/operator/listing",
    "listing_id": "9001",
    "is_newbuild": True,
    "newbuild_details": {
        "newbuild_name": "Testgade Have",
        "company_name": "Test Ejendomme A/S",
        "company_link": "https://example.test/operator/testgade-have",
    },
}

# A canonical detail page: the authoritative Boligtype label, a private kitchen,
# a private bathroom and an unlimited period, i.e. a listing that passes every
# existing gate.
DETAIL_TEXT = ("Boligtype Lejlighed. 1 værelse, eget køkken og eget "
               "badeværelse. Ubegrænset lejeperiode.")

# The same advert as a Lejebolig room: the detail page carries no Boligtype label
# and the product title says "værelse på" (section 4.1 precedence 3).
ROOM_TEXT = "Lys bolig med eget tekøkken og eget bad. Depositum 3 måneder."


def _record(site="boligportal", listing_id="9001", *, operator=True,
            text=DETAIL_TEXT, title="1-værelses lejlighed", address="Jorisvej 1a, 2300 København",
            **over):
    """One stored advert: detail evidence, plus its own operator block."""
    rec = {
        "site": site,
        "listing_id": str(listing_id),
        "url": f"https://example.test/{site}/{listing_id}",
        "title": title,
        "address": address,
        "normalized_address": address,
        "postcode": "2300",
        "geo_precision": "street",
        "listing_type": "lejlighed",
        "card_class": "apartment",
        "rooms": 1,
        "size_m2": 32.0,
        "rent_total": 8300,
        "deposit": 20000,
        "prepaid_rent": 0,
        "available_from": "2027-01-20",
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "lat": 55.65,
        "lon": 12.60,
    }
    rec.update(over)
    return with_detail_evidence(rec, text=text, operator=operator)


def _unknown_unit_record(listing_id, **over):
    """A stored advert whose unit type is unanswered.

    Spec 08 section 4.3 row 5 makes the unknown unit type the ONLY remaining
    route to a `review` verdict, so the review cases build it deliberately: no
    card marker in the title, no `Boligtype` label in the detail text and a
    neutral ``listing_type``.
    """
    return _record(listing_id=listing_id, title="Annonce 5",
                   listing_type="ukendt", card_class="unknown",
                   text=ROOM_TEXT, operator=False, **over)


def _seed(store, rec):
    """Put one record in the store the way a scan would, and return its property."""
    prop_id, _bw, _status = store.resolve_property(
        addr=rec["normalized_address"], lat=rec["lat"], lon=rec["lon"],
        rooms=rec.get("rooms"), size_m2=rec.get("size_m2"),
        postcode=rec.get("postcode"), geo_precision=rec.get("geo_precision"))
    raw = dict(rec)
    raw["property_id"] = prop_id
    store.record_source_listing(
        property_id=prop_id, site=rec["site"], listing_id=str(rec["listing_id"]),
        url=rec["url"], raw_data=raw, card_class=rec.get("card_class"),
        detail_state="fetched", card_price=rec.get("card_price"),
        card_text=rec.get("card_text"))
    return prop_id


def _stored_row(store, rec):
    row = store.get_source_listing(rec["site"], str(rec["listing_id"]))
    assert row is not None, f"{rec['site']}#{rec['listing_id']} was not stored"
    return dict(row)


def _current(store, rec):
    row = _stored_row(store, rec)
    evaluation = store.current_evaluation(row["id"])
    assert evaluation is not None, f"{rec['listing_id']} has no current evaluation"
    return dict(evaluation)


def _verdict(decision):
    """The trust verdict as a comparable tuple: outcome, reason, three states."""
    return (decision.outcome, decision.reason, decision.unit_type_state,
            decision.tenancy_channel_state, decision.building_form_state)


def _result_with(trust_decision):
    """An `EvaluationResult` carrying one composed verdict.

    It is the exact input `filters.source_trust_values` reads, which is how the
    mapping cases that no stored record can produce (an empty advertiser name, an
    absent decision) are pinned against the real result type.
    """
    return filters.EvaluationResult(
        passed=False, quarantined=False, quarantine_reasons=[], rejected=False,
        rejection_reason=None, category=None, category_name=None, score=0.0,
        score_breakdown={}, risk_state="low", risk_evidence=[],
        confidence_points=0, confidence_details=[], city_distance_m=None,
        metro_walk_m=None, straight_dist_m=None, nearest_station=None,
        walk_min=None, transit_station=None, transit_walk_m=None,
        is_target_window=False, available_date_str=None, rent_total=None,
        move_in_cost=None, move_in_cost_verified=False, lease_verified=False,
        kitchen_verified=False, bathroom_verified=False, rooms_verified=False,
        trust_decision=trust_decision)


def _stored_verdict(store, rec):
    """Re-classify a stored row exactly as the local pass merges it."""
    return _verdict(filters.classify_listing_trust(
        source_record_view(_stored_row(store, rec)), store))


def _seed_profile(store, profile_key, address, *, days_ago=2,
                  state="managed_multi_unit_block"):
    """One reviewed building profile for an exact house-numbered address."""
    import datetime
    verified = (datetime.datetime.now()
                - datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")
    return store.upsert_building_profile(
        profile_key=profile_key, building_form_state=state,
        management_state="professional", evidence_kind="operator_page",
        display_name=address.split(",")[0], normalized_address=address,
        operator_name="Test Ejendomme A/S",
        evidence_url=f"https://example.test/operator/{profile_key}",
        evidence_excerpt=f"drift af {address.split(',')[0]}",
        verified_at=verified)


@pytest.fixture(autouse=True)
def offline_metro(monkeypatch):
    """A deterministic metro at Rådhuspladsen, as the shared fixtures have."""
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5,
                                              "metro"))


# --------------------------------------------------------------------------- #
# Duty 4 clause 1 - the discriminating pair                                      #
# --------------------------------------------------------------------------- #
def test_operator_block_is_eligible_and_still_traverses_the_gates(mock_stations):
    """A migrated fixture is eligible, and eligibility does not bypass a gate."""
    rec = _record()
    decision = filters.classify_listing_trust(rec, None)
    assert decision.outcome == "eligible"

    res = filters.evaluate_listing(rec, mock_stations)
    assert res.trust_outcome == "eligible"
    assert res.trust_reason is None
    assert res.passed is True
    assert res.quarantined is False and res.rejected is False

    # The same advert above the hard rent cap is still refused by the gate: the
    # trust row only decides that the gates run, never what they decide.
    over_cap = _record(listing_id="9001b", rent_total=config.RENT_CAP_MAX + 1)
    capped = filters.evaluate_listing(over_cap, mock_stations)
    assert capped.trust_outcome == "eligible"
    assert capped.rejected is True
    assert str(config.RENT_CAP_MAX) in (capped.rejection_reason or "")


def test_the_same_record_without_the_block_is_eligible_and_the_unknown_unit_reviews(
        temp_db, mock_stations):
    """Spec 08 section 4.3: the block's absence no longer creates a review row.

    The same record without its operator block is row 4's `eligible` now, and
    row 5's unknown unit type is the only remaining `review` verdict - reached
    store-free and on both entry paths, which is what keeps the fixture
    migration from hiding the review route.
    """
    rec = _record(operator=False)
    assert "source_record" not in rec
    no_block = filters.evaluate_listing(rec, mock_stations)
    assert no_block.trust_outcome == "eligible"
    assert no_block.trust_reason is None
    assert no_block.passed is True
    assert no_block.quarantined is False and no_block.rejected is False

    review = _unknown_unit_record(listing_id="9010")
    res = filters.evaluate_listing(review, mock_stations)
    # v3.11 (spec 10 section 5.2): an unknown unit type ranks with the
    # Unit-type-unconfirmed badge instead of holding.
    assert res.quarantined is False
    assert res.rejected is False and res.passed is True
    assert res.rejection_reason is None
    assert res.trust_outcome == "review"
    assert res.trust_reason == config.TRUST_REASON_UNIT_UNVERIFIED
    assert any(f.code == "unit_type_unconfirmed" for f in res.soft_flags)

    # Scan path: stored, evaluated, matched.
    run.process_candidate(_unknown_unit_record(listing_id="9010"), mock_stations,
                          temp_db, None, dry_run=True)
    scan_eval = _current(temp_db, _unknown_unit_record(listing_id="9010"))
    assert scan_eval["outcome"] == "match"
    assert scan_eval["trust_outcome"] == "review"
    assert temp_db.get_match_row(scan_eval["property_id"]) is not None

    # Local path: the same verdict from the stored record alone.
    local_review = _unknown_unit_record(listing_id="9011")
    _seed(temp_db, local_review)
    run.refresh_local(temp_db, export=False, quiet=True)
    local_eval = _current(temp_db, local_review)
    assert local_eval["trust_outcome"] == "review"
    assert local_eval["trust_reason"] == config.TRUST_REASON_UNIT_UNVERIFIED
    assert temp_db.get_match_row(local_eval["property_id"]) is not None


def test_the_eligible_sibling_of_that_pair_still_matches(temp_db, mock_stations):
    """The pair is an evidence difference, not a bypass: with the block present
    the same shape materialises a match row on both entry paths, and since v3.7
    the block earns the badge rather than admitting the advert."""
    run.process_candidate(_record(listing_id="9012"), mock_stations, temp_db, None,
                          dry_run=True)
    assert temp_db.get_match_row(_current(temp_db, _record(listing_id="9012"))["property_id"]) is not None

    local = _record(listing_id="9013")
    prop_id = _seed(temp_db, local)
    run.refresh_local(temp_db, export=False, quiet=True)
    assessment = _current(temp_db, local)
    assert assessment["outcome"] == "match"
    assert assessment["trust_outcome"] == "eligible"
    assert temp_db.get_match_row(prop_id) is not None


# --------------------------------------------------------------------------- #
# Duty 4 clause 2 - the room reason                                              #
# --------------------------------------------------------------------------- #
def test_a_room_advert_rejects_with_the_section_6_2_reason(mock_stations):
    """Section 4.4 row 1: a card phrase is enough, and the primary reason is
    section 6.2's own wording (section 7.1 item 1)."""
    rec = _record(title="14m² værelse på Frederiksberg", text=ROOM_TEXT)
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.rejected is True
    assert res.rejection_reason == config.TRUST_REASON_ROOM == "room rental / not self-contained"
    assert res.trust_outcome == "reject"
    assert res.trust_reason == config.TRUST_REASON_ROOM
    assert res.unit_type_state == "room_rental"


def test_a_room_rejects_on_both_entry_paths_and_never_matches(temp_db, mock_stations):
    """A room advert is absent from `matches` however it was processed."""
    room = _record(listing_id="9020", title="Værelse på Amager", text=ROOM_TEXT)
    run.process_candidate(_record(listing_id="9020", title="Værelse på Amager",
                                  text=ROOM_TEXT), mock_stations, temp_db, None,
                          dry_run=True)
    scan_eval = _current(temp_db, room)
    assert scan_eval["trust_outcome"] == "reject"
    assert scan_eval["outcome"] == "rejected"
    assert temp_db.get_match_row(scan_eval["property_id"]) is None

    local_room = _record(listing_id="9021", title="Værelse på Amager", text=ROOM_TEXT)
    prop_id = _seed(temp_db, local_room)
    run.refresh_local(temp_db, export=False, quiet=True)
    local_eval = _current(temp_db, local_room)
    assert local_eval["trust_outcome"] == "reject"
    assert local_eval["reason"] == config.TRUST_REASON_ROOM
    assert temp_db.get_match_row(prop_id) is None


# --------------------------------------------------------------------------- #
# Duty 4 clause 3 and 4 - the mapping's precedence over the gates                #
# --------------------------------------------------------------------------- #
def test_a_review_row_that_fails_a_gate_stays_rejected(mock_stations):
    """Spec 08 section 4.4: a review verdict never hides a gate rejection, so
    the rent gate owns the row and the trust reason is appended to
    `quarantine_reasons` instead of replacing the rejection."""
    rec = _unknown_unit_record("9501", rent_total=config.RENT_CAP_MAX + 1)
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.rejected is True
    assert res.quarantined is False
    assert res.passed is False
    assert res.rejection_reason == (f"rent {config.RENT_CAP_MAX + 1} > "
                                    f"{config.RENT_CAP_MAX}")
    assert res.trust_outcome == "review"
    assert res.trust_reason == config.TRUST_REASON_UNIT_UNVERIFIED


def test_a_reject_row_reports_the_trust_reason_not_the_gate_reason(mock_stations):
    """Section 5.3: `reject` maps to `rejected` with the rejecting component's own
    reason; the gate's own verdict stays visible in the other result fields."""
    rec = _record(title="Værelse på Frederiksberg", text=ROOM_TEXT,
                  rent_total=config.RENT_CAP_MAX + 1)
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.rejected is True
    assert res.quarantined is False
    assert res.rejection_reason == config.TRUST_REASON_ROOM
    assert res.trust_outcome == "reject"


def test_an_eligible_rows_trust_reason_is_none_and_its_outcome_is_the_gates(mock_stations):
    """Plan duty 1 (P) 1 and 3: the eligible path changes nothing the gates do."""
    rec = _record()
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.trust_reason is None
    assert res.trust_decision is not None
    assert res.trust_decision.outcome == "eligible"
    assert res.passed is True

    quarantined = filters.evaluate_listing(
        _record(listing_id="9001c", rent_total=None, card_price=None),
        mock_stations)
    assert quarantined.trust_outcome == "eligible"
    assert quarantined.quarantined is True
    assert "rent unknown" in quarantined.quarantine_reasons
    assert quarantined.rejection_reason is None


def test_the_trust_verdict_is_attached_on_every_path(mock_stations):
    """Duty 1: the five result attributes plus the composed object, eligible
    included, and never a new key on the record."""
    for rec in (_record(), _record(operator=False),
                _record(title="Værelse på Amager", text=ROOM_TEXT)):
        keys_before = set(rec)
        res = filters.evaluate_listing(rec, mock_stations)
        assert res.unit_type_state in config.TRUST_UNIT_TYPE_STATES
        assert res.tenancy_channel_state in config.TRUST_TENANCY_CHANNEL_STATES
        assert res.building_form_state in config.TRUST_BUILDING_FORM_STATES
        assert res.trust_outcome in config.TRUST_OUTCOMES
        assert isinstance(res.trust_decision, filters.TrustDecision)
        assert res.trust_decision.outcome == res.trust_outcome
        assert res.trust_reason == res.trust_decision.reason
        # The trust verdict lives on the result and in the store, never on the
        # record. The keys the gates have always written - `rec["rooms"]`,
        # `rec["rent_total"]`, the move-in figures and the `_unit_decision` the
        # composer consumes - are the only additions, and they are pre-existing
        # behaviour (plan duty 1, item 1).
        assert set(rec) - keys_before <= {"_unit_decision", "move_in_cost",
                                         "move_in_cost_verified", "rooms",
                                         "rent_total"}
        assert "trust_decision" not in rec
        assert "_trust_decision" not in rec


# --------------------------------------------------------------------------- #
# Duty 4 clause 5 - the section 5.1 mapping                                      #
# --------------------------------------------------------------------------- #
def test_the_mapping_keys_are_exactly_record_source_trust_keywords():
    """The write site passes the dict through as keywords (plan duty 2 (a))."""
    parameters = set(inspect.signature(Store.record_source_trust).parameters)
    parameters -= {"self", "site", "listing_id"}
    values = filters.source_trust_values(_result_with(None))
    assert set(values) == parameters
    assert set(values) == set(filters.SOURCE_TRUST_COLUMNS)
    assert set(values.values()) == {None}, "an absent decision establishes nothing"


def test_the_mapping_writes_the_groups_its_conditions_admit(mock_stations):
    """Every group rule of plan duty 2 (a), read from `source_trust_values`."""
    # 1. The advert's own project block: unit trio from the detail label, the
    #    landlord triple from the structured advertiser, the building state from
    #    route 2, and no profile id (no profile informed the verdict).
    eligible = filters.source_trust_values(
        filters.evaluate_listing(_record(), mock_stations))
    assert eligible == {
        "unit_type_state": "self_contained_apartment",
        "unit_type_evidence_source": "detail",
        "unit_type_evidence_excerpt": "Boligtype lejlighed",
        "tenancy_channel_state": "professional_direct",
        "landlord_name": "Test Ejendomme A/S",
        "landlord_evidence_source": "structured",
        "landlord_evidence_excerpt": "Testgade Have Test Ejendomme A/S",
        "building_form_state": "managed_multi_unit_block",
        "building_profile_id": None,
    }

    # 2. A card-derived unit verdict is never persisted: it is re-derived from the
    #    record's own stored text on every pass, and the precedence-1 slot would
    #    let it outrank a later authoritative `Boligtype` (sections 3.1-3.2).
    card_only = filters.source_trust_values(filters.evaluate_listing(
        _record(listing_id="9030", text=ROOM_TEXT, title="Lejlighed i København",
                card_class="apartment"), mock_stations))
    assert card_only["unit_type_state"] is None
    assert card_only["unit_type_evidence_source"] is None
    assert card_only["unit_type_evidence_excerpt"] is None

    # 3. A definite detail verdict, here the room one, is written.
    detail_room = filters.source_trust_values(filters.evaluate_listing(
        _record(listing_id="9031", text="Boligtype Værelse. Eget bad.",
                title="Lejlighed i København"), mock_stations))
    assert detail_room["unit_type_state"] == "room_rental"
    assert detail_room["unit_type_evidence_source"] == "detail"

    # 4. A review row writes no state: the definite-only rule, so an earlier
    #    definite fact is never overwritten by this pass's ignorance.
    review = filters.source_trust_values(filters.evaluate_listing(
        _record(listing_id="9032", operator=False), mock_stations))
    assert review["tenancy_channel_state"] is None
    assert review["building_form_state"] is None
    assert review["landlord_name"] is None

    # 5. A sublet is a definite section 4.2 state, and it writes no landlord.
    sublet = filters.source_trust_values(filters.evaluate_listing(
        _record(listing_id="9033",
                text="Boligtype Lejlighed. Udbydes til fremleje i 6 måneder."),
        mock_stations))
    assert sublet["tenancy_channel_state"] == "sublet"
    assert sublet["landlord_name"] is None
    assert sublet["landlord_evidence_source"] is None


def test_a_professional_direct_verdict_with_an_empty_name_writes_the_evidence():
    """Spec 08 section 5: the evidence pair is persisted for every source-scoped
    professional fact - routes 3c and 3d are badge evidence and name no landlord -
    so only `landlord_name` is conditional on a name.

    Both *naming* routes require one (route 3a the block's `company_name`, route
    3b a non-empty stored name), so this case is composed directly: it pins the
    mapping's own condition rather than a route.
    """
    tenancy = filters.EvidenceDecision(
        state="professional_direct", decision="pass",
        reason="professional advertiser", source="structured",
        excerpt="Testgade Have", landlord_name="   ")
    decision = filters.TrustDecision(
        outcome="eligible", reason=None,
        unit_type=filters.EvidenceDecision(
            state="self_contained_apartment", decision="pass",
            reason="self-contained apartment", source="detail",
            excerpt="Boligtype lejlighed"),
        tenancy_channel=tenancy,
        building_form=filters.EvidenceDecision(
            state="managed_multi_unit_block", decision="pass",
            reason="professionally managed multi-unit block",
            source="operator_page", excerpt="drift"))
    values = filters.source_trust_values(_result_with(decision))
    assert values["landlord_name"] is None
    assert values["landlord_evidence_source"] == "structured"
    assert values["landlord_evidence_excerpt"] == "Testgade Have"
    # The state itself is still definite, so it is written.
    assert values["tenancy_channel_state"] == "professional_direct"
    assert values["building_form_state"] == "managed_multi_unit_block"


def test_a_profile_derived_verdict_writes_its_profile_id(temp_db, mock_stations):
    """Section 7.2's evidence date: the profile that informed the verdict is
    readable from the column, including when its evidence was stale."""
    _seed_profile(temp_db, "testgade-have", "Testgade 12, 2300 København")
    profile = temp_db.get_building_profile("testgade-have")
    managed = _record(listing_id="9040", address="Testgade 12, 2300 København")
    values = filters.source_trust_values(
        filters.evaluate_listing(managed, mock_stations, store=temp_db))
    assert values["building_form_state"] == "managed_multi_unit_block"
    assert values["building_profile_id"] == profile["id"]

    _seed_profile(temp_db, "stalevej-9", "Stalevej 9, 2300 København", days_ago=400)
    stale_profile = temp_db.get_building_profile("stalevej-9")
    stale = _record(listing_id="9041", address="Stalevej 9, 2300 København")
    stale_values = filters.source_trust_values(
        filters.evaluate_listing(stale, mock_stations, store=temp_db))
    assert stale_values["building_form_state"] is None
    assert stale_values["building_profile_id"] == stale_profile["id"]


# --------------------------------------------------------------------------- #
# Duty 4 clause 6 - the round trip on both entry paths                           #
# --------------------------------------------------------------------------- #
def test_the_scan_path_round_trips_and_is_idempotent(temp_db, mock_stations):
    """A pass writes the section 5.1 facts beside its evaluation, and reading the
    row back re-classifies to the same verdict (duty 2's read-back repair)."""
    rec = _record(listing_id="9050")
    run.process_candidate(dict(rec), mock_stations, temp_db, None, dry_run=True)
    row = _stored_row(temp_db, rec)
    evaluation = _current(temp_db, rec)

    assert row["unit_type_state"] == "self_contained_apartment"
    assert row["tenancy_channel_state"] == "professional_direct"
    assert row["landlord_name"] == "Test Ejendomme A/S"
    assert row["landlord_evidence_source"] == "structured"
    assert row["building_form_state"] == "managed_multi_unit_block"
    # Section 5.3's evaluation columns come from the same result.
    assert evaluation["trust_outcome"] == "eligible"
    assert evaluation["trust_reason"] is None
    assert evaluation["unit_type_state"] == row["unit_type_state"]
    assert evaluation["tenancy_channel_state"] == row["tenancy_channel_state"]
    assert evaluation["building_form_state"] == row["building_form_state"]
    assert evaluation["rules_version"] == config.eligibility_rules_version()
    assert evaluation["rules_version"].startswith(config.RULES_VERSION_TAG + "+")
    assert _stored_verdict(temp_db, rec) == (
        "eligible", None, "self_contained_apartment", "professional_direct",
        "managed_multi_unit_block")

    # A second pass writes the same nine values.
    first = [row[column] for column in filters.SOURCE_TRUST_COLUMNS]
    run.process_candidate(dict(rec), mock_stations, temp_db, None, dry_run=True)
    second = [_stored_row(temp_db, rec)[column]
              for column in filters.SOURCE_TRUST_COLUMNS]
    assert second == first


def test_the_local_path_round_trips_and_is_idempotent(temp_db, mock_stations):
    rec = _record(listing_id="9051")
    _seed(temp_db, rec)
    run.refresh_local(temp_db, export=False, quiet=True)
    row = _stored_row(temp_db, rec)
    evaluation = _current(temp_db, rec)

    assert row["tenancy_channel_state"] == "professional_direct"
    assert row["landlord_name"] == "Test Ejendomme A/S"
    assert row["building_form_state"] == "managed_multi_unit_block"
    assert evaluation["trust_outcome"] == "eligible"
    assert evaluation["rules_version"].startswith(config.RULES_VERSION_TAG + "+")
    assert _stored_verdict(temp_db, rec) == (
        "eligible", None, "self_contained_apartment", "professional_direct",
        "managed_multi_unit_block")

    first = [row[column] for column in filters.SOURCE_TRUST_COLUMNS]
    run.refresh_local(temp_db, export=False, quiet=True)
    second = [_stored_row(temp_db, rec)[column]
              for column in filters.SOURCE_TRUST_COLUMNS]
    assert second == first


def test_the_local_path_reclassifies_a_stored_detail_label(temp_db, mock_stations):
    """Section 6.2: a stored detail `Boligtype` is re-read without a re-fetch, so
    a room row is rejected on the local path and its detail fact is persisted."""
    room = _record(listing_id="9052", text="Boligtype Værelse. Eget bad og køkken.",
                   title="Lejlighed i København")
    prop_id = _seed(temp_db, room)
    run.refresh_local(temp_db, export=False, quiet=True)
    row = _stored_row(temp_db, room)
    evaluation = _current(temp_db, room)
    assert evaluation["trust_outcome"] == "reject"
    assert evaluation["reason"] == config.TRUST_REASON_ROOM
    assert row["unit_type_state"] == "room_rental"
    assert row["unit_type_evidence_source"] == "detail"
    assert temp_db.get_match_row(prop_id) is None


# --------------------------------------------------------------------------- #
# Duty 4 clause 7 - the stored landlord fact is a real channel                   #
# --------------------------------------------------------------------------- #
def test_a_stored_landlord_fact_keeps_the_channel_when_the_block_is_lost(temp_db, mock_stations):
    """Section 4.2 bullet 3b: with the operator block gone from `raw_data` but the
    section 5.1 landlord triple kept, the row is still professional; clearing the
    triple drops it back to the unidentified-advertiser pass. That is the check
    that the columns carry the channel instead of decorating it - and spec 08
    section 4.1 step 4 makes that pass `eligible` rather than a review row."""
    rec = _record(listing_id="9060")
    _seed(temp_db, rec)
    run.refresh_local(temp_db, export=False, quiet=True)
    row = _stored_row(temp_db, rec)
    assert row["landlord_evidence_source"] == "structured"

    stripped = _stored_row(temp_db, rec)
    raw = json.loads(stripped["raw_data"] or "{}")
    raw.pop("source_record", None)
    temp_db.conn.execute("UPDATE source_listings SET raw_data=? WHERE id=?",
                         (json.dumps(raw, ensure_ascii=False), stripped["id"]))
    temp_db.conn.commit()

    kept = _stored_row(temp_db, rec)
    decision = filters.classify_listing_trust(source_record_view(kept), temp_db)
    assert decision.tenancy_channel_state == "professional_direct"
    assert decision.tenancy_channel.source == "structured"

    temp_db.conn.execute(
        "UPDATE source_listings SET landlord_name=NULL, landlord_evidence_source=NULL, "
        "landlord_evidence_excerpt=NULL WHERE id=?", (row["id"],))
    temp_db.conn.commit()
    cleared = filters.classify_listing_trust(
        source_record_view(_stored_row(temp_db, rec)), temp_db)
    assert cleared.tenancy_channel_state == "unknown"
    assert cleared.reason is None
    assert cleared.outcome == "eligible"


# --------------------------------------------------------------------------- #
# Duty 4 clause 8 - the section 7.2 stored-conflict shape                        #
# --------------------------------------------------------------------------- #
def test_a_stored_conflicting_building_fact_is_a_badge_side_note_every_pass(temp_db, mock_stations):
    """Section 7.2: contradicting stored evidence is named, and the write never
    erases the stored definite fact (so the conflict is reported again). Spec 08
    section 4.2 makes the named conflict a pass: it withholds the badge instead
    of holding the row, and the review reason vocabulary no longer has a string
    for it."""
    _seed_profile(temp_db, "testgade-have", "Testgade 12, 2300 København")
    rec = _record(listing_id="9070", address="Testgade 12, 2300 København")
    _seed(temp_db, rec)
    # A prior definite verdict in the section 5.1 column, the shape a manual
    # review or a migration row leaves behind.
    temp_db.record_source_trust(rec["site"], rec["listing_id"],
                                building_form_state="small_or_private_property")
    for _pass in range(2):
        run.refresh_local(temp_db, export=False, quiet=True)
        evaluation = _current(temp_db, rec)
        assert evaluation["trust_outcome"] == "eligible"
        assert evaluation["trust_reason"] is None
        row = _stored_row(temp_db, rec)
        assert row["building_form_state"] == "small_or_private_property"
        assert row["building_profile_id"] is not None
        decision = filters.classify_listing_trust(source_record_view(row), temp_db)
        assert decision.outcome == "eligible"
        assert decision.building_form_state == "unknown"
        assert decision.building_form.reason == config.TRUST_REASON_CONFLICT


# --------------------------------------------------------------------------- #
# Acceptance: offline, alert-free, and never a match for review/reject           #
# --------------------------------------------------------------------------- #
def test_the_scan_path_alerts_nothing_for_review_or_reject(temp_db, mock_stations, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "notify_match",
                        lambda payload, prefix="": sent.append(("match", payload)))
    monkeypatch.setattr(notify, "send_text",
                        lambda text, parse_html=False: sent.append(("text", text)))

    def _no_socket(*args, **kwargs):
        raise AssertionError("the scan path must not reach the network here")

    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)

    run.process_candidate(_record(listing_id="9080",
                                  rent_total=config.RENT_CAP_MAX + 1),
                          mock_stations, temp_db, None, dry_run=False)
    run.process_candidate(_record(listing_id="9081", title="Værelse på Amager",
                                  text=ROOM_TEXT), mock_stations, temp_db, None,
                          dry_run=False)
    assert sent == []
    for listing_id in ("9080", "9081"):
        evaluation = _current(temp_db, _record(listing_id=listing_id))
        assert evaluation["outcome"] == "rejected"
        assert temp_db.get_match_row(evaluation["property_id"]) is None


def test_local_refresh_is_offline_and_alert_free(temp_db, mock_stations, monkeypatch):
    """Section 12: the local pass makes no network call and sends no alert, even
    though it re-classifies and re-matches what it finds."""
    sent = []
    monkeypatch.setattr(notify, "notify_match",
                        lambda payload, prefix="": sent.append(("match", payload)))
    monkeypatch.setattr(notify, "send_text",
                        lambda text, parse_html=False: sent.append(("text", text)))

    def _no_socket(*args, **kwargs):
        raise AssertionError("local refresh must not reach the network")

    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)

    # One distinct address and coordinate per record, so each row's own property
    # is examined (the store merges two listings within 25 m, same rooms and same
    # size into one property, and one property holds one match row).
    eligible = _record(listing_id="9090")
    review = _unknown_unit_record("9091", address="Testvej 2, 2300 København",
                                  lat=55.66, lon=12.61)
    room = _record(listing_id="9092", title="Værelse på Amager", text=ROOM_TEXT,
                   address="Testvej 3, 2300 København", lat=55.67, lon=12.62)
    for rec in (eligible, review, room):
        _seed(temp_db, rec)

    result = run.refresh_local(temp_db, export=False, quiet=True)
    assert sent == []
    assert result["counts"]["evaluated"] == 3
    assert temp_db.get_match_row(_current(temp_db, eligible)["property_id"]) is not None
    # v3.11 (spec 10): an unknown unit type ranks with a badge and holds a row.
    assert temp_db.get_match_row(_current(temp_db, review)["property_id"]) is not None
    room_eval = _current(temp_db, room)
    assert temp_db.get_match_row(room_eval["property_id"]) is None


def test_refresh_local_records_the_migration_summary(temp_db, mock_stations):
    """Duty 3's pinned contract: the caller's summary is stored verbatim and read
    back through `Store.last_local_run_detail`; without one, `detail` is the
    legacy mode string.
    """
    summary = {"relinked_sources": 3, "trusted_blocks": 1,
               "needs_verification": 2, "excluded": 4}
    run.refresh_local(temp_db, export=False, quiet=True, migration_summary=summary)
    detail = temp_db.last_local_run_detail()
    assert detail["mode"] == "no-network"
    assert detail["migration"] == summary

    run.refresh_local(temp_db, export=False, quiet=True)
    legacy = temp_db.last_local_run_detail()
    assert legacy == {"mode": "no-network", "migration": None}
