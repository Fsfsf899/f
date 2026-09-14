"""
Walk-Forward حقيقي — البند 6.
==============================
الفرق عن V3: هناك كانت مجرد تقسيم للبيانات وتشغيل نفس المعاملات.
هنا دورة تحسين/تجميد/اختبار فعلية:

    TRAIN → بحث في شبكة المعاملات → تجميد الأفضل
          → اختبار OOS ببيانات لم تُرَ → تقدّم النافذة → إعادة

ضمانات:
  • بيانات OOS لا تدخل التحسين إطلاقاً
  • النافذة التالية تُدرَّب على بيانات جديدة (rolling)
  • تداخل TRAIN/TEST يُفحص برمجياً ويُرفض
"""
import numpy as np
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable, Any
from copy import deepcopy
from ..core.config import Config, ValidationConfig
from ..data.types import OHLCV
from ..backtest.engine import BacktestEngine
from ..signals.engine import SignalEngine


@dataclass
class Window:
    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    best_params: Dict[str, Any] = field(default_factory=dict)
    train_metrics: Dict = field(default_factory=dict)
    test_metrics: Dict = field(default_factory=dict)
    n_candidates: int = 0

    def overlaps(self) -> bool:
        """يجب أن يكون False دائماً — تداخل = تسريب."""
        return self.test_start <= self.train_end


DEFAULT_GRID = {
    'min_score': [3.0, 4.0, 5.0],
    'atr_stop_mult': [1.5, 1.8, 2.2],
    'atr_target_mult': [3.0, 3.6, 4.5],
}

# شبكات مصغَّرة ومبرَّرة لمعاملات Breakout/Pullback — القسم الرابع.
# لا بحث في كل المعاملات دفعة واحدة؛ اثنان لكل نموذج، ثلاث قيم لكل
# واحد (9 تركيبات) — يكفي لرصد Parameter Plateau بلا انفجار حسابي.
BREAKOUT_GRID = {
    'breakout.min_distance_atr': [0.05, 0.10, 0.20],
    'breakout.max_extension_atr': [1.0, 1.5, 2.0],
}
PULLBACK_GRID = {
    'pullback.zone_atr': [0.35, 0.50, 0.65],
    'pullback.max_depth_atr': [1.0, 1.5, 2.0],
}


def _apply(base: Config, params: Dict) -> Config:
    """
    يدعم مسارات منقوطة (`breakout.min_distance_atr`) عبر أي قسم
    فرعي من Config، بلا تخصيص لـ cfg.signal فقط كما كان. كل مفتاح
    بلا نقطة يُفسَّر كـ `signal.<key>` — يحافظ على DEFAULT_GRID القديم
    دون تعديله (توافق خلفي).

    ⚠️ مسار غير موجود **يرفع خطأ فوراً** — لا تجاهل صامت لمعامل غير
    مُطبَّق مع الاستمرار كأن شيئاً لم يحدث (هذا بالضبط ما كان يحدث
    سابقاً: breakout.*/pullback.* كانت تُتجاهَل بصمت لأن hasattr كانت
    تُفحَص على cfg.signal فقط دائماً).
    """
    cfg = deepcopy(base)
    for path, v in params.items():
        full_path = path if '.' in path else f'signal.{path}'
        parts = full_path.split('.')
        obj = cfg
        for part in parts[:-1]:
            if not hasattr(obj, part):
                raise ValueError(
                    f"مسار معامل غير موجود: '{full_path}' — "
                    f"'{part}' ليس حقلاً في {type(obj).__name__}")
            obj = getattr(obj, part)
        leaf = parts[-1]
        if not hasattr(obj, leaf):
            raise ValueError(
                f"مسار معامل غير موجود: '{full_path}' — "
                f"'{leaf}' ليس حقلاً في {type(obj).__name__}")
        setattr(obj, leaf, v)
    return cfg


def _grid(grid: Dict[str, List]) -> List[Dict]:
    keys = list(grid)
    out = [{}]
    for k in keys:
        out = [dict(d, **{k: v}) for d in out for v in grid[k]]
    return out


