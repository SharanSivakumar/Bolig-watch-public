"""Parser fixture tests for Lejebolig and BoligPortal without live network dependency."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from bs4 import BeautifulSoup
import fetchers
import run


def _lejebolig_batches(batches, failure=False, stalled=False, recommendations_at=None):
    page = MagicMock()
    state = {"batch": 0}

    def cards():
        rows = []
        for listing_id in batches[state["batch"]]:
            href = f"/lejebolig/{listing_id}/studio"
            # The image and descriptive text link point to the same listing.
            rows.append(SimpleNamespace(get_attribute=lambda key, h=href: h,
                                        inner_text=lambda: ""))
            rows.append(SimpleNamespace(get_attribute=lambda key, h=href: h,
                                        inner_text=lambda: "Studio lejlighed 8.000,-"))
        return rows

    def advance(event):
        if failure:
            raise TimeoutError("show-more request timed out")
        if not stalled:
            state["batch"] += 1

    more = MagicMock()
    more.count.return_value = 1
    more.first.is_visible.side_effect = lambda: stalled or state["batch"] < len(batches) - 1
    more.first.dispatch_event.side_effect = advance
    counter = MagicMock()
    counter.count.return_value = 1
    counter.first.inner_text.return_value = "4"
    links = MagicMock()
    links.count.side_effect = lambda: len(cards())
    absent = MagicMock()
    absent.count.return_value = 0
    recommendations = MagicMock()
    recommendations.count.side_effect = lambda: int(
        recommendations_at is not None and state["batch"] >= recommendations_at)

    def locator(selector):
        if selector.startswith("#search-more"):
            return more
        if selector == ".result-count .item-count":
            return counter
        if selector == "a[href*='/lejebolig/']":
            return links
        if selector == "#search-items .popular-area-header":
            return recommendations
        return absent

    page.locator.side_effect = locator
    page.query_selector_all.side_effect = lambda selector: cards()
    page.expect_response.return_value.__enter__.return_value.value = SimpleNamespace(ok=True, status=200)
    return page


def test_lejebolig_loads_later_batches_and_deduplicates():
    page = _lejebolig_batches([[1, 2], [1, 2, 3], [1, 2, 3, 4]])
    result = fetchers.lejebolig_search(page, "koebenhavn-sv")
    assert [row["listing_id"] for row in result] == ["1", "2", "3", "4"]
    assert result.areas[0]["status"] == "ok"
    assert result.areas[0]["pages_loaded"] == 3
    assert result.areas[0]["expected_cards"] == 4


def test_lejebolig_failed_pagination_keeps_cards_and_cannot_expire(temp_db):
    page = _lejebolig_batches([[1, 2], [1, 2, 3, 4]], failure=True)
    result = fetchers.lejebolig_search(page, "koebenhavn-sv")
    assert [row["listing_id"] for row in result] == ["1", "2"]
    assert result.areas[0]["status"] == "failed"
    scan_id = temp_db.start_scan_run("lejebolig")
    run._record_areas(temp_db, scan_id, "lejebolig", result)
    totals = temp_db.scan_area_totals(scan_id)
    coverage = temp_db.compute_coverage_status("lejebolig", scan_id, len(result),
        totals["areas_total"], totals["areas_ok"], totals["areas_failed"])
    assert not temp_db.coverage_counts_as_miss(coverage)


def test_lejebolig_batch_cap_reports_incomplete_coverage():
    page = _lejebolig_batches([[1, 2], [1, 2, 3, 4]])
    result = fetchers.lejebolig_search(page, "koebenhavn-sv", max_pages=1)
    assert len(result) == 2
    assert result.areas[0]["status"] == "failed"
    assert "capped" in result.areas[0]["error"]


def test_lejebolig_stalled_pagination_stops_without_claiming_coverage():
    page = _lejebolig_batches([[1, 2]], stalled=True)
    result = fetchers.lejebolig_search(page, "koebenhavn-sv")
    assert len(result) == 2
    assert result.areas[0]["status"] == "failed"
    assert "no progress" in result.areas[0]["error"]


def test_lejebolig_stops_when_area_ends_and_nearby_suggestions_begin():
    page = _lejebolig_batches([[1, 2], [1, 2, 3], [1, 2, 3, 4]], recommendations_at=1)
    result = fetchers.lejebolig_search(page, "koebenhavn-sv")
    assert [row["listing_id"] for row in result] == ["1", "2", "3"]
    assert result.areas[0]["status"] == "ok"
    assert result.areas[0]["pages_loaded"] == 2


def test_boligportal_json_hydration_parser():
    sample_json = {
        "props": {
            "page_props": {
                "results": [
                    {
                        "rentable_id": 98765,
                        "title": "Hyggelig 1-værelses studio på Nørrebro",
                        "street_name": "Nørrebrogade",
                        "street_number": "45",
                        "postal_code": 2200,
                        "category": "rental_apartment",
                        "rooms": 1,
                        "size_m2": 32.0,
                        "monthly_rent": 7500,
                        "monthly_rent_extra_costs": 500,
                        "deposit": 22500,
                        "prepaid_rent": 7500,
                        "rental_period": 0,  # Unlimited
                        "available_from": "2027-02-01",
                        "url": "/lejlighed/98765",
                        "description": "Flot studio med eget køkken og eget bad. Ubegrænset lejeperiode.",
                    }
                ]
            }
        }
    }

    mock_page = MagicMock()
    mock_page.evaluate.return_value = sample_json
    mock_page.locator.return_value.count.return_value = 0

    recs = fetchers.boligportal_search(mock_page, max_pages=1)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["site"] == "boligportal"
    assert rec["listing_id"] == "98765"
    assert rec["rooms"] == 1
    assert rec["rent_base"] == 7500
    assert rec["utilities_amount"] == 500
    assert rec["rent_total"] == 8000
    assert rec["deposit"] == 22500
    assert rec["rental_period"] == "ubegrænset"
    assert rec["available_from"] == "2027-02-01"


def test_lejebolig_detail_parser():
    body_text = """
    Poppelstykket 12, Sydhavnen - Lejebolig
    Husleje: 8.900 kr.
    A/C-forbrug: 400 kr.
    Depositum: 26.700 kr.
    Forudbetalt leje: 8.900 kr.
    Indflytningspris: 44.900 kr.
    Antal værelser: 1
    Areal: 36 m²
    Ledig: 01-02-2027
    Lejeperiode: Ubegrænset
    Beskrivelse: Flot 1-værelses lejlighed med eget køkken og eget badeværelse.
    """
    mock_page = MagicMock()
    mock_page.inner_text.return_value = body_text
    mock_page.title.return_value = "Poppelstykket 12 - 1 værelses lejlighed"
    mock_page.query_selector.return_value = None

    cand = {
        "site": "lejebolig",
        "listing_id": "777",
        "url": "https://www.lejebolig.dk/lejebolig/777/lejlighed",
        "card_text": "Poppelstykket 12",
        "listing_type": "lejlighed",
    }
    rec = fetchers.lejebolig_detail(mock_page, cand)
    assert rec["rent_base"] == 8900
    assert rec["utilities_amount"] == 400
    assert rec["rent_total"] == 9300
    assert rec["deposit"] == 26700
    assert rec["prepaid_rent"] == 8900
    assert rec["move_in_total_explicit"] == 44900
    assert rec["rooms"] == 1
    assert rec["size_m2"] == 36.0
    assert rec["rental_period"].lower() == "ubegrænset"


# --- BoligPortal pagination (spec 11.1) ------------------------------------

def _bp_result(rid, rooms=1, size=32.0):
    return {
        "rentable_id": rid, "title": f"Studio {rid}",
        "street_name": "Testvej", "street_number": str(rid), "postal_code": 2200,
        "category": "rental_apartment", "rooms": rooms, "size_m2": size,
        "monthly_rent": 7500, "monthly_rent_extra_costs": 500,
        "deposit": 22500, "prepaid_rent": 7500, "rental_period": 0,
        "available_from": "2027-02-01", "url": f"/lejlighed/{rid}",
        "description": "Eget køkken og eget bad.",
    }


def _paged_store(pages, limit=18, result_count=None):
    """A mock page whose store JSON serves `pages` in order, one call at a time."""
    mock_page = MagicMock()
    mock_page.locator.return_value.count.return_value = 0
    seq = [{"props": {"page_props": {"results": p, "limit": limit,
                                     "result_count": (result_count
                                                      if result_count is not None
                                                      else sum(len(x) for x in pages))}}}
           for p in pages]
    mock_page.evaluate.side_effect = seq + [{"props": {"page_props": {"results": []}}}] * 40
    return mock_page


def test_boligportal_pages_until_result_count_is_exhausted():
    pages = [[_bp_result(i) for i in range(1, 19)],
             [_bp_result(i) for i in range(19, 37)],
             [_bp_result(i) for i in range(37, 41)]]
    mock_page = _paged_store(pages, limit=18, result_count=40)

    recs = fetchers.boligportal_search(mock_page, search_urls=["https://x/?q=1"])
    assert len(recs) == 40
    assert mock_page.evaluate.call_count == 3, "must stop once result_count is reached"


def test_boligportal_reads_page_size_from_the_response():
    """Page size comes from page_props.limit, not a hardcoded 18."""
    pages = [[_bp_result(i) for i in range(1, 6)],
             [_bp_result(i) for i in range(6, 11)]]
    mock_page = _paged_store(pages, limit=5, result_count=10)
    recs = fetchers.boligportal_search(mock_page, search_urls=["https://x/?q=1"])
    assert len(recs) == 10


def test_boligportal_stops_on_an_empty_page():
    mock_page = _paged_store([[_bp_result(1)], []], limit=18, result_count=9999)
    recs = fetchers.boligportal_search(mock_page, search_urls=["https://x/?q=1"])
    assert len(recs) == 1


def test_boligportal_respects_max_pages():
    pages = [[_bp_result(i) for i in range(1 + n * 18, 19 + n * 18)] for n in range(10)]
    mock_page = _paged_store(pages, limit=18, result_count=99999)
    recs = fetchers.boligportal_search(mock_page, max_pages=3, search_urls=["https://x/?q=1"])
    assert len(recs) == 54
    assert mock_page.evaluate.call_count == 3


def test_boligportal_deduplicates_across_search_urls():
    shared = [_bp_result(1), _bp_result(2)]
    mock_page = _paged_store([shared, shared], limit=18, result_count=2)
    recs = fetchers.boligportal_search(mock_page, search_urls=["https://x/?a=1", "https://x/?b=2"])
    assert [r["listing_id"] for r in recs] == ["1", "2"]


def test_boligportal_stores_every_card_including_three_room_flats():
    """Spec 06 acceptance 2: discovery stores the card, the envelope skips the fetch.

    A 3-room card used to be discarded in the parser, before anything was
    stored, so allowing a third room later would have needed a fresh scan. The
    card is now stored and only the detail page is skipped, with the reason.
    """
    page_one = [_bp_result(1, rooms=1), _bp_result(2, rooms=2), _bp_result(3, rooms=3),
                _bp_result(4, rooms=None)]
    mock_page = _paged_store([page_one], limit=18, result_count=4)
    recs = fetchers.boligportal_search(mock_page, search_urls=["https://x/?q=1"])
    ids = [r["listing_id"] for r in recs]
    assert ids == ["1", "2", "3", "4"], "every card the portal returns must be stored"

    by_id = {r["listing_id"]: r for r in recs}
    assert run.card_envelope_state(by_id["3"]) == "skipped:rooms"
    # Unknown rooms is not "too many rooms": it is exactly what a detail page
    # is for, so it stays inside the envelope.
    assert run.card_envelope_state(by_id["4"]) == "fetch"
    assert run.card_envelope_state(by_id["1"]) == "fetch"


def test_boligportal_records_the_postcode_for_benchmarking():
    mock_page = _paged_store([[_bp_result(1)]], limit=18, result_count=1)
    rec = fetchers.boligportal_search(mock_page, search_urls=["https://x/?q=1"])[0]
    assert rec["postcode"] == "2200"
    assert rec["size_m2"] == 32.0
