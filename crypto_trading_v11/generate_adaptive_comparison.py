#!/usr/bin/env python3
"""
مقارنة قبل/بعد الإدارة التكيّفية — Part B البندان 32 و33.
=========================================================
يُشغِّل نفس الباكتست بنفس البيانات ونفس البذرة مرّتين: بالطبقة
معطَّلة ثم مفعَّلة، ويطبع الفروق ومقاييس كل حالة سوق.

⚠️ لا يختار فائزاً ولا يدّعي تحسّناً (البند 31). يطبع الأرقام كما هي.
البيانات هنا اصطناعية ما لم يُمرَّر مصدر حقيقي — والأرقام الناتجة
عنها لا تُثبت أي أداء، إنما تُثبت أن الآلية تعمل وتُقاس.
"""
import argparse
import copy
import json
import sys
from collections import Counter, defaultdict

sys.path.insert(0, '.')

import numpy as np

from src.backtest.engine import BacktestEngine
from src.core.config import Config
from src.market.regime import detect_series
from src.trade_management.hysteresis import confirm_series


def base_cfg() -> Config:
    c = Config()
    c.signal.min_score = 1.0
    c.no_trade.min_data_quality = 0.5
    c.no_trade.require_btc_ok = False
    return c


def extra_metrics(result, data, cfg) -> dict:
    """المقاييس التي يطلبها البند 32 ولا يحسبها محرك المقاييس أصلاً."""
    tr = result.trades
    n = len(tr) or 1
    reasons = Counter(t['exit_reason'] for t in tr)
    mfe = [t.get('mfe_pct') or 0.0 for t in tr]
    mae = [t.get('mae_pct') or 0.0 for t in tr]
    return {
        'stop_out_rate': round(reasons.get('STOP_LOSS', 0) / n * 100, 2),
        'take_profit_rate': round(reasons.get('TAKE_PROFIT', 0) / n * 100, 2),
        'break_even_rate': round(reasons.get('BREAK_EVEN', 0) / n * 100, 2),
        'trailing_rate': round(reasons.get('TRAILING_STOP', 0) / n * 100, 2),
        'stagnation_rate': round(reasons.get('STAGNATION_EXIT', 0) / n * 100, 2),
        'avg_mfe_pct': round(float(np.mean(mfe)) if mfe else 0.0, 4),
        'avg_mae_pct': round(float(np.mean(mae)) if mae else 0.0, 4),
        'exit_reasons': dict(reasons),
    }


def by_regime(result, data, cfg) -> dict:
    """
    أداء كل حالة سوق (البند 33) — الحالة تُقرأ عند شمعة **الدخول**،
    مثبَّتة بنفس التهدئة، فلا يُنسب أداء صفقة إلى حالة لم تكن قائمة
    حين فُتحت.
    """
    raw = detect_series(data.close, cfg.regime, None, data.interval)
    conf = confirm_series([str(x) for x in raw], cfg.adaptive)
    buckets = defaultdict(list)
    for t in result.trades:
        i = t.get('entry_index')
        if i is None or i >= len(conf):
            continue
        buckets[conf[i]].append(t['pnl'])
    out = {}
    for reg, pnls in sorted(buckets.items()):
        a = np.array(pnls, dtype=float)
        wins = a[a > 0]
        losses = a[a < 0]
        out[reg] = {
            'trades': len(a),
            'win_rate': round(float((a > 0).mean() * 100), 2),
            'net_pnl': round(float(a.sum()), 2),
            'profit_factor': (round(float(wins.sum() / -losses.sum()), 3)
                              if losses.size and losses.sum() else None),
        }
    return out


def run(data, cfg, capital) -> dict:
    r = BacktestEngine(cfg, initial_capital=capital).run(data, data_quality=0.95)
    m = dict(r.metrics)
    m.update(extra_metrics(r, data, cfg))
    return {'metrics': m, 'by_regime': by_regime(r, data, cfg),
            'fingerprint': r.config_fingerprint}


