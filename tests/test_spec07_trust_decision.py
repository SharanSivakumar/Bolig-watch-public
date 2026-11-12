"""Spec 07 section 4.4 — the final trust decision table (v36c3).

Covers the composed trust gate: ``filters.TrustDecision`` and the pure
``filters.classify_listing_trust(rec, store)`` (spec 07 sections 4.4, 6.3 and 11,
plan duties 1-3). Everything here is local and deterministic: the composer reads
the stored record and, at most, the store's reviewed profiles. It fetches
nothing, writes nothing and sends nothing.

Duty 3 is covered too: every trust-rule input lives in ``config`` and is named in
``ELIGIBILITY_CONSTANTS``, the manual tag moved to v3.7, and
``eligibility_rules_version()`` therefore changes when a trust rule changes.

Deliberately NOT here: wiring into ``evaluate_listing``, persistence of the trust
facts/outcome, match rebuilding (v36i1/v36i2) and the four views (v36q1-v36u3).
"""
import copy
import datetime
import json
import socket

import pytest

import config
import filters
from filters import (EvidenceDecision, TrustDecision, classify_building_form,
                     classify_listing_trust, classify_tenancy_channel,
                     classify_unit_type)


# --- Fixtures -----------------------------------------------------------------
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

# One exact house-numbered address per building-form verdict. The reviewed
# profiles below sit on the first two; the third has no profile at all.
ADDR_MANAGED = "Testvej 1, 2000 Frederiksberg"
ADDR_SMALL = "Testvej 2, 2000 Frederiksberg"
ADDR_UNKNOWN = "Testvej 3, 2000 Frederiksberg"

BUILDING_ADDRESS = {
    "managed_multi_unit_block": ADDR_MANAGED,
    "small_or_private_property": ADDR_SMALL,
    "unknown": ADDR_UNKNOWN,
}


def _iso(days_ago):
    when = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    return when.isoformat(timespec="seconds")


def _seed_profiles(store):
    """One fresh reviewed managed block and one fresh small/private property."""
    store.upsert_building_profile(
        profile_key="managed-1", building_form_state="managed_multi_unit_block",
        management_state="professional", evidence_kind="manual",
        normalized_address=ADDR_MANAGED, operator_name="Ejendomsselskabet Test A/S",
        evidence_url="https://operator.test/testvej-1",
        evidence_excerpt="Testvej 1 administreres af Ejendomsselskabet Test A/S",
        verified_at=_iso(2))
    store.upsert_building_profile(
        profile_key="small-1", building_form_state="small_or_private_property",
        management_state="private", evidence_kind="manual",
        normalized_address=ADDR_SMALL, operator_name="Privat udlejer",
        evidence_url="https://operator.test/testvej-2",
        evidence_excerpt="Testvej 2 er en ejerudlejet villa",
        verified_at=_iso(2))


# The unit-type dimension. Titles/card metadata stay in the class the case names,
# and the authoritative detail label carries the same verdict, so a case holds
# whether a fixture exercises the detail route or the card route.
UNIT_CASES = {
    "self_contained_apartment": {"title": "1-værelses lejlighed",
                                 "card_class": "apartment",
                                 "listing_type": "lejlighed",
                                 "detail_label": "Lejlighed"},
    "room_rental": {"title": "14m² værelse på Frederiksberg",
                    "card_class": "room", "listing_type": "ukendt",
                    "detail_label": "Værelse"},
    "house_or_other": {"title": "Villa i Rødovre", "card_class": "unknown",
                       "listing_type": "hus", "detail_label": "Hus"},
    "unknown": {"title": "Annonce 5", "card_class": "unknown",
                "listing_type": "ukendt", "detail_label": None},
}

# The tenancy dimension. Every sentence is the advert's own text and carries no
# word the unit or building classifier would read, so the three dimensions stay
# independent: the wording of one never decides another.
TENANCY_BODIES = {
    "professional_direct": "Eget køkken og eget badeværelse.",
    "sublet": "Boligen udbydes som fremleje i seks måneder.",
    "private_landlord": "Jeg udlejer direkte til lejer.",
    "unknown": "Eget køkken og eget badeværelse.",
}


def _matrix_rec(unit, tenancy, building, **over):
    """One stored advert standing for an (unit, tenancy, building) combination."""
    case = UNIT_CASES[unit]
    body = TENANCY_BODIES[tenancy]
    address = BUILDING_ADDRESS[building]
    detail = f"Boligtype {case['detail_label']}. {body}" if case["detail_label"] else body
    rec = {
        "site": "test",
        "listing_id": f"{unit}-{tenancy}-{building}",
        "url": f"https://example.test/{unit}",
        "title": case["title"],
        "card_class": case["card_class"],
        "listing_type": case["listing_type"],
        "rooms": 1,
        "rent_total": 7800,
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": body,
        "address": address,
        "normalized_address": address,
        "postcode": "2000",
        "eligibility_text": detail,
        "eligibility_source_url": f"https://example.test/{unit}",
        "eligibility_evidence_state": "verified",
        "eligibility_checked_at": "2026-09-15T10:00:00",
        "detail_state": "fetched",
    }
    if tenancy == "professional_direct":
        rec["source_record"] = {"id": 1, "is_newbuild": True,
                                "newbuild_details": {"newbuild_name": "Poppelstykket",
                                                     "company_name": "LIV Residential"}}
    rec.update(over)
    return rec


