"""Spec 07 section 9.2 — room/studio fixtures for the four confirmed Lejebolig
room listings and the Jorisvej counterexample.

Phase 2 (unit-type correctness) per specs/housing-search/
07-TRUSTED-BLOCKS-AND-INVENTORY-RECOVERY.md section 9.2. Everything is local:
``classify_unit_type`` and ``evaluate_listing`` are deterministic from stored
data and make no network call, and the temp_db integration path goes through
``refresh_local`` (no portal, no alerts). This row deliberately does NOT
implement the Phase-3 tenancy/building/final-trust gates.
"""
import json

import pytest

import config
import geo
import notify
import run
from filters import classify_unit_type, evaluate_listing


# --- Exact-text fixtures for the four confirmed room listings (spec 07 §3.1) ---
def _room(listing_id, title, card_text, description, address, lat, lon, size_m2=14.0):
    """A room listing as it is actually stored in production: the pre-Phase-2
    raw record still carries ``listing_type='lejlighed'`` (Lejebolig hardcoded
    every card to lejlighed back then) plus the fetched detail body whose
    authoritative ``Boligtype Værelse`` label is what now classifies it a room.
    Keeping listing_type='lejlighed' is what lets the detail label — not the
    stale type column — drive the unit decision (spec 07 §6.2, §4.1)."""
    return {
        "site": "lejebolig",
        "listing_id": str(listing_id),
        "url": f"https://www.lejebolig.dk/lejebolig/{listing_id}/",
        "title": title,
        "address": address,
        "listing_type": "lejlighed",
        "rooms": 1,
        "size_m2": size_m2,
        "rent_total": 6000,
        "deposit": 0,
        "prepaid_rent": 0,
        "available_from": "2027-01-20",
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": description,
        "card_text": card_text,
        "card_class": "unknown",
        "detail_state": "fetched",
        "eligibility_text": description,
        "eligibility_source_url": f"https://www.lejebolig.dk/lejebolig/{listing_id}/",
        "eligibility_evidence_state": "verified",
        "eligibility_checked_at": "2026-09-15T10:00:00",
        "lat": lat,
        "lon": lon,
    }


MARIENDALSVEJ = _room(
    1902889, "Mariendalsvej", "14m² værelse på Frederiksberg",
    "Boligtype Værelse. 14m² værelse på Frederiksberg tæt på byen.",
    "Mariendalsvej, 2000 Frederiksberg", 55.681, 12.533)

VED_KLOEVERMARKEN = _room(
    1907503, "Ved Kløvermarken", "Værelse på Amager",
    "Boligtype Værelse. Værelse på Amager tæt på metroen.",
    "Ved Kløvermarken, 2300 København S", 55.655, 12.625)

STORE_KONGENSGADE = _room(
    1788585, "Store Kongensgade", "Værelse i penthouse",
    "Boligtype Værelse. A room in a penthouse on Store Kongensgade.",
    "Store Kongensgade, 1264 København K", 55.685, 12.588)

GLAHNS_ALLE = _room(
    1420677, "Glahns Alle", "Værelse på Frederiksberg",
    "Boligtype Værelse. Værelse på Frederiksberg med fælles køkken.",
    "Glahns Alle, 2000 Frederiksberg", 55.681, 12.526)

JORISVEJ = {
    "site": "lejebolig",
    "listing_id": "1908482",
    "url": "https://www.lejebolig.dk/lejebolig/1908482/",
    "title": "Jorisvej - 1-værelses",
    "address": "Jorisvej, 2300 København S",
    "listing_type": "lejlighed",
    "rooms": 1,
    "size_m2": 32.0,
    "rent_total": 8300,
    "deposit": 20000,
    "prepaid_rent": 0,
    "available_from": "2027-01-20",
    "rental_period": "ubegrænset",
    "rental_period_raw": 0,
    "description": "Boligtype Lejlighed. 1 værelses lejlighed med eget køkken "
                   "og eget badeværelse. Ubetinget lejeperiode.",
    "card_text": "1 værelse for 8.600 kr ved Jorisvej",
    "card_class": "unknown",
    "detail_state": "fetched",
    "eligibility_text": "Boligtype Lejlighed. 1 værelses lejlighed med eget "
                        "køkken og eget badeværelse. Ubetinget lejeperiode.",
    "eligibility_source_url": "https://www.lejebolig.dk/lejebolig/1908482/",
    "eligibility_evidence_state": "verified",
    "eligibility_checked_at": "2026-09-15T10:00:00",
    "lat": 55.640,
    "lon": 12.597,
}


