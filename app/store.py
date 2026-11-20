import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.errors import DomainError

SCHEMA = (
    'CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, '
    'version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")',
    'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS batches (key TEXT PRIMARY KEY,batch_id TEXT NOT NULL,'
    'period TEXT NOT NULL,generation INTEGER NOT NULL,status TEXT NOT NULL,'
    'sealed_at TEXT,superseded_by TEXT,body TEXT NOT NULL,created_at TEXT NOT NULL,'
    'updated_at TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS batch_events (id INTEGER PRIMARY KEY AUTOINCREMENT,'
    'batch_key TEXT NOT NULL,event TEXT NOT NULL,detail TEXT NOT NULL,at TEXT NOT NULL)',
)


def connection():
    path = Path(os.environ.get('DB_PATH', 'data/records.db'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    for statement in SCHEMA:
        db.execute(statement)
    return db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _event(db, key, event, detail):
    db.execute('INSERT INTO batch_events(batch_key,event,detail,at) VALUES (?,?,?,?)',
               (key, event, json.dumps(detail, ensure_ascii=False, sort_keys=True), _now()))


NON_CORE = ('replay', 'sealable', 'generation', 'stored_status', 'sealed_at', 'search')


def _clean(batch):
    """Store core batch fields only; presentation extras are recomputed."""
    return {key: value for key, value in batch.items() if key not in NON_CORE}


def _register(db, batch):
    """Register one built batch as the active generation of its period.

    Same key   -> same batch: refreshed in place, no second payable set.
    New key    -> newer generation: every other generation of the period is
                  marked 已作废 with a pointer to the new one; none is deleted.
    """
    batch = _clean(batch)
    key = batch['batch_key']
    now = _now()
    reconcile = batch.get('reconcile') or {}
    # An unbalanced or empty batch is stored so it can be inspected and is
    # refused at sealing, but it never displaces a valid earlier generation.
    displaces = bool(reconcile.get('balanced')) and not reconcile.get('empty')
    row = db.execute('SELECT * FROM batches WHERE key=?', (key,)).fetchone()
    if row is None:
        generation = (db.execute('SELECT MAX(generation) FROM batches WHERE period=?',
                                 (batch['period'],)).fetchone()[0] or 0) + 1
        db.execute('INSERT INTO batches(key,batch_id,period,generation,status,body,created_at,updated_at)'
                   ' VALUES (?,?,?,?,?,?,?,?)',
                   (key, batch['batch_id'], batch['period'], generation, batch['status'],
                    json.dumps(batch, ensure_ascii=False), now, now))
        _event(db, key, '登记', {'generation': generation, 'total': batch['total'],
                                 'status': batch['status']})
    else:
        if row['superseded_by'] or row['status'] == '已作废':
            # Re-submitting an input that has already been superseded must not
            # resurrect it: the current generation stays authoritative.
            _event(db, key, '重跑已作废', {'superseded_by': row['superseded_by'],
                                          'total': batch['total']})
            return key
        sealed = row['sealed_at']
        if sealed:
            # A sealed batch's evidence is immutable; a rerun of the same input
            # changes nothing and must not rewrite the archived body.
            _event(db, key, '重跑命中已封存', {'total': batch['total']})
            return key
        status = batch['status']
        db.execute('UPDATE batches SET body=?,status=?,superseded_by=NULL,updated_at=? WHERE key=?',
                   (json.dumps(batch, ensure_ascii=False), status, now, key))
        _event(db, key, '重跑复用', {'total': batch['total'], 'status': status})
    if not displaces:
        return key
    for other in db.execute('SELECT key,status,sealed_at,generation FROM batches WHERE period=? AND key<>? '
                            'AND status<>?', (batch['period'], key, '已作废')).fetchall():
        db.execute('UPDATE batches SET status=?,superseded_by=?,updated_at=? WHERE key=?',
                   ('已作废', key, now, other['key']))
        _event(db, other['key'], '作废', {'superseded_by': key, 'was_status': other['status'],
                                          'sealed': bool(other['sealed_at']),
                                          'generation': other['generation'],
                                          'reason': '补卡或规则更新后重算'})
    db.execute('UPDATE batches SET superseded_by=NULL WHERE key=?', (key,))
    return key


def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',
                   (run_id, json.dumps(body, ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) "
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (run_id,))
        payroll = body.get('payroll') or {}
        current = (payroll.get('current') or {}).get('batch_key')
        ordered = sorted(payroll.get('batches') or [],
                         key=lambda batch: batch.get('batch_key') != current)
        for batch in ordered:
            # The featured batch is authoritative if a copy has drifted; it is
            # registered last so the run's own period always features it.
            _register(db, payroll['current'] if batch.get('batch_key') == current else batch)
    return get(run_id)


def get(run_id):
    with connection() as db:
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    if not row:
        raise DomainError('NOT_FOUND', '批次不存在', 404)
    body = json.loads(row['body'])
    body.update(run_id=row['id'], review_version=row['version'], decision=row['decision'])
    return body


def history():
    with connection() as db:
        rows = db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id="
                          "(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,"
                          'rowid DESC LIMIT 100').fetchall()
    return [dict(row) for row in rows]


def review(run_id, version, decision):
    if decision not in ('通过', '退回'):
        raise DomainError('DECISION', '复核状态无效')
    with connection() as db:
        cur = db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',
                         (decision, run_id, version))
        if cur.rowcount != 1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                raise DomainError('NOT_FOUND', '批次不存在', 404)
            raise DomainError('STALE', '复核版本已更新，请刷新', 409)
    return get(run_id)


