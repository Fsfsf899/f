import numpy as np
from final_engine import FinalBacktest, FinalSignalEngine, WalkForward
from execution_engine import CostModel

def gen_data(days=120, seed=7):
    """بيانات واقعية: اتجاهات + نطاقات + تقلبات (سوق حقيقي مختلط)"""
    rng = np.random.default_rng(seed)
    bars = days * 96
    price = 50000.0
    closes = []
    regime_len = 0
    regime = 'range'
    drift = 0.0
    vol = 0.004
    for i in range(bars):
        if regime_len <= 0:
            regime = rng.choice(['up','down','range','volatile'], p=[.28,.22,.38,.12])
            regime_len = int(rng.integers(200, 900))
            drift = {'up':0.00012,'down':-0.0001,'range':0.0,'volatile':0.0}[regime]
            vol = {'up':0.0035,'down':0.004,'range':0.0025,'volatile':0.011}[regime]
        regime_len -= 1
        price *= (1 + drift + rng.normal(0, vol))
        closes.append(price)
    c = np.array(closes)
    o = np.concatenate([[c[0]], c[:-1]])
    wick = np.abs(rng.normal(0, 0.0018, bars)) * c
    h = np.maximum(o, c) + wick
    l = np.minimum(o, c) - wick
    v = rng.lognormal(7, 0.5, bars)
    return o, h, l, c, v

o,h,l,c,v = gen_data(120)
print("="*72)
print(f"📊 بيانات الاختبار: {len(c)} شمعة (15د) ≈ 120 يوم | سوق مختلط")
print("="*72)

# مقارنة: بدون فلاتر vs مع فلاتر، وبدون تكاليف vs مع تكاليف
scenarios = [
    ("بدون فلاتر + بدون تكاليف (الوهم)",
     FinalSignalEngine(min_score=2, use_regime_filter=False, use_htf_filter=False),
     CostModel(maker_fee=0, taker_fee=0, slippage_bps=0)),
    ("بدون فلاتر + تكاليف حقيقية",
     FinalSignalEngine(min_score=2, use_regime_filter=False, use_htf_filter=False),
     CostModel()),
    ("مع كل الفلاتر + تكاليف حقيقية ⭐",
     FinalSignalEngine(min_score=4, use_regime_filter=True, use_htf_filter=True),
     CostModel()),
]

for name, eng, cm in scenarios:
    bt = FinalBacktest(10000, cost_model=cm, engine=eng)
    r = bt.run(o,h,l,c,v)
    print(f"\n▸ {name}")
    if r['total_trades'] == 0:
        print("   لا صفقات")
        continue
    print(f"   صفقات: {r['total_trades']} | فوز: {r['win_rate']}% | PF: {r['profit_factor']}")
    print(f"   العائد: {r['return_pct']}% | أقصى تراجع: {r['max_dd']}% | التوقع/صفقة: ${r['expectancy']}")

print("\n" + "="*72)
print("🔬 WALK-FORWARD VALIDATION (الاختبار الصادق)")
print("="*72)
wf = WalkForward.run(o,h,l,c,v, n_windows=4)
for w in wf['windows']:
    if w.get('total_trades',0)>0:
        print(f"  نافذة {w['window']}: {w['total_trades']} صفقة | فوز {w['win_rate']}% | عائد {w['return_pct']}% | PF {w['profit_factor']}")
    else:
        print(f"  نافذة {w['window']}: لا صفقات")
print(f"\n  متوسط العائد: {wf.get('avg_return')}% | الانحراف: ±{wf.get('std_return')}%")
print(f"  نوافذ رابحة: {wf.get('profitable_windows')}")
print(f"  الحكم: {wf.get('verdict')}")
