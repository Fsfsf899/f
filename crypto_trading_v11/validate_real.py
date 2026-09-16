#!/usr/bin/env python3
"""
التحقق على بيانات سوق حقيقية — البندان 14 و36.

⛔ لا fallback اصطناعي. تعذّر الوصول ⇒ VALIDATION BLOCKED.
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

from src.core.config import Config, ValidationConfig
from src.data.binance import BinancePublic, DataUnavailable
from src.data.cache import Cache
from src.data.validation import validate as dq_validate
from src.backtest.engine import BacktestEngine
from src.signals.engine import SignalEngine
from src.validation import walk_forward, stress
from src.validation.calibration import build_from_backtest
from src.validation.lookahead import check_arrays, check_decisions
from src.indicators.engine import IndicatorEngine
from src.market.structure import StructureEngine

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')
os.makedirs(REPORTS, exist_ok=True)

BLOCKED = 'BLOCKED'
INSUFFICIENT = 'INSUFFICIENT_DATA'
FAILED = 'FAILED'
PASSED = 'PASSED'

EDGE_UNKNOWN = 'UNKNOWN'
EDGE_NEGATIVE = 'NEGATIVE'
EDGE_WEAK = 'WEAK'
EDGE_PROMISING = 'PROMISING'
EDGE_ROBUST = 'ROBUST'


def classify_edge(oos_pf, oos_trades, consistency, stress_verdict, vc):
    """
    PASSED لا تعني ربحاً. تعني أن المنهجية والشروط الإحصائية استوفيت.
    حالة الحافة منفصلة تماماً.
    """
    if oos_trades < vc.min_trades:
        return EDGE_UNKNOWN, f'عينة OOS غير كافية ({oos_trades})'
    if oos_pf is None or oos_pf < 1.0:
        return EDGE_NEGATIVE, f'PF خارج العينة {oos_pf} — لا حافة'
    if oos_pf < vc.min_profit_factor or consistency < vc.min_wf_consistency:
        return EDGE_WEAK, f'PF {oos_pf}, ثبات {consistency} — دون العتبة'
    if stress_verdict == stress.ROBUST:
        return EDGE_ROBUST, 'تصمد أمام التكاليف المتصاعدة'
    return EDGE_PROMISING, f'اجتازت العتبات لكن {stress_verdict}'


def run_symbol(sym, interval, days, capital, cfg, vc, client, cache):
    out = {'symbol': sym, 'interval': interval, 'days': days}
    try:
        data = cache.get(client, sym, interval, days, verbose=False)
    except DataUnavailable as e:
        out['status'] = BLOCKED
        out['error'] = str(e)[:300]
        return out

    q = dq_validate(data, now_ms=client.now_ms())
    out['data_quality'] = q.to_dict()
    out['bars'] = len(data)
    if q.score < vc.min_data_quality:
        out['status'] = INSUFFICIENT
        out['reason'] = f'جودة البيانات {q.score} < {vc.min_data_quality}'
        return out
    if len(data) < vc.wf_train_bars + vc.wf_test_bars:
        out['status'] = INSUFFICIENT
        out['reason'] = f'{len(data)} شمعة غير كافية'
        return out

    lk = {
        'indicators': check_arrays(
            lambda x: IndicatorEngine().compute(x, key=None), data)['passed'],
        'structure': check_arrays(
            lambda x: {k: v for k, v in StructureEngine().compute(
                x.high, x.low, x.close, x.open_time).items()
                if isinstance(v, np.ndarray)}, data)['passed'],
        'decisions': check_decisions(
            lambda d, i: {'d': SignalEngine().evaluate(
                d, i, data_quality=0.95).decision}, data, sample=15)['passed'],
    }
    out['lookahead'] = lk
    if not all(lk.values()):
        out['status'] = FAILED
        out['reason'] = 'انحياز نظر للأمام'
        return out

    bt = BacktestEngine(cfg, SignalEngine(cfg), capital).run(
        data, data_quality=q.score)
    out['backtest'] = bt.metrics

    wf = walk_forward.run(data, cfg, vc, capital=capital, verbose=False)
    out['walk_forward'] = {k: v for k, v in wf.items() if k != 'windows'}
    out['wf_windows'] = [
        {'window': w['index'] + 1,
         'train': [w['train_start'], w['train_end']],
         'oos': [w['test_start'], w['test_end']],
         'params': w['best_params'],
         'train_metrics': {k: w['train_metrics'].get(k) for k in
                           ('total_trades', 'profit_factor', 'net_return_pct')},
         'oos_metrics': {k: w['test_metrics'].get(k) for k in
                         ('total_trades', 'win_rate', 'profit_factor',
                          'expectancy', 'net_return_pct', 'max_drawdown_pct',
                          'sharpe', 'sortino', 'calmar')}}
        for w in wf.get('windows', [])]

    st = stress.run(data, cfg, capital, data_quality=q.score)
    out['stress'] = st.to_dict()

    cal, rep = build_from_backtest(bt.trades)
    out['calibration'] = rep.to_dict()

    oos_pf = wf.get('oos_aggregate_pf')
    oos_n = wf.get('oos_trades', 0)
    cons = wf.get('consistency', 0.0)
    edge, note = classify_edge(oos_pf, oos_n, cons, st.verdict, vc)
    out['edge_status'] = edge
    out['edge_note'] = note
    out['status'] = (PASSED if oos_n >= vc.min_trades else INSUFFICIENT)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbols', default='BTCUSDT,ETHUSDT,SOLUSDT')
    ap.add_argument('--interval', default='4h')
    ap.add_argument('--days', type=int, default=730)
    ap.add_argument('--capital', type=float, default=10000)
    a = ap.parse_args()

    cfg, vc = Config(), ValidationConfig()
    syms = [s.strip().upper() for s in a.symbols.split(',') if s.strip()]

    print('=' * 68)
    print(f'  التحقق على بيانات حقيقية — {", ".join(syms)} {a.interval} {a.days}د')
    print(f'  نسخة {cfg.version} | بصمة {cfg.fingerprint()}')
    print('=' * 68)

    client = BinancePublic()
    try:
        client.sync_time()
    except Exception as e:
        print(f'\n⛔ VALIDATION BLOCKED: REAL MARKET DATA UNAVAILABLE')
        print(f'   السبب: {str(e)[:220]}')
        print(f'   لا يوجد بديل اصطناعي. النتيجة: لا نتائج.')
        blob = {'validation_status': BLOCKED,
                'edge_status': EDGE_UNKNOWN,
                'reason': 'REAL_MARKET_DATA_UNAVAILABLE',
                'error': str(e)[:400],
                'symbols': syms,
                'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'note': 'لم يُستخدم أي بديل اصطناعي — لا نتائج أداء'}
        with open(f'{REPORTS}/real_validation.json', 'w') as f:
            json.dump(blob, f, ensure_ascii=False, indent=2)
        return 2

    cache = Cache()
    results = [run_symbol(s, a.interval, a.days, a.capital, cfg, vc,
                          client, cache) for s in syms]

    for r in results:
        print(f"\n  ── {r['symbol']} ──")
        if r['status'] in (BLOCKED, INSUFFICIENT, FAILED):
            print(f"     {r['status']}: {r.get('reason') or r.get('error')}")
            continue
        m, wf = r['backtest'], r['walk_forward']
        print(f"     شموع {r['bars']} | جودة {r['data_quality']['score']}")
        print(f"     باكتست: {m['total_trades']} صفقة | PF {m['profit_factor']} | "
              f"صافي {m['net_return_pct']}% | DD {m['max_drawdown_pct']}%")
        print(f"     OOS: {wf['oos_trades']} صفقة | PF {wf.get('oos_aggregate_pf')} | "
              f"ثبات {wf.get('consistency')}")
        print(f"     إجهاد: {r['stress']['verdict']}")
        print(f"     الحافة: {r['edge_status']} — {r['edge_note']}")

    statuses = [r['status'] for r in results]
    edges = [r.get('edge_status', EDGE_UNKNOWN) for r in results]
    overall = (PASSED if all(s == PASSED for s in statuses)
               else FAILED if FAILED in statuses
               else INSUFFICIENT)
    order = [EDGE_NEGATIVE, EDGE_UNKNOWN, EDGE_WEAK, EDGE_PROMISING, EDGE_ROBUST]
    worst = min(edges, key=lambda e: order.index(e)) if edges else EDGE_UNKNOWN

    print('\n' + '=' * 68)
    print(f'  VALIDATION_STATUS  : {overall}')
    print(f'  STRATEGY_EDGE      : {worst}')
    print('  PASSED = استوفت المنهجية والشروط الإحصائية. لا تعني ربحاً.')
    print('=' * 68)

    blob = {'validation_status': overall, 'edge_status': worst,
            'symbols': results, 'config_fingerprint': cfg.fingerprint(),
            'strategy_version': cfg.version,
            'generated_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    with open(f'{REPORTS}/real_validation.json', 'w') as f:
        json.dump(blob, f, ensure_ascii=False, indent=2, default=str)
    print(f'\n  التقرير: reports/real_validation.json')
    return 0 if overall == PASSED else 1


if __name__ == '__main__':
    sys.exit(main())
