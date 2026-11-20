"""Replayable payroll batches.

A batch settles exactly one period under one rule registry and keeps, together:
the adjudicated rule binding of every shift (the switch basis), the rule version
attached to every clock segment, and one payable line per priced part.  A
payable references its segment, the segment references its basis, and the basis
carries the original punch lines verbatim, so the batch can be rebuilt from its
own evidence without re-reading the source file.

Identity
    ``batch_key`` = sha256(period, accepted evidence digest, rule digest).
    Same key  -> the same batch, stored once; a rerun cannot pay twice.
    New key   -> a new generation; the previous generation of that period is
                 marked 作废 and kept, never deleted.

Totals
    A batch may only be sealed when its own cross-footing balances: every
    segment priced exactly once, minutes conserved, one rule version per
    segment, and the sum of payables equal to the declared batch total.
"""

import hashlib
import json
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal

from app.errors import DomainError
from app.modules.m4_segments.rules import split_shift
from app.payroll.policy import binding, registry_digest, rules_for
from app.payroll.pricing import money, price_segments

ROW_FIELDS = ('id', 'employee', 'shift_day', 'version', 'punch', 'at', 'shift_start',
              'shift_end', 'break_start', 'break_end', 'hourly_rate', 'paid', 'note')


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def row_evidence(row):
    if row is None:
        return None
    item = {field: row.get(field) for field in ROW_FIELDS if field in row}
    item['line'] = row.get('_line')
    item['raw'] = row.get('_raw') or {field: row.get(field) for field in ROW_FIELDS}
    return item


def original_shift(shift):
    """Original punch rows of a shift, kept verbatim for replay."""
    return {
        'shift_id': shift['id'],
        'employee': shift['employee'],
        'shift_day': shift['day'],
        'in': row_evidence(shift.get('in')),
        'out': row_evidence(shift.get('out')),
        'sources': [row_evidence(row) for row in shift.get('sources') or []],
    }


def evidence_digest(shifts):
    """Digest of only the original rows that this batch settles.

    A correction in an unrelated period therefore does not invalidate this
    batch; a correction or rule change that touches it does.
    """
    rows = []
    for record in shifts.values():
        for row in record['sources']:
            rows.append({key: row.get(key) for key in
                         ('line', 'id', 'employee', 'shift_day', 'version', 'punch', 'at',
                          'shift_start', 'shift_end', 'break_start', 'break_end',
                          'hourly_rate', 'paid')})
    rows.sort(key=lambda item: (str(item['line']), str(item['id'])))
    return _digest(rows)


def batch_key(period, evidence, rule_digest):
    return hashlib.sha256(f'{period}|{evidence}|{rule_digest}'.encode()).hexdigest()[:24]


def segment_ref(period, shift_id, segment):
    return f"{period}|{shift_id}|{segment['start']}|{segment['end']}"


def _bind(state, rules):
    """Bind every complete shift to one rule version; do not price yet.

    A shift whose rows were already quarantined upstream is not reported
    again; it is already visible in the run's quarantine list.
    """
    settled = defaultdict(list)
    isolated = []
    accepted = {row.get('_line') for row in state.get('rows') or []}
    for shift in sorted(state.get('shifts') or [], key=lambda item: (item['employee'], item['id'])):
        lines = {row.get('_line') for row in shift.get('sources') or []}
        if lines and not lines <= accepted:
            continue
        try:
            basis = binding(shift, rules)
            segments = split_shift(shift)
        except DomainError as error:
            isolated.append({'basis_id': 'B-' + shift['id'], 'shift_id': shift['id'],
                             'employee': shift['employee'], 'attribution_day': shift['day'],
                             'code': error.code, 'message': error.message,
                             'original': original_shift(shift)})
            continue
        settled[basis['settle_period']].append({'basis': basis, 'segments': segments,
                                                'shift': shift})
    return settled, isolated


def _price(entries, rules):
    """Price segments under each shift's bound rule.

    Daily thresholds are shared per (employee, calendar day, rule version), so
    two versions never consume one another's regular hours.
    """
    used = defaultdict(int)
    rows_by_shift = defaultdict(list)
    by_version = defaultdict(list)
    for entry in entries:
        by_version[entry['basis']['bound_rule']].extend(entry['segments'])
    for version in sorted(by_version):
        rule = next(item for item in rules if item['version'] == version)
        for row in price_segments(by_version[version], rule, used):
            rows_by_shift[row['id']].append(row)
    return rows_by_shift


