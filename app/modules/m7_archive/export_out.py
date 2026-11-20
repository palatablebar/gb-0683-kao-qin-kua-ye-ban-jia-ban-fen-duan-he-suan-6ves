import csv
import hashlib
import io
import json

from app.payroll.batch import replay


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def batch_sections(batch):
    """One row per payable, segment, switch basis and original shift."""
    yield ['批次', batch['batch_id'], '', '', batch['status'],
           _dump({key: batch[key] for key in ('batch_key', 'period', 'total', 'rule_versions',
                                              'rule_registry_digest', 'evidence_digest',
                                              'carry_in', 'reconcile', 'switch')})]
    for basis in batch['bindings']:
        yield ['切换依据', batch['batch_id'], basis['attribution_day'], basis['basis_id'],
               basis['bound_rule'],
               _dump({key: basis[key] for key in ('shift_id', 'employee', 'work_start', 'work_end',
                                                  'cutover', 'in_flight', 'rule_effective_from',
                                                  'rule_on_attribution_day', 'settle_period', 'decision',
                                                  'reason', 'segment_refs', 'payable_ids')})]
    for shift_id, record in sorted(batch['evidence']['shifts'].items()):
        yield ['原始班次', batch['batch_id'], str((record.get('in') or {}).get('line', '')),
               shift_id, '', _dump(record)]
    for segment in batch['segments']:
        yield ['工时片段', batch['batch_id'], str(segment.get('line', '')), segment['segment_ref'],
               segment['rule_version'],
               _dump({key: segment[key] for key in ('shift_id', 'basis_id', 'employee',
                                                    'calendar_day', 'start', 'end', 'minutes',
                                                    'rate', 'period')})]
    for payable in batch['payables']:
        yield ['应付', batch['batch_id'], str(payable.get('line', '')), payable['payable_id'],
               payable['code'],
               _dump({key: payable[key] for key in ('basis_id', 'segment_ref', 'employee',
                                                    'calendar_day', 'start', 'end', 'minutes',
                                                    'rate', 'multiplier', 'amount', 'rule_version')})]
    for item in batch.get('isolated', []):
        yield ['在途未结算', batch['batch_id'],
               str((item['original'].get('in') or {}).get('line', '')),
               item['basis_id'], item['code'], _dump(item)]


def bundle(state):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['记录类型', '批次', '来源行', '业务ID', '原因码', '内容'])
    rid = state.get('run_id', 'preview')
    writer.writerow(['运行清单', rid, '', '', '', _dump(state.get('manifest', {}))])
    writer.writerow(['对账', rid, '', '', '', _dump(state.get('reconcile', {}))])
    payroll = state.get('payroll') or {}
    current = payroll.get('current')
    if current:
        writer.writerow(['规则切换', current['batch_id'], '', '', payroll['switch'].get('status') or '',
                         _dump({'period': payroll['period'], 'switch': payroll['switch'],
                                'rule_versions': payroll['rule_versions'],
                                'rule_registry_digest': payroll['rule_registry_digest'],
                                'replay': replay(current)})])
        for row in batch_sections(current):
            writer.writerow(row)
    else:
        for row in state.get('calculations', []):
            writer.writerow(['计算', rid, row.get('line', ''), row.get('id', ''), row.get('code', ''),
                             _dump(row)])
    for row in state['quarantine']:
        writer.writerow(['隔离', rid, row['line'], row.get('id', ''), row['code'],
                         _dump(row.get('raw', {}))])
    return '﻿' + out.getvalue()


def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()


def batch_bundle(row):
    """Export one stored batch (store row or bare body) from its own evidence."""
    meta = {key: row.get(key) for key in ('generation', 'status', 'sealed_at',
                                          'superseded_by', 'created_at', 'updated_at')}
    batch = row['body'] if 'body' in row else row
    manifest = {'period': batch['period'], 'rule_versions': batch['rule_versions'],
                'rule_registry_digest': batch['rule_registry_digest'],
                'replay': replay(batch), **meta}
    return bundle({'run_id': batch['batch_id'], 'manifest': manifest,
                   'reconcile': batch['reconcile'],
                   'payroll': {'current': batch, 'switch': batch['switch'],
                               'period': batch['period'],
                               'rule_versions': batch['rule_versions'],
                               'rule_registry_digest': batch['rule_registry_digest']},
                   'quarantine': []})


def batch_checksum(batch):
    return hashlib.sha256(batch_bundle(batch).encode('utf-8')).hexdigest()
