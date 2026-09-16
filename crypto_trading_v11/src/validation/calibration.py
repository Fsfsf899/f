"""
معايرة الاحتمال — البند 7.
==========================
قبل هذا الملف، النظام لا يملك أي احتمال. النقاط الخام ليست احتمالاً،
وعرضها كنسبة نجاح كان الخلل الأساسي في V3.

المعايرة تحوّل النقاط الخام إلى احتمال بمعنى حقيقي:
"من بين الإشارات التي حصلت على هذه النقاط تاريخياً، ربح X%".

طريقتان:
  Platt   : انحدار لوجستي أحادي — يحتاج عينة أقل، يفترض شكلاً سيغمياً
  Isotonic: رتيب غير معلمي — أدق مع عينة كبيرة، لا يفترض شكلاً

المقاييس:
  Brier Score      : متوسط مربع الخطأ (أقل = أفضل، 0.25 = تخمين عشوائي)
  Calibration Error: |الاحتمال المتوقَّع − التكرار الفعلي| بالمتوسط
  Reliability      : جدول لكل شريحة احتمال

⚠️ تُدرَّب على TRAIN فقط. استخدام OOS في المعايرة = تسريب.
"""
import numpy as np
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple


@dataclass
class CalibrationReport:
    method: str
    n_samples: int
    brier_raw: float
    brier_calibrated: float
    calibration_error_raw: float
    calibration_error_calibrated: float
    base_rate: float
    reliability: List[Dict] = field(default_factory=list)
    improved: bool = False

    def to_dict(self) -> Dict:
        return {k: (round(v, 5) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def brier(prob: np.ndarray, outcome: np.ndarray) -> float:
    return float(np.mean((np.asarray(prob, float) - np.asarray(outcome, float)) ** 2))


def reliability_table(prob: np.ndarray, outcome: np.ndarray,
                      n_bins: int = 10) -> Tuple[List[Dict], float]:
    """جدول الموثوقية + متوسط خطأ المعايرة الموزون."""
    prob = np.asarray(prob, float); outcome = np.asarray(outcome, float)
    edges = np.linspace(0, 1, n_bins + 1)
    rows, err, total = [], 0.0, len(prob)
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        m = (prob >= lo) & (prob < hi if i < n_bins - 1 else prob <= hi)
        k = int(m.sum())
        if k == 0:
            continue
        pred = float(prob[m].mean())
        actual = float(outcome[m].mean())
        rows.append({'bin': f'{lo:.1f}-{hi:.1f}', 'n': k,
                     'predicted': round(pred, 4), 'actual': round(actual, 4),
                     'gap': round(actual - pred, 4)})
        err += abs(actual - pred) * k
    return rows, (err / total if total else 0.0)


class ProbabilityCalibrator:
    """
    is_fitted = False  ⇒  النظام لا يعرض أي احتمال إطلاقاً.
    هذا مقصود: احتمال غير معاير أسوأ من لا احتمال.
    """

    def __init__(self, method: str = 'isotonic'):
        assert method in ('isotonic', 'platt')
        self.method = method
        self.is_fitted = False
        self._model = None
        self._base_rate = 0.5
        self.report: Optional[CalibrationReport] = None

    def fit(self, raw_scores, outcomes, min_samples: int = 50) -> CalibrationReport:
        x = np.asarray(raw_scores, float).ravel()
        y = np.asarray(outcomes, float).ravel()
        ok = np.isfinite(x) & np.isfinite(y)
        x, y = x[ok], y[ok]

        if len(x) < min_samples or len(np.unique(y)) < 2:
            self.is_fitted = False
            self.report = CalibrationReport(
                method='INSUFFICIENT_DATA', n_samples=len(x),
                brier_raw=brier(x, y) if len(x) else float('nan'),
                brier_calibrated=float('nan'),
                calibration_error_raw=float('nan'),
                calibration_error_calibrated=float('nan'),
                base_rate=float(y.mean()) if len(y) else float('nan'))
            return self.report

        self._base_rate = float(y.mean())

        if self.method == 'isotonic':
            from sklearn.isotonic import IsotonicRegression
            m = IsotonicRegression(out_of_bounds='clip', y_min=0.0, y_max=1.0)
            m.fit(x, y)
        else:
            from sklearn.linear_model import LogisticRegression
            m = LogisticRegression(C=1e6, solver='lbfgs')
            m.fit(x.reshape(-1, 1), y.astype(int))

        self._model = m
        self.is_fitted = True

        p = self.predict(x)
        rel, ce_cal = reliability_table(p, y)
        _, ce_raw = reliability_table(x, y)
        b_raw, b_cal = brier(x, y), brier(p, y)

        self.report = CalibrationReport(
            method=self.method, n_samples=len(x),
            brier_raw=b_raw, brier_calibrated=b_cal,
            calibration_error_raw=ce_raw, calibration_error_calibrated=ce_cal,
            base_rate=self._base_rate, reliability=rel,
            improved=bool(b_cal <= b_raw))
        return self.report

    def predict(self, raw_scores) -> np.ndarray:
        if not self.is_fitted:
            raise RuntimeError("غير معاير — لا يجوز إنتاج احتمال")
        x = np.asarray(raw_scores, float).ravel()
        p = (self._model.predict(x) if self.method == 'isotonic'
             else self._model.predict_proba(x.reshape(-1, 1))[:, 1])
        return np.clip(p, 0.01, 0.99)      # لا 0% ولا 100% — لا يقين مطلق

    def predict_one(self, raw_score: float) -> float:
        return float(self.predict([raw_score])[0])


def build_from_backtest(trades: List[Dict], method: str = 'isotonic'
                        ) -> Tuple[ProbabilityCalibrator, CalibrationReport]:
    """
    يبني معايراً من صفقات فعلية.
    المدخل: raw_probability المسجّل عند الدخول. المخرج: هل ربحت؟
    """
    xs = [t['raw_probability'] for t in trades if t.get('raw_probability') is not None]
    ys = [1.0 if t['pnl'] > 0 else 0.0
          for t in trades if t.get('raw_probability') is not None]
    cal = ProbabilityCalibrator(method)
    rep = cal.fit(xs, ys)
    return cal, rep
