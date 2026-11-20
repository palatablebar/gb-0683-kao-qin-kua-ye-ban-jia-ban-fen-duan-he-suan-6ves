"""Pay-rule versions and the cut-over adjudication for in-flight shifts.

Adjudication
    A rule version is bound to a whole *shift*, never to an individual clock
    segment.  The version that governs is the one in force when the shift is
    completed (its OUT punch, i.e. the moment the shift settles).  An in-flight
    shift that starts before the cut-over and ends after it is therefore priced
    end-to-end by the new version and settled in the new period.

    Why not the two obvious alternatives:
      * keep the old version for the whole shift -> the hours actually worked
        after the cut-over would escape the new rules entirely;
      * split the shift at the cut-over instant -> one shift would reference two
        rule versions at once, which cannot be replayed as a single settlement.

    Consequence that makes old accounts stable: the crossing shift is never
    carried back into the already settled period, so a sealed period is not
    reopened at every cut-over.  Each clock segment belongs to exactly one
    version (the shift's version) and exactly one period (the shift's settlement
    period).  The basis of every adjudication is recorded verbatim on the batch.
"""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.errors import DomainError

ZONE = ZoneInfo('Asia/Shanghai')

# Rule versions are effective at 00:00 Asia/Shanghai on the first of a month.
# The values below are the project's sample rules; real labour law is out of scope.
BASE_RULES = [
    {
        'version': '2026.10-v1',
        'label': '2026年10月计薪规则',
        'effective_from': '2026-10-01T00:00:00+08:00',
        'daily_minutes': 480,
        'multipliers': {'REGULAR': '1', 'OVERTIME': '1.5', 'REST': '2', 'HOLIDAY': '3'},
        'holidays': ['2026-10-01'],
    },
    {
        'version': '2026.11-v1',
        'label': '2026年11月计薪规则',
        'effective_from': '2026-11-01T00:00:00+08:00',
        'daily_minutes': 480,
        'multipliers': {'REGULAR': '1', 'OVERTIME': '1.5', 'REST': '2', 'HOLIDAY': '3'},
        'holidays': [],
    },
]


def _decimal(value, code='SWITCH'):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise DomainError(code, '倍率必须是正数')


def _validate(rule, existing):
    try:
        version = rule['version']
        label = rule.get('label') or version
        instant = datetime.fromisoformat(str(rule['effective_from']))
        if instant.tzinfo is None:
            raise ValueError()
        instant = instant.astimezone(ZONE)
        if (instant.hour, instant.minute, instant.second, instant.microsecond) != (0, 0, 0, 0) or instant.day != 1:
            raise ValueError()
        daily = int(rule['daily_minutes'])
        if daily <= 0 or daily > 1440:
            raise ValueError()
        multipliers = {
            key: str(_decimal(rule['multipliers'][key]))
            for key in ('REGULAR', 'OVERTIME', 'REST', 'HOLIDAY')
        }
        holidays = sorted(date.fromisoformat(str(item)).isoformat() for item in rule.get('holidays', []))
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError('SWITCH', '规则切换参数无效：' + str(exc))
    candidate = {
        'version': str(version), 'label': str(label), 'effective_from': instant.isoformat(),
        'daily_minutes': daily, 'multipliers': multipliers, 'holidays': holidays,
    }
    if not candidate['version']:
        raise DomainError('SWITCH', '规则版本不能为空')
    if any(item['version'] == candidate['version'] for item in existing):
        raise DomainError('SWITCH', '规则版本重复')
    replaced = [item for item in existing
                if item['effective_from'] == candidate['effective_from']]
    if replaced and not rule.get('replace'):
        raise DomainError('SWITCH', '同一生效时点已存在版本，更新须显式声明 replace')
    if not replaced:
        latest = max(existing, key=lambda item: item['effective_from'])
        if instant <= datetime.fromisoformat(latest['effective_from']):
            raise DomainError('SWITCH', '新规则生效时点必须晚于当前版本；修正旧期须以 replace 覆盖同时点版本')
    candidate['replaces'] = [item['version'] for item in replaced]
    return candidate


