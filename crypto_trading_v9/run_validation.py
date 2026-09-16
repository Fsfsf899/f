#!/usr/bin/env python3
"""
تشغيل التحقق الكامل وتوليد التقارير.

الاستخدام:
  python3 run_validation.py                 # على بيانات بينانس الحقيقية
  python3 run_validation.py --fixture       # على بيانات اختبار (للتحقق من الآلية فقط)
"""
import sys, os, json, argparse, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

from src.core.config import Config, ValidationConfig
from src.data.binance import BinancePublic, DataUnavailable
from src.data.cache import Cache
from src.data.validation import validate
from src.backtest.engine import BacktestEngine
from src.signals.engine import SignalEngine
from src.validation import walk_forward
from src.validation.calibration import build_from_backtest
from src.validation.lookahead import check_arrays, check_decisions
from src.indicators.engine import IndicatorEngine
from src.market.structure import StructureEngine

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reports')
os.makedirs(REPORTS, exist_ok=True)


def load(symbol, interval, days, use_fixture):
    if use_fixture:
        from tests.fixtures import make_fixture
        n = int(days * 86400000 / __import__('src.data.types', fromlist=['INTERVAL_MS']).INTERVAL_MS[interval])
        d = make_fixture(min(n, 6000), interval, seed=42, symbol=symbol)
        return d, 'TEST_FIXTURE (ليست بيانات سوق)', 0.95
    c = BinancePublic(); c.sync_time()
    d = Cache().get(c, symbol, interval, days)
    q = validate(d, now_ms=c.now_ms())
    return d, 'Binance (بيانات حقيقية)', q.score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', default='BTCUSDT')
    ap.add_argument('--interval', default='4h')
    ap.add_argument('--days', type=int, default=730)
    ap.add_argument('--capital', type=float, default=10000)
    ap.add_argument('--fixture', action='store_true')
    a = ap.parse_args()

    print('=' * 66)
    print(f'  تقرير التحقق — {a.symbol} {a.interval} {a.days} يوم')
    print('=' * 66)

    try:
        data, source, dq = load(a.symbol, a.interval, a.days, a.fixture)
    except DataUnavailable as e:
        print(f'\n❌ BLOCKED: تعذّر جلب بيانات حقيقية')
        print(f'   REASON: {e}')
        print(f'   SAFE FALLBACK: لا نتائج. شغّل --fixture لاختبار الآلية فقط،')
        print(f'   أو استخدم VPN. لا تُولَّد أرقام بديلة.')
        json.dump({'status': 'BLOCKED', 'reason': str(e)},
                  open(f'{REPORTS}/validation_report.json', 'w'), ensure_ascii=False, indent=2)
        return 2

    cfg = Config()
    print(f'\n  المصدر: {source}')
    print(f'  الشموع: {len(data)} | جودة البيانات: {dq:.3f}')
    print(f'  نسخة الاستراتيجية: {cfg.version} | بصمة الإعدادات: {cfg.fingerprint()}')

    results = {'meta': {'symbol': a.symbol, 'interval': a.interval, 'days': a.days,
                        'bars': len(data), 'source': source, 'data_quality': round(dq, 4),
                        'strategy_version': cfg.version,
                        'config_fingerprint': cfg.fingerprint(),
                        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'is_real_market_data': not a.fixture}}
    tests = {}

    # 1) look-ahead
    print('\n[1] اختبار Look-Ahead')
    lk_i = check_arrays(lambda x: IndicatorEngine().compute(x, key=None), data)
    lk_s = check_arrays(lambda x: {k: v for k, v in StructureEngine()
                                   .compute(x.high, x.low, x.close, x.open_time).items()
                                   if isinstance(v, np.ndarray)}, data)
    def dec(dd, i):
        s = SignalEngine().evaluate(dd, i, data_quality=0.95)
        return {'d': s.decision, 'sc': float(s.score), 'st': s.stars}
    lk_d = check_decisions(dec, data, sample=20)
    for nm, r in [('المؤشرات', lk_i), ('هيكل السوق', lk_s), ('القرارات', lk_d)]:
        print(f'    {nm:14s} {"PASS" if r["passed"] else "FAIL"}')
    tests['lookahead'] = all(r['passed'] for r in (lk_i, lk_s, lk_d))
    results['lookahead'] = {'indicators': lk_i['passed'], 'structure': lk_s['passed'],
                            'decisions': lk_d['passed']}

    # 2) backtest
    print('\n[2] الباكتست (بتكاليف واقعية)')
    bt = BacktestEngine(cfg, SignalEngine(cfg), a.capital)
    res = bt.run(data, data_quality=max(dq, 0.5))
    m = res.metrics
    print(f'    صفقات {m["total_trades"]} | فوز {m["win_rate"]}% | PF {m["profit_factor"]}')
    print(f'    إجمالي {m["gross_return_pct"]}% − رسوم ${m["total_fees"]} '
          f'− انزلاق ${m["total_slippage"]} = صافي {m["net_return_pct"]}%')
    print(f'    أقصى تراجع {m["max_drawdown_pct"]}% | Sharpe {m["sharpe"]} | '
          f'Sortino {m["sortino"]} | Calmar {m["calmar"]}')
    results['backtest'] = {'metrics': m, 'rejections': res.rejections,
                           'signals_evaluated': res.signals_evaluated,
                           'signals_buy': res.signals_buy,
                           'open_at_end_closed': res.open_at_end_closed}
    tests['backtest_runs'] = True

    # 3) walk-forward
    print('\n[3] Walk-Forward (تحسين → تجميد → OOS)')
    vc = ValidationConfig()
    if len(data) < vc.wf_train_bars + vc.wf_test_bars:
        vc = ValidationConfig(wf_train_bars=max(400, int(len(data) * 0.4)),
                              wf_test_bars=max(150, int(len(data) * 0.2)),
                              wf_step_bars=max(150, int(len(data) * 0.2)))
    wf = walk_forward.run(data, cfg, vc, capital=a.capital, verbose=False)
    print(f'    نوافذ {wf["n_windows"]} | صفقات OOS {wf["oos_trades"]} | '
          f'PF مجمّع {wf.get("oos_aggregate_pf")}')
    print(f'    نوافذ رابحة {wf.get("profitable_windows")} | ثبات {wf.get("consistency")}')
    print(f'    الحكم: {wf["verdict"]} — {wf["verdict_text"]}')
    results['walk_forward'] = wf
    tests['walk_forward'] = wf['verdict'] == 'PASS'

    # 4) calibration
    print('\n[4] معايرة الاحتمال')
    cal, rep = build_from_backtest(res.trades)
    if cal.is_fitted:
        print(f'    {rep.method} | n={rep.n_samples} | '
              f'Brier {rep.brier_raw:.4f} → {rep.brier_calibrated:.4f}')
        tests['calibration'] = bool(rep.improved and rep.brier_calibrated <= vc.max_brier_score)
    else:
        print(f'    غير معاير — {rep.method} (n={rep.n_samples})')
        print(f'    النظام لن يعرض أي احتمال. هذا مقصود.')
        tests['calibration'] = False
    results['calibration'] = rep.to_dict()

    # 5) validation gate
    print('\n[5] بوابة التحقق')
    n_oos = wf['oos_trades']; pf = wf.get('oos_aggregate_pf')
    dd = m['max_drawdown_pct']
    gate = [
        (n_oos >= vc.min_trades, f'صفقات OOS {n_oos} ≥ {vc.min_trades}'),
        (pf is not None and pf >= vc.min_profit_factor,
         f'PF خارج العينة {pf} ≥ {vc.min_profit_factor}'),
        (dd <= vc.max_drawdown_pct, f'أقصى تراجع {dd}% ≤ {vc.max_drawdown_pct}%'),
        (wf.get('consistency', 0) >= vc.min_wf_consistency,
         f'ثبات {wf.get("consistency")} ≥ {vc.min_wf_consistency}'),
        (cal.is_fitted, 'الاحتمال معاير'),
        (dq >= vc.min_data_quality, f'جودة البيانات {dq:.3f} ≥ {vc.min_data_quality}'),
    ]
    for ok, txt in gate:
        print(f'    {"PASS" if ok else "FAIL"}  {txt}')
    passed = all(ok for ok, _ in gate)
    results['validation_gate'] = {'passed': passed,
                                  'checks': [{'passed': ok, 'check': t} for ok, t in gate],
                                  'thresholds': vc.__dict__}
    tests['validation_gate'] = passed

    print('\n' + '=' * 66)
    for k, v in tests.items():
        print(f'  {"PASS" if v else "FAIL"}  {k}')
    print('=' * 66)
    if passed:
        print('  ✅ اجتاز التحقق. الخطوة التالية: Testnet — وليس مالاً حقيقياً بعد.')
    else:
        print('  ❌ STRATEGY FAILED VALIDATION')
        print('     لا تربط هذا بحساب حقيقي. النتيجة صادقة ومقبولة —')
        print('     معظم الاستراتيجيات البسيطة تفشل عند اختبارها بأمانة.')
    if a.fixture:
        print('\n  ⚠️  هذه نتائج FIXTURE اصطناعية — لا تدل على أداء سوق حقيقي.')
    print('=' * 66)

    results['tests'] = tests
    json.dump(results, open(f'{REPORTS}/validation_report.json', 'w'),
              ensure_ascii=False, indent=2, default=str)
    json.dump(res.trades, open(f'{REPORTS}/backtest_trades.json', 'w'),
              ensure_ascii=False, indent=2, default=str)
    print(f'\n  التقارير: reports/validation_report.json, reports/backtest_trades.json')
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