# ---------------------------------------------------------------- payroll batches

def _batch_row(row):
    item = dict(row)
    item['body'] = json.loads(item['body'])
    return item


def batch(key):
    with connection() as db:
        row = db.execute('SELECT * FROM batches WHERE key=? OR batch_id=?', (key, key)).fetchone()
    if not row:
        raise DomainError('NOT_FOUND', '计薪批次不存在', 404)
    return _batch_row(row)


def batches(period=None):
    with connection() as db:
        if period:
            rows = db.execute('SELECT * FROM batches WHERE period=? ORDER BY generation DESC',
                              (period,)).fetchall()
        else:
            rows = db.execute('SELECT * FROM batches ORDER BY period DESC,generation DESC').fetchall()
    return [_batch_row(row) for row in rows]


def events(key):
    with connection() as db:
        rows = db.execute('SELECT event,detail,at FROM batch_events WHERE batch_key=? ORDER BY id',
                          (key,)).fetchall()
    return [{'event': row['event'], 'detail': json.loads(row['detail']), 'at': row['at']} for row in rows]


def period_totals(period):
    """Only the active generation of a period counts towards payables."""
    active = [item for item in batches(period) if item['status'] != '已作废']
    return {
        'period': period,
        'active': [{'batch_id': item['batch_id'], 'total': item['body']['total'],
                    'status': item['status']} for item in active],
        'total': str(sum((Decimal(item['body']['total']) for item in active), Decimal('0'))),
        'invalidated': [{'batch_id': item['batch_id'], 'total': item['body']['total'],
                         'superseded_by': item['superseded_by']}
                        for item in batches(period) if item['status'] == '已作废'],
    }


def seal(key):
    with connection() as db:
        row = db.execute('SELECT * FROM batches WHERE key=? OR batch_id=?', (key, key)).fetchone()
        if not row:
            raise DomainError('NOT_FOUND', '计薪批次不存在', 404)
        if row['superseded_by'] or row['status'] == '已作废':
            raise DomainError('STALE', '批次已作废，不能封存当前结论', 409)
        body = json.loads(row['body'])
        reconcile = body.get('reconcile') or {}
        if not reconcile.get('balanced'):
            raise DomainError('UNBALANCED', '应付未与批次总额对平，不能封存')
        if reconcile.get('empty'):
            raise DomainError('EMPTY', '本期没有应付，无可封存内容')
        if row['sealed_at']:
            return _batch_row(db.execute('SELECT * FROM batches WHERE key=?', (row['key'],)).fetchone())
        now = _now()
        db.execute('UPDATE batches SET sealed_at=?,status=?,updated_at=? WHERE key=?',
                   (now, '已封存', now, row['key']))
        _event(db, row['key'], '封存', {'total': body['total'],
                                        'reconcile_total': reconcile.get('recomputed_total'),
                                        'balance_difference': reconcile.get('difference')})
        return _batch_row(db.execute('SELECT * FROM batches WHERE key=?', (row['key'],)).fetchone())