# --- Spec 07 §9.2 positive phrases: each must reject as room rental ---
@pytest.mark.parametrize("phrase,field", [
    ("Værelse på Amager", "card_text"),
    ("14m² værelse på Frederiksberg", "card_text"),
    ("Room in a penthouse on the top floor", "description"),
    ("Room in shared apartment near the station", "description"),
])
def test_room_card_phrases_reject(phrase, field):
    rec = {"site": "lejebolig", "listing_id": "X", "title": "Lejebolig",
           "listing_type": "lejlighed", "card_class": "unknown",
           "card_text": "Lejebolig", field: phrase,
           "url": "https://www.lejebolig.dk/lejebolig/X/"}
    dec = classify_unit_type(rec)
    assert dec.state == "room_rental"
    assert dec.decision == "reject"
    assert "room rental" in (dec.reason or "")


def test_detail_boligtype_vaerelse_rejects():
    """The authoritative detail label 'Boligtype Værelse' is a hard room
    rejection (spec 07 §4.1 precedence 2, §9.2)."""
    rec = dict(JORISVEJ)
    rec["description"] = "Boligtype Værelse. Værelse på Frederiksberg."
    rec["eligibility_text"] = rec["description"]
    dec = classify_unit_type(rec)
    assert dec.state == "room_rental"
    assert dec.decision == "reject"
    assert dec.source == "detail"


def test_card_room_marker_decides_unknown_card():
    """A room marker in the card/title (MARIENDALSVEJ '14m² værelse på...') is
    decisive even when the card class is unknown (spec 11 section 3: the room
    signal runs before stored detail and reports source 'card')."""
    rec = dict(MARIENDALSVEJ)  # card_class unknown, card '14m² værelse på...'
    dec = classify_unit_type(rec)
    assert dec.state == "room_rental"
    assert dec.decision == "reject"
    assert dec.source == "card"


def test_detail_boligtype_vaerelse_overrides_apartment_card():
    """Detail 'Boligtype Værelse' overrides an incorrect apartment-looking card
    guess (spec 07 §4.1 precedence: detail beats card)."""
    rec = dict(JORISVEJ)
    rec["card_text"] = "1-værelses lejlighed ved Store Kongensgade"
    rec["description"] = "Boligtype Værelse. A room in a penthouse."
    rec["eligibility_text"] = rec["description"]
    dec = classify_unit_type(rec)
    assert dec.state == "room_rental"
    assert dec.decision == "reject"
    assert dec.source == "detail"


def test_kitchen_bath_fallback_unreachable_for_room():
    """A room rental with strong private kitchen/bath wording is still rejected
    as a room before the apartment kitchen/bathroom fallback can pass it (spec
    07 §9.2, §4.1: an authoritative room result is never overridden by kitchen/
    bathroom fallback assumptions)."""
    rec = dict(MARIENDALSVEJ)
    rec["description"] = ("Boligtype Værelse. 14m² værelse på Frederiksberg. "
                          "Eget køkken og eget badeværelse.")
    rec["eligibility_text"] = rec["description"]
    res = evaluate_listing(rec, stations=[])
    assert res.rejected
    assert "room rental" in (res.rejection_reason or "")


# --- Spec 07 §9.2 negative phrases: must NOT classify as room rental ---
@pytest.mark.parametrize("rec_factory", [
    lambda: {"site": "lejebolig", "listing_id": "A", "title": "Ét værelses lejlighed",
             "listing_type": "lejlighed", "card_class": "apartment",
             "description": "Eget køkken og eget badeværelse.",
             "url": "https://www.lejebolig.dk/lejebolig/A/"},
    lambda: {"site": "lejebolig", "listing_id": "B",
             "title": "1 værelse for 8.600 kr ved Østerbro",
             "listing_type": "lejlighed", "card_class": "apartment",
             "description": "Boligtype Lejlighed. Eget køkken og eget badeværelse.",
             "url": "https://www.lejebolig.dk/lejebolig/B/"},
    lambda: {"site": "lejebolig", "listing_id": "C",
             "title": "1-værelses lejlighed i København",
             "listing_type": "lejlighed", "card_class": "apartment",
             "description": "Eget køkken og eget badeværelse.",
             "url": "https://www.lejebolig.dk/lejebolig/C/"},
])
def test_studio_room_count_phrases_not_room(rec_factory):
    """"ét værelse", '1 værelse for 8.600 kr' and '1-værelses lejlighed'
    describe the room COUNT of a real studio, never a rented bedroom (spec 07
    §3.1 Jorisvej counterexample, §9.2)."""
    dec = classify_unit_type(rec_factory())
    assert dec.state != "room_rental"
    assert dec.decision != "reject"


def test_detail_boligtype_lejlighed_is_apartment():
    """Detail 'Boligtype Lejlighed' classifies as a self-contained apartment,
    not a room (spec 07 §4.1 precedence 2, §9.2)."""
    dec = classify_unit_type(JORISVEJ)
    assert dec.state == "self_contained_apartment"
    assert dec.decision == "pass"


