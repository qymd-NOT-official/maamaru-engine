"""日服资源目标：只依赖独立读数，不加载活动或自动排班规则。"""
from datetime import datetime, timezone, timedelta
from . import advisor


def report(store, path):
    now = datetime.now(timezone(timedelta(hours=8)))
    rates = advisor.estimate_daily_rates([], today=now.date())
    stock = store.client_item_inventory()
    current = {name: row['count'] for name, row in stock['resources'].items()}
    goals = [advisor.evaluate_goal(g, current=current.get(g['resource']),
        rate_info=rates.get(g['resource']), floor_yield=None, today=now.date(),
        now=now, event_windows=[], window_impacts={}) for g in advisor.load_goals(path)]
    return {'schema_version': advisor.PLANNING_SCHEMA_VERSION, 'generated_at': now.timestamp(),
        'today': now.date().isoformat(), 'rate_window_days': advisor.RATE_WINDOW_DAYS,
        'event_rate_window_days': advisor.EVENT_RATE_WINDOW_DAYS, 'rates': rates,
        'current': current, 'goals': goals, 'events': [], 'acquisition': {},
        'fragments': {}, 'fragment_notes': None, 'resource_watch': None,
        'koban_per_floor': None, 'osaka_floor_speed': None, 'client_inventory': stock,
        'koban_watch': {'current': current.get('小判'), 'reserved': 0, 'budgets': [],
            'available': current.get('小判'), 'confirmed_spending': None, 'spending_days': 14}}
