from decimal import Decimal

from app.payroll.batch import build
from app.payroll.pricing import money, price_segments

# Re-exported for direct unit tests of the pricing kernel.
__all__ = ['price', 'price_segments', 'apply']


def price(segments, holidays):
    rule = {'version': 'test', 'daily_minutes': 480, 'holidays': sorted(holidays),
            'multipliers': {'REGULAR': '1', 'OVERTIME': '1.5', 'REST': '2', 'HOLIDAY': '3'}}
    return price_segments(segments, rule)


def apply(state):
    payroll = build(state)
    batches = payroll['batches']
    # Every segment belongs to exactly one batch, so concatenating payables of
    # all batches of this run counts each piece of work once.
    calculations = [item for batch in batches for item in batch['payables']]
    total = money(sum((Decimal(str(item['amount'])) for item in calculations), Decimal('0')))
    paid = sum((Decimal(str(row['paid'])) for row in state['rows']), Decimal('0'))
    featured = payroll['current']
    state['payroll'] = payroll
    state['calculations'] = calculations
    state['business'] = {
        'total': str(total),
        'paid': str(paid),
        'difference': str(money(total - paid)),
        'minutes': sum(int(item['minutes']) for item in calculations),
        'unit': 'CNY',
        'period': featured['period'],
        'batch_id': featured['batch_id'],
        'batch_total': featured['total'],
        'batch_balanced': featured['reconcile']['balanced'],
        'batches': [{'period': batch['period'], 'batch_id': batch['batch_id'],
                     'total': batch['total'], 'status': batch['status'],
                     'balanced': batch['reconcile']['balanced']} for batch in batches],
        'featured': featured['period'],
    }
    return state
