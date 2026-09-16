"""
اختبار انحياز النظر للأمام — البند 2.

⚠️ ملاحظة: هذا الملف يستخدم np.random عمداً — لتشويه البيانات
المستقبلية أثناء الاختبار. إنه أداة تحقّق، وليس مصدر بيانات سوق،
ولا يُستدعى من أي مسار يولّد إشارة أو ينفّذ صفقة.
======================================
المبدأ: احسب المؤشر/الإشارة على البيانات الكاملة، ثم غيّر كل البيانات
بعد نقطة زمنية معينة تغييراً جذرياً، وأعد الحساب.

كل قيمة قبل نقطة القطع يجب أن تبقى **متطابقة تماماً**.
أي اختلاف = تسريب معلومة مستقبلية = FAIL.
"""
import numpy as np
from typing import Callable, Dict, List, Optional
from ..data.types import OHLCV


def mutate_future(data: OHLCV, cut: int, seed: int = 1234,
                  mode: str = "shock") -> OHLCV:
    """
    يغيّر البيانات بعد الفهرس cut تغييراً جذرياً مع الحفاظ على صلاحية OHLC.
    البيانات حتى cut (شاملاً) تبقى كما هي بايت ببايت.
    """
    rng = np.random.default_rng(seed)
    o, h, l, c, v = (data.open.copy(), data.high.copy(), data.low.copy(),
                     data.close.copy(), data.volume.copy())
    n = len(data)
    if cut >= n - 1:
        return data

    k = n - cut - 1
    if mode == "shock":
        factor = 1.0 + rng.normal(0.35, 0.25, k)     # انهيار/انفجار عنيف
    elif mode == "flat":
        factor = np.ones(k) * 0.5
    else:
        factor = 1.0 + rng.uniform(-0.5, 0.5, k)

    base = c[cut]
    new_c = np.maximum(base * np.cumprod(factor), 1e-6)
    c[cut+1:] = new_c
    o[cut+1:] = np.concatenate([[c[cut]], new_c[:-1]])
    spread = np.abs(new_c) * 0.01
    h[cut+1:] = np.maximum(o[cut+1:], c[cut+1:]) + spread
    l[cut+1:] = np.minimum(o[cut+1:], c[cut+1:]) - spread
    v[cut+1:] = np.maximum(v[cut+1:] * rng.uniform(0.1, 5.0, k), 1.0)

    return OHLCV(data.symbol, data.interval, data.open_time.copy(),
                 o, h, l, c, v, source="mutated", fetched_at=data.fetched_at)


def check_arrays(fn: Callable[[OHLCV], Dict[str, np.ndarray]],
                 data: OHLCV, cut: Optional[int] = None,
                 tol: float = 1e-9, seed: int = 1234) -> Dict:
    """
    fn: دالة تأخذ OHLCV وتُرجع قاموس مصفوفات (مثل IndicatorEngine.compute).
    يفحص كل مصفوفة على حدة.
    """
    n = len(data)
    cut = cut if cut is not None else int(n * 0.7)

    base = fn(data)
    mut = fn(mutate_future(data, cut, seed))

    failures: List[Dict] = []
    checked = 0
    for name, a in base.items():
        b = mut.get(name)
        if b is None or len(a) != len(b):
            failures.append({'name': name, 'reason': 'مصفوفة مفقودة أو بطول مختلف'})
            continue
        x, y = np.asarray(a, float)[:cut+1], np.asarray(b, float)[:cut+1]
        both_nan = np.isnan(x) & np.isnan(y)
        diff = np.abs(np.where(both_nan, 0.0, np.nan_to_num(x) - np.nan_to_num(y)))
        nan_mismatch = np.isnan(x) != np.isnan(y)
        bad = (diff > tol) | nan_mismatch
        checked += 1
        if bad.any():
            first = int(np.argmax(bad))
            failures.append({'name': name, 'n_diff': int(bad.sum()),
                             'first_index': first, 'max_diff': float(diff.max())})

    return {'passed': len(failures) == 0, 'cut': cut, 'n_bars': n,
            'arrays_checked': checked, 'failures': failures}


def check_decisions(decide: Callable[[OHLCV, int], dict],
                    data: OHLCV, cut: Optional[int] = None,
                    sample: int = 60, seed: int = 1234,
                    keys: Optional[List[str]] = None) -> Dict:
    """
    decide: دالة تأخذ (OHLCV, index) وتُرجع قرار عند تلك الشمعة.
    يقارن القرارات عند فهارس <= cut قبل التغيير وبعده.
    """
    n = len(data)
    cut = cut if cut is not None else int(n * 0.7)
    mutated = mutate_future(data, cut, seed)

    start = max(210, int(cut * 0.5))
    if cut <= start:
        return {'passed': True, 'cut': cut, 'checked': 0, 'failures': [],
                'note': 'نطاق الفحص قصير جداً'}
    idxs = np.unique(np.linspace(start, cut, min(sample, cut - start + 1)).astype(int))

    failures = []
    for i in idxs:
        a, b = decide(data, int(i)), decide(mutated, int(i))
        ks = keys or sorted(set(a) | set(b))
        for k in ks:
            va, vb = a.get(k), b.get(k)
            if isinstance(va, float) and isinstance(vb, float):
                same = (np.isnan(va) and np.isnan(vb)) or abs(va - vb) < 1e-9
            else:
                same = va == vb
            if not same:
                failures.append({'index': int(i), 'key': k, 'base': va, 'mutated': vb})
                break

    return {'passed': len(failures) == 0, 'cut': cut,
            'checked': len(idxs), 'failures': failures[:10]}
