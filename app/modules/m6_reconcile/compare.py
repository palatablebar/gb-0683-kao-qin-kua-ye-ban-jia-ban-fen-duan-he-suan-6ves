from decimal import Decimal

from app.payroll.pricing import money


def balance(state):
    accepted = len(state['rows'])
    isolated = len(state['quarantine'])
    accounted = accepted + isolated + state['blank_count'] + state['header_count'] + state['duplicate_count']
    payroll = state.get('payroll') or {}
    batches = payroll.get('batches') or []
    payable_total = money(sum(
        (Decimal(str(batch['total'])) for batch in batches), Decimal('0')))
    evidence_total = money(sum(
        (Decimal(str(item['amount'])) for batch in batches for item in batch['payables']),
        Decimal('0')))
    unsealable = [batch['batch_id'] for batch in batches
                  if not batch['reconcile']['balanced']]
    empty = [batch['batch_id'] for batch in batches if batch['reconcile'].get('empty')]
    return {
        'input': state['input_count'],
        'accepted': accepted,
        'isolated': isolated,
        'blank': state['blank_count'],
        'headers': state['header_count'],
        'duplicates': state['duplicate_count'],
        'accounted': accounted,
        'balanced': accounted == state['input_count'],
        'business': state.get('business', {}),
        'payroll': {
            'batches': len(batches),
            'batch_total': str(payable_total),
            'payable_total': str(evidence_total),
            'difference': str(money(evidence_total - payable_total)),
            'balanced': bool(batches) and evidence_total == payable_total and not unsealable,
            'unsealable': unsealable,
            'empty': empty,
        },
    }
