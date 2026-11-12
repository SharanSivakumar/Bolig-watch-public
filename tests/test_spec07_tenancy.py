"""Spec 07 section 9.3 — tenancy channel and landlord evidence.

Covers the Part-A classifier of the v3.6 trust gate: the pure, record-only
``filters.classify_tenancy_channel(rec)`` (spec 07 sections 4.2, 6.3, 9.3 cases
1, 2, 4, 5, 7, 8). Everything is local and deterministic: the function reads
only the stored record, fetches nothing, and writes nothing.

Spec 08 section 4.1 (v37c1) added routes 3c (the advert's own BoligPortal
portfolio) and 3d (a professional operator portal) and made the fall-through a
**pass**: an unidentified advertiser is no longer a reason to hold a listing, so
every case below that used to end in ``decision="review"`` now ends in
``decision="pass"`` with the step-4 reason. The hard rejections (sublet,
first-person private landlord) and the portal-chrome / repeated-inventory
exclusions are unchanged.

Deliberately NOT here: the final trust decision table (section 4.4), the
building-form gate (section 4.3, v36c2) and the wiring into ``evaluate_listing``
(v36i1) are later rows.
"""
import copy
import json
import socket

import pytest

from filters import EvidenceDecision, classify_tenancy_channel


# --- Fixtures -----------------------------------------------------------------
# The project/company advertiser block exactly as it is stored on the ad's own
# canonical record (production boligportal#5813542 / Poppelstykket, the shape
# recorded in PHASE_v36/v36c1_tenancy/PLAN.md section 0a).
NEWBUILD_DETAILS = {
    "newbuild_id": 76666,
    "newbuild_name": "Poppelstykket",
    "company_name": "LIV Residential",
    "company_link": "https://livresidential.dk/bygninger/kobenhavn/poppelstykket",
    "units_amount": 360,
    "min_rooms": 1,
    "max_rooms": 2,
}

# The portal's own chrome, as production stores it (spec 07 section 4.2 bullet 2,
# case 9.3.7). Never evidence about the advertiser.
PORTAL_CHROME = [
    "Lejebolig.dk",
    "CVR-nr. 27258948",
    "Kontakt udlejer",
    "Valideret af BoligPortal",
    "Om udlejeren",
    "Mere end 1000 annoncer",
]


def _rec(**over):
    """A stored apartment advert with no advertiser evidence at all."""
    rec = {
        "site": "boligportal",
        "listing_id": "5813542",
        "url": "https://www.boligportal.dk/lejligheder/koebenhavn/poppelstykket-76666",
        "title": "Poppelstykket",
        "listing_type": "lejlighed",
        "card_class": "apartment",
        "rooms": 1,
        "rent_total": 7800,
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Ny lejlighed i Poppelstykket med eget køkken.",
    }
    rec.update(over)
    return rec


def _with_detail(rec, text):
    """Stamp the authoritative detail-page text a fetched listing carries."""
    rec = dict(rec)
    rec["description"] = text
    rec["eligibility_text"] = text
    rec["eligibility_source_url"] = rec.get("url")
    rec["eligibility_evidence_state"] = "verified"
    rec["eligibility_checked_at"] = "2026-09-15T10:00:00"
    rec["detail_state"] = "fetched"
    return rec


def _with_newbuild(rec, ad_id=5660253, details=None, is_newbuild=True):
    """Give the advert its own canonical source_record with a project block."""
    rec = dict(rec)
    rec["source_record"] = {"id": ad_id, "is_newbuild": is_newbuild,
                            "newbuild_details": NEWBUILD_DETAILS if details is None else details}
    return rec


# --- Step 3a: structured professional advertiser (case 9.3.1) -----------------
def test_project_advertiser_is_professional_direct():
    """A company advert on the ad's own canonical record is source-scoped
    structured professional-advertiser evidence (section 4.2 bullet 3)."""
    dec = classify_tenancy_channel(_with_newbuild(_rec()))
    assert dec.state == "professional_direct"
    assert dec.decision == "pass"
    assert dec.source == "structured"
    assert dec.excerpt and "Poppelstykket" in dec.excerpt
    assert "LIV Residential" in dec.excerpt


