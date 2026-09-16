#!/usr/bin/env python3
"""
Ablation Study فعلي — القسم الخامس. كل تجربة: باكتست مع الشرط، ثم
بلا الشرط، على نفس fixture بالضبط. لا اعتماد شرط لمجرد رفع Win Rate
أو خفض عدد الصفقات — القراءة النهائية بشرية في التقرير المرفق.
"""
import json
import os
import sys
import tempfile
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import Config
from src.backtest.engine import BacktestEngine
from src.storage.database import Database
from src.signals.entry import BreakoutEntryModel, PullbackEntryModel
from src.research.router_adapter import IsolatedModelAdapter
from tests.fixtures import make_fixture

TAG = {'data_source': 'SYNTHETIC_FIXTURE_NOT_REAL_MARKET_DATA',
       'valid_for_profitability_claims': False}

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')
os.makedirs(REPORTS, exist_ok=True)


def summarize(m):
    return {k: m.get(k) for k in
           ('total_trades', 'win_rate', 'profit_factor', 'expectancy',
            'net_return_pct', 'max_drawdown_pct', 'total_fees', 'total_slippage')}


def run_bt(cfg, model_cls, db=None):
    m = model_cls(cfg)
    eng = (IsolatedModelAdapter(cfg, m, db=db) if db is not None
          else IsolatedModelAdapter(cfg, m))
    return BacktestEngine(cfg, eng, 10000)


def experiment(name, setup_type, base_mutator, ablate_mutator, model_cls, data,
               needs_db=False):
    base_cfg = deepcopy(Config())
    base_mutator(base_cfg)
    db1 = Database(os.path.join(tempfile.mkdtemp(), 'a.db')) if needs_db else None
    res_base = run_bt(base_cfg, model_cls, db=db1).run(data, data_quality=0.95)

    abl_cfg = deepcopy(base_cfg)
    ablate_mutator(abl_cfg)
    db2 = Database(os.path.join(tempfile.mkdtemp(), 'b.db')) if needs_db else None
    res_abl = run_bt(abl_cfg, model_cls, db=db2).run(data, data_quality=0.95)

    b, a = summarize(res_base.metrics), summarize(res_abl.metrics)
    return {
        **TAG,
        'experiment_id': f'ablation_{name}',
        'strategy_version': base_cfg.version,
        'config_fingerprint_with_condition': base_cfg.fingerprint(),
        'config_fingerprint_without_condition': abl_cfg.fingerprint(),
        'setup_type': setup_type,
        'removed_condition': name,
        'with_condition': b,
        'without_condition': a,
        'trades_added': a['total_trades'] - b['total_trades'],
        'trades_removed': max(0, b['total_trades'] - a['total_trades']),
        'pf_delta': ((a['profit_factor'] or 0) - (b['profit_factor'] or 0)
                    if a['profit_factor'] and b['profit_factor'] else None),
        'expectancy_delta': ((a['expectancy'] or 0) - (b['expectancy'] or 0)
                             if a['expectancy'] is not None
                             and b['expectancy'] is not None else None),
    }


def main():
    print('=' * 68); print('  Ablation Study فعلي — fixture اصطناعي واحد')
    print('=' * 68)
    data = make_fixture(4000, '1h', seed=42, kind='mixed')

    experiments = []

    def bo(mutate, needs_db=False):
        return lambda name, ablate: experiment(
            name, 'BREAKOUT', lambda c: (setattr(c.breakout, 'enabled', True), mutate(c)),
            ablate, BreakoutEntryModel, data, needs_db=needs_db)

    def pb(mutate):
        return lambda name, ablate: experiment(
            name, 'PULLBACK', lambda c: (setattr(c.pullback, 'enabled', True), mutate(c)),
            ablate, PullbackEntryModel, data)

    noop = lambda c: None

    plan = [
        # 1-6: Breakout
        ('breakout_volume_filter', bo(lambda c: setattr(c.breakout, 'volume_filter_enabled', True)),
         lambda c: setattr(c.breakout, 'volume_filter_enabled', False)),
        ('breakout_extension_filter', bo(noop),
         lambda c: setattr(c.breakout, 'extension_filter_enabled', False)),
        ('breakout_false_breakout_filter', bo(noop),
         lambda c: (setattr(c.breakout, 'body_filter_enabled', False),
                    setattr(c.breakout, 'wick_filter_enabled', False))),
        ('breakout_retest_confirmation',
         bo(lambda c: setattr(c.breakout, 'retest_enabled', True), needs_db=True),
         lambda c: setattr(c.breakout, 'retest_enabled', False)),
        ('breakout_higher_timeframe_filter',
         bo(lambda c: setattr(c.breakout, 'htf_confirmation_enabled', True)),
         lambda c: setattr(c.breakout, 'htf_confirmation_enabled', False)),
        ('breakout_resistance_distance_filter', bo(noop),
         lambda c: setattr(c.breakout, 'resistance_distance_filter_enabled', False)),
        # 7-10: Pullback
        ('pullback_support_confirmation', pb(noop),
         lambda c: setattr(c.pullback, 'support_filter_enabled', False)),
        ('pullback_candle_confirmation', pb(lambda c: setattr(c.pullback, 'confirmation_required', True)),
         lambda c: setattr(c.pullback, 'confirmation_required', False)),
        ('pullback_regime_filter', pb(noop),
         lambda c: setattr(c.pullback, 'regime_filter_enabled', False)),
        ('pullback_net_rr_filter', pb(noop),
         lambda c: setattr(c.pullback, 'net_rr_filter_enabled', False)),
        # 11-13: عامة
        ('breakout_candle_size_filter', bo(noop),
         lambda c: setattr(c.breakout, 'candle_size_filter_enabled', False)),
        ('breakout_consecutive_loss_guard_analysis_only', bo(noop),
         lambda c: setattr(c.risk, 'max_consecutive_losses', 999999)),
        ('pullback_consecutive_loss_guard_analysis_only', pb(noop),
         lambda c: setattr(c.risk, 'max_consecutive_losses', 999999)),
    ]

    results = []
    for name, exp_fn, ablate in plan:
        r = exp_fn(name, ablate)
        results.append(r)
        b, a = r['with_condition'], r['without_condition']
        print(f"  {name:42s} صفقات: {b['total_trades']:3d}→{a['total_trades']:3d}  "
             f"PFΔ={r['pf_delta']}")

    with open(f'{REPORTS}/ablation_study.json', 'w') as f:
        json.dump({'meta': TAG, 'experiments': results}, f,
                 ensure_ascii=False, indent=2, default=str)

    print('\n' + '=' * 68)
    print('  ⛔ fixture اصطناعي واحد. لا اعتماد شرط بسبب هذه الأرقام فقط.')
    print('=' * 68)


if __name__ == '__main__':
    main()
