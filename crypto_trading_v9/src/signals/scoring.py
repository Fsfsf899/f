"""
النقاط والثقة والاحتمال — البندان 7 و8.
========================================
ثلاثة مفاهيم منفصلة تماماً. الخلط بينها كان الخلل الأكبر في V3.

SCORE
    مجموع مرجّح لأدلة داعمة. مقياس **قوة** لا احتمال.
    نطاقه مفتوح ولا يُقرأ كنسبة مئوية.

CONFIDENCE
    مدى اكتمال الأدلة وجودة البيانات — أي "كم نعرف؟"
    لا يقول شيئاً عن اتجاه السعر. إشارة بأدلة كاملة لكنها سيئة
    تحصل على ثقة عالية ونقاط منخفضة.

PROBABILITY
    احتمال النجاح المقدَّر. **لا يُشتق من عدد الشروط.**
    يأتي حصراً من معايرة على نتائج تاريخية فعلية
    (src/validation/calibration.py). قبل المعايرة تكون None،
    ويجب ألا يُعرض أي رقم.

النجوم: خريطة رتيبة صارمة. لا عملية باقٍ (%) إطلاقاً.
"""
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

# حدود النجوم — رتيبة تصاعدياً بالتعريف
STAR_THRESHOLDS: List[Tuple[float, int]] = [
    (2.0, 1),   # score <  2.0  → 1 نجمة
    (3.5, 2),   # score <  3.5  → 2
    (5.0, 3),   # score <  5.0  → 3
    (6.5, 4),   # score <  6.5  → 4
]
MAX_STARS = 5

STAR_LABEL = {1: 'ضعيف جداً', 2: 'ضعيف', 3: 'متوسط', 4: 'قوي', 5: 'قوي جداً'}


def score_to_stars(score: float) -> int:
    """
    خريطة رتيبة: score₁ ≤ score₂ ⇒ stars₁ ≤ stars₂. مضمون بالبناء.
    NaN تُعامل كأضعف حالة.
    """
    if score is None or not np.isfinite(score):
        return 1
    for threshold, stars in STAR_THRESHOLDS:
        if score < threshold:
            return stars
    return MAX_STARS


@dataclass
class Evidence:
    """دليل واحد. weight موجب = داعم، سالب = مضاد."""
    name: str
    weight: float
    present: bool
    detail: str = ''


@dataclass
class ScoreResult:
    score: float
    stars: int
    star_label: str
    confidence: float
    evidence_present: List[str] = field(default_factory=list)
    evidence_against: List[str] = field(default_factory=list)
    evidence_missing: List[str] = field(default_factory=list)
    max_possible: float = 0.0
    raw_probability: Optional[float] = None
    calibrated_probability: Optional[float] = None
    probability_source: str = 'UNCALIBRATED'

    def to_dict(self) -> Dict:
        return {
            'score': round(self.score, 3), 'stars': self.stars,
            'star_label': self.star_label,
            'confidence': round(self.confidence, 4),
            'evidence_present': self.evidence_present,
            'evidence_against': self.evidence_against,
            'evidence_missing': self.evidence_missing,
            'max_possible': round(self.max_possible, 3),
            'raw_probability': (None if self.raw_probability is None
                                else round(self.raw_probability, 4)),
            'calibrated_probability': (None if self.calibrated_probability is None
                                       else round(self.calibrated_probability, 4)),
            'probability_source': self.probability_source,
        }


def aggregate(evidence: List[Evidence], data_quality: float = 1.0,
              evaluable: Optional[List[str]] = None) -> ScoreResult:
    """
    يحوّل قائمة أدلة إلى ScoreResult.

    الثقة = (نسبة الأدلة القابلة للتقييم) × (جودة البيانات).
    ملاحظة: لا علاقة للثقة باتجاه الأدلة — فقط بمدى توفرها.
    """
    evaluable = evaluable if evaluable is not None else [e.name for e in evidence]
    ev_set = set(evaluable)

    score = 0.0
    present, against, missing = [], [], []
    max_possible = 0.0

    for e in evidence:
        if e.weight > 0:
            max_possible += e.weight
        if e.name not in ev_set:
            missing.append(e.name)
            continue
        if e.present:
            score += e.weight
            (present if e.weight > 0 else against).append(
                f"{e.name}{': ' + e.detail if e.detail else ''}")

    coverage = len(ev_set) / max(len(evidence), 1)
    confidence = float(np.clip(coverage * float(np.clip(data_quality, 0, 1)), 0, 1))

    stars = score_to_stars(score)
    return ScoreResult(score=float(score), stars=stars,
                       star_label=STAR_LABEL[stars], confidence=confidence,
                       evidence_present=present, evidence_against=against,
                       evidence_missing=missing, max_possible=max_possible)


def normalized_score(result: ScoreResult) -> float:
    """
    نقاط مطبّعة في [0,1] — تُستخدم كمدخل خام للمعايرة فقط.
    ليست احتمالاً، ولا يجوز عرضها كنسبة نجاح.
    """
    if result.max_possible <= 0:
        return 0.0
    return float(np.clip(result.score / result.max_possible, 0.0, 1.0))