def test_project_advertiser_excerpt_is_bounded_and_named():
    """The excerpt is the development name plus the company name, bounded to
    the section 5.1 limit even when the stored strings are long."""
    details = dict(NEWBUILD_DETAILS, newbuild_name="N" * 400, company_name="C" * 400)
    dec = classify_tenancy_channel(_with_newbuild(_rec(), details=details))
    assert dec.state == "professional_direct"
    assert len(dec.excerpt or "") == 200


def test_is_newbuild_false_is_not_professional():
    """A project block without the portal's own ``is_newbuild`` flag is not the
    structured evidence the rule names, so the advert stays unknown rather than
    being upgraded by a nested mapping (section 4.2 bullet 5: never professional
    by assumption). Spec 08 section 4.1 step 4 makes that unknown a pass, so the
    advert simply carries no badge."""
    dec = classify_tenancy_channel(_with_newbuild(_rec(), is_newbuild=False))
    assert dec.state == "unknown"
    assert dec.decision == "pass"


def test_other_listings_cards_are_not_advertiser_evidence():
    """``page_props.results`` / ``other_results`` / ``similar_ads`` carry OTHER
    listings' cards and are not source-scoped (plan duty 1 step 3a), so a company
    block found only there must not prove anything about this advert."""
    rec = _rec(detail_hydration={"props": {"page_props": {
        "results": [{"newbuild_details": dict(NEWBUILD_DETAILS)}],
        "other_results": [{"newbuild_details": dict(NEWBUILD_DETAILS)}],
        "similar_ads": [{"newbuild_details": dict(NEWBUILD_DETAILS)}],
        "serialized_new_build": dict(NEWBUILD_DETAILS, ad_id=999999),
    }}})
    dec = classify_tenancy_channel(rec)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.excerpt is None


def test_serialized_new_build_of_another_ad_is_not_read():
    """The repeated ``serialized_new_build`` copy is only this advert's when its
    ``ad_id`` equals ``source_record.id``; another ad's copy is ignored."""
    rec = _rec(source_record={"id": 111, "is_newbuild": True,
                              "newbuild_details": {"company_name": "Bohavn"}},
               detail_hydration={"props": {"page_props": {
                   "serialized_new_build": {"ad_id": 222, "company_name": "Someone Else"}}}})
    dec = classify_tenancy_channel(rec)
    assert dec.state == "professional_direct"
    assert dec.excerpt and "Bohavn" in dec.excerpt
    assert "Someone Else" not in dec.excerpt


# --- Step 3b: an already-stored source-scoped landlord fact -------------------
@pytest.mark.parametrize("evidence_source", ["structured", "detail", "manual"])
def test_stored_landlord_fact_is_professional_direct(evidence_source):
    """A stored source-scoped landlord fact in the section 5.1 vocabulary is the
    only channel by which a reviewed/configured-operator verdict reaches this
    record-only function (plan duty 1 step 3b)."""
    rec = _rec(landlord_name="Bohavn",
               landlord_evidence_source=evidence_source,
               landlord_evidence_excerpt="Bohavn administrerer Englandshaven")
    dec = classify_tenancy_channel(rec)
    assert dec.state == "professional_direct"
    assert dec.decision == "pass"
    assert dec.source == evidence_source
    assert dec.excerpt == "Bohavn administrerer Englandshaven"


def test_unknown_landlord_evidence_source_is_not_professional():
    """Only the pinned vocabulary counts; an unknown provenance is not evidence."""
    dec = classify_tenancy_channel(_rec(landlord_name="Bohavn",
                                        landlord_evidence_source="guessed"))
    assert dec.state == "unknown"


def test_stored_landlord_excerpt_is_bounded():
    dec = classify_tenancy_channel(_rec(landlord_name="Bohavn",
                                        landlord_evidence_source="manual",
                                        landlord_evidence_excerpt="x" * 500))
    assert dec.state == "professional_direct"
    assert len(dec.excerpt or "") == 200


# --- Case 9.3.7: the portal's own chrome is never advertiser evidence ---------
@pytest.mark.parametrize("token", PORTAL_CHROME)
def test_portal_identity_tokens_are_never_professional(token):
    """Each pinned portal-chrome token, used as the landlord fact, must classify
    ``unknown`` rather than ``professional_direct`` (section 4.2 bullet 2,
    case 9.3.7). Spec 08 section 4.1 step 4 makes that unknown a pass carrying
    the step-4 reason: chrome is not evidence for *naming* a landlord, and the
    absence of a name is no longer a reason to hold the advert."""
    dec = classify_tenancy_channel(_rec(landlord_name=token,
                                        landlord_evidence_source="manual",
                                        landlord_evidence_excerpt=token))
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "advertiser not identified"