KEYS = [('total_trades', 'عدد الصفقات', '{:.0f}'),
        ('win_rate', 'نسبة الربح %', '{:.2f}'),
        ('net_profit', 'صافي الربح', '{:+.2f}'),
        ('profit_factor', 'عامل الربح', '{:.3f}'),
        ('max_drawdown_pct', 'أقصى انخفاض %', '{:.2f}'),
        ('expectancy', 'التوقّع لكل صفقة', '{:+.4f}'),
        ('avg_holding_bars', 'متوسط مدة الحفظ', '{:.1f}'),
        ('total_fees', 'الرسوم', '{:.2f}'),
        ('total_slippage', 'الانزلاق', '{:.2f}'),
        ('stop_out_rate', 'خروج بالوقف %', '{:.2f}'),
        ('take_profit_rate', 'خروج بالهدف %', '{:.2f}'),
        ('break_even_rate', 'خروج بالتعادل %', '{:.2f}'),
        ('trailing_rate', 'خروج بالتتبّع %', '{:.2f}'),
        ('stagnation_rate', 'خروج بالركود %', '{:.2f}'),
        ('avg_mfe_pct', 'متوسط MFE %', '{:+.3f}'),
        ('avg_mae_pct', 'متوسط MAE %', '{:+.3f}')]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bars', type=int, default=6000)
    ap.add_argument('--seed', type=int, default=11)
    ap.add_argument('--interval', default='1h')
    ap.add_argument('--capital', type=float, default=10_000.0)
    ap.add_argument('--json', default='')
    a = ap.parse_args()

    from tests.fixtures import make_fixture
    data = make_fixture(a.bars, a.interval, seed=a.seed)

    off = base_cfg()
    on = copy.deepcopy(off); on.adaptive.enabled = True

    # التتبّع والركود معطَّلان في الإعداد الافتراضي، فلا يُطلِق
    # المقارنةُ الأساسيةُ أيّاً منهما. سيناريو "الكل" يُشغّلهما صراحةً
    # كي لا تُقرأ نسبة 0% على أنها ميزة لا تعمل.
    full = copy.deepcopy(on)
    full.signal.trailing_stop_enabled = True
    full.adaptive.max_trade_duration_hours = 6.0
    full.adaptive.min_mfe_pct = 1.0
    full.adaptive.min_progress_pct = 0.5

    r_off = run(data, off, a.capital)
    r_on = run(data, on, a.capital)
    r_full = run(data, full, a.capital)

    print('=' * 74)
    print('مقارنة: قبل الإدارة التكيّفية vs بعدها')
    print(f'بيانات اصطناعية — {a.bars} شمعة {a.interval}، بذرة {a.seed}')
    print('⚠️ الأرقام تُثبت عمل الآلية، ولا تُثبت أي ربحية.')
    print('=' * 74)
    print(f'{"المقياس":<24}{"قبل":>15}{"بعد":>15}{"+تتبّع/ركود":>15}')
    print('-' * 74)
    for key, label, fmt in KEYS:
        a_v = r_off['metrics'].get(key)
        b_v = r_on['metrics'].get(key)
        c_v = r_full['metrics'].get(key)
        if a_v is None or b_v is None or c_v is None:
            continue
        try:
            print(f'{label:<24}{fmt.format(a_v):>15}{fmt.format(b_v):>15}'
                  f'{fmt.format(c_v):>15}')
        except (TypeError, ValueError):
            continue

    for title, res in (('قبل', r_off), ('بعد', r_on),
                       ('+تتبّع/ركود', r_full)):
        print(f'\nأداء كل حالة سوق — {title}:')
        if not res['by_regime']:
            print('  (لا صفقات مصنَّفة)')
        for reg, v in res['by_regime'].items():
            print(f"  {reg:<20} صفقات={v['trades']:<5} ربح%={v['win_rate']:<7}"
                  f" صافي={v['net_pnl']:<10} PF={v['profit_factor']}")

    if a.json:
        with open(a.json, 'w') as f:
            json.dump({'before': r_off, 'after': r_on,
                       'after_all_features': r_full,
                       'data': {'bars': a.bars, 'seed': a.seed,
                                'interval': a.interval,
                                'synthetic': True}}, f, indent=2,
                      ensure_ascii=False, default=str)
        print(f'\nكُتب: {a.json}')


if __name__ == '__main__':
    main()
