"""Cut-over adjudication, replayability and batch invalidation."""

import csv
import io
from copy import deepcopy
from collections import defaultdict
from decimal import Decimal

import pytest

from app import store
from app.errors import DomainError
from app.modules.m7_archive.export_out import batch_bundle
from app.payroll.batch import build, replay
from app.payroll.pricing import price_segments
from app.workflow import load, run

CLEAN = load('clean.csv').decode('utf-8-sig')


def csv_source(out_at='2026-11-01T06:00:00+08:00', rate='20', day='2026-10-31', employee='E001',
               keep_others=True):
    """Move one employee's whole night shift onto ``day``, across the cut-over.

    Rows are quoted so the original embedded newline in ``note`` survives.
    """
    reader = csv.DictReader(io.StringIO(CLEAN))
    fields = reader.fieldnames
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for row in reader:
        mine = row['employee'] == employee and row['id'] in ('00001', '00002')
        if not keep_others and not mine:
            continue
        if mine:
            row = dict(row, shift_day=day)
            if row['punch'] == 'IN':
                row['at'] = day + 'T22:00:00+08:00'
                row['paid'] = '220'
            else:
                row['at'] = out_at
                row['paid'] = '0'
            row['hourly_rate'] = rate
        writer.writerow(row)
    return out.getvalue().encode('utf-8-sig')


@pytest.fixture
def crossing():
    return run(csv_source(), {})


@pytest.fixture
def only_crossing():
    return run(csv_source(keep_others=False), {})


def test_crossing_shift_binds_one_version(only_crossing):
    current = only_crossing['payroll']['current']
    assert current['period'] == '2026-11'
    basis = current['bindings'][0]
    assert basis['in_flight'] is True and basis['cutover'] == '2026-11-01T00:00:00+08:00'
    assert basis['rule_on_attribution_day'] == '2026.10-v1'
    assert basis['rule_version'] == '2026.11-v1'
    assert {segment['rule_version'] for segment in current['segments']} == {'2026.11-v1'}
    assert {payable['rule_version'] for payable in current['payables']} == {'2026.11-v1'}


def test_crossing_shift_is_not_paid_twice(only_crossing):
    payroll = only_crossing['payroll']
    previous = next(batch for batch in payroll['batches'] if batch['period'] == '2026-10')
    assert previous['payables'] == [] and previous['segments'] == []
    assert previous['reconcile']['balanced'] is True and previous['reconcile']['empty'] is True
    assert [item['shift_id'] for item in previous['deferred_out']] == ['E001/2026-10-31']
    assert previous['deferred_out'][0]['settle_period'] == '2026-11'
    total = sum(Decimal(batch['total']) for batch in payroll['batches'])
    assert total == Decimal('280.00')


def test_every_segment_maps_to_one_payable_set(only_crossing):
    current = only_crossing['payroll']['current']
    refs = [segment['segment_ref'] for segment in current['segments']]
    assert len(refs) == len(set(refs))
    for segment in current['segments']:
        parts = [payable for payable in current['payables']
                 if payable['segment_ref'] == segment['segment_ref']]
        assert sum(part['minutes'] for part in parts) == segment['minutes']
        assert {part['rule_version'] for part in parts} == {segment['rule_version']}
        assert {part['basis_id'] for part in parts} == {segment['basis_id']}
    assert all(payable['rule_version'] == '2026.11-v1' for payable in current['payables'])


def test_original_shift_and_basis_are_kept(only_crossing):
    current = only_crossing['payroll']['current']
    record = current['evidence']['shifts']['E001/2026-10-31']
    assert record['in']['raw']['at'] == '2026-10-31T22:00:00+08:00'
    assert record['in']['line'] >= 2
    assert len(record['sources']) == 2
    basis = current['bindings'][0]
    assert basis['reason'] and basis['decision'] and basis['attribution_day'] == '2026-10-31'
    assert all(payable['basis_id'] == basis['basis_id'] for payable in current['payables'])


def test_night_under_new_version_is_priced_by_new_rules(only_crossing):
    payroll = only_crossing['payroll']
    nov = next(batch for batch in payroll['batches'] if batch['period'] == '2026-11')
    # 2026-10-31 is a Saturday: 420 min at 2x, 20 CNY/h.
    assert nov['total'] == '280.00'
    assert {payable['code'] for payable in nov['payables']} == {'REST'}


def test_batch_is_replayable(only_crossing):
    current = only_crossing['payroll']['current']
    result = replay(current)
    assert result['matches'] is True and result['differences'] == []
    assert result['replay_total'] == current['total']


def test_replay_detects_tampering(only_crossing):
    current = deepcopy(only_crossing['payroll']['current'])
    current['payables'][0]['amount'] = '999.00'
    result = replay(current)
    assert result['matches'] is False
    assert any(item.get('field') == 'amount' for item in result['differences'])