def _assemble(period, rules, switch_info, entries, isolated, deferred_out=None):
    rule_digest = registry_digest(rules)
    evidence = {entry['shift']['id']: original_shift(entry['shift']) for entry in entries}
    in_digest = evidence_digest(evidence)
    key = batch_key(period, in_digest, rule_digest)
    priced = _price(entries, rules)

    bindings, segments, payables = [], [], []
    for entry in sorted(entries, key=lambda item: (item['basis']['employee'], item['basis']['shift_id'])):
        basis = dict(entry['basis'])
        basis['rule_version'] = basis['bound_rule']
        basis['segment_refs'] = [segment_ref(period, basis['shift_id'], s) for s in entry['segments']]
        basis['payable_ids'] = []
        bindings.append(basis)
        for segment in entry['segments']:
            item = dict(segment)
            item.update(basis_id=basis['basis_id'], shift_id=basis['shift_id'],
                        employee=basis['employee'], rule_version=basis['bound_rule'],
                        period=period, segment_ref=segment_ref(period, basis['shift_id'], segment))
            segments.append(item)
        for index, row in enumerate(sorted(priced.get(entry['shift']['id'], []),
                                          key=lambda item: (item['start'], item['code']))):
            item = dict(row)
            item.update(payable_id=f"P-{basis['shift_id']}-{index + 1:02d}",
                        basis_id=basis['basis_id'], shift_id=basis['shift_id'],
                        period=period, segment_ref=segment_ref(period, basis['shift_id'], row))
            payables.append(item)
            basis['payable_ids'].append(item['payable_id'])

    bindings.sort(key=lambda item: (item['employee'], item['shift_id']))
    segments.sort(key=lambda item: (item['start'], item['employee'], item['segment_ref']))
    payables.sort(key=lambda item: (item['start'], item['employee'], item['payable_id']))
    total = money(sum((Decimal(str(item['amount'])) for item in payables), Decimal('0')))
    reconcile = reconcile_batch(period, bindings, segments, payables, total)
    batch = {
        'batch_id': 'PB-' + period.replace('-', '') + '-' + key[:12],
        'batch_key': key,
        'period': period,
        'rule_registry_digest': rule_digest,
        'evidence_digest': in_digest,
        'rule_versions': sorted({rule['version'] for rule in rules}),
        'switch': deepcopy(switch_info),
        'bindings': bindings,
        'segments': segments,
        'payables': payables,
        'carry_in': [item['basis_id'] for item in bindings if item['in_flight']],
        'deferred_out': deferred_out or [],
        'isolated': isolated,
        'total': str(total),
        'reconcile': reconcile,
        'evidence': {'shifts': evidence, 'rule_registry': deepcopy(rules)},
        'status': '待封存',
    }
    batch['status'] = batch_status(batch)
    return batch


def batch_status(batch):
    """Batch status depends on its own cross-footing, not on run metadata.

    A rejected rule switch leaves the existing registry (and therefore the
    batch identity) untouched; it is shown as run evidence and must not
    silently relabel an already valid batch.
    """
    if not batch['reconcile']['balanced']:
        return '应付未对平·不可封存'
    if batch['reconcile'].get('empty'):
        return '本期无应付'
    return '待封存'


def reconcile_batch(period, bindings, segments, payables, declared_total):
    """Independent cross-footing: payables <-> segments <-> batch total."""
    problems = []
    by_segment = defaultdict(list)
    for item in payables:
        by_segment[item['segment_ref']].append(item)
    for segment in segments:
        parts = by_segment.get(segment['segment_ref'], [])
        if not parts:
            problems.append('未计薪片段 ' + segment['segment_ref'])
            continue
        if sum(int(part['minutes']) for part in parts) != int(segment['minutes']):
            problems.append('片段分钟未守恒 ' + segment['segment_ref'])
        if {part['rule_version'] for part in parts} != {segment['rule_version']}:
            problems.append('片段引用多个规则版本 ' + segment['segment_ref'])
        if any(part['basis_id'] != segment['basis_id'] for part in parts):
            problems.append('应付与切换依据不一致 ' + segment['segment_ref'])
    orphan = set(by_segment) - {segment['segment_ref'] for segment in segments}
    if orphan:
        problems.append('应付指向未知片段：' + ','.join(sorted(orphan)))
    bound = {item['basis_id'] for item in bindings}
    if any(item['basis_id'] not in bound for item in payables):
        problems.append('应付缺少切换依据')
    recomputed = sum((money(Decimal(str(item['rate'])) * int(item['minutes']) *
                            Decimal(str(item['multiplier'])) / 60) for item in payables),
                     Decimal('0'))
    total = money(Decimal(str(declared_total)))
    difference = money(recomputed - total)
    if difference != 0:
        problems.append('应付合计与批次总额不符')
    empty = not bindings and not segments and not payables
    return {
        'period': period,
        'batch_total': str(total),
        'recomputed_total': str(recomputed),
        'difference': str(difference),
        'basis_count': len(bindings),
        'segment_count': len(segments),
        'payable_count': len(payables),
        'empty': empty,
        'balanced': not problems,
        'problems': problems,
    }


