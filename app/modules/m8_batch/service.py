from app.payroll.batch import replay


def execute(state):
    """Attach replay evidence to every batch; never mutates stored batches."""
    payroll = state.get('payroll')
    if not payroll:
        return state
    for batch in payroll['batches']:
        batch['replay'] = replay(batch)
        batch['sealable'] = batch['status'] == '待封存' and batch['reconcile']['balanced']
    state['switch'] = payroll['switch']
    return state
