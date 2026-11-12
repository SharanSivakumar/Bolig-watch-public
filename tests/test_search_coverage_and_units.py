"""Coverage and exact-unit regressions from the September 2026 audit."""
import datetime
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import config
import digest
import fetchers
import filters
import run
import viewer
from conftest import _candidate
from store import resolve_complex_key_for


def test_complex_boundaries_and_unit_suffixes():
    for address in ('Jorisvej 1a, 2300 København S', 'Jorisvej 5, 3. 12, 2300',
                    'Jorisvej 13 st. th, 2300'):
        assert resolve_complex_key_for(address, '2300') == 'jorisvej-complex'
    assert resolve_complex_key_for('Jorisvej', '2300') == 'jorisvej-complex'
    assert resolve_complex_key_for('Jorisvej 99, 2300', '2300') is None
    assert resolve_complex_key_for('Jorisvej 5, 2770', '2770') is None
    assert resolve_complex_key_for('Else Alfelts Vej 42, 2300', '2300') == 'midgaard-complex'
    assert resolve_complex_key_for('Else Alfelts Vej 60B, 2300', '2300') is None
    assert resolve_complex_key_for('C.F. Richs Vej 105C, 1. 105, 2000', '2000') == 'richsgaarden-complex'
    assert resolve_complex_key_for('C.F. Richs Vej 5, 2000', '2000') is None
    assert resolve_complex_key_for('Finsensvej 1, 2000', '2000') is None
    assert resolve_complex_key_for('Njalsgade 4, 2300', '2300') is None


def test_all_units_keep_independent_facts_and_equal_price_options(alert_env):
    store, sent, stations = alert_env
    a = _candidate(listing_id='UNIT-A', rent_total=8000, size_m2=31,
                   available_from='2027-01-01', deposit=20000)
    b = _candidate(listing_id='UNIT-B', rent_total=8000, size_m2=38,
                   available_from='2027-02-15', deposit=24000)
    for rec in (a, b):
        run.process_candidate(rec, stations, store, None, dry_run=True)
    rows = viewer.listing_rows(store)
    assert len(rows) == 1
    units = {u['listing_id']: u for u in rows[0]['units']}
    assert set(units) == {'UNIT-A', 'UNIT-B'}
    assert units['UNIT-A']['size_m2'] == 31
    assert units['UNIT-B']['size_m2'] == 38
    assert units['UNIT-A']['available_from'] == '2027-01-01'
    assert units['UNIT-B']['available_from'] == '2027-02-15'
    assert units['UNIT-A']['comparison']['move_in_cost'] == 28000
    assert units['UNIT-B']['comparison']['move_in_cost'] == 32000
    assert units['UNIT-A']['comparison']['subject_id'] != units['UNIT-B']['comparison']['subject_id']
    assert sent == []


def test_review_and_rejected_units_do_not_lend_a_group_price(alert_env):
    store, _, stations = alert_env
    for rec in (_candidate(listing_id='OK', rent_total=8500),
                _candidate(listing_id='SHARED', rent_total=5000,
                           description='Kun fælles køkken og fælles bad.',
                           eligibility_text='Kun fælles køkken og fælles bad.')):
        run.process_candidate(rec, stations, store, None, dry_run=True)
    row = viewer.listing_rows(store)[0]
    assert [u['listing_id'] for u in row['units']] == ['OK']
    assert row['group_price_min'] == 8500
    assert row['source_count'] == 2


def test_digest_and_cooldown_cover_the_whole_complex(alert_env):
    store, sent, stations = alert_env
    for rec in (_candidate(listing_id='ONE', address='Jorisvej 1a, 2300 København',
                           available_from='2027-02-01', rent_total=8300),
                _candidate(listing_id='TWO', address='Jorisvej 5, 2300 København',
                           available_from='2026-12-01', rent_total=8400, lat=55.651)):
        run.process_candidate(rec, stations, store, None, dry_run=True)
    sources = store.conn.execute('SELECT property_id FROM source_listings ORDER BY id').fetchall()
    assert sources[0][0] != sources[1][0]
    assert len(store.grouped_matches()) == 1
    text = digest.generate_daily_digest(store, segments={})
    # The best-value intel line is absent without a benchmark.
    assert text.count('Jorisvej complex') == 1
    store.mark_actionable_alerted(sources[0][0])
    assert store.alert_cooldown_active(sources[1][0])
    store.clear_match(sources[0][0])
    assert store.alert_cooldown_active(sources[1][0]), 'removing a representative must retain cooldown'
    assert sent == []