def test_card_room_marker_overrides_detail_lejlighed():
    """A room marker in the card text ('Værelse til leje med 1 værelse') is
    decisive and beats an authoritative detail 'Boligtype Lejlighed' (spec 11
    section 3: room card markers override the detail label)."""
    rec = dict(JORISVEJ)
    rec["card_text"] = "Værelse til leje med 1 værelse"
    dec = classify_unit_type(rec)
    assert dec.state == "room_rental"
    assert dec.decision == "reject"
    assert dec.source == "card"


# --- Spec 07 §9.2 integration: temp_db local refresh ---
def _seed_listing(store, rec, n):
    """Insert a property + a linked, active source listing carrying the full
    (reconstructed) record in raw_data, the way local processing reads it."""
    store.conn.execute(
        "INSERT INTO properties(normalized_address, lat, lon, bw_id) "
        "VALUES(?,?,?,?)",
        (rec["address"], rec["lat"], rec["lon"], "BW%d" % n))
    pid = store.conn.execute(
        "SELECT id FROM properties WHERE bw_id=?", ("BW%d" % n,)).fetchone()["id"]
    store.conn.execute(
        "INSERT INTO source_listings(property_id, site, listing_id, url, "
        "lifecycle_state, first_seen_at, last_seen_at, raw_data) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (pid, rec["site"], rec["listing_id"], rec["url"], "active",
         "2026-09-14T10:00:00", "2026-09-14T10:00:00", json.dumps(rec)))
    store.conn.commit()
    sid = store.conn.execute(
        "SELECT id FROM source_listings WHERE site=? AND listing_id=?",
        (rec["site"], rec["listing_id"])).fetchone()["id"]
    return pid, sid


def test_four_rooms_excluded_from_matches_after_refresh(
        temp_db, mock_stations, monkeypatch):
    """Spec 07 §9.2: after a local refresh the four confirmed room listings
    cannot surface through Store.get_matches() (they reject as room rental) and
    Jorisvej is not mistaken for a room."""
    monkeypatch.setattr(geo, "load_stations", lambda: mock_stations)
    monkeypatch.setattr(run.benchmarks, "compute_benchmarks", lambda store: {})
    # Local refresh must make zero alerts; any attempt fails the test.
    monkeypatch.setattr(notify, "notify_match",
                        lambda *a, **k: pytest.fail("alert sent during refresh"))
    monkeypatch.setattr(notify, "send_text",
                        lambda *a, **k: pytest.fail("alert sent during refresh"))

    room_prop_ids = []
    room_sids = []
    for i, rec in enumerate([MARIENDALSVEJ, VED_KLOEVERMARKEN,
                             STORE_KONGENSGADE, GLAHNS_ALLE], start=101):
        pid, sid = _seed_listing(temp_db, rec, i)
        room_prop_ids.append(pid)
        room_sids.append(sid)
    jo_pid, jo_sid = _seed_listing(temp_db, JORISVEJ, 201)

    run.refresh_local(temp_db, export=False, quiet=True)

    # None of the four room properties may hold a match row.
    matched_prop_ids = {m["property_id"] for m in temp_db.get_matches()}
    assert not (set(room_prop_ids) & matched_prop_ids)

    # Each room listing is recorded as a rejection with the correct reason.
    for sid in room_sids:
        rows = temp_db.conn.execute(
            "SELECT reason FROM rejection_history WHERE source_listing_id=?",
            (sid,)).fetchall()
        assert rows, "expected a rejection_history row for the room listing"
        assert any("room rental" in (r["reason"] or "") for r in rows)

    # Jorisvej is a self-contained apartment by the unit-type gate.
    raw = json.loads(temp_db.get_source_listing("lejebolig", "1908482")["raw_data"])
    unit = classify_unit_type(raw)
    assert unit.decision != "reject"
    assert unit.state == "self_contained_apartment"


def test_room_ids_are_rejected_not_quarantined(temp_db, mock_stations, monkeypatch):
    """The four rooms reject at the unit-type gate; they must not be held as a
    review/quarantine item instead (spec 07 §9.2, §2 decision 1)."""
    monkeypatch.setattr(geo, "load_stations", lambda: mock_stations)
    monkeypatch.setattr(run.benchmarks, "compute_benchmarks", lambda store: {})
    monkeypatch.setattr(notify, "notify_match",
                        lambda *a, **k: pytest.fail("alert sent"))
    monkeypatch.setattr(notify, "send_text",
                        lambda *a, **k: pytest.fail("alert sent"))
    for i, rec in enumerate([MARIENDALSVEJ, VED_KLOEVERMARKEN,
                             STORE_KONGENSGADE, GLAHNS_ALLE], start=301):
        _seed_listing(temp_db, rec, i)
    run.refresh_local(temp_db, export=False, quiet=True)
    rejected = temp_db.conn.execute(
        "SELECT COUNT(*) AS c FROM rejection_history").fetchone()["c"]
    assert rejected == 4
