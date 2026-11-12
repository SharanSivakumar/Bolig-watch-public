"""Spec 11 (v3.12) regression and lock-in tests for the 2026-10-09 hardening audit.

Every decision from AUDIT-2026-10-09-filters.md section 6 that was applied in
v3.12 is pinned here, once per affected live listing (the audit's §6.11 list)
plus one table-driven lock-in test that fails if any hard rule is demoted back
to a badge or a decision constant moves.

The lock-in test reads the live config objects directly, so it survives refactors
of the filter functions while still failing the moment a rule is weakened.
"""
import pytest

import config
import filters
import geo
from conftest import with_detail_evidence


def _rec(**over):
    rec = {
        "site": "lejebolig", "listing_id": "X", "url": "https://example.test/x",
        "title": "1-værelses lejlighed", "address": "Testgade 1, 2300 København S",
        "listing_type": "lejlighed", "card_class": "unknown", "rooms": 1,
        "size_m2": 32.0, "rent_total": 8000, "deposit": 0, "prepaid_rent": 0,
        "available_from": "2027-02-01", "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Eget køkken og eget badeværelse.",
        "lat": 55.65, "lon": 12.60,
    }
    rec.update(over)
    return with_detail_evidence(rec)


def _roomish(title, **over):
    """A listing whose title/card names a room but whose detail is apartment-like."""
    return _rec(title=title, card_text=title, description="Eget køkken og eget badeværelse.",
                eligibility_text="Eget køkken og eget badeværelse.", **over)


# --------------------------------------------------------------------------- #
# Decision 1A - hard rent cap 9.700 kr and the affordability ramp              #
# --------------------------------------------------------------------------- #
def test_hard_cap_inclusive_at_9700_and_hard_above():
    assert filters.evaluate_listing(_rec(rent_total=9700), []).passed
    over = filters.evaluate_listing(_rec(rent_total=9701), [])
    assert over.rejected
    assert over.rejection_reason == "rent 9701 > 9700"


def test_affordability_ramp_ends_at_9700():
    for rent, want in ((7500, 35.0), (8500, 19.1), (9700, 0.0)):
        res = filters.evaluate_listing(_rec(rent_total=rent), [])
        assert res.passed
        assert abs(res.score_breakdown["affordability"] - want) < 1e-6


# --------------------------------------------------------------------------- #
# Decision 2 - room / klubværelse / 'til 1 person' from title+card+url        #
# --------------------------------------------------------------------------- #
def test_ebertsgade_1915148_klubvaerelse_filed_as_lejlighed_rejects():
    """The audit's #1 leak: a 'klubværelse til 1 person' the portal filed as
    'Boligtype Lejlighed' and ranked #1 must reject as a room rental."""
    rec = _rec(listing_id="1915148", title="Lyst klubværelse til 1 person",
               card_text="Lyst klubværelse til 1 person", size_m2=15.0,
               description="10 klubværelser, som deler tre toiletter, to "
                           "badefaciliteter og et rummeligt fælleskøkken",
               eligibility_text="10 klubværelser, som deler tre toiletter, to "
                                "badefaciliteter og et rummeligt fælleskøkken")
    res = filters.evaluate_listing(rec, [])
    assert res.rejected
    assert "room" in res.rejection_reason
    unit = filters.classify_unit_type(rec)
    assert unit.state == "room_rental"
    assert unit.source == "card"


@pytest.mark.parametrize("title", [
    "Værelse på Vesterbro",
    "Møbleret værelse på 33 m2",
    "Værelse i Sydhavnen",
    "Værelse til leje",
    "Room for rent",
    "Room with shared kitchen",
    "Delebolig på Amager",
])
def test_decisive_room_markers_in_title_or_card_reject(title):
    unit = filters.classify_unit_type(_roomish(title))
    assert unit.state == "room_rental"
    assert unit.decision == "reject"
    assert unit.source == "card"


