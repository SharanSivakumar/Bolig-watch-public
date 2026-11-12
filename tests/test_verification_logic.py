"""Regressions from the September 2026 verification review."""

import filters
import fetchers


def test_known_randers_location_beats_missing_detail_evidence():
    rec = {
        "site": "boligportal", "listing_id": "77726",
        "title": "Lejlighed på Kavalerivej 22, St. tv",
        "address": "Kavalerivej 22, St. tv, 8930 Randers NØ",
        "listing_type": "lejlighed", "rooms": 3, "rent_total": 9246,
        "lat": 56.48470431, "lon": 10.03094436,
    }
    result = filters.evaluate_listing(rec, [])
    assert result.rejected
    assert result.rejection_reason.startswith("outside Greater Copenhagen")


def test_project_minimum_price_is_not_an_individual_unit():
    rec = {
        "site": "boligportal", "listing_id": "5789826",
        "url": "https://www.boligportal.dk/projektudlejning/englandshaven-76645",
        "title": "Englandshaven", "address": "Englandsvej 51, 2300 København S",
        "listing_type": "lejlighed", "rooms": 1, "rent_total": 7500,
        "lat": 55.65, "lon": 12.60,
    }
    result = filters.evaluate_listing(rec, [])
    assert result.rejected
    assert result.rejection_reason == "project page / no individually priced unit"


def test_known_total_rent_beats_missing_detail_evidence():
    rec = {
        "site": "boligportal", "listing_id": "5818749",
        "title": "Velkommen til Blækhus Vanløse",
        "listing_type": "lejlighed", "rooms": 1,
        "rent_base": 9450, "utilities_amount": 550, "rent_total": 10000,
        "lat": 55.68841023, "lon": 12.49417228,
    }
    result = filters.evaluate_listing(rec, [])
    assert result.rejected
    assert result.rejection_reason == "rent 10000 > 9700"


def test_communal_kitchen_amenity_does_not_override_apartment_fallback():
    # v3.12 (spec 11) section 4A: a shared-kitchen mention with NO private
    # kitchen evidence quarantines instead of falling back to an assumption.
    rec = {
        "site": "boligportal", "listing_id": "5818742",
        "listing_type": "lejlighed", "card_class": "apartment",
        "eligibility_evidence_state": "verified",
        "eligibility_text": (
            "På Blækhus får du en række skønne faciliteter såsom moderne "
            "vaskeri, fælleskøkkener og fælleslokaler. "
            "Boligtype Lejlighed. Værelser 1. Lejeperiode Ubegrænset."
        ),
    }
    kitchen = filters.analyse_kitchen(rec, rooms_verified=True)
    assert kitchen.decision == "quarantine"
    assert kitchen.kitchen_state == "quarantined"

    rec["eligibility_text"] = "Lejlighed med fælleskøkken."
    unconfirmed = filters.analyse_kitchen(rec, rooms_verified=True)
    assert unconfirmed.decision == "quarantine"
    assert unconfirmed.kitchen_state == "quarantined"
    rec["eligibility_text"] = "Du deler køkken med de øvrige beboere."
    assert filters.analyse_kitchen(rec, rooms_verified=True).decision == "reject"


def test_boligportal_address_uses_locality_instead_of_kobenhavn():
    row = {
        "rentable_id": 1, "street_name": "Vanløse Allé", "street_number": "79",
        "postal_code": 2720, "city": "København", "city_area": "Vanløse",
        "category": "rental_apartment", "monthly_rent": 8800,
        "url": "/lejligheder/kobenhavn/1",
    }
    assert fetchers._bp_parse_result(row, "2026-09-30")["address"] == (
        "Vanløse Allé 79, 2720 Vanløse")
    row["city_area"] = None
    assert fetchers._bp_parse_result(row, "2026-09-30")["address"] == (
        "Vanløse Allé 79, 2720")
