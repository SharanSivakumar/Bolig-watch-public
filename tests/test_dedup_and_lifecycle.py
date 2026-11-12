"""Tests for cross-portal deduplication, observation logging, price tracking, and lifecycle states."""
import dataclasses
import pytest
import config
from store import Store, format_bw_id


@dataclasses.dataclass
class DummyEval:
    category: int = 1
    category_name: str = "Metro Close"
    score: float = 90.0
    score_breakdown: dict = dataclasses.field(default_factory=lambda: {"metro": 40.0, "rent": 20.0, "city": 15.0, "date": 10.0, "confidence": 5.0})
    risk_state: str = "low"
    risk_evidence: list = dataclasses.field(default_factory=lambda: ["No obvious warning signals"])
    confidence_points: int = 5
    city_distance_m: float = 2000.0
    metro_walk_m: float = 400.0
    straight_dist_m: float = 300.0
    nearest_station: str = "København Syd"
    walk_min: int = 5
    available_date_str: str = "2027-02-01"
    rent_total: int = 8000
    move_in_cost: int = 32000
    move_in_cost_verified: bool = True
    lease_verified: bool = True
    kitchen_verified: bool = True
    bathroom_verified: bool = True
    rooms_verified: bool = True


def test_cross_portal_exact_address_merge(temp_db):
    store = temp_db
    addr = "Poppelstykket 12, 2450 København SV"

    # Portal 1: Lejebolig
    prop1_id, bw1_id, status1 = store.resolve_property(
        addr=addr, lat=55.6515, lon=12.5125, rooms=1, size_m2=36.0
    )
    assert status1 == "new"
    s1_id = store.record_source_listing(
        property_id=prop1_id, site="lejebolig", listing_id="1001", url="https://lejebolig.dk/1001"
    )

    # Portal 2: BoligPortal (same address)
    prop2_id, bw2_id, status2 = store.resolve_property(
        addr=addr, lat=55.6515, lon=12.5125, rooms=1, size_m2=36.0
    )
    assert status2 == "exact_address"
    assert prop1_id == prop2_id
    assert bw1_id == bw2_id

    s2_id = store.record_source_listing(
        property_id=prop2_id, site="boligportal", listing_id="2002", url="https://boligportal.dk/2002"
    )

    # Check that both URLs are linked to the same property
    prop_data = store.get_property_by_bw_id(bw1_id)
    assert len(prop_data["sources"]) == 2
    sites = {s["site"] for s in prop_data["sources"]}
    assert sites == {"lejebolig", "boligportal"}


def test_medium_confidence_duplicate_does_not_auto_merge(temp_db):
    store = temp_db
    # Property 1
    p1, bw1, _ = store.resolve_property(
        addr="Gammel Kongevej 10", lat=55.6740, lon=12.5500, rooms=1, size_m2=30.0
    )

    # Property 2: 15m away, but 2 rooms instead of 1 room -> should NOT auto-merge
    p2, bw2, status2 = store.resolve_property(
        addr="Gammel Kongevej 12", lat=55.6741, lon=12.5501, rooms=2, size_m2=65.0
    )
    assert status2 == "new"
    assert p1 != p2

    # Verify possible duplicate was recorded
    cur = store.conn.execute("SELECT * FROM possible_duplicates WHERE property_id_1=?", (p1,))
    rows = cur.fetchall()
    assert len(rows) >= 1


def test_price_change_thresholds(temp_db):
    store = temp_db
    p_id, bw_id, _ = store.resolve_property("Testvej 1", 55.6, 12.5, 1, 35.0)
    s_id = store.record_source_listing(p_id, "boligportal", "3001", "https://bp.dk/3001")

    # Obs 1: 8000 DKK
    rec1 = {"rent_total": 8000, "move_in_cost": 32000, "available_from": "2027-02-01"}
    _, ch1 = store.record_observation(s_id, p_id, 1, rec1, DummyEval(rent_total=8000))
    assert "price_change" not in ch1

    # Obs 2: 7801 DKK (-199 DKK) -> history only, below 200 DKK threshold
    rec2 = {"rent_total": 7801, "move_in_cost": 31204, "available_from": "2027-02-01"}
    _, ch2 = store.record_observation(s_id, p_id, 2, rec2, DummyEval(rent_total=7801))
    assert "price_change" not in ch2

    # Obs 3: 7601 DKK (-200 DKK from 7801) -> included in changes
    rec3 = {"rent_total": 7601, "move_in_cost": 30404, "available_from": "2027-02-01"}
    _, ch3 = store.record_observation(s_id, p_id, 3, rec3, DummyEval(rent_total=7601))
    assert "price_change" in ch3
    assert ch3["price_change"]["diff"] == -200


def test_lifecycle_missing_expired_relisted_transitions(temp_db):
    store = temp_db
    p_id, bw_id, _ = store.resolve_property("Testvej 2", 55.6, 12.5, 1, 35.0)
    s_id = store.record_source_listing(p_id, "lejebolig", "4001", "https://lb.dk/4001")

    # Initial state is active
    cur = store.conn.execute("SELECT lifecycle_state, consecutive_misses FROM source_listings WHERE id=?", (s_id,))
    row = cur.fetchone()
    assert row["lifecycle_state"] == "active"
    assert row["consecutive_misses"] == 0

    # Scan 1: not observed -> missing (misses = 1)
    store.update_lifecycle_after_scan("lejebolig", observed_source_ids=[])
    cur = store.conn.execute("SELECT lifecycle_state, consecutive_misses FROM source_listings WHERE id=?", (s_id,))
    row = cur.fetchone()
    assert row["lifecycle_state"] == "missing"
    assert row["consecutive_misses"] == 1

    # Scan 2: not observed (misses = 2)
    store.update_lifecycle_after_scan("lejebolig", observed_source_ids=[])
    cur = store.conn.execute("SELECT consecutive_misses FROM source_listings WHERE id=?", (s_id,))
    assert cur.fetchone()["consecutive_misses"] == 2

    # Scan 3: not observed (misses = 3) -> presumed expired
    store.update_lifecycle_after_scan("lejebolig", observed_source_ids=[])
    cur = store.conn.execute("SELECT lifecycle_state, consecutive_misses FROM source_listings WHERE id=?", (s_id,))
    row = cur.fetchone()
    assert row["lifecycle_state"] == "expired"
    assert row["consecutive_misses"] == 3

    # Scan 4: reappears -> relisted!
    store.update_lifecycle_after_scan("lejebolig", observed_source_ids=[s_id])
    cur = store.conn.execute("SELECT lifecycle_state, consecutive_misses FROM source_listings WHERE id=?", (s_id,))
    row = cur.fetchone()
    assert row["lifecycle_state"] == "relisted"
    assert row["consecutive_misses"] == 0