def _empty_block_rec(address=ADDR_UNKNOWN, **over):
    """A stored apartment advert with no advertiser or building evidence."""
    rec = {
        "site": "test", "listing_id": "P1", "url": "https://example.test/p1",
        "title": "1-værelses lejlighed", "card_class": "apartment",
        "listing_type": "lejlighed", "rooms": 1, "rent_total": 7800,
        "rental_period": "ubegrænset", "rental_period_raw": 0,
        "description": "Eget køkken og eget badeværelse.",
        "address": address, "normalized_address": address, "postcode": "2000",
        "eligibility_text": "Boligtype Lejlighed. Eget køkken og eget badeværelse.",
        "eligibility_source_url": "https://example.test/p1",
        "eligibility_evidence_state": "verified",
        "eligibility_checked_at": "2026-09-15T10:00:00",
        "detail_state": "fetched",
    }
    rec.update(over)
    return rec


def _with_project(rec, link=None, details=None, is_newbuild=True, ad_id=5660253):
    """Give the advert its own canonical source_record with a project block."""
    rec = dict(rec)
    block = dict(NEWBUILD_DETAILS if details is None else details)
    if link is None:
        block.pop("company_link", None)
    else:
        block["company_link"] = link
    rec["source_record"] = {"id": ad_id, "is_newbuild": is_newbuild,
                            "newbuild_details": block}
    return rec


# --- Section 4.4's table, as the tests read it ---------------------------------
REJECT_UNIT_STATES = ("room_rental", "house_or_other")
REJECT_TENANCY_STATES = ("sublet", "private_landlord")
REJECT_BUILDING_STATES = ("small_or_private_property",)


def _expected(unit_state, tenancy_state, building_state):
    """The section 4.4 result for one combination, read from the table itself.

    Returns ``(outcome, reason)``, where a reason of ``None`` means "the row's
    own component supplies it". Two things the table's rows leave implicit are
    expressed here exactly once: an explicit rejection from ANY component wins in
    the table's column order (unit, then tenancy, then building - plan duty 2 and
    row 6's "unless explicit rejection evidence exists"), and a positive
    small/private property is a rejection (v36c2's pack hands that final mapping
    to v36c3; section 1 and section 2.4 make it non-qualifying).
    """
    if unit_state in REJECT_UNIT_STATES:
        return "reject", None
    if tenancy_state in REJECT_TENANCY_STATES:
        return "reject", None
    if building_state in REJECT_BUILDING_STATES:
        return "reject", None
    for unit_states, tenancy_states, building_states, outcome, reason in config.TRUST_DECISION_TABLE:
        if "any" not in unit_states and unit_state not in unit_states:
            continue
        if "any" not in tenancy_states and tenancy_state not in tenancy_states:
            continue
        if "any" not in building_states and building_state not in building_states:
            continue
        return outcome, reason
    raise AssertionError(f"no table row for {unit_state}/{tenancy_state}/{building_state}")


def _rejecting_component(decision):
    """The first component that rejects, in section 4.4's precedence order."""
    for component in decision.components():
        if component.decision == "reject":
            return component
    return None


# --------------------------------------------------------------------------- #
# Section 4.4 — the table and its vocabulary                                    #
# --------------------------------------------------------------------------- #
def test_decision_table_is_section_4_3_in_order():
    """The table as written in spec 08 section 4.3, row for row, in order.

    v3.7 replaced the two v3.6 review rows (unknown advertiser / unknown
    building) with one ``eligible`` row: the apartment continues through the
    existing gates whether the evidence is present or absent.
    """
    assert config.TRUST_DECISION_TABLE == (
        (("room_rental", "house_or_other"), ("any",), ("any",), "reject", None),
        (("self_contained_apartment",), ("sublet", "private_landlord"), ("any",),
         "reject", None),
        (("self_contained_apartment",), ("professional_direct", "unknown"),
         ("managed_multi_unit_block", "unknown"), "eligible", None),
        (("unknown",), ("any",), ("any",), "review",
         config.TRUST_REASON_UNIT_UNVERIFIED),
    )
    assert len(config.TRUST_DECISION_TABLE) == 4