def test_sub_15_m2_room_marked_card_rejects():
    rec = _roomish("14m² værelse på Frederiksberg", size_m2=14.0)
    unit = filters.classify_unit_type(rec)
    assert unit.state == "room_rental"
    assert unit.decision == "reject"


@pytest.mark.parametrize("title", [
    "1-værelses lejlighed på Amager",
    "1 værelse på 33 m2",        # room COUNT of a real studio, not a bedroom
    "Ét værelses lejlighed",
    "Kælder til leje",
])
def test_room_count_phrases_are_not_room_rentals(title):
    r"""Spec 07 §9.2 negative phrases plus the v3.12 (?<!\d ) guard: '1 værelse
    på 33 m2' describes a studio, never a rented bedroom."""
    unit = filters.classify_unit_type(_roomish(title))
    assert unit.state != "room_rental"
    assert unit.decision != "reject"


# --------------------------------------------------------------------------- #
# Decision 3 - private landlord is a hard reject again                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("wording", [
    "Jeg udlejer min lejlighed.",
    "Vi er et ældre par, der ønsker en rolig lejer.",
    "Boligen ligger i tilknytning til udlejers rækkehus.",
    "Vores lejlighed udlejes.",
    "Min egen lejlighed.",
    "Jeg lejer ud.",
])
def test_private_landlord_wording_rejects(wording):
    res = filters.evaluate_listing(
        _rec(title=wording, description=wording, eligibility_text=wording), [])
    assert res.rejected
    assert res.rejection_reason == "private landlord"


def test_5455177_aeldre_par_private_landlord_rejects():
    res = filters.evaluate_listing(_rec(
        listing_id="5455177", title="Vi er et ældre par",
        description="Vi er et ældre par, der ønsker en rolig og venlig lejer. "
                    "Boligen ligger i tilknytning til udlejers rækkehus.",
        eligibility_text="Vi er et ældre par, der ønsker en rolig og venlig lejer. "
                         "Boligen ligger i tilknytning til udlejers rækkehus."), [])
    assert res.rejected
    assert res.rejection_reason == "private landlord"


# --------------------------------------------------------------------------- #
# Decision 4 - unknown tenancy never ranks as verified                         #
# --------------------------------------------------------------------------- #
def test_unknown_tenancy_carries_the_landlord_unverified_flag():
    rec = _rec(title="Lejlighed",
               description="Boligtype Lejlighed. Eget køkken og eget bad.",
               eligibility_text="Boligtype Lejlighed. Eget køkken og eget bad.")
    rec.pop("source_record")  # no professional operator block -> tenancy unknown
    res = filters.evaluate_listing(rec, [])
    assert res.passed, res.rejection_reason
    assert res.tenancy_channel_state == "unknown"
    assert any(f.code == "landlord_unverified" for f in res.soft_flags)


# --------------------------------------------------------------------------- #
# Decision 5 - kitchen evidence                                              #
# --------------------------------------------------------------------------- #
def test_jorisvej_1908481_private_plus_shared_kitchen_passes_verified():
    rec = _rec(listing_id="1908481", title="Jorisvej - 1-værelses",
               description="Boligtype Lejlighed. Eget badeværelse og et særligt "
                           "køkken til boligen. Fælleskøkken.",
               eligibility_text="Boligtype Lejlighed. Eget badeværelse og et "
                                "særligt køkken til boligen. Fælleskøkken.")
    k = filters.analyse_kitchen(rec, rooms_verified=True)
    assert k.decision == "pass"
    assert k.kitchen_state == "verified"
    assert filters.evaluate_listing(rec, []).passed


def test_gyngemose_5836720_hth_koekken_passes_verified():
    rec = _rec(listing_id="5836720", title="Lejlighed på Gyngemose Parkvej",
               description="Boligtype Lejlighed. Dit eget HTH-køkken.",
               eligibility_text="Boligtype Lejlighed. Dit eget HTH-køkken.")
    k = filters.analyse_kitchen(rec, rooms_verified=True)
    assert k.decision == "pass"
    assert k.kitchen_state == "verified"


