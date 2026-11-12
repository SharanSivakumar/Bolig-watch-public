"""Market benchmark segments, the fallback chain and per-listing value points."""
import datetime

import pytest

import benchmarks
import config


SEGMENTS = {
    "2300|A": {"n": 12, "median_kr_m2": 260.0, "p25_kr_m2": 230.0, "p75_kr_m2": 290.0},
    "2300": {"n": 40, "median_kr_m2": 250.0, "p25_kr_m2": 220.0, "p75_kr_m2": 280.0},
    "A": {"n": 90, "median_kr_m2": 268.0, "p25_kr_m2": 240.0, "p75_kr_m2": 300.0},
    "all": {"n": 412, "median_kr_m2": 240.0, "p25_kr_m2": 200.0, "p75_kr_m2": 280.0},
}


def test_quartiles():
    p25, median, p75 = benchmarks._quartiles([10, 20, 30, 40])
    assert median == 25
    assert p25 <= median <= p75


def test_segment_chain_prefers_the_most_specific_segment():
    key, seg = benchmarks.lookup_segment(SEGMENTS, "2300", "A")
    assert key == "2300|A"
    assert seg["median_kr_m2"] == 260.0


def test_segment_chain_skips_thin_segments():
    """A segment below BENCHMARK_MIN_N is skipped, not used with a bad median."""
    thin = dict(SEGMENTS)
    thin["2300|A"] = {"n": config.BENCHMARK_MIN_N - 1, "median_kr_m2": 999.0,
                      "p25_kr_m2": 900.0, "p75_kr_m2": 1100.0}
    key, seg = benchmarks.lookup_segment(thin, "2300", "A")
    assert key == "2300"

    thin["2300"] = {"n": 2, "median_kr_m2": 111.0, "p25_kr_m2": 1.0, "p75_kr_m2": 2.0}
    key, _seg = benchmarks.lookup_segment(thin, "2300", "A")
    assert key == "A"

    assert benchmarks.lookup_segment(thin, "9999", None)[0] == "all"
    assert benchmarks.lookup_segment({}, "2300", "A") == (None, None)


@pytest.mark.parametrize("ratio,expected_pts", [
    (0.75, config.SCORE_VALUE_PTS),
    (1.00, config.SCORE_VALUE_PTS / 2),
    (1.25, 0.0),
    (0.50, config.SCORE_VALUE_PTS),
    (1.50, 0.0),
])
def test_value_points_at_each_ratio(ratio, expected_pts):
    median = SEGMENTS["2300|A"]["median_kr_m2"]
    size = 30.0
    rent = ratio * median * size
    out = benchmarks.value_fields(rent, size, SEGMENTS, "2300", "A")
    assert out["value_pts"] == pytest.approx(expected_pts, abs=0.01)
    assert out["value_ratio"] == pytest.approx(ratio, abs=0.001)


def test_value_label_reads_as_a_percentage_against_market():
    out = benchmarks.value_fields(8300, 32.0, SEGMENTS, "2300", "A")
    assert out["kr_per_m2"] == pytest.approx(259.4, abs=0.1)
    # The spec's label always carries an explicit sign, so at-market reads "+0%".
    assert out["value_label"] == "+0% vs market"
    assert out["benchmark_segment"] == "2300|A"

    cheap = benchmarks.value_fields(7000, 32.0, SEGMENTS, "2300", "A")
    assert cheap["value_label"] == "-16% vs market"

    dear = benchmarks.value_fields(9500, 32.0, SEGMENTS, "2300", "A")
    assert dear["value_label"] == "+14% vs market"


def test_size_unknown_uses_the_damped_rent_proxy():
    out = benchmarks.value_fields(5000, None, SEGMENTS, "2300", "A")
    assert out["kr_per_m2"] is None
    assert out["value_ratio"] is None
    assert out["value_label"] == "size unknown"
    # 0.6 * 25 * clamp((9500-5000)/4500) = 15.0
    assert out["value_pts"] == pytest.approx(15.0, abs=0.01)

    tiny = benchmarks.value_fields(5000, 5.0, SEGMENTS, "2300", "A")
    assert tiny["value_label"] == "size unknown", "a sub-10 m2 size is not trustworthy"