def build(state):
    """Build every period batch the run touches and feature the current period."""
    parameters = state.get('parameters') or {}
    rules, switch_info = rules_for(parameters)
    settled, isolated = _bind(deepcopy(state), rules)
    requested = parameters.get('period')
    if requested:
        try:
            from datetime import date
            date.fromisoformat(str(requested) + '-01')
        except ValueError:
            raise DomainError('PERIOD', '计薪期间必须是 YYYY-MM')
        period = str(requested)
    else:
        period = max(settled) if settled else max(rule['effective_from'][:7] for rule in rules)
    periods = {period}
    for settle, entries in settled.items():
        periods.add(settle)
        for entry in entries:
            if entry['basis']['in_flight']:
                periods.add(entry['basis']['work_start'][:7])
    periods = sorted(periods)
    # A shift crossing a cut-over settles in the later period; the earlier
    # period must show it as deferred rather than silently dropping it.
    deferred = defaultdict(list)
    for settle, entries in settled.items():
        for entry in entries:
            start_period = entry['basis']['work_start'][:7]
            if start_period != settle:
                deferred[start_period].append({'basis_id': entry['basis']['basis_id'],
                                               'shift_id': entry['basis']['shift_id'],
                                               'employee': entry['basis']['employee'],
                                               'cutover': entry['basis']['cutover'],
                                               'settle_period': settle,
                                               'bound_rule': entry['basis']['bound_rule'],
                                               'reason': entry['basis']['reason']})
    batches = []
    for item in periods:
        scoped_isolated = deepcopy(isolated) if item == period else []
        batches.append(_assemble(item, rules, switch_info, settled.get(item, []), scoped_isolated,
                                 deferred.get(item, [])))
    current = next(batch for batch in batches if batch['period'] == period)
    return {
        'current': current,
        'batches': batches,
        'period': period,
        'switch': switch_info,
        'rule_versions': [rule['version'] for rule in rules],
        'rule_registry_digest': registry_digest(rules),
    }


def _row_from_evidence(record):
    if record is None:
        return None
    row = dict(record.get('raw') or {})
    for key in ROW_FIELDS:
        if key in record:
            row[key] = record[key]
    row['_line'] = record.get('line')
    row['_raw'] = record.get('raw') or {}
    return row


def replay(batch):
    """Rebuild a batch from its own evidence and compare every payable."""
    rules = batch['evidence']['rule_registry']
    entries = []
    for record in (batch['evidence']['shifts'] or {}).values():
        shift = {
            'id': record['shift_id'], 'employee': record['employee'], 'day': record['shift_day'],
            'in': _row_from_evidence(record.get('in')),
            'out': _row_from_evidence(record.get('out')),
            'sources': [_row_from_evidence(row) for row in record.get('sources') or []],
        }
        basis = binding(shift, rules)
        entries.append({'basis': basis, 'segments': split_shift(shift), 'shift': shift})
    priced = _price(entries, rules)
    rebuilt = []
    for entry in entries:
        for index, row in enumerate(sorted(priced.get(entry['shift']['id'], []),
                                          key=lambda item: (item['start'], item['code']))):
            item = dict(row)
            item.update(payable_id=f"P-{entry['shift']['id']}-{index + 1:02d}",
                        basis_id=entry['basis']['basis_id'])
            rebuilt.append(item)
    original = {item['payable_id']: item for item in batch['payables']}
    replay_map = {item['payable_id']: item for item in rebuilt}
    differences = []
    for key in sorted(set(original) | set(replay_map)):
        left = original.get(key)
        right = replay_map.get(key)
        if left is None or right is None:
            differences.append({'payable_id': key, 'problem': '只在单侧存在'})
            continue
        for field in ('minutes', 'amount', 'code', 'multiplier', 'rule_version', 'basis_id'):
            if str(left.get(field)) != str(right.get(field)):
                differences.append({'payable_id': key, 'field': field,
                                    'stored': str(left.get(field)), 'replayed': str(right.get(field))})
    stored_total = money(Decimal(str(batch['total'])))
    replay_total = money(sum((Decimal(str(item['amount'])) for item in rebuilt), Decimal('0')))
    if stored_total != replay_total:
        differences.append({'field': 'total', 'stored': str(stored_total),
                            'replayed': str(replay_total)})
    return {
        'batch_id': batch['batch_id'],
        'period': batch['period'],
        'stored_total': str(stored_total),
        'replay_total': str(replay_total),
        'payable_count': len(rebuilt),
        'matches': not differences,
        'differences': differences,
    }
