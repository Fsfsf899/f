"""
أنواع طبقة إدارة الصفقة التكيّفية — Part B.

فصل الأنواع عن المحرك مقصود: الباكتست والتنفيذ الحي يبنيان نفس
`TradeState` و`MarketView` من مصدرين مختلفين تماماً، ثم يستدعيان
نفس المحرك بالضبط. هذا هو ما يمنع ظهور "محرّكين" (البند 21).
"""
from dataclasses import dataclass, field
from typing import Dict, Optional

# ── قرارات الخروج/الإدارة (البند 21) ──
HOLD            = 'HOLD'
BREAK_EVEN      = 'BREAK_EVEN'
TRAILING_STOP   = 'TRAILING_STOP'
REDUCE_TARGET   = 'REDUCE_TARGET'
TIGHTEN_STOP    = 'TIGHTEN_STOP'
STAGNATION_EXIT = 'STAGNATION_EXIT'
REGIME_EXIT     = 'REGIME_EXIT'
NO_ACTION       = 'NO_ACTION'

# أسباب الامتناع — تُسجَّل كما هي في سجل التدقيق
R_DISABLED         = 'ADAPTIVE_DISABLED'
R_INSUFFICIENT     = 'INSUFFICIENT_DATA'
R_NO_ATR           = 'ATR_UNAVAILABLE'
R_RR_GUARD         = 'RR_BELOW_MIN'
R_WOULD_WIDEN      = 'WOULD_WIDEN_RISK'
R_TARGET_BELOW_PX  = 'TARGET_BELOW_PRICE'
R_STRONG_TREND     = 'STRONG_TREND_HOLD'
R_NOT_STAGNANT     = 'PROGRESS_ADEQUATE'
R_MOVE_TOO_SMALL   = 'MOVE_BELOW_THRESHOLD'


@dataclass
class TradeState:
    """
    لقطة المركز المفتوح — محايدة تجاه الوضع (ورقي/حي/باكتست).

    `entry_price` هو سعر التنفيذ الفعلي (شاملاً انزلاق الدخول)،
    و`entry_fee` الرسوم المدفوعة فعلاً عند الدخول. الاثنان ضروريان
    لحساب تعادل **صافٍ** لا تعادل سعري ساذج (البند 9).
    """
    entry_price: float
    qty: float
    initial_stop: float
    current_stop: float
    initial_target: float
    current_target: float
    opened_ms: int = 0
    entry_fee: float = 0.0
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    breakeven_done: bool = False
    trailing_active: bool = False
    target_adjusted: bool = False
    bars_held: int = 0
    position_id: Optional[int] = None
    symbol: str = ''

    @property
    def r_unit(self) -> float:
        """وحدة المخاطرة الأصلية — أساس كل قياس بـ R."""
        return self.entry_price - self.initial_stop


@dataclass
class MarketView:
    """
    ما يجوز للمحرك رؤيته لحظة القرار — ولا شيء غيره.

    ⚠️ حاجز منع النظر للأمام (البند 29): هذا الكائن يُبنى من الشمعة
    المغلقة الحالية فقط. لا يحوي أي حقل مستقبلي، وهذا مقصود بنيوياً:
    ما لا يصل المحرك لا يمكن أن يسرّبه.
    """
    price: float                      # إغلاق الشمعة الحالية
    atr: float = 0.0
    regime: str = 'UNKNOWN'
    regime_confidence: float = 0.0
    resistance: Optional[float] = None
    high: Optional[float] = None      # قمة الشمعة الحالية (لتحديث MFE)
    low: Optional[float] = None
    now_ms: int = 0
    bar_ms: int = 0


@dataclass
class ExitDecision:
    """
    قرار واحد موحَّد. `new_stop`/`new_target` لا تعني التنفيذ —
    تعني ما **يجب** أن يصبح عليه المركز؛ التنفيذ مسؤولية الطبقة
    التي تملك الأوامر (OCO/الوسيط الورقي/الباكتست).
    """
    decision: str = HOLD
    new_stop: Optional[float] = None
    new_target: Optional[float] = None
    reason: str = ''
    reason_ar: str = ''
    audit: Dict = field(default_factory=dict)

    @property
    def changes_stop(self) -> bool:
        return self.new_stop is not None

    @property
    def changes_target(self) -> bool:
        return self.new_target is not None

    @property
    def is_exit(self) -> bool:
        return self.decision in (STAGNATION_EXIT, REGIME_EXIT)

    def to_dict(self) -> Dict:
        return {'decision': self.decision, 'new_stop': self.new_stop,
                'new_target': self.new_target, 'reason': self.reason,
                'reason_ar': self.reason_ar, 'audit': dict(self.audit)}