def test_export_carries_each_layer(only_crossing):
    current = only_crossing['payroll']['current']
    text = batch_bundle(current)
    kinds = {row[0] for row in csv.reader(io.StringIO(text.lstrip('﻿')))}
    assert {'批次', '切换依据', '原始班次', '工时片段', '应付'} <= kinds
    assert '2026.11-v1' in text and '2026.10-v1' in text
    rows = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    payables = [row for row in rows if row[0] == '应付']
    assert sum(Decimal(row[5] and __import__('json').loads(row[5])['amount']) for row in payables) \
        == Decimal(current['total'])


def test_unbalanced_batch_cannot_be_sealed(crossing, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    broken = deepcopy(crossing)
    broken['payroll']['current']['total'] = '1.00'
    broken['payroll']['current']['reconcile']['balanced'] = False
    broken['payroll']['current']['reconcile']['problems'] = ['应付合计与批次总额不符']
    store.save(broken['run_id'], broken)
    with pytest.raises(DomainError) as error:
        store.seal(broken['payroll']['current']['batch_key'])
    assert error.value.code == 'UNBALANCED'


def test_rerun_same_source_stores_one_batch(crossing, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    state = run(csv_source(), {})
    store.save(crossing['run_id'], crossing)
    store.save(state['run_id'], state)
    stored = store.batches('2026-11')
    assert len(stored) == 1 and stored[0]['generation'] == 1
    assert store.period_totals('2026-11')['total'] == stored[0]['body']['total']


def test_correction_invalidates_old_batch_and_keeps_it(crossing, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(crossing['run_id'], crossing)
    first = store.batches('2026-11')[0]

    corrected = run(csv_source(rate='30'), {})
    store.save(corrected['run_id'], corrected)

    stored = store.batches('2026-11')
    assert len(stored) == 2
    new = next(item for item in stored if item['key'] != first['key'])
    old = store.batch(first['key'])
    assert old['status'] == '已作废' and old['superseded_by'] == new['key']
    assert old['body']['payables'] and old['body']['bindings']
    assert old['body']['evidence']['shifts']
    assert new['status'] == '待封存' and new['generation'] == 2
    with pytest.raises(DomainError) as error:
        store.seal(first['key'])
    assert error.value.code == 'STALE'


def test_corrected_batch_is_replayable_after_invalidation(crossing, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(crossing['run_id'], crossing)
    corrected = run(csv_source(rate='30'), {})
    store.save(corrected['run_id'], corrected)
    stored = store.batches('2026-11')
    assert [item['status'] for item in stored] == ['待封存', '已作废']
    old = stored[1]
    assert replay(old['body'])['matches'] is True and old['body']['total'] == '280.00'
    assert replay(stored[0]['body'])['matches'] is True and stored[0]['body']['total'] == '420.00'


def test_correction_repays_once(crossing, tmp_path, monkeypatch):
    """The invalidated generation no longer counts; the new one counts once."""
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(crossing['run_id'], crossing)
    store.seal(crossing['payroll']['current']['batch_key'])
    corrected = run(csv_source(rate='30'), {})
    store.save(corrected['run_id'], corrected)
    totals = store.period_totals('2026-11')
    assert len(totals['active']) == 1
    assert totals['total'] == corrected['payroll']['current']['total']
    assert len(totals['invalidated']) == 1


def test_failed_switch_keeps_old_version(crossing):
    bad = {'rule_switch': {'version': 'x', 'effective_from': '2026-11-01T00:00:00'}}
    payroll = build(dict(crossing, parameters=bad))
    assert payroll['switch']['status'] == '失败'
    assert payroll['rule_versions'] == ['2026.10-v1', '2026.11-v1']
    assert payroll['rule_registry_digest'] == build(crossing)['rule_registry_digest']
    assert all(batch['status'] == '待封存' for batch in payroll['batches'])
    assert payroll['current']['reconcile']['balanced'] is True
    assert payroll['current']['total'] == crossing['payroll']['current']['total']


def test_successful_switch_changes_batch_identity(only_crossing):
    before = only_crossing['payroll']['current']
    new_rule = {'version': '2026.11-v2', 'effective_from': '2026-11-01T00:00:00+08:00',
                'replace': True, 'daily_minutes': 480,
                'multipliers': {'REGULAR': '1', 'OVERTIME': '1.5', 'REST': '2', 'HOLIDAY': '3'},
                'holidays': []}
    after = build(dict(only_crossing, parameters={'rule_switch': new_rule}))
    assert after['switch']['status'] == '成功'
    assert '2026.11-v2' in after['rule_versions']
    assert after['current']['batch_key'] != before['batch_key']
    assert after['current']['bindings'][0]['rule_version'] == '2026.11-v2'


def test_rejected_switch_leaves_old_batch_untouched(crossing, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(crossing['run_id'], crossing)
    first = store.batches('2026-11')[0]
    bad = {'rule_switch': {'version': 'bad', 'effective_from': '2026-10-15T00:00:00+08:00'},
           'period': '2026-11'}
    state = run(csv_source(), bad)
    store.save(state['run_id'], state)
    stored = store.batches('2026-11')
    assert len(stored) == 1 and stored[0]['key'] == first['key']
    assert stored[0]['status'] == '待封存'
    assert state['payroll']['switch']['status'] == '失败'


def test_rule_update_invalidates_previous_batch(crossing, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(crossing['run_id'], crossing)
    first = store.batches('2026-11')[0]
    new_rule = {'version': '2026.11-v2', 'effective_from': '2026-11-01T00:00:00+08:00',
                'replace': True, 'daily_minutes': 480,
                'multipliers': {'REGULAR': '1', 'OVERTIME': '1.5', 'REST': '2', 'HOLIDAY': '3'},
                'holidays': []}
    updated = run(csv_source(), {'rule_switch': new_rule})
    store.save(updated['run_id'], updated)
    old = store.batch(first['key'])
    assert old['status'] == '已作废'
    assert old['body']['total'] and old['body']['payables']
    assert any(event['event'] == '作废' for event in store.events(first['key']))


def test_unknown_period_has_no_payable(crossing):
    payroll = build(dict(crossing, parameters={'period': '2026-12'}))
    empty = payroll['current']
    assert empty['payables'] == [] and empty['reconcile']['empty'] is True
    assert empty['status'] == '本期无应付'


def test_resubmitting_old_source_does_not_resurrect_it(crossing, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(crossing['run_id'], crossing)
    corrected = run(csv_source(rate='30'), {})
    store.save(corrected['run_id'], corrected)
    store.save(crossing['run_id'], crossing)          # stale input resubmitted
    stored = store.batches('2026-11')
    assert [item['status'] for item in stored] == ['待封存', '已作废']
    assert store.period_totals('2026-11')['total'] == '420.00'


def test_corrected_input_carries_every_segment(crossing):
    corrected = run(csv_source(rate='30'), {})
    payroll = corrected['payroll']
    segments = [segment for batch in payroll['batches'] for segment in batch['segments']]
    payables = [payable for batch in payroll['batches'] for payable in batch['payables']]
    assert len(segments) == len({segment['segment_ref'] for segment in segments})
    assert sum(payable['minutes'] for payable in payables) == sum(s['minutes'] for s in segments)
    assert corrected['business']['minutes'] == sum(s['minutes'] for s in segments)


def test_switch_failure_is_evidence_not_a_state_change(crossing):
    failed = run(csv_source(), {'rule_switch': {'version': 'bad'}})
    plain = run(csv_source(), {})
    assert failed['payroll']['switch']['status'] == '失败'
    assert failed['payroll']['current']['batch_key'] == plain['payroll']['current']['batch_key']
    assert failed['payroll']['current']['status'] == '待封存'


def test_daily_threshold_is_shared_within_a_version_only():
    segments = [
        {'id': 'A', 'employee': 'E', 'shift_day': '2026-10-05', 'calendar_day': '2026-10-05',
         'start': '2026-10-05T09:00:00+08:00', 'end': '2026-10-05T14:00:00+08:00',
         'minutes': 300, 'rate': '20', 'line': 2},
        {'id': 'B', 'employee': 'E', 'shift_day': '2026-10-05', 'calendar_day': '2026-10-05',
         'start': '2026-10-05T15:00:00+08:00', 'end': '2026-10-05T20:00:00+08:00',
         'minutes': 300, 'rate': '20', 'line': 3},
    ]
    old = {'version': 'old', 'daily_minutes': 480, 'holidays': [],
           'multipliers': {'REGULAR': '1', 'OVERTIME': '1.5', 'REST': '2', 'HOLIDAY': '3'}}
    used = defaultdict(int)
    combined = price_segments(segments, old, used)
    assert [(x['code'], x['minutes']) for x in combined] ==         [('REGULAR', 300), ('REGULAR', 180), ('OVERTIME', 120)]
    # A different version gets its own (employee, day, version) allowance, so
    # it must not inherit the first version's consumed regular minutes.
    fresh = defaultdict(int)
    other = price_segments(segments, dict(old, version='other'), fresh)
    assert [(x['code'], x['minutes']) for x in other] ==         [('REGULAR', 300), ('REGULAR', 180), ('OVERTIME', 120)]


def test_sealed_batch_evidence_is_immutable(crossing, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(crossing['run_id'], crossing)
    key = crossing['payroll']['current']['batch_key']
    sealed = store.seal(key)
    again = run(csv_source(), {})
    store.save(again['run_id'], again)
    after = store.batch(key)
    assert after['sealed_at'] == sealed['sealed_at']
    assert after['body'] == sealed['body']
    assert after['status'] == '已封存'


def test_sealed_batch_can_be_superseded_but_still_replayable(crossing, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    store.save(crossing['run_id'], crossing)
    key = crossing['payroll']['current']['batch_key']
    store.seal(key)
    corrected = run(csv_source(rate='30'), {})
    store.save(corrected['run_id'], corrected)
    old = store.batch(key)
    assert old['status'] == '已作废' and old['sealed_at']
    assert replay(old['body'])['matches'] is True
    assert any(event['event'] == '作废' and event['detail']['sealed']
               for event in store.events(key))

