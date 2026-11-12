"""The JSON profile loader: validation, overrides and derived constants."""
import json
import os

import pytest

import config
import profile


def _write(tmp_path, data, name="profile.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _apply(tmp_path, monkeypatch, data):
    path = _write(tmp_path, data)
    monkeypatch.setenv(profile.ENV_VAR, path)
    namespace = dict(vars(config))
    profile.apply(namespace)
    return namespace


def test_example_profile_is_valid_and_loadable():
    path = os.path.join(config.BASE_DIR, "profile.example.json")
    data = profile.load(path)
    assert data["search"]["rent_cap_dkk"] > 0


def test_profile_overrides_budget_and_rebuilds_the_curve(tmp_path, monkeypatch):
    ns = _apply(tmp_path, monkeypatch, {
        "search": {"rent_cap_dkk": 12500, "affordable_rent_dkk": 9500},
    })
    assert ns["RENT_CAP_MAX"] == 12500
    assert ns["AFFORDABILITY_ZERO_RENT"] == 12500.0
    assert ns["AFFORDABILITY_SPAN"] == 3000.0
    assert ns["AFFORDABILITY_CURVE"] == [(9500.0, 35.0), (12500.0, 0)]


def test_target_date_moves_the_default_window(tmp_path, monkeypatch):
    ns = _apply(tmp_path, monkeypatch, {
        "search": {"target_move_date": "2027-09-01"},
    })
    assert ns["TARGET_MOVE_DATE"] == "2027-09-01"
    assert ns["PREFERRED_WINDOW_START"] == "2027-08-17"
    assert ns["PREFERRED_WINDOW_END"] == "2027-09-16"


def test_unknown_key_is_an_error_not_a_silent_typo(tmp_path, monkeypatch):
    path = _write(tmp_path, {"search": {"rent_capp_dkk": 9000}})
    monkeypatch.setenv(profile.ENV_VAR, path)
    with pytest.raises(profile.ProfileError, match="rent_capp_dkk"):
        profile.apply(dict(vars(config)))


def test_scoring_weights_must_sum_to_100(tmp_path, monkeypatch):
    path = _write(tmp_path, {"scoring": {"affordability": 50}})
    monkeypatch.setenv(profile.ENV_VAR, path)
    with pytest.raises(profile.ProfileError, match="add up to 100"):
        profile.apply(dict(vars(config)))


def test_never_zones_and_station_ratings_are_parsed(tmp_path, monkeypatch):
    ns = _apply(tmp_path, monkeypatch, {
        "fit": {
            "default_rating": 0.25,
            "station_ratings": {"Test Station": 0.75},
            "never_postcodes": {"9999": "Nowhere"},
            "never_zones": [
                {"name": "Test zone", "lat": 55.1, "lon": 12.1, "radius_m": 250},
            ],
        },
    })
    assert ns["AREA_DEFAULT_RATING"] == 0.25
    assert ns["AREA_STATION_RATINGS"] == {"Test Station": 0.75}
    assert ns["NEVER_POSTCODES"] == {"9999": "Nowhere"}
    assert ns["NEVER_ZONES"] == (("Test zone", 55.1, 12.1, 250.0),)


def test_comment_keys_are_ignored(tmp_path, monkeypatch):
    ns = _apply(tmp_path, monkeypatch, {
        "search": {"_comment": "human note", "rent_cap_dkk": 11000},
        "complexes": {"_example": "not a group"},
    })
    assert ns["RENT_CAP_MAX"] == 11000
    assert ns["COMPLEX_GROUPS"] == {}


def test_missing_explicit_profile_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(profile.ENV_VAR, str(tmp_path / "nope.json"))
    with pytest.raises(profile.ProfileError, match="profile not found"):
        profile.apply(dict(vars(config)))