def test_portal_identity_landlord_name_with_clean_excerpt_is_absent():
    """The plan's literal case: ``landlord_name`` is ``Lejebolig.dk``."""
    dec = classify_tenancy_channel(_rec(landlord_name="Lejebolig.dk",
                                        landlord_evidence_source="detail",
                                        landlord_evidence_excerpt="CVR-nr. 27258948"))
    assert dec.state == "unknown"


def test_portal_identity_excerpt_with_clean_landlord_name_is_absent():
    """A clean ``landlord_name`` cannot rescue an excerpt that is portal chrome."""
    dec = classify_tenancy_channel(_rec(landlord_name="Udlejer",
                                        landlord_evidence_source="detail",
                                        landlord_evidence_excerpt="Valideret af BoligPortal"))
    assert dec.state == "unknown"


# --- Case 9.3.8: repeated inventory / advertisement counts are not evidence ---
def test_num_rentables_only_is_unknown():
    """``landlord_information.num_rentables`` on its own is an advertisement
    count, not an advertiser identity (section 4.2 bullet 5, section 12).

    Spec 08 section 4.1 route 3c admits the portfolio only from this advert's
    OWN block, i.e. only when ``page_props.ad.id == source_record.id`` (section
    12: other adverts' cards embedded in a detail page are never this advert's
    evidence). This record carries no ``ad`` block at all, so the count proves
    nothing and the verdict is the step-4 unknown, which v3.7 makes a pass. The
    source-scoped route is pinned in ``tests/test_spec08.py``.
    """
    rec = _rec(detail_hydration={"props": {"page_props": {"landlord_information": {
        "num_rentables": 17413,
        "profile_created": "2011-04-02",
        "last_activity_date": "2026-09-14",
        "bank_id_verified": True,
    }}}})
    dec = classify_tenancy_channel(rec)
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "advertiser not identified"


def test_three_cards_at_one_address_are_not_landlord_evidence():
    """Several adverts at one address prove repeated inventory, not who the
    landlord is (case 9.3.8), and an unlimited lease proves nothing either
    (section 12)."""
    cards = [{"url": f"https://www.boligportal.dk/lejligheder/x-{n}"} for n in range(3)]
    rec = _rec(rental_period="ubegrænset", rental_period_raw=0,
               detail_hydration={"props": {"page_props": {"results": cards}}})
    dec = classify_tenancy_channel(rec)
    assert dec.state == "unknown"
    assert dec.decision == "pass"


# --- Case 9.3.5: a missing advertiser name is unknown ------------------------
def test_missing_advertiser_name_is_unknown():
    """Every stored boligportal row carries ``is_owner=False`` and no advertiser
    name; that is an honest unknown, never professional by assumption. Spec 08
    section 4.1 step 4 states the reason and makes it a pass."""
    dec = classify_tenancy_channel(_rec(source_record={"id": 1, "is_owner": False,
                                                      "has_owner": False}))
    assert dec.state == "unknown"
    assert dec.decision == "pass"
    assert dec.reason == "advertiser not identified"
    assert dec.source is None


# --- Case 9.3.2: explicit private / first-person wording ---------------------
@pytest.mark.parametrize("wording", [
    "Jeg udlejer min lejlighed",
    "I min lejlighed på Nørrebro udlejer jeg et møbleret værelse",
    "udlejer derfor to tilstødende værelser i min lejlighed",
    "Jeg udlejer min lejlighed fra d. 1/10",
    "I'm subletting my apartment",
])
def test_private_landlord_wording_rejects(wording):
    """The production-confirmed first-person private rental wordings reject as
    ``private_landlord`` (section 4.2 bullet 4, case 9.3.2)."""
    dec = classify_tenancy_channel(_with_detail(_rec(), wording))
    assert dec.state == "private_landlord"
    assert dec.decision == "reject"
    assert dec.reason == "private landlord"
    assert len(dec.excerpt or "") <= 200


