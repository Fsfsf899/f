#!/usr/bin/env python3
"""
مولّد تقارير البحث الاستراتيجي — على بيانات fixture اصطناعية فقط.
====================================================================
⛔ لا بيانات سوق حقيقية استُخدمت هنا. Binance محجوب في بيئة البناء
(مؤكَّد فعلياً — انظر reports/real_validation.json). كل رقم في هذا
الملف وما يُنتجه من JSON **لإثبات أن آلية البحث نفسها تعمل ميكانيكياً
بشكل صحيح** (لا تسرّب TRAIN/OOS، لا نظر للمستقبل، الحدود تتحسّن مع
سعة الحد الأقصى، إلخ) — وليس دليلاً على ربحية أي شيء في سوق حقيقي.

كل ملف JSON يُنتَج هنا يحمل الحقل:
    "data_source": "SYNTHETIC_FIXTURE_NOT_REAL_MARKET_DATA"
    "valid_for_profitability_claims": false
حتى لو فُتح مباشرة بلا قراءة أي تقرير نصي مرافق.
"""
import json
import os
import sys
import time
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import Config
from src.signals.engine import SignalEngine
from src.backtest.engine import BacktestEngine
from src.validation import walk_forward, stress
from tests.fixtures import make_fixture

TAG = {'data_source': 'SYNTHETIC_FIXTURE_NOT_REAL_MARKET_DATA',
       'valid_for_profitability_claims': False,
       'note': 'بيانات اختبار مولَّدة رياضياً — لا علاقة لها بأي سوق حقيقي. '
               'لا يجوز استخدام هذه الأرقام كدليل على ربحية أي إعداد.'}

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')
os.makedirs(REPORTS, exist_ok=True)


def summarize(m):
    return {k: m.get(k) for k in
           ('total_trades', 'win_rate', 'profit_factor', 'gross_profit_factor',
            'expectancy', 'net_return_pct', 'max_drawdown_pct', 'sharpe',
            'sortino', 'calmar', 'total_fees', 'total_slippage')}