def registry(parameters=None):
    """Return the active rule list plus an auditable switch record.

    A candidate switch arrives as ``parameters['rule_switch']``.  If it is
    invalid the old registry is retained untouched; only a valid candidate is
    appended.  A failed attempt never invalidates an existing batch.
    """
    parameters = parameters or {}
    rules = [deepcopy(rule) for rule in BASE_RULES]
    info = {
        'status': '无需切换',
        'requested': None,
        'active': [rule['version'] for rule in rules],
        'retained': max(rules, key=lambda item: item['effective_from'])['version'],
        'code': None,
        'message': None,
    }
    candidate = parameters.get('rule_switch')
    if candidate is not None:
        info['requested'] = candidate.get('version') if isinstance(candidate, dict) else str(candidate)
        try:
            if not isinstance(candidate, dict):
                raise DomainError('SWITCH', '规则切换参数必须是对象')
            rule = _validate(candidate, rules)
            if rule['replaces']:
                rules = [item for item in rules if item['version'] not in rule['replaces']]
            rules.append(rule)
            rules.sort(key=lambda item: item['effective_from'])
            info.update(status='成功', active=[item['version'] for item in rules],
                        retained=rules[-1]['version'], applied=rule['version'],
                        replaced=rule['replaces'])
        except DomainError as error:
            # Keep the old version: the failed candidate is evidence only.
            info.update(status='失败', code=error.code, message=error.message)
    return rules, info


def rule_for_day(day, rules):
    try:
        target = date.fromisoformat(str(day))
    except ValueError:
        raise DomainError('RULE', '归属日无效')
    eligible = [rule for rule in rules if date.fromisoformat(rule['effective_from'][:10]) <= target]
    if not eligible:
        raise DomainError('RULE', '归属日没有适用的计薪规则：' + str(day))
    return max(eligible, key=lambda item: item['effective_from'])


def binding(shift, rules):
    """Resolve one whole shift to exactly one rule version and one period.

    The governing version is the one in force at the OUT punch.  For a shift
    that does not cross a cut-over this equals the attribution-day version; for
    an in-flight shift it is the new version, and the shift is settled in the
    new period instead of reopening the old one.  A shift that merely ends on
    the cut-over instant is not in flight (half-open work interval).
    """
    if not shift.get('in') or not shift.get('out'):
        raise DomainError('MISSING', '班次缺少进出打卡')
    start = datetime.fromisoformat(shift['in']['at']).astimezone(ZONE)
    end = datetime.fromisoformat(shift['out']['at']).astimezone(ZONE)
    start_rule = rule_for_day(start.date().isoformat(), rules)
    # Work is half-open [start, end): a shift ending exactly on the cut-over
    # instant contains no post-cut-over minutes and stays with the old version.
    crossings = sorted(
        datetime.fromisoformat(item['effective_from'])
        for item in rules
        if start < datetime.fromisoformat(item['effective_from']) < end
    )
    in_flight = bool(crossings)
    cutover = crossings[0] if crossings else None
    rule = rule_for_day(end.date().isoformat(), rules) if in_flight else start_rule
    settle_period = end.date().isoformat()[:7] if in_flight else shift['day'][:7]
    if in_flight:
        decision = '整班按班次完成时点规则版本绑定'
        reason = ('归属日 ' + shift['day'] + ' 早于切换点 ' + cutover.date().isoformat() +
                  '，班次完成时点已在新规则生效后：整班只绑定完成时点版本 ' + rule['version'] +
                  '，计入 ' + settle_period + ' 批次，不拆分、不回灌前一期、不重复计薪')
    else:
        decision = '整班按归属日规则版本绑定'
        reason = '归属日与完成时点同处 ' + rule['version'] + ' 生效区间，整班适用单一版本'
    return {
        'basis_id': 'B-' + shift['id'],
        'shift_id': shift['id'],
        'employee': shift['employee'],
        'attribution_day': shift['day'],
        'work_start': start.isoformat(),
        'work_end': end.isoformat(),
        'cutover': cutover.isoformat() if cutover else None,
        'in_flight': in_flight,
        'bound_rule': rule['version'],
        'rule_effective_from': rule['effective_from'],
        'rule_on_attribution_day': start_rule['version'],
        'settle_period': settle_period,
        'decision': decision,
        'reason': reason,
    }


def rules_for(parameters):
    rules, info = registry(parameters)
    override = (parameters or {}).get('holidays')
    if override is not None:
        try:
            holidays = sorted({date.fromisoformat(str(item)).isoformat() for item in override})
        except (TypeError, ValueError):
            raise DomainError('RULE', 'holidays 参数必须是日期列表')
        for rule in rules:
            rule['holidays'] = holidays
    return rules, info


def registry_digest(rules):
    import hashlib, json
    payload = [
        {
            'version': rule['version'], 'effective_from': rule['effective_from'],
            'daily_minutes': rule['daily_minutes'], 'multipliers': rule['multipliers'],
            'holidays': rule['holidays'],
        }
        for rule in sorted(rules, key=lambda item: item['effective_from'])
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
