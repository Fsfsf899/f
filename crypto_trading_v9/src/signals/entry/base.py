"""
عقد الإشارة الموحَّد لكل نماذج الدخول — البند 3.
=================================================
كل نموذج (Baseline/Breakout/Pullback) يُرجع نفس الشكل. الفصل بين
الدرجات الفرعية (trend/momentum/structure/risk/execution) مقصود —
رقم واحد غير قابل للتفسير هو بالضبط ما يمنعه هذا العقد.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

BASELINE = 'BASELINE'
BREAKOUT = 'BREAKOUT'
PULLBACK = 'PULLBACK'
BREAKOUT_RETEST = 'BREAKOUT_RETEST'
NO_TRADE = 'NO_TRADE'

SETUP_TYPES = (BASELINE, BREAKOUT, PULLBACK, BREAKOUT_RETEST, NO_TRADE)


@dataclass
class SubScores:
    """درجات فرعية منفصلة — لا تُجمَع في رقم واحد بلا تفسير."""
    trend_score: float = 0.0
    momentum_score: float = 0.0
    structure_score: float = 0.0
    risk_score: float = 0.0
    execution_score: float = 0.0

    def final_score(self) -> float:
        """
        مجموع بسيط شفّاف — ليس متوسطاً مرجَّحاً مخفياً. أي وزن مختلف
        يجب أن يكون معاملاً صريحاً في Config، لا ثابتاً هنا.
        """
        return (self.trend_score + self.momentum_score
                + self.structure_score + self.risk_score
                + self.execution_score)

    def to_dict(self) -> Dict[str, float]:
        d = dict(self.__dict__)
        d['final_score'] = self.final_score()
        return d


@dataclass
class EntrySignal:
    setup_type: str = NO_TRADE
    eligible: bool = False
    score: float = 0.0
    confidence: Optional[float] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    net_risk_reward: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    rejection_reasons: List[str] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    sub_scores: Optional[SubScores] = None
    setup_id: Optional[str] = None       # فقط لـ BREAKOUT_RETEST/PULLBACK

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d['sub_scores'] = self.sub_scores.to_dict() if self.sub_scores else None
        return d

    @staticmethod
    def rejected(setup_type: str, reasons: List[str],
                diagnostics: Optional[Dict] = None) -> 'EntrySignal':
        return EntrySignal(setup_type=setup_type, eligible=False,
                           rejection_reasons=list(reasons),
                           diagnostics=diagnostics or {})
