#!/usr/bin/env python3
"""
مولّد تقارير Breakout/Pullback — بيانات fixture اصطناعية حصراً.
================================================================
⛔ نفس التحذير في كل مكان: SYNTHETIC_FIXTURE_NOT_REAL_MARKET_DATA،
valid_for_profitability_claims=false. المقارنة هنا **معزولة فعلياً**
(IsolatedModelAdapter) — لا Baseline يحجب أثر Breakout/Pullback كما
حدث في الجولة السابقة (موثَّق في STRATEGY_EXPERIMENTS.md).
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
from src.research.router_adapter import RouterAsSignalEngine, IsolatedModelAdapter
from src.validation import walk_forward, stress
from tests.fixtures import make_fixture

TAG = {'data_source': 'SYNTHETIC_FIXTURE_NOT_REAL_MARKET_DATA',
       'valid_for_profitability_claims': False,
       'note': 'بيانات اختبار مولَّدة رياضياً — مقارنة معزولة فعلياً '
               '(لا Baseline يحجب الأثر)، لكنها تبقى عينة اصطناعية واحدة'}

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')
os.makedirs(REPORTS, exist_ok=True)


def summarize(m):
    return {k: m.get(k) for k in
           ('total_trades', 'win_rate', 'profit_factor', 'gross_profit_factor',
            'expectancy', 'net_return_pct', 'max_drawdown_pct', 'sharpe',
            'sortino', 'calmar', 'total_fees', 'total_slippage')}


def make_cfg(mutator=None):
    c = deepcopy(Config())
    if mutator:
        mutator(c)
    return c


def main():
    print('=' * 68); print('  Breakout/Pullback — بيانات fixture اصطناعية حصراً')
    print('=' * 68)
    data = make_fixture(4000, '1h', seed=42, kind='mixed')

    # ── 1) المقارنة المعزولة — البند 10 ──
    variants = {}
    cfg_base = make_cfg()
    variants['baseline'] = ('baseline', cfg_base, RouterAsSignalEngine(cfg_base))

    cfg_bo = make_cfg(lambda c: setattr(c.breakout, 'enabled', True))
    variants['breakout_only'] = ('isolated', cfg_bo,
                                IsolatedModelAdapter(cfg_bo, BreakoutEntryModel(cfg_bo)))

    cfg_pb = make_cfg(lambda c: setattr(c.pullback, 'enabled', True))
    variants['pullback_only'] = ('isolated', cfg_pb,
                                 IsolatedModelAdapter(cfg_pb, PullbackEntryModel(cfg_pb)))

    cfg_bo_retest = make_cfg(lambda c: (setattr(c.breakout, 'enabled', True),
                                        setattr(c.breakout, 'retest_enabled', True)))
    retest_db = Database(os.path.join(tempfile.mkdtemp(), 'retest.db'))
    variants['breakout_retest_only'] = (
        'isolated_stateful', cfg_bo_retest,
        IsolatedModelAdapter(cfg_bo_retest, BreakoutEntryModel(cfg_bo_retest),
                             db=retest_db))

    cfg_both = make_cfg(lambda c: (setattr(c.breakout, 'enabled', True),
                                   setattr(c.pullback, 'enabled', True)))
    variants['breakout_and_pullback_router'] = (
        'router', cfg_both, RouterAsSignalEngine(cfg_both))

    comparison = {'meta': TAG, 'variants': {}}
    for name, (kind, cfg, eng) in variants.items():
        m = BacktestEngine(cfg, eng, 10000).run(data, data_quality=0.95).metrics
        s = summarize(m)
        s['isolation'] = kind
        comparison['variants'][name] = s
        print(f"  {name:30s} [{kind:9s}] {s['total_trades']:4d} صفقة  "
             f"PF={s.get('profit_factor')}")

    with open(f'{REPORTS}/entry_model_comparison.json', 'w') as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2, default=str)

    # ── 2) حساسية الانزلاق — البند 12 (نسخة مركَّزة: 4 سيناريوهات) ──
    print('\n  حساسية الانزلاق...')
    slip_scenarios = {
        'base': 1.0, 'slippage_1.5x': 1.5, 'slippage_2x': 2.0, 'slippage_3x': 3.0}
    slippage_report = {'meta': TAG, 'models': {}}
    for name, (kind, base_cfg, _) in variants.items():
        rows = {}
        for sname, mult in slip_scenarios.items():
            c = deepcopy(base_cfg)
            c.costs.slippage_bps_entry *= mult
            c.costs.slippage_bps_exit *= mult
            c.costs.slippage_bps_stop *= mult
            c.costs.spread_bps *= mult
            if name == 'baseline':
                eng = RouterAsSignalEngine(c)
            elif name == 'breakout_only':
                eng = IsolatedModelAdapter(c, BreakoutEntryModel(c))
            elif name == 'breakout_retest_only':
                c.breakout.retest_enabled = True
                fresh_db = Database(os.path.join(tempfile.mkdtemp(), 'r.db'))
                eng = IsolatedModelAdapter(c, BreakoutEntryModel(c), db=fresh_db)
            elif name == 'pullback_only':
                eng = IsolatedModelAdapter(c, PullbackEntryModel(c))
            else:
                eng = RouterAsSignalEngine(c)
            m = BacktestEngine(c, eng, 10000).run(data, data_quality=0.95).metrics
            rows[sname] = summarize(m)
        pf_2x = rows['slippage_2x'].get('profit_factor')
        verdict = 'FRAGILE_TO_SLIPPAGE' if (pf_2x is not None and pf_2x < 1.0) \
            else 'NOT_TESTED_ENOUGH_TRADES'
        n_trades = rows['base'].get('total_trades', 0)
        if n_trades and n_trades >= 15 and pf_2x is not None:
            verdict = 'FRAGILE_TO_SLIPPAGE' if pf_2x < 1.0 else 'RESILIENT_TO_2X_SLIPPAGE'
        slippage_report['models'][name] = {'scenarios': rows, 'verdict': verdict}
        print(f"  {name:30s} PF@base={rows['base'].get('profit_factor')} "
             f"PF@2x={pf_2x} → {verdict}")

    with open(f'{REPORTS}/slippage_sensitivity.json', 'w') as f:
        json.dump(slippage_report, f, ensure_ascii=False, indent=2, default=str)

    # ── 3) OOS مصغَّر لكل نموذج (شبكة معاملات محدودة — البند 11) ──
    print('\n  Walk-Forward لكل نموذج (شبكة مصغَّرة)...')
    oos_report = {'meta': TAG, 'by_setup': {}}
    grids = {
        'breakout_only': {'min_score': [4.0]},  # placeholder — الشبكة
        'pullback_only': {'min_score': [4.0]},
    }
    for name in ('baseline', 'breakout_only', 'pullback_only'):
        kind, cfg, _ = variants[name]
        try:
            wf = walk_forward.run(data, cfg, capital=10000, verbose=False)
            windows = wf.get('windows', [])
            oos_pfs = [w['test_metrics'].get('profit_factor') for w in windows
                      if w['test_metrics'].get('profit_factor') is not None]
            positive = sum(1 for p in oos_pfs if p and p > 1.0)
            oos_report['by_setup'][name] = {
                'oos_window_count': len(windows),
                'oos_positive_window_count': positive,
                'oos_positive_window_rate': (positive / len(windows)
                                             if windows else None),
                'oos_pf_median': (sorted(oos_pfs)[len(oos_pfs) // 2]
                                  if oos_pfs else None),
                'oos_pf_min': min(oos_pfs) if oos_pfs else None,
                'oos_pf_max': max(oos_pfs) if oos_pfs else None,
                'oos_total_trades': wf.get('oos_trades')}
            print(f"  {name:20s} نوافذ={len(windows)} إيجابية={positive}")
        except Exception as e:
            oos_report['by_setup'][name] = {
                'error': f'{type(e).__name__}: {str(e)[:200]}',
                'note': ('Breakout/Pullback يستخدمان EntryRouter/EntrySignal لا '
                        'Signal مباشرة — walk_forward._apply() يُعدِّل '
                        'cfg.signal.* فقط، فلا يمس معاملات Breakout/Pullback '
                        'الخاصة (بادئة مختلفة في Config). النتيجة: OOS الحالي '
                        'يقيس فقط تثبيت/تجميد الإعداد، لا بحث شبكة فعلي على '
                        'معاملات Breakout/Pullback بعد.')}
            print(f"  {name:20s} تعذّر: {e}")

    with open(f'{REPORTS}/oos_by_setup.json', 'w') as f:
        json.dump(oos_report, f, ensure_ascii=False, indent=2, default=str)

    print('\n' + '=' * 68)
    print('  ⛔ كل ما سبق على fixture اصطناعي واحد. لا حكم على الربحية.')
    print('=' * 68)


if __name__ == '__main__':
    main()