def test_state_vocabularies_are_the_spec_states():
    """Sections 4.1-4.3's states, verbatim, and no others."""
    assert config.TRUST_UNIT_TYPE_STATES == ("self_contained_apartment", "room_rental",
                                             "house_or_other", "unknown")
    assert config.TRUST_TENANCY_CHANNEL_STATES == ("professional_direct",
                                                   "private_landlord", "sublet",
                                                   "unknown")
    assert config.TRUST_BUILDING_FORM_STATES == ("managed_multi_unit_block",
                                                 "small_or_private_property",
                                                 "unknown")


def test_trust_outcomes_are_section_5_3():
    """Section 5.3's `trust_outcome` vocabulary, and nothing else."""
    assert config.TRUST_OUTCOMES == ("eligible", "review", "reject")


def test_reason_strings_are_the_spec_words():
    """The surviving reason strings are the spec's own words, and the two v3.6
    hold reasons spec 08 section 5 removes are gone from `config` entirely."""
    assert config.TRUST_REASON_UNIT_UNVERIFIED == "self-contained apartment unverified"
    assert config.TRUST_REASON_ROOM == "room rental / not self-contained"
    assert config.TRUST_REASON_STALE == "verification stale"
    assert config.TRUST_REASON_CONFLICT == "conflicting building evidence"
    for removed in ("TRUST_REASON_LANDLORD_UNVERIFIED",
                    "TRUST_REASON_BLOCK_UNVERIFIED"):
        assert not hasattr(config, removed), removed
        assert removed not in config.ELIGIBILITY_CONSTANTS, removed


def test_room_reason_matches_the_unit_classifier():
    """The trust gate states section 6.2's room reason, and the unit classifier
    answers the same string, so the pinned constant cannot drift from it."""
    rec = _matrix_rec("room_rental", "unknown", "unknown")
    assert config.TRUST_REASON_ROOM == classify_unit_type(rec).reason


# --------------------------------------------------------------------------- #
# The complete matrix                                                          #
# --------------------------------------------------------------------------- #
def test_every_section_4_4_combination(temp_db):
    """All 4 x 4 x 3 combinations of the three decisions, against the table."""
    _seed_profiles(temp_db)
    seen = set()
    for unit_state in config.TRUST_UNIT_TYPE_STATES:
        for tenancy_state in config.TRUST_TENANCY_CHANNEL_STATES:
            for building_state in config.TRUST_BUILDING_FORM_STATES:
                rec = _matrix_rec(unit_state, tenancy_state, building_state)
                decision = classify_listing_trust(rec, temp_db)

                # The components really are the three verdicts the fixture means.
                assert decision.unit_type_state == unit_state, rec["listing_id"]
                assert decision.tenancy_channel_state == tenancy_state, rec["listing_id"]
                assert decision.building_form_state == building_state, rec["listing_id"]

                outcome, reason = _expected(unit_state, tenancy_state, building_state)
                assert decision.outcome == outcome, rec["listing_id"]
                if reason is not None:
                    assert decision.reason == reason, rec["listing_id"]
                else:
                    # `None` = the rejecting component's own accurate reason.
                    component = _rejecting_component(decision)
                    if outcome == "reject":
                        assert component is not None, rec["listing_id"]
                        expected_reason = (config.TRUST_REASON_ROOM
                                           if unit_state == "room_rental"
                                           and component is decision.unit_type
                                           else component.reason)
                        assert decision.reason == expected_reason, rec["listing_id"]
                    else:
                        assert decision.reason is None, rec["listing_id"]
                assert decision.outcome in config.TRUST_OUTCOMES
                seen.add((unit_state, tenancy_state, building_state))
    assert len(seen) == 4 * 4 * 3


def test_matrix_is_deterministic_and_does_not_mutate_the_record(temp_db):
    """Two runs over the same record give equal decisions, and the composer adds
    no key and changes no value on the record it was given."""
    _seed_profiles(temp_db)
    for unit in config.TRUST_UNIT_TYPE_STATES:
        for tenancy in config.TRUST_TENANCY_CHANNEL_STATES:
            for building in config.TRUST_BUILDING_FORM_STATES:
                rec = _matrix_rec(unit, tenancy, building)
                before = copy.deepcopy(rec)
                first = classify_listing_trust(rec, temp_db)
                second = classify_listing_trust(rec, temp_db)
                assert first == second
                assert rec == before
                assert "_trust_decision" not in rec


# --------------------------------------------------------------------------- #
# Row by row                                                                   #
# --------------------------------------------------------------------------- #
def test_the_professional_managed_apartment_is_eligible(temp_db):
    """The fully evidenced row of the table (spec 08 section 4.3 row 4).

    Since v3.7 this is one eligible combination among four: the evidence earns
    the badge, it is no longer what admits the advert.
    """
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "professional_direct",
                    "managed_multi_unit_block"), temp_db)
    assert decision.outcome == "eligible"
    assert decision.reason is None
    assert decision.unit_type_state == "self_contained_apartment"
    assert decision.tenancy_channel_state == "professional_direct"
    assert decision.building_form_state == "managed_multi_unit_block"
    assert all(component.decision == "pass" for component in decision.components())