def main():
    print('=' * 68)
    print('  بحث استراتيجي — بيانات fixture اصطناعية حصراً')
    print('=' * 68)

    data = make_fixture(4000, '1h', seed=42, kind='mixed')
    print(f'  fixture: {len(data)} شمعة، seed=42 — اصطناعية بالكامل')

    baseline_cfg = Config()

    variants = {
        'baseline_atr_default': Config(),
        'net_rr_disabled': _with(lambda c: setattr(
            c.signal, 'use_net_risk_reward', False)),
        'stop_structure': _with(lambda c: setattr(
            c.signal, 'stop_method', 'structure')),
        'stop_hybrid': _with(lambda c: setattr(
            c.signal, 'stop_method', 'hybrid')),
        'time_exit_20bars': _with(lambda c: setattr(
            c.signal, 'max_holding_bars', 20)),
        'trailing_stop_on': _with(lambda c: (
            setattr(c.signal, 'trailing_stop_enabled', True),
            setattr(c.signal, 'trailing_atr_mult', 2.5))),
    }

    # ── 1) دراسة الاستبعاد (Ablation) — كل متغيّر مقابل الأساس ──
    ablation = {'meta': TAG, 'baseline': None, 'variants': {}}
    base_m = BacktestEngine(baseline_cfg, SignalEngine(baseline_cfg), 10000).run(
        data, data_quality=0.95).metrics
    ablation['baseline'] = summarize(base_m)
    print(f"\n  الأساس: {base_m['total_trades']} صفقة، "
         f"PF صافٍ={base_m.get('profit_factor')}")

    for name, cfg in variants.items():
        if name == 'baseline_atr_default':
            continue
        m = BacktestEngine(cfg, SignalEngine(cfg), 10000).run(
            data, data_quality=0.95).metrics
        s = summarize(m)
        s['trade_count_delta'] = s['total_trades'] - ablation['baseline']['total_trades']
        s['net_return_delta_pct'] = (
            (s['net_return_pct'] or 0) - (ablation['baseline']['net_return_pct'] or 0))
        ablation['variants'][name] = s
        print(f"  {name:24s} {s['total_trades']:4d} صفقة  "
             f"Δصفقات={s['trade_count_delta']:+3d}  "
             f"PF={s.get('profit_factor')}")

    with open(f'{REPORTS}/strategy_candidates.json', 'w') as f:
        json.dump(ablation, f, ensure_ascii=False, indent=2, default=str)

    # ── 1ب) مقارنة نماذج الدخول — Baseline vs Breakout vs Pullback ──
    # عبر BacktestEngine الحقيقي (لا عدّ إشارات فقط) باستخدام محوّل
    # RouterAsSignalEngine — بلا أي تعديل على BacktestEngine نفسه.
    print('\n  مقارنة نماذج الدخول (Baseline/Breakout/Pullback)...')
    from src.research.router_adapter import RouterAsSignalEngine

    entry_variants = {
        'baseline_only': Config(),
        'breakout_enabled': _with(lambda c: setattr(c.breakout, 'enabled', True)),
        'pullback_enabled': _with(lambda c: setattr(c.pullback, 'enabled', True)),
        'breakout_and_pullback': _with(lambda c: (
            setattr(c.breakout, 'enabled', True),
            setattr(c.pullback, 'enabled', True))),
    }
    entry_comparison = {'meta': TAG, 'variants': {}}
    for name, cfg in entry_variants.items():
        m = BacktestEngine(cfg, RouterAsSignalEngine(cfg), 10000).run(
            data, data_quality=0.95).metrics
        s = summarize(m)
        entry_comparison['variants'][name] = s
        print(f"  {name:24s} {s['total_trades']:4d} صفقة  PF={s.get('profit_factor')}")
    with open(f'{REPORTS}/entry_model_comparison.json', 'w') as f:
        json.dump(entry_comparison, f, ensure_ascii=False, indent=2, default=str)

    # ── 2) Walk-Forward — الأساس فقط (الأثقل حساباً) ──
    print('\n  Walk-Forward (قد يستغرق دقيقة)...')
    try:
        wf = walk_forward.run(data, baseline_cfg, capital=10000, verbose=False)
        wf_out = {'meta': TAG,
                  'oos_trades': wf.get('oos_trades'),
                  'oos_aggregate_pf': wf.get('oos_aggregate_pf'),
                  'consistency': wf.get('consistency'),
                  'n_windows': len(wf.get('windows', [])),
                  'windows': [
                      {'window': w['index'] + 1,
                       'train_metrics': summarize(w['train_metrics']),
                       'oos_metrics': summarize(w['test_metrics']),
                       'best_params': w['best_params']}
                      for w in wf.get('windows', [])]}
    except Exception as e:
        wf_out = {'meta': TAG, 'error': f'{type(e).__name__}: {str(e)[:300]}',
                  'note': 'تعذّر تشغيل walk-forward على هذه العينة'}
    with open(f'{REPORTS}/walk_forward_results.json', 'w') as f:
        json.dump(wf_out, f, ensure_ascii=False, indent=2, default=str)
    print(f"  OOS: {wf_out.get('oos_trades', '—')} صفقة عبر "
         f"{wf_out.get('n_windows', 0)} نافذة")

    # ── 3) اختبار الضغط — الأساس ──
    print('\n  اختبار الضغط...')
    st = stress.run(data, baseline_cfg, 10000, data_quality=0.95)
    st_out = {'meta': TAG, 'verdict': st.verdict, 'note': st.note,
             'scenarios': st.scenarios}
    with open(f'{REPORTS}/stress_results.json', 'w') as f:
        json.dump(st_out, f, ensure_ascii=False, indent=2, default=str)
    print(f'  الحكم: {st.verdict}')

    # ── 4) تفصيل الدخول/الخروج — الأساس ──
    bt = BacktestEngine(baseline_cfg, SignalEngine(baseline_cfg), 10000)
    res = bt.run(data, data_quality=0.95)
    by_reason = {}
    for t in res.trades:
        r = t['exit_reason']
        by_reason.setdefault(r, []).append(t['pnl'])
    breakdown = {'meta': TAG,
                'total_trades': len(res.trades),
                'signals_evaluated': res.signals_evaluated,
                'signals_buy': res.signals_buy,
                'rejections_top10': dict(list(res.rejections.items())[:10]),
                'by_exit_reason': {
                    r: {'count': len(v), 'sum_pnl': round(sum(v), 4),
                       'avg_pnl': round(sum(v) / len(v), 4)}
                    for r, v in by_reason.items()}}
    with open(f'{REPORTS}/entry_exit_breakdown.json', 'w') as f:
        json.dump(breakdown, f, ensure_ascii=False, indent=2, default=str)

    # ── 5) خط الأساس (البند 3) ──
    baseline_json = {**TAG, 'symbol': data.symbol, 'interval': data.interval,
                     'bars': len(data), 'period_note': 'fixture اصطناعي — لا فترة سوق حقيقية',
                     **base_m}
    with open(f'{REPORTS}/strategy_baseline.json', 'w') as f:
        json.dump(baseline_json, f, ensure_ascii=False, indent=2, default=str)

    print('\n' + '=' * 68)
    print('  ⛔ كل ما سبق على بيانات اصطناعية. لا حكم على الربحية ممكن.')
    print('=' * 68)


def _with(mutator):
    c = deepcopy(Config())
    mutator(c)
    return c


if __name__ == '__main__':
    main()