@pytest.mark.parametrize("text", [
    "Boligtype Lejlighed. Fælleskøkken.",
    "Boligtype Lejlighed. Fælleskøkken fordelt på etagerne.",
    "På Blækhus får du en række skønne faciliteter såsom moderne vaskeri, "
    "fælleskøkkener og fælleslokaler.",
])
def test_shared_kitchen_mentioned_with_no_private_evidence_quarantines(text):
    """Decision 5: shared-only kitchen is held (quarantine), never ranked as
    verified and never rejected outright."""
    k = filters.analyse_kitchen(_rec(title="Lejlighed", description=text,
                                     eligibility_text=text), rooms_verified=True)
    assert k.decision == "quarantine"
    assert k.kitchen_state == "quarantined"


def test_explicit_shared_only_kitchen_still_rejects():
    k = filters.analyse_kitchen(_rec(title="Lejlighed",
                                     description="Du deler køkken med de øvrige beboere.",
                                     eligibility_text="Du deler køkken med de øvrige beboere."),
                                rooms_verified=True)
    assert k.decision == "reject"


# --------------------------------------------------------------------------- #
# Decision 6 - bathroom evidence                                              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "Badeværelset ligger over gangen, og deles med min veninde.",
    "Værelserne deler tre toiletter, to badefaciliteter.",
    "Fælles toilet på gangen.",
    "Delt badeværelse.",
])
def test_shared_bathroom_wording_rejects(text):
    res = filters.evaluate_listing(_rec(title="Lejlighed", description=text,
                                        eligibility_text=text), [])
    assert res.rejected
    assert res.rejection_reason == "shared bathroom"


# --------------------------------------------------------------------------- #
# Decision 7 - kollegium decisive in title/URL despite 'Kun for studerende Nej' #
# --------------------------------------------------------------------------- #
def test_fuglebakkekollegiet_5491285_rejects_as_student_housing():
    rec = _rec(listing_id="5491285", title="Studio lejlighed beliggende på "
                                           "fuglebakkekollegiet",
               description="Boligtype Lejlighed. Perfekt til unge og/eller "
                           "studerende. Kun for studerende: Nej. Alder: 18-28 år.",
               eligibility_text="Boligtype Lejlighed. Perfekt til unge og/eller "
                                "studerende. Kun for studerende: Nej. Alder: 18-28 år.")
    res = filters.evaluate_listing(rec, [], profile_age=23)
    assert res.rejected
    assert res.rejection_reason == "student housing"


def test_kollegium_in_url_rejects_even_with_student_not_required():
    rec = _rec(title="Studio lejlighed",
               url="https://www.boligportal.dk/udlejninger/fuglebakkekollegiet/",
               description="Kun for studerende: Nej.",
               eligibility_text="Kun for studerende: Nej.")
    res = filters.evaluate_listing(rec, [])
    assert res.rejected
    assert res.rejection_reason == "student housing"


def test_studiebolig_title_with_contradictory_fact_quarantines_not_rejects():
    """Only the kollegium family is decisive in the title: a 'Studiebolig' title
    with 'Kun for studerende: Nej' stays a demographic conflict, not a reject
    (spec 09 acceptance 15)."""
    rec = _rec(title="Studiebolig forbeholdt studerende",
               description="Eget køkken og bad. Kun for studerende: Nej.",
               eligibility_text="Boligtype Lejlighed. Eget køkken og bad. "
                                "Studiebolig. Kun for studerende: Nej.")
    res = filters.evaluate_listing(rec, [], profile_age=23)
    assert res.student_requirement_state == "conflict"
    assert not res.rejected
    assert res.passed


