from app.modules.m7_archive.export_out import batch_checksum, checksum


def execute(state):
    payroll = state.get('payroll') or {}
    current = payroll.get('current')
    replay = None
    if current:
        from app.payroll.batch import replay as replay_batch
        replay = replay_batch(current)
    state['archive'] = {
        'sha256': checksum(state),
        'evidence_count': len(state.get('calculations', [])) + len(state['quarantine']),
        'ready': bool(state.get('reconcile', {}).get('balanced')) and bool(
            (current or {}).get('reconcile', {}).get('balanced')),
        'batch': current['batch_id'] if current else None,
        'batch_sha256': batch_checksum(current) if current else None,
        'sealed': (current or {}).get('status') == '已封存',
        'replay': replay,
    }
    return state