def test_no_benchmark_falls_back_without_claiming_a_ratio():
    out = benchmarks.value_fields(8300, 32.0, {}, "2300", "A")
    assert out["value_ratio"] is None
    assert out["value_label"] == "no market benchmark"
    assert out["value_pts"] > 0


def test_missing_rent_scores_nothing():
    assert benchmarks.value_fields(None, 32.0, SEGMENTS, "2300", "A")["value_pts"] == 0.0
    assert benchmarks.value_fields(0, 32.0, SEGMENTS, "2300", "A")["value_pts"] == 0.0


def test_postcode_extraction():
    assert benchmarks.postcode_from_address("Jorisvej 1a, 2300 København S") == "2300"
    assert benchmarks.postcode_from_address("no postcode here") is None
    assert benchmarks.postcode_from_address(None) is None


def test_tier_for_commute_boundaries():
    assert benchmarks.tier_for_commute(0) == "A"
    assert benchmarks.tier_for_commute(config.TIER_A_MAX_MIN) == "A"
    assert benchmarks.tier_for_commute(config.TIER_A_MAX_MIN + 1) == "B"
    assert benchmarks.tier_for_commute(config.TIER_B_MAX_MIN) == "B"
    assert benchmarks.tier_for_commute(config.TIER_B_MAX_MIN + 1) == "C"
    assert benchmarks.tier_for_commute(config.TIER_C_MAX_MIN) == "C"
    assert benchmarks.tier_for_commute(config.TIER_C_MAX_MIN + 1) is None
    assert benchmarks.tier_for_commute(None) is None


# --- End to end against a synthetic observation set -------------------------

def _seed(store, n, rent, size, postcode, lat=55.66, lon=12.60, days_ago=1, rooms=1):
    """Insert n distinct source listings with one observation each."""
    now = datetime.datetime.now()
    seen = (now - datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")
    for i in range(n):
        addr = f"{postcode} Testvej {i} r{rent}"
        pid = store._find_or_create_property_direct(addr, lat, lon, rooms, size, "new", seen)
        store.conn.execute("UPDATE properties SET postcode=? WHERE id=?", (postcode, pid))
        sid = store.record_source_listing(pid, "test", f"{postcode}-{rent}-{i}", "http://x")
        store.conn.execute("""
            INSERT INTO observations(source_listing_id, property_id, observed_at, rent_total)
            VALUES(?,?,?,?)
        """, (sid, pid, seen, rent))
    store.conn.commit()


def test_benchmarks_are_computed_and_persisted(temp_db):
    store = temp_db
    _seed(store, 10, 9000, 30.0, "2300")   # 300 kr/m2
    _seed(store, 10, 6000, 30.0, "2450")   # 200 kr/m2

    segments = benchmarks.compute_benchmarks(store)
    assert segments["2300"]["n"] == 10
    assert segments["2300"]["median_kr_m2"] == pytest.approx(300.0)
    assert segments["2450"]["median_kr_m2"] == pytest.approx(200.0)
    assert segments["all"]["n"] == 20
    assert segments["all"]["median_kr_m2"] == pytest.approx(250.0)

    stored = store.get_latest_benchmarks()
    assert stored["2300"]["median_kr_m2"] == pytest.approx(300.0)


def test_population_ignores_stale_and_unusable_observations(temp_db):
    store = temp_db
    _seed(store, 9, 9000, 30.0, "2300")
    _seed(store, 5, 4000, 30.0, "2300", days_ago=config.BENCHMARK_WINDOW_DAYS + 10)
    _seed(store, 5, 9000, 4.0, "2300")     # below BENCHMARK_MIN_SIZE_M2
    _seed(store, 5, 9000, 30.0, "2300", rooms=4)

    segments = benchmarks.compute_benchmarks(store)
    assert segments["2300"]["n"] == 9
    assert segments["2300"]["median_kr_m2"] == pytest.approx(300.0)


def test_summary_line_shape():
    line = benchmarks.summary_line(SEGMENTS)
    assert line.startswith(f"Market ({config.BENCHMARK_WINDOW_DAYS} d): A 268 kr/m2")
    assert line.endswith("n=412")
    assert benchmarks.summary_line({}) == (
        f"Market ({config.BENCHMARK_WINDOW_DAYS} d): no segment data · n=0")