def test_private_wording_beats_structured_professional_evidence():
    """Precedence: explicit private wording outranks a project-advertiser block
    on the same record (plan duty 1, first match wins)."""
    rec = _with_newbuild(_with_detail(_rec(), "Jeg udlejer min lejlighed på Nørrebro."))
    dec = classify_tenancy_channel(rec)
    assert dec.state == "private_landlord"
    assert dec.decision == "reject"


# --- Duty 2 / case 9.3.4: explicit sublet, ahead of everything ---------------
@pytest.mark.parametrize("wording", [
    "Lejligheden er til fremleje i 6 måneder.",
    "Boligen fremlejes møbleret.",
    "This is a sublet of a furnished flat.",
    "Sublease available from October.",
    "Room in shared apartment near the station.",
])
def test_sublet_wording_rejects_as_sublet(wording):
    """The spec's literal sublet token list rejects with the accurate sublet
    reason (section 4.2 bullet 1, plan duty 2)."""
    dec = classify_tenancy_channel(_with_detail(_rec(), wording))
    assert dec.state == "sublet"
    assert dec.decision == "reject"
    assert dec.reason == "sublet"


def test_sublet_reason_beats_unlimited_period():
    """A sublet rejects as ``sublet`` even when ``rental_period`` says unlimited
    (case 9.3.4): sublet detection precedes generic time-limited handling."""
    rec = _with_detail(_rec(rental_period="ubegrænset", rental_period_raw=0),
                       "Fremleje af møbleret lejlighed, ubegrænset periode.")
    dec = classify_tenancy_channel(rec)
    assert dec.state == "sublet"
    assert dec.reason == "sublet"
    assert "time-limited" not in (dec.reason or "")


def test_sublet_beats_structured_professional_evidence():
    """Precedence: sublet outranks the project-advertiser block on the same record."""
    rec = _with_newbuild(_with_detail(_rec(), "Fremlejes i 12 måneder."))
    dec = classify_tenancy_channel(rec)
    assert dec.state == "sublet"
    assert dec.decision == "reject"


# --- Contract: determinism, bounded excerpts, no network ----------------------
def test_classifier_is_deterministic_and_does_not_mutate_the_record():
    recs = [
        _with_newbuild(_rec()),
        _with_detail(_rec(), "Jeg udlejer min lejlighed."),
        _with_detail(_rec(), "Fremleje i 3 måneder."),
        _rec(landlord_name="Lejebolig.dk", landlord_evidence_source="manual"),
        _rec(),
    ]
    for rec in recs:
        before = copy.deepcopy(rec)
        first = classify_tenancy_channel(rec)
        second = classify_tenancy_channel(rec)
        assert (first.state, first.decision, first.reason, first.source,
                first.excerpt) == (second.state, second.decision, second.reason,
                                   second.source, second.excerpt)
        assert rec == before, "classifier mutated its input record"


def test_every_excerpt_is_bounded_to_two_hundred_characters():
    long = "z" * 600
    recs = [
        _with_newbuild(_rec(), details=dict(NEWBUILD_DETAILS, company_name=long)),
        _rec(landlord_name="Bohavn", landlord_evidence_source="manual",
             landlord_evidence_excerpt=long),
        _with_detail(_rec(), f"Fremleje. {long}"),
        _with_detail(_rec(), f"Jeg udlejer min lejlighed. {long}"),
    ]
    for rec in recs:
        dec = classify_tenancy_channel(rec)
        assert isinstance(dec, EvidenceDecision)
        assert dec.excerpt is None or len(dec.excerpt) <= 200


def test_classifier_makes_no_network_call(monkeypatch):
    """Section 12: trust classification makes zero network calls."""

    def _boom(*args, **kwargs):
        raise AssertionError("classify_tenancy_channel attempted a network call")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    for rec in (_with_newbuild(_rec()),
                _with_detail(_rec(), "Fremleje i 3 måneder."),
                _rec(landlord_name="Bohavn", landlord_evidence_source="manual")):
        classify_tenancy_channel(rec)


def test_classifier_is_json_round_trip_safe():
    """The stored record reaches the classifier as JSON from ``raw_data``."""
    rec = _with_newbuild(_rec())
    dec_a = classify_tenancy_channel(rec)
    dec_b = classify_tenancy_channel(json.loads(json.dumps(rec)))
    assert dec_a.state == dec_b.state == "professional_direct"
    assert dec_a.excerpt == dec_b.excerpt
