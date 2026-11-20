"""Decimal pricing for one shift under one rule version.

The caller groups segments by shift; every segment passed here is already
bound to a single rule version.  Daily-hour accumulation is keyed by
(employee, calendar day, rule version) so two rule versions never share a
threshold.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

DEFAULT_MULTIPLIERS = {'REGULAR': '1', 'OVERTIME': '1.5', 'REST': '2', 'HOLIDAY': '3'}


def money(value):
    return value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def price_segments(segments, rule, used=None):
    used = used if used is not None else defaultdict(int)
    holidays = set(rule.get('holidays') or [])
    daily = int(rule.get('daily_minutes', 480))
    multipliers = rule.get('multipliers') or DEFAULT_MULTIPLIERS
    out = []
    for s in sorted(segments, key=lambda r: (r['start'], r['employee'])):
        d = date.fromisoformat(s['calendar_day'])
        key = (s['employee'], s['calendar_day'], rule['version'])
        minutes = int(s['minutes'])
        rate = Decimal(str(s['rate']))
        if s['calendar_day'] in holidays:
            parts = [(minutes, Decimal(multipliers['HOLIDAY']), 'HOLIDAY')]
        elif d.weekday() >= 5:
            parts = [(minutes, Decimal(multipliers['REST']), 'REST')]
        else:
            regular = min(minutes, max(0, daily - used[key]))
            used[key] += minutes
            parts = [(regular, Decimal(multipliers['REGULAR']), 'REGULAR'),
                     (minutes - regular, Decimal(multipliers['OVERTIME']), 'OVERTIME')]
        for count, multiplier, code in parts:
            if not count:
                continue
            item = dict(s)
            item.update(minutes=count, multiplier=str(multiplier), code=code,
                        amount=str(money(rate * count * multiplier / 60)),
                        rule_version=rule['version'])
            out.append(item)
    return out