def test_only_apartment_rows_without_rejection_evidence_are_eligible(temp_db):
    """Spec 08 section 4.3: only the fourth row can become a match.

    Every self-contained apartment is eligible whatever the tenancy and building
    evidence say, and nothing else is - an explicit rejection from any component
    still wins (section 4.3 rows 1-3 and row 5's "unless" clause).
    """
    _seed_profiles(temp_db)
    eligible = set()
    for unit in config.TRUST_UNIT_TYPE_STATES:
        for tenancy in config.TRUST_TENANCY_CHANNEL_STATES:
            for building in config.TRUST_BUILDING_FORM_STATES:
                decision = classify_listing_trust(
                    _matrix_rec(unit, tenancy, building), temp_db)
                expected = (unit == "self_contained_apartment"
                            and tenancy not in REJECT_TENANCY_STATES
                            and building not in REJECT_BUILDING_STATES)
                assert (decision.outcome == "eligible") is expected, \
                    (unit, tenancy, building)
                if expected:
                    eligible.add((unit, tenancy, building))
                    assert decision.reason is None, (unit, tenancy, building)
    # (professional_direct | unknown) x (managed_multi_unit_block | unknown)
    assert len(eligible) == 4


def test_room_rental_rejects_whatever_else_is_verified(temp_db):
    """Row 1: a room rejects even with a professional advertiser and a verified
    managed block (section 4.4 "any / any", section 4.1)."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("room_rental", "professional_direct",
                    "managed_multi_unit_block"), temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == "room rental / not self-contained"


def test_house_or_other_rejects_with_its_own_reason(temp_db):
    """Row 1 covers "other" too; the unit decision's own accurate reason is
    preserved rather than flattened into the room wording."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("house_or_other", "professional_direct",
                    "managed_multi_unit_block"), temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == "house / other (not an apartment)"


def test_sublet_rejects_even_when_the_period_is_unlimited(temp_db):
    """Row 2 with case 9.3.4: `fremleje` rejects as `sublet`, never reported as
    "time-limited", even with an unlimited period and a verified block."""
    _seed_profiles(temp_db)
    rec = _matrix_rec("self_contained_apartment", "sublet", "managed_multi_unit_block")
    assert rec["rental_period"] == "ubegrænset"
    decision = classify_listing_trust(rec, temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == "sublet"
    assert decision.tenancy_channel.source == "detail"


def test_private_landlord_rejects(temp_db):
    """Row 2 with case 9.3.2: explicit first-person private wording."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "private_landlord",
                    "managed_multi_unit_block"), temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == "private landlord"


def test_unknown_tenancy_with_a_managed_block_is_eligible(temp_db):
    """Spec 08 section 4.3 row 4: a missing advertiser is never professional by
    assumption, and it no longer holds the listing - the advert is eligible and
    simply carries no `Professionally managed` badge."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "unknown",
                    "managed_multi_unit_block"), temp_db)
    assert decision.outcome == "eligible"
    assert decision.reason is None
    assert decision.tenancy_channel_state == "unknown"
    assert decision.tenancy_channel.decision == "pass"
    assert decision.building_form_state == "managed_multi_unit_block"


def test_professional_apartment_with_an_unknown_block_is_eligible(temp_db):
    """Spec 08 section 4.3 row 4 with a professional advertiser and no building
    evidence: eligible, with the `Professionally managed` badge and no block
    badge."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "professional_direct", "unknown"),
        temp_db)
    assert decision.outcome == "eligible"
    assert decision.reason is None
    assert decision.tenancy_channel_state == "professional_direct"
    assert decision.building_form_state == "unknown"
    assert decision.building_form.decision == "pass"


def test_unknown_unit_reviews_as_self_contained_unverified(temp_db):
    """Row 6: with no rejection evidence anywhere, the unanswered question is
    the unit type itself."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("unknown", "unknown", "unknown"), temp_db)
    assert decision.outcome == "review"
    assert decision.reason == "self-contained apartment unverified"


def test_both_evidence_axes_unknown_are_eligible_and_keep_their_reasons(temp_db):
    """Spec 08 section 4.3 row 4 with both evidence axes absent: the row
    continues through the existing gates, and each component still states why it
    has nothing to show (section 7.1's reason order)."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "unknown", "unknown"), temp_db)
    assert decision.outcome == "eligible"
    assert decision.reason is None
    # The component reasons are still stated, for the badge-less disclosure.
    assert decision.tenancy_channel.reason == "advertiser not identified"
    assert decision.building_form.reason == "building form not identified"


@pytest.mark.parametrize("tenancy,expected", [
    ("sublet", "sublet"),
    ("private_landlord", "private landlord"),
])
def test_unknown_unit_does_not_hide_explicit_tenancy_rejection(temp_db, tenancy, expected):
    """Row 6's "unless explicit rejection evidence exists": an unknown unit type
    must not downgrade a sublet or a private landlord to a mere review."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("unknown", tenancy, "unknown"), temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == expected