def test_current_review_supersedes_a_historical_rejection(alert_env):
    store, _, stations = alert_env
    rec = _candidate(listing_id='RECOVERED')
    run.process_candidate(rec, stations, store, None, dry_run=True)
    source = store.get_source_listing('test', 'RECOVERED')
    store.record_rejection(source['property_id'], source['id'], 'test', 'RECOVERED', 'old price')
    store.conn.execute('DELETE FROM matches')
    store.conn.execute("UPDATE evaluations SET outcome='quarantined' WHERE is_current=1")
    old = (datetime.datetime.now() - datetime.timedelta(hours=26)).isoformat()
    store.conn.execute('UPDATE source_listings SET detail_fetched_at=?', (old,))
    store.conn.commit()
    assert [r['listing_id'] for r in run.select_for_decision_revalidation(store)] == ['RECOVERED']


def test_discovered_fresh_cards_still_receive_daily_decision_revalidation(alert_env, monkeypatch):
    store, _, stations = alert_env
    config_now = datetime.datetime.now()
    rec = _candidate(listing_id='DAILY', site='boligportal')
    counters = run.ScanCounters()
    detail_calls = []
    def detail(page, card):
        detail_calls.append(card['listing_id'])
        return dict(rec)
    run.scan_card(dict(rec), stations, store, None, True, None, False,
                  detail_fetcher=detail, counters=counters)
    old = (config_now - datetime.timedelta(hours=26)).isoformat()
    store.conn.execute('UPDATE source_listings SET detail_fetched_at=?', (old,))
    store.conn.commit()
    detail_calls.clear()
    run._run_card_portal('boligportal', lambda page: fetchers.SearchResult([dict(rec)]),
                         detail, None, stations, store, None, True, 0, None, False,
                         run.ScanCounters())
    assert detail_calls == ['DAILY']


def test_boligportal_cap_empty_and_repeated_pages_are_incomplete(monkeypatch):
    cases = [([{'limit': 1, 'result_count': 2, 'results': [{'id': 1}]}], 1),
             ([{'limit': 1, 'result_count': 2, 'results': [{'id': 1}]},
               {'results': []}], 3),
             ([{'limit': 1, 'result_count': 3, 'results': [{'id': 1}]},
               {'limit': 1, 'results': [{'id': 1}]}], 3)]
    for pages, cap in cases:
        it = iter(pages)
        monkeypatch.setattr(fetchers, '_bp_page', lambda *args: next(it))
        result = fetchers.boligportal_search(MagicMock(), max_pages=cap, search_urls=['query'])
        assert result.areas[0]['status'] == 'failed'
        assert len(result) == 1


def test_explicit_gone_representative_swaps_every_unit_fact(alert_env):
    store, sent, stations = alert_env
    # Both start on or after the target date, so no early-start cost reorders them.
    for rec in (_candidate(listing_id='CHEAP', rent_total=7900, size_m2=31,
                           available_from='2027-02-01', deposit=19000),
                _candidate(listing_id='SIBLING', rent_total=8400, size_m2=39,
                           available_from='2027-02-15', deposit=24000)):
        run.process_candidate(rec, stations, store, None, dry_run=True)
    source = store.get_source_listing('test', 'CHEAP')
    assert store.get_match_row(source['property_id'])['listing_id'] == 'CHEAP'
    store.record_source_explicit_gone('test', 'CHEAP', 'udlejet')
    store.clear_stale_matches()
    rep = store.get_match_row(source['property_id'])
    assert rep['listing_id'] == 'SIBLING'
    assert rep['rent_total'] == 8400
    assert rep['size_m2'] == 39
    assert rep['available_from'] == '2027-02-15'
    assert rep['move_in_cost'] == 32400
    assert sent == []