# --------------------------------------------------------------------------- #
# Decision 8 - senior rule: bare feature label is a non-event                  #
# --------------------------------------------------------------------------- #
def test_bare_aeldrebolig_feature_label_is_a_non_event():
    res = filters.evaluate_listing(_rec(
        title="Lejlighed på Testgade",
        description="Boligtype Lejlighed. Ældrebolig Elevator Parkering.",
        eligibility_text="Boligtype Lejlighed. Ældrebolig Elevator Parkering."), [])
    assert not res.rejected


@pytest.mark.parametrize("text", [
    "Ældrebolig på Testgade",                     # title product name
    "Seniorbolig for 55+ med eget køkken og bad.",  # age restriction
    "Boligen udlejes kun til beboere over 60 aar.",
])
def test_genuine_senior_product_age_or_wording_rejects(text):
    res = filters.evaluate_listing(_rec(title=text, description=text,
                                        eligibility_text=text), [])
    assert res.rejected
    assert res.rejection_reason == "elderly / senior housing"


def test_senior_negations_do_not_reject():
    for text in ("Besøgende fra over 60 lande.", "Seniorvenlig: Nej. Eget køkken."):
        res = filters.evaluate_listing(_rec(title="Lejlighed", description=text,
                                            eligibility_text=text), [])
        assert not res.rejected


# --------------------------------------------------------------------------- #
# Decision 9 - time-limited leases                                            #
# --------------------------------------------------------------------------- #
def _lease_rec(text):
    return _rec(rental_period=None, rental_period_raw=None, description=text,
                eligibility_text=text)


def test_1691356_lejeperiode_under_1_ar_and_ledig_i_maaneder_rejects():
    ok, reason = filters.verify_lease_duration(
        _lease_rec("Lejeperiode Under 1 år. Ledig i 1.5 måneder fra 1 Nov til "
                   "15 dec 2026."))
    assert ok is False
    assert "time-limited" in reason


def test_1837825_udlejes_i_2_ar_rejects():
    ok, reason = filters.verify_lease_duration(_lease_rec("Udlejes i 2 år."))
    assert ok is False
    assert "time-limited" in reason


@pytest.mark.parametrize("text", [
    "Lejeperiode op til 2 år.",
    "Lejeperiode tidsbegrænset 1-2 år.",
    "Udlejes 1-2 år.",
    "Ledig i 3 måneder.",
])
def test_more_time_limited_wording_rejects(text):
    ok, _ = filters.verify_lease_duration(_lease_rec(text))
    assert ok is False


def test_an_age_requirement_is_not_a_time_limited_lease():
    ok, reason = filters.verify_lease_duration(
        _lease_rec("Kun for beboere under 30 år."))
    assert ok is not False
    assert "time-limited" not in reason


def test_unbounded_1_ar_og_mere_is_not_time_limited():
    ok, _ = filters.verify_lease_duration(_lease_rec("1 år og mere."))
    assert ok is not False


# --------------------------------------------------------------------------- #
# Decision 10 - the age parser ignores labelled lease durations               #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "Lejeperiode Under 1 år.",
    "Lejeperiode op til 2 år.",
    "Lejeperiode 1-2 år.",
])
def test_lease_durations_are_not_parsed_as_tenant_age(text):
    state, _, _, _, _ = filters.parse_age_requirement({"eligibility_text": text})
    assert state == "none"


def test_genuine_age_wording_still_parses():
    state, lo, hi, _, _ = filters.parse_age_requirement(
        {"eligibility_text": "Alder: 18-35 år."})
    assert state == "bounded"
    assert (lo, hi) == (18, 35)