def _objective(m: Dict, min_trades: int = 5) -> float:
    """
    دالة الهدف للتحسين داخل TRAIN فقط.
    تعاقب قلة الصفقات لتجنب اختيار معاملات مبنية على 2-3 صفقات.
    """
    n = m.get('total_trades', 0)
    if n < min_trades:
        return -1e9
    pf = m.get('profit_factor')
    if pf is None:
        pf = 3.0 if m.get('net_profit', 0) > 0 else 0.0
    dd = max(m.get('max_drawdown_pct', 0.0), 1.0)
    return float(m.get('net_return_pct', 0.0)) / dd + min(pf, 5.0) * 0.5


def run(data: OHLCV, base_cfg: Optional[Config] = None,
        val_cfg: Optional[ValidationConfig] = None,
        grid: Optional[Dict[str, List]] = None,
        capital: float = 10_000.0,
        data_quality: float = 0.95,
        optimize: bool = True,
        verbose: bool = True,
        engine_factory: Optional[Callable[[Config], Any]] = None) -> Dict:
    """
    `engine_factory(cfg) -> engine` — يبني الكائن الذي يُمرَّر إلى
    `BacktestEngine`. الافتراضي `SignalEngine(cfg)` (Baseline، توافق
    خلفي كامل). لبحث Breakout/Pullback فعلياً، مرِّر مثلاً:

        engine_factory=lambda c: IsolatedModelAdapter(c, BreakoutEntryModel(c))

    بدون هذا، كانت `run()` تُبني `SignalEngine(cfg)` دائماً بصرف
    النظر عن أي معامل breakout.*/pullback.* في `params` — فحتى مع
    إصلاح `_apply()` لتطبيق المعامل صحيحاً في `cfg`، كانت النتيجة لا
    تزال تقيس Baseline فعلياً لأن المحرك المُستخدَم في المحاكاة لم
    يكن يقرأ ذلك الحقل مطلقاً.
    """
    base_cfg = base_cfg or Config()
    vc = val_cfg or ValidationConfig()
    grid = grid or DEFAULT_GRID
    combos = _grid(grid) if optimize else [{}]
    factory = engine_factory or (lambda c: SignalEngine(c))

    n = len(data)
    tr, te, st = vc.wf_train_bars, vc.wf_test_bars, vc.wf_step_bars
    windows: List[Window] = []

    start = 0
    wi = 0
    while start + tr + te <= n:
        w = Window(wi, start, start + tr - 1, start + tr, start + tr + te - 1)
        assert not w.overlaps(), "تداخل TRAIN/TEST — تسريب"

        train = data.slice(w.train_start, w.train_end + 1)
        # نافذة الاختبار تُمرَّر مع سابقة كافية للإحماء، لكن التقييم
        # يبدأ فقط بعد بداية الاختبار الحقيقية.
        warm = max(base_cfg.signal.warmup_bars + 60, 260)
        te_from = max(0, w.test_start - warm)
        test = data.slice(te_from, w.test_end + 1)

        best, best_obj, best_m = {}, -np.inf, {}
        for params in combos:
            cfg = _apply(base_cfg, params)   # يرفع خطأ فوراً إن كان مساراً غير صحيح
            bt = BacktestEngine(cfg, factory(cfg), capital)
            m = bt.run(train, data_quality=data_quality).metrics
            ob = _objective(m)
            if ob > best_obj:
                best, best_obj, best_m = params, ob, m

        # ── المعاملات مجمّدة الآن. اختبار OOS.
        cfg = _apply(base_cfg, best)
        bt = BacktestEngine(cfg, factory(cfg), capital)
        res = bt.run(test, data_quality=data_quality)

        w.best_params = best
        w.train_metrics = best_m
        w.test_metrics = res.metrics
        w.n_candidates = len(combos)
        windows.append(w)

        if verbose:
            tm, sm = best_m, res.metrics
            print(f"  نافذة {wi+1}: train[{w.train_start}:{w.train_end}] "
                  f"test[{w.test_start}:{w.test_end}] | params={best}")
            print(f"     TRAIN: {tm.get('total_trades',0)} صفقة, "
                  f"PF={tm.get('profit_factor')}, ret={tm.get('net_return_pct')}%")
            print(f"     OOS  : {sm.get('total_trades',0)} صفقة, "
                  f"PF={sm.get('profit_factor')}, ret={sm.get('net_return_pct')}%")

        start += st
        wi += 1

    oos = [w.test_metrics for w in windows if w.test_metrics.get('total_trades', 0) > 0]
    total_oos_trades = sum(m['total_trades'] for m in oos)

    if not oos:
        return {'windows': [w.__dict__ for w in windows], 'n_windows': len(windows),
                'oos_trades': 0, 'verdict': 'NO_OOS_TRADES',
                'verdict_text': 'لا صفقات خارج العينة — لا يمكن الحكم',
                'params_grid_paths': list(grid.keys())}

    rets = [m['net_return_pct'] for m in oos]
    pfs = [m['profit_factor'] for m in oos if m['profit_factor'] is not None]
    profitable = sum(1 for r in rets if r > 0)
    consistency = profitable / len(oos)
    expectancies = [m['expectancy'] for m in oos if m.get('expectancy') is not None]

    gp = sum(m['gross_profit'] for m in oos)
    gl = sum(m['gross_loss'] for m in oos)
    agg_pf = round(gp / gl, 3) if gl > 0 else None

    def _median(xs):
        if not xs:
            return None
        s = sorted(xs)
        return s[len(s) // 2]

    if total_oos_trades < vc.min_trades:
        verdict, text = 'INSUFFICIENT_SAMPLE', (
            f'{total_oos_trades} صفقة OOS < {vc.min_trades} — العينة لا تكفي لأي استنتاج')
    elif agg_pf is not None and agg_pf >= vc.min_profit_factor and consistency >= vc.min_wf_consistency:
        verdict, text = 'PASS', 'اجتاز عتبات الثبات والربحية خارج العينة'
    elif agg_pf is not None and agg_pf >= 1.0:
        verdict, text = 'MARGINAL', 'فوق التعادل لكن دون عتبة الأمان — لا يصلح لمال حقيقي'
    else:
        verdict, text = 'FAIL', 'STRATEGY FAILED VALIDATION — خاسرة خارج العينة'

    return {
        'windows': [w.__dict__ for w in windows],
        'n_windows': len(windows),
        'oos_trades': total_oos_trades,
        'oos_aggregate_pf': agg_pf,
        'oos_avg_return_pct': round(float(np.mean(rets)), 3),
        'oos_std_return_pct': round(float(np.std(rets)), 3),
        'oos_avg_win_rate': round(float(np.mean([m['win_rate'] for m in oos])), 2),
        'oos_max_dd_pct': round(float(max(m['max_drawdown_pct'] for m in oos)), 3),
        'profitable_windows': f'{profitable}/{len(oos)}',
        'consistency': round(consistency, 3),
        'params_stability': _stability(windows),
        'verdict': verdict, 'verdict_text': text,
        'thresholds': {'min_trades': vc.min_trades,
                       'min_profit_factor': vc.min_profit_factor,
                       'min_consistency': vc.min_wf_consistency},
        # البند 4.8 — أي مسارات معاملات طُبِّقت فعلياً في هذا البحث
        'params_grid_paths': list(grid.keys()),
        # مؤشرات القسم الرابع الإلزامية — لا تعتمد على Aggregate PF وحده
        'oos_window_count': len(oos),
        'oos_positive_window_count': profitable,
        'oos_positive_window_rate': round(consistency, 3),
        'oos_pf_median': _median(pfs),
        'oos_pf_min': min(pfs) if pfs else None,
        'oos_pf_max': max(pfs) if pfs else None,
        'oos_expectancy_median': _median(expectancies),
        'oos_max_drawdown': round(float(max(m['max_drawdown_pct'] for m in oos)), 3),
        'oos_consistency_score': round(consistency, 3),
    }


def _stability(windows: List[Window]) -> Dict:
    """هل يختار المحسِّن نفس المعاملات كل نافذة؟ التذبذب = عدم استقرار."""
    if not windows:
        return {}
    keys = set()
    for w in windows:
        keys |= set(w.best_params)
    out = {}
    for k in keys:
        vals = [w.best_params.get(k) for w in windows]
        uniq = len(set(map(str, vals)))
        out[k] = {'values': vals, 'unique': uniq,
                  'stable': uniq <= max(1, len(windows) // 3)}
    return out