def test_lejebolig_information_link_wins_over_image_label():
    def element(text, specs=()):
        return SimpleNamespace(inner_text=lambda: text,
            get_attribute=lambda name: '/lejebolig/123/lejlighed-paa-testgade',
            query_selector_all=lambda selector: specs)
    def spec(icon, value):
        return SimpleNamespace(query_selector=lambda selector: (
            SimpleNamespace(get_attribute=lambda name: icon) if selector == 'img'
            else SimpleNamespace(inner_text=lambda: value)))
    info = element('Lejlighed på Testgade\n8.000,-\n32\n1\nUbegrænset',
        [spec('resultat-stat-areal-light.png', '32'),
         spec('resultat-stat-vaerelser-light.png', '1'),
         spec('resultat-stat-periode-light.png', 'Ubegrænset')])
    page = SimpleNamespace(query_selector_all=lambda selector:
        [element('Se billeder hos udlejer'), info])
    seen = set()
    result = fetchers._lejebolig_cards(page, seen)
    assert len(result) == 1
    assert result[0]['card_price'] == 8000
    assert result[0]['rooms'] == 1
    assert result[0]['size_m2'] == 32
    assert result[0]['card_class'] == 'apartment'
    assert seen == {'123'}


def test_lease_years_are_not_tenant_age_requirements():
    rec = {'eligibility_text': 'Lejeperiode Under 1 år. Møbleret lejlighed.'}
    assert filters.parse_age_requirement(rec, profile_age=23)[:4] == ('none', None, None, 'eligible')
    rec.update(age_requirement_state='bounded', age_max=1,
               eligibility_fact_excerpt='Husleje 9000 kr. Lejeperiode Under 1 år. Boligtype lejlighed.')
    assert filters.parse_age_requirement(rec, profile_age=23)[:4] == ('none', None, None, 'eligible')
    rec['eligibility_text'] += ' Alder: mellem 18 og 35 år.'
    assert filters.parse_age_requirement(rec, profile_age=36)[:4] == ('bounded', 18, 35, 'ineligible')


def test_superseded_building_rejection_does_not_block_detail_revalidation(alert_env):
    store, _, stations = alert_env
    run.process_candidate(_candidate(listing_id='PROFILE-REVIEW'), stations, store, None, dry_run=True)
    source = store.get_source_listing('test', 'PROFILE-REVIEW')
    store.conn.execute('DELETE FROM matches')
    store.conn.execute("UPDATE evaluations SET outcome='quarantined' WHERE is_current=1")
    store.record_building_disqualification('Jorisvej 1a, 2300 København', 'old student finding')
    store.conn.execute("UPDATE building_disqualifications SET state='superseded'")
    store.conn.commit()
    assert [r['listing_id'] for r in run.select_for_decision_revalidation(store)] == ['PROFILE-REVIEW']


def test_writer_lock_covers_early_profile_commands(tmp_path):
    import fcntl
    import subprocess
    import sys
    target = tmp_path / 'untouched.db'
    with open(str(target) + '.writer.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run([sys.executable, 'run.py', '--refresh-local', '--db', str(target)],
                                text=True, capture_output=True)
    assert result.returncode == 75
    assert 'Another BoligWatch writer' in result.stdout
    assert not target.exists()


def test_summary_counts_distinct_complexes_with_all_unit_categories(alert_env):
    store, _, stations = alert_env
    for rec in (_candidate(listing_id='GROUP-A', address='Jorisvej 1a, 2300 København',
                           available_from='2027-02-01', rent_total=8300),
                _candidate(listing_id='GROUP-B', address='Jorisvej 5, 2300 København',
                           available_from='2026-12-01', rent_total=8400, lat=55.651)):
        run.process_candidate(rec, stations, store, None, dry_run=True)
    stats = viewer.dashboard_stats(store)
    assert stats['total_matches'] == 1
    # One complex: counted at most once in each rank it has a unit in (v3.10's
    # early-start cost can put the Dec and Feb units in different ranks).
    assert all(v <= 1 for v in stats['rank_counts'].values())
    assert sum(stats['rank_counts'].values()) >= 1
    assert stats['actionable_count'] == 1
    assert stats['intel_count'] == 1
    text = digest.generate_daily_digest(store, segments={})
    assert text.count('Jorisvej complex') == 1
    assert '2 eligible adverts; 1 actionable' in text


def test_digest_price_drops_are_consecutive_and_evidence_comparable(temp_db):
    source = temp_db.record_source_listing(None, 'test', 'PRICE-HISTORY', 'https://example.invalid/unit')
    for price, state in ((10000, 'fetched'), (9000, 'fetched'), (9000, 'fetched'),
                         (8500, 'skipped:fresh'), (9000, 'fetched'), (8500, 'fetched')):
        temp_db.record_observation(source, None, None, {'rent_total': price}, None, detail_state=state)
    rows = digest._price_drops_since(temp_db, '2000-01-01')
    assert [(r['old_rent'],r['new_rent']) for r in rows] == [(10000,9000),(9000,8500)]