def test_unknown_unit_does_not_hide_a_small_or_private_block(temp_db):
    """The same "unless" clause for positive building rejection evidence."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("unknown", "unknown", "small_or_private_property"), temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == "small or private property"


def test_small_or_private_block_rejects_a_professional_apartment(temp_db):
    """v36c2's deferred mapping: a reviewed small/private property does not
    qualify (section 1, section 2.4), so the trust outcome is a rejection."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "professional_direct",
                    "small_or_private_property"), temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == "small or private property"
    assert decision.building_form.state == "small_or_private_property"


def test_a_room_reason_is_not_hidden_by_a_small_or_private_block(temp_db):
    """Precedence: unit before building."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("room_rental", "unknown", "small_or_private_property"), temp_db)
    assert decision.reason == "room rental / not self-contained"


def test_sublet_outranks_a_small_or_private_block(temp_db):
    """Precedence: tenancy before building."""
    _seed_profiles(temp_db)
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "sublet",
                    "small_or_private_property"), temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == "sublet"


def test_jeg_udlejer_et_vaerelse_i_min_lejlighed_rejects_as_room_rental(temp_db):
    """Case 9.3.3 as a card-level advert: the room wording of the ad's own text
    makes it a room rental, and the room reason is the one reported."""
    _seed_profiles(temp_db)
    wording = "Jeg udlejer et værelse i min lejlighed på Nørrebro."
    rec = _empty_block_rec(address=ADDR_MANAGED, description=wording,
                           eligibility_text=wording, card_class="unknown",
                           listing_type="ukendt")
    decision = classify_listing_trust(rec, temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == "room rental / not self-contained"


def test_jeg_udlejer_min_lejlighed_rejects_as_private_landlord(temp_db):
    """The same advert shape without room wording is a private landlord, not a
    room and not eligible."""
    _seed_profiles(temp_db)
    rec = _empty_block_rec(address=ADDR_MANAGED,
                           description="Jeg udlejer min lejlighed direkte til lejer.")
    decision = classify_listing_trust(rec, temp_db)
    assert decision.outcome == "reject"
    assert decision.reason == "private landlord"


def test_portal_chrome_is_not_advertiser_evidence_through_the_composer(temp_db):
    """Case 9.3.7: a Lejebolig footer/CVR fact is absent evidence, so it names no
    landlord. Spec 08 section 4.1 step 4 makes the absent fact a pass, so the
    advert is eligible without a `Professionally managed` badge."""
    _seed_profiles(temp_db)
    rec = _empty_block_rec(address=ADDR_MANAGED,
                           landlord_name="Lejebolig.dk",
                           landlord_evidence_source="detail",
                           landlord_evidence_excerpt="CVR-nr. 27258948")
    decision = classify_listing_trust(rec, temp_db)
    assert decision.tenancy_channel_state == "unknown"
    assert decision.tenancy_channel.reason == "advertiser not identified"
    assert decision.tenancy_channel.landlord_name is None
    assert decision.outcome == "eligible"


# --------------------------------------------------------------------------- #
# The building lifecycle, carried through the composer                         #
# --------------------------------------------------------------------------- #
def test_stale_block_evidence_passes_and_keeps_its_reason(temp_db):
    """Section 7.2: evidence older than 90 days is `verification stale`, which
    section 7.2 renders as a badge suffix. The lifecycle reason is retained on
    the component and spec 08 section 4.2 makes the verdict a pass, so staleness
    is a missing badge rather than a hold."""
    _seed_profiles(temp_db)
    temp_db.conn.execute("UPDATE building_profiles SET verified_at=? WHERE profile_key=?",
                         (_iso(config.BUILDING_EVIDENCE_MAX_DAYS + 5), "managed-1"))
    temp_db.conn.commit()
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "professional_direct",
                    "managed_multi_unit_block"), temp_db)
    assert decision.outcome == "eligible"
    assert decision.reason is None
    assert decision.building_form.reason == "verification stale"
    assert decision.building_form_state == "unknown"


def test_superseded_block_profile_passes(temp_db):
    """Section 7.2: a superseded profile is unknown immediately, and spec 08
    section 4.2 makes that unknown a pass."""
    _seed_profiles(temp_db)
    temp_db.supersede_building_profile("managed-1", "replaced by a later review")
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "professional_direct",
                    "managed_multi_unit_block"), temp_db)
    assert decision.outcome == "eligible"
    assert decision.reason is None
    assert decision.building_form.reason == "building profile superseded"


def test_conflicting_stored_building_evidence_passes(temp_db):
    """Section 7.2: contradicting stored evidence names the conflict, never a
    badge, and spec 08 section 4.2 makes the named conflict a pass."""
    _seed_profiles(temp_db)
    rec = _matrix_rec("self_contained_apartment", "professional_direct",
                      "managed_multi_unit_block")
    rec["building_form_state"] = "small_or_private_property"
    decision = classify_listing_trust(rec, temp_db)
    assert decision.outcome == "eligible"
    assert decision.reason is None
    assert decision.building_form.reason == "conflicting building evidence"
    assert decision.building_form_state == "unknown"


def test_profile_for_another_house_number_does_not_leak_into_the_verdict(temp_db):
    """Case 9.3.9: exact scope only - a profile for a different house number
    never leaks into this advert's verdict. Since v3.7 the advert is eligible
    without building evidence anyway, so the leak is pinned where it shows: no
    managed-block state, no profile id, and the unidentified-form reason."""
    _seed_profiles(temp_db)
    rec = _matrix_rec("self_contained_apartment", "professional_direct", "unknown")
    rec["normalized_address"] = "Testvej 99, 2000 Frederiksberg"
    rec["address"] = rec["normalized_address"]
    decision = classify_listing_trust(rec, temp_db)
    assert decision.building_form_state == "unknown"
    assert decision.building_form.reason == "building form not identified"
    assert decision.building_form.profile_id is None
    assert decision.outcome == "eligible"


# --------------------------------------------------------------------------- #
# Store-free route, precomputed unit decision, evaluate_listing agreement       #
# --------------------------------------------------------------------------- #
def test_operator_project_page_is_eligible_without_a_store():
    """Section 4.3 bullet 2 / section 4.2 bullet 3: the advert's own project
    block naming a development, a professional operator and that operator's page
    is enough to compose the eligible row with `store=None`."""
    rec = _with_project(_matrix_rec("self_contained_apartment", "professional_direct",
                                    "unknown"),
                        link="https://livresidential.dk/bygninger/kobenhavn/poppelstykket")
    decision = classify_listing_trust(rec, None)
    assert decision.outcome == "eligible"
    assert decision.building_form.source == "structured"


def test_project_block_without_the_operator_page_passes_without_a_store():
    """The same block without its operator page is not positive building
    evidence, so there is no `Multi-unit block` badge; spec 08 section 4.3 row 4
    makes the advert eligible anyway."""
    rec = _with_project(_matrix_rec("self_contained_apartment", "professional_direct",
                                    "unknown"))
    decision = classify_listing_trust(rec, None)
    assert decision.outcome == "eligible"
    assert decision.reason is None
    assert decision.building_form_state == "unknown"
    assert decision.building_form.reason == "building form not identified"


def test_empty_record_reviews_as_self_contained_unverified():
    """Nothing stored at all is an honest review, never a pass (sections 2.5 and
    11), and never an exception."""
    decision = classify_listing_trust({}, None)
    assert decision.outcome == "review"
    assert decision.reason == "self-contained apartment unverified"


def test_precomputed_unit_decision_agrees_with_recomputation(temp_db):
    """The composer consumes `rec["_unit_decision"]` when the caller already ran
    `classify_unit_type`, and recomputing from the same record gives the same
    answer (the function is deterministic from stored data)."""
    _seed_profiles(temp_db)
    for unit in config.TRUST_UNIT_TYPE_STATES:
        rec = _matrix_rec(unit, "professional_direct", "managed_multi_unit_block")
        recomputed = classify_listing_trust(rec, temp_db)
        rec["_unit_decision"] = classify_unit_type(rec)
        consumed = classify_listing_trust(rec, temp_db)
        assert consumed == recomputed
        assert consumed.unit_type == classify_unit_type(rec)


def test_evaluate_listing_exposes_the_unit_decision_the_composer_consumes():
    """`evaluate_listing` sets `rec["_unit_decision"]` before the apartment
    kitchen/bathroom fallback, and that is exactly the decision the trust gate
    consumes for the same record."""
    rec = _matrix_rec("room_rental", "unknown", "unknown")
    result = filters.evaluate_listing(rec, stations=[])
    assert result.rejected
    assert isinstance(rec["_unit_decision"], EvidenceDecision)
    assert rec["_unit_decision"] == classify_unit_type(rec)
    decision = classify_listing_trust(rec, None)
    assert decision.outcome == "reject"
    assert decision.reason == "room rental / not self-contained"


# --------------------------------------------------------------------------- #
# Contract: bounds, purity, no network, no writes                              #
# --------------------------------------------------------------------------- #
def test_every_component_excerpt_is_bounded_to_200_characters(temp_db):
    """Section 6.3: each decision carries an excerpt of at most 200 characters,
    including when the stored strings are far longer."""
    _seed_profiles(temp_db)
    long_room = "værelse på Frederiksberg " * 40
    room_rec = _matrix_rec("room_rental", "private_landlord",
                           "managed_multi_unit_block", title=long_room)
    room_rec["description"] = long_room
    room_rec["eligibility_text"] = "Eget køkken og eget badeværelse."  # no detail label
    room_decision = classify_listing_trust(room_rec, temp_db)
    assert room_decision.unit_type.source == "card"
    # v3.12 (spec 11 section 4.1) room-signal excerpts are a focused window
    # around the decisive marker, still hard-bounded to 200 characters.
    assert len(room_decision.unit_type.excerpt) <= 200

    tenancy_rec = _empty_block_rec(landlord_name="Ejendomsselskabet Test A/S",
                                   landlord_evidence_source="manual",
                                   landlord_evidence_excerpt="E" * 400)
    tenancy_decision = classify_listing_trust(tenancy_rec, temp_db)
    assert tenancy_decision.tenancy_channel.state == "professional_direct"
    assert len(tenancy_decision.tenancy_channel.excerpt) == 200

    for decision in (room_decision, tenancy_decision):
        for component in decision.components():
            assert component.excerpt is None or len(component.excerpt) <= 200


def test_composer_makes_no_network_call(temp_db, monkeypatch):
    """Section 12: trust classification is local-only."""
    _seed_profiles(temp_db)
    def _no_socket(*args, **kwargs):
        raise AssertionError("trust classification must not touch the network")

    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)
    decision = classify_listing_trust(
        _matrix_rec("self_contained_apartment", "professional_direct",
                    "managed_multi_unit_block"), temp_db)
    assert decision.outcome == "eligible"


def test_composer_writes_nothing_to_the_store(temp_db):
    """The composer reads stored facts and reviewed profiles only."""
    _seed_profiles(temp_db)
    before = temp_db.conn.total_changes
    classify_listing_trust(
        _matrix_rec("self_contained_apartment", "professional_direct", "unknown"),
        temp_db)
    assert temp_db.conn.total_changes == before


def test_json_round_trip_record_is_decided(temp_db):
    """A record as it is stored and reloaded (JSON) decides identically: the
    classifier reads plain stored data, no in-process objects."""
    _seed_profiles(temp_db)
    rec = _matrix_rec("self_contained_apartment", "professional_direct",
                      "managed_multi_unit_block")
    reloaded = json.loads(json.dumps(rec))
    assert classify_listing_trust(reloaded, temp_db) == classify_listing_trust(rec, temp_db)


def test_store_none_is_allowed_for_every_combination():
    """`store` is optional, as in the building classifier: every combination
    still decides. A store-free pass can no longer see a reviewed profile, so
    every combination that carries no rejecting component decides `eligible`;
    the rest are `reject` or `review` and never anything outside the
    vocabulary."""
    for unit in config.TRUST_UNIT_TYPE_STATES:
        for tenancy in config.TRUST_TENANCY_CHANNEL_STATES:
            for building in config.TRUST_BUILDING_FORM_STATES:
                decision = classify_listing_trust(_matrix_rec(unit, tenancy, building), None)
                assert decision.outcome in config.TRUST_OUTCOMES
                expected_eligible = (unit == "self_contained_apartment"
                                     and tenancy not in REJECT_TENANCY_STATES)
                assert (decision.outcome == "eligible") is expected_eligible, \
                    (unit, tenancy, building)


def test_summary_and_component_accessors():
    """The value object's small surface: summary(), components() and the three
    column-named state accessors."""
    rec = _with_project(_matrix_rec("self_contained_apartment", "professional_direct",
                                    "unknown"),
                        link="https://livresidential.dk/x")
    decision = classify_listing_trust(rec, None)
    assert isinstance(decision, TrustDecision)
    assert decision.summary() == "eligible (eligible)"
    assert [c.state for c in decision.components()] == [
        decision.unit_type_state, decision.tenancy_channel_state,
        decision.building_form_state]
    rejected = classify_listing_trust(_matrix_rec("room_rental", "unknown", "unknown"),
                                      None)
    assert rejected.summary() == "reject (room rental / not self-contained)"


# --------------------------------------------------------------------------- #
# Duty 3 — rule inputs, the manual tag and version sensitivity                  #
# --------------------------------------------------------------------------- #
TRUST_RULE_INPUTS = (
    "TRUST_OUTCOMES", "TRUST_UNIT_TYPE_STATES", "TRUST_TENANCY_CHANNEL_STATES",
    "TRUST_BUILDING_FORM_STATES", "TRUST_DECISION_TABLE",
    "TRUST_REASON_ROOM", "TRUST_REASON_UNIT_UNVERIFIED",
    "TRUST_REASON_STALE", "TRUST_REASON_CONFLICT",
    "UNIT_ROOM_CARD_MARKERS", "UNIT_SIZE_MARKED_ROOM_RE", "UNIT_APARTMENT_CARD_RE",
    "TENANCY_PORTAL_IDENTITY_TOKENS", "TENANCY_SUBLET_PATTERNS",
    "TENANCY_PRIVATE_LANDLORD_PATTERNS", "TENANCY_LANDLORD_EVIDENCE_SOURCES",
    "TENANCY_PORTFOLIO_MIN_RENTABLES", "TENANCY_PROFESSIONAL_PORTALS",
    "BUILDING_EVIDENCE_MAX_DAYS", "BUILDING_FORM_MIN_LISTINGS",
    "BUILDING_PROFILE_EVIDENCE_KINDS", "BUILDING_PROFILE_DEFINITE_STATES",
)


def test_every_trust_rule_input_is_named_in_eligibility_constants():
    """Section 6.3: a trust rule that is not in the hashed set would let a
    classification change reuse old verdicts."""
    for name in TRUST_RULE_INPUTS + ("RULES_VERSION_TAG",):
        assert name in config.ELIGIBILITY_CONSTANTS, name
        assert config.__dict__.get(name) is not None, name


def test_eligibility_constants_all_resolve():
    """A misspelt name in ELIGIBILITY_CONSTANTS would hash as None and silently
    weaken the version, so every name must resolve."""
    unresolved = [name for name in config.ELIGIBILITY_CONSTANTS
                  if config.__dict__.get(name) is None]
    assert unresolved == []


def test_filters_binds_the_config_rule_inputs():
    """filters.py holds no trust rule of its own: every input is the config
    object, so changing it in config changes the classifier too."""
    assert filters._ROOM_CARD_MARKERS is config.UNIT_ROOM_CARD_MARKERS
    assert filters._SIZE_MARKED_ROOM_RE.pattern == config.UNIT_SIZE_MARKED_ROOM_RE
    assert filters._APARTMENT_CARD_RE.pattern == config.UNIT_APARTMENT_CARD_RE
    assert filters._PORTAL_IDENTITY_TOKENS is config.TENANCY_PORTAL_IDENTITY_TOKENS
    assert filters.SUBLET_PATTERNS is config.TENANCY_SUBLET_PATTERNS
    assert filters.PRIVATE_LANDLORD_PATTERNS is config.TENANCY_PRIVATE_LANDLORD_PATTERNS
    assert filters._LANDLORD_EVIDENCE_SOURCES is config.TENANCY_LANDLORD_EVIDENCE_SOURCES
    assert filters.BUILDING_FORM_STATES is config.TRUST_BUILDING_FORM_STATES
    assert filters.BUILDING_EVIDENCE_MAX_DAYS == config.BUILDING_EVIDENCE_MAX_DAYS
    assert filters.BUILDING_FORM_MIN_LISTINGS == config.BUILDING_FORM_MIN_LISTINGS
    # Spec 08 section 6.2 removed the config constant that named this phrase, so
    # the fall-through wording is now filters' own section 4.2 text.
    assert filters.BUILDING_UNVERIFIED_REASON == "building form not identified"
    assert filters.PORTFOLIO_MIN_RENTABLES == config.TENANCY_PORTFOLIO_MIN_RENTABLES
    assert filters.PROFESSIONAL_PORTALS is config.TENANCY_PROFESSIONAL_PORTALS
    assert filters.BUILDING_STALE_REASON == config.TRUST_REASON_STALE
    assert filters.BUILDING_CONFLICT_REASON == config.TRUST_REASON_CONFLICT
    assert filters._PROFILE_EVIDENCE_SOURCES is config.BUILDING_PROFILE_EVIDENCE_KINDS
    assert filters._DEFINITE_BUILDING_STATES is config.BUILDING_PROFILE_DEFINITE_STATES


def test_rules_version_tag_is_a_v3_7_value():
    """Spec 08 section 5 / duty 3: the manual rule tag moved to v3.7 / v3.8, so every
    local evaluation produced by this build is a new version."""
    assert config.RULES_VERSION_TAG.startswith("v3.")
    version = config.eligibility_rules_version()
    assert version.startswith(config.RULES_VERSION_TAG + "+")
    assert version != "v3.6-1+01dda21c"


@pytest.mark.parametrize("name,value", [
    ("TENANCY_SUBLET_PATTERNS", (r"\bfremlejes\b", "room in shared apartment")),
    ("UNIT_ROOM_CARD_MARKERS", ("værelse på ",)),
    ("TRUST_DECISION_TABLE", ()),
    ("TRUST_REASON_UNIT_UNVERIFIED", "unit type unverified"),
    ("BUILDING_EVIDENCE_MAX_DAYS", 60),
    ("TRUST_OUTCOMES", ("eligible", "review")),
])
def test_a_trust_rule_change_moves_the_rules_version(monkeypatch, name, value):
    """Section 6.3's purpose: changing a trust rule input forces a new local
    evaluation instead of reusing the old one's verdicts."""
    before = config.eligibility_rules_version()
    monkeypatch.setattr(config, name, value)
    after = config.eligibility_rules_version()
    assert after != before
    assert after.startswith(config.RULES_VERSION_TAG + "+")


def test_rules_version_is_stable_when_nothing_changes():
    """Deterministic: two calls with unchanged rules agree."""
    assert config.eligibility_rules_version() == config.eligibility_rules_version()