# --------------------------------------------------------------------------- #
# Decision 11 - commute over 50 is a hard rejection again                      #
# --------------------------------------------------------------------------- #
def _evaluate_commute(monkeypatch, commute):
    monkeypatch.setattr(
        __import__("features"), "get_place_features",
        lambda lat, lon, store=None, refresh=False: {
            "commute_min": commute, "buzz_score": 0.5, "buzz_detail": "Testgade",
            "water_dist_m": 600.0, "water_kind": "harbour", "water_name": "Inderhavnen",
            "features_pending": 0})
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda *a: ("MetroX", 280.0, 400.0, 5))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda *a: ("STogY", 280.0, 400.0, 5, "s-tog"))
    monkeypatch.setattr(geo, "distance_to_norreport", lambda *a: 2000.0)
    monkeypatch.setattr(geo, "road_route_to_norreport", lambda *a: 2000.0)
    monkeypatch.setattr(geo, "distance_to_radhuspladsen", lambda *a: 3000.0)
    return filters.evaluate_listing(_rec(rent_total=8500), [])


def test_commute_at_ceiling_is_eligible(mock_stations, monkeypatch):
    res = _evaluate_commute(monkeypatch, commute=50)
    assert res.rejected is False
    assert res.passed


def test_commute_above_ceiling_rejects(mock_stations, monkeypatch):
    res = _evaluate_commute(monkeypatch, commute=51)
    assert res.rejected
    assert "commute" in res.rejection_reason


# --------------------------------------------------------------------------- #
# Decision 12 - youth / andels rule unchanged                                  #
# --------------------------------------------------------------------------- #
def test_youth_housing_still_rejects():
    res = filters.evaluate_listing(_rec(
        title="Lejlighed",
        description="Lejligheden er en ungdomsbolig.",
        eligibility_text="Lejligheden er en ungdomsbolig."), [])
    assert res.rejected


# --------------------------------------------------------------------------- #
# Lock-in test: a hard rule demoted or a constant moved fails the suite        #
# --------------------------------------------------------------------------- #
def test_hard_rules_and_constants_are_locked_in():
    """Fails if any 2026-10-09 hard rule is demoted to a badge or the decision
    constants drift back. Mirrors the audit's §6 change list."""
    # 1A rent cap and affordability ramp.
    assert config.RENT_CAP_MAX == 9700
    assert config.AFFORDABILITY_ZERO_RENT == 9700.0
    assert config.AFFORDABILITY_SPAN == 2200.0
    assert config.AFFORDABILITY_CURVE == [(7500, 35), (9700, 0)]
    # 11 commute ceiling is a hard rejection.
    assert config.COMMUTE_MAX_MIN == 50
    # 2 room signal: the guarded 'værelse på' pattern and the minimum area.
    assert config.UNIT_ROOM_MIN_AREA_M2 == 15.0
    room_markers = " ".join(config.UNIT_ROOM_TITLE_MARKERS)
    assert r"(?<!\d )\bv[æa]relse\s+p[åa]\b" in room_markers
    assert "klubv[æa]relse" in room_markers
    assert r"til\s+1\s+person" in room_markers
    assert r"room\s+for\s+rent" in room_markers
    # 3 private landlord wording is present.
    pl = " ".join(config.TENANCY_PRIVATE_LANDLORD_PATTERNS)
    for phrase in (r"ældre\s+par", "r[æa]kkehus", "egen", "lejer"):
        assert phrase in pl, phrase
    # 5/6 kitchen + bathroom shared wording is present.
    kitchen = " ".join(filters.ONLY_SHARED_KITCHEN_PATTERNS)
    assert "fælles" in kitchen
    bath = " ".join(filters.SHARED_BATHROOM_NEG)
    assert "deles" in bath and "fælles" in bath
    # 9 time-limited wording is present.
    tl = " ".join(filters.TIME_LIMITED_PATTERNS)
    assert "under" in tl and "m[aå]neder" in tl
    # 7 kollegium is decisive in the title (unconditional student check).
    assert "kollegie" in " ".join(filters.STUDENT_NAME_REGEX)
    # The new rule inputs are hashed into the rules version.
    assert "UNIT_ROOM_TITLE_MARKERS" in config.ELIGIBILITY_CONSTANTS
    assert "UNIT_ROOM_MIN_AREA_M2" in config.ELIGIBILITY_CONSTANTS
    assert config.RULES_VERSION_TAG.startswith("v3.12")
