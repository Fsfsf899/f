"""
حالة الحساب — المصدر الوحيد لـ «كم من المال متاح فعلاً؟»
=========================================================
الأقسام 3-8 و14-17 من مواصفة V11 FINAL.

المبدأ الحاكم: **الرصيد الكلي ليس نقداً قابلاً للإنفاق.** قبل هذه
الوحدة كان `live_trader.equity()` يُمرِّر `free + locked` مباشرةً إلى
`PositionSizer` — أي أن رصيداً محجوزاً في أوامر قائمة كان يُحتسَب كأنه
متاح للدخول. المواصفة تمنع ذلك صراحةً (القسم 3، والمحظور «treat locked
balance as free balance» في القسم 32).

عقد واحد لكل البيئات (القسم 15): `PaperAccountProvider` و
`ExchangeAccountProvider` يُنتجان `AccountSnapshot` بنفس الشكل تماماً،
فيستهلكه `PositionSizer` بلا علم ببيئته ولا فرع خاص بها.

فشل مغلق (القسم 17): أي تعذّر أو قِدَم أو تشوّه ⇒ `status` غير OK،
و`usable_equity = 0.0`، و`can_trade = False`. لا تخمين ولا رصيد أخير.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ── حالات اللقطة (القسم 14) ──
OK = 'OK'
STALE = 'STALE'
UNAVAILABLE = 'UNAVAILABLE'
RESTRICTED = 'RESTRICTED'

# ── أسباب منع التداول (الأقسام 3، 7، 8) ──
R_UNAVAILABLE = 'ACCOUNT_BALANCE_UNAVAILABLE'
R_STALE = 'ACCOUNT_BALANCE_STALE'
R_INSUFFICIENT = 'INSUFFICIENT_USABLE_BALANCE'
R_RESTRICTED = 'ACCOUNT_RESTRICTED'


@dataclass
class AccountSnapshot:
    """
    لقطة حساب بلحظة معلومة. `taken_ms` إلزامي — القسم 6 يشترط أن كل
    قرار تحجيم يستند إلى لقطة ذات طابع زمني معروف، لا إلى رقم بلا عمر.
    """
    quote_asset: str = 'USDT'
    total_balance: float = 0.0        # free + locked للعملة المقابلة
    available_balance: float = 0.0    # free فقط — القابل للإنفاق الآن
    locked_balance: float = 0.0       # محجوز في أوامر قائمة
    position_value: float = 0.0       # قيمة المراكز المفتوحة (تعرّض قائم)
    reserve_amount: float = 0.0       # احتياطي واجب البقاء (القسم 8)
    assets: Dict[str, Dict[str, float]] = field(default_factory=dict)
    taken_ms: int = 0
    status: str = UNAVAILABLE
    source: str = ''                  # paper | testnet | live
    reasons: list = field(default_factory=list)
    error: str = ''

    # ── الحقوق ──
    @property
    def total_equity(self) -> float:
        """
        الحقوق الكلية = نقد (متاح + محجوز) + قيمة المراكز المفتوحة.

        ⚠️ لا تُستخدَم للتحجيم. القسم 5 يفصل بوضوح بين TOTAL_BALANCE
        و USABLE_EQUITY؛ هذا الحقل للعرض والتقارير فقط.

        لا ازدواج حساب (القسم 5): `position_value` يُقوَّم من الأصول
        الأساسية للمراكز المفتوحة، و`total_balance` من العملة المقابلة
        وحدها — مجموعتان منفصلتان لا تتقاطعان.
        """
        return self.total_balance + self.position_value

    @property
    def usable_equity(self) -> float:
        """
        الأساس الوحيد المشروع للتحجيم: **المتاح فعلاً ناقص الاحتياطي**.
        لا يشمل المحجوز ولا قيمة المراكز المفتوحة — لا يمكن إنفاق أيٍّ
        منهما على دخول جديد.
        """
        if self.status != OK:
            return 0.0
        return max(0.0, self.available_balance - self.reserve_amount)

    @property
    def age_seconds(self) -> float:
        if not self.taken_ms:
            return float('inf')
        return max(0.0, (time.time() * 1000 - self.taken_ms) / 1000.0)

    @property
    def can_trade(self) -> bool:
        return self.status == OK and self.usable_equity > 0

    def blocking_reason(self) -> Optional[str]:
        """سبب المنع الكنسي، أو None إن كان الحساب صالحاً للدخول."""
        if self.status == UNAVAILABLE:
            return R_UNAVAILABLE
        if self.status == STALE:
            return R_STALE
        if self.status == RESTRICTED:
            return R_RESTRICTED
        if self.usable_equity <= 0:
            return R_INSUFFICIENT
        return None

    def to_dict(self) -> Dict[str, Any]:
        """شكل نقطة حالة الحساب (القسم 14). **لا يحوي أي سر إطلاقاً.**"""
        return {
            'account_currency': self.quote_asset,
            'balance_timestamp': self.taken_ms,
            'balance_age_seconds': round(self.age_seconds, 3)
                                   if self.taken_ms else None,
            'total_balance': round(self.total_balance, 8),
            'available_balance': round(self.available_balance, 8),
            'locked_balance': round(self.locked_balance, 8),
            'total_equity': round(self.total_equity, 8),
            'usable_equity': round(self.usable_equity, 8),
            'reserve_amount': round(self.reserve_amount, 8),
            'existing_exposure': round(self.position_value, 8),
            'available_for_new_trade': round(self.usable_equity, 8),
            'assets': {k: {kk: round(vv, 8) for kk, vv in v.items()}
                       for k, v in self.assets.items()},
            'status': self.status,
            'source': self.source,
            'reasons': list(self.reasons),
            'blocking_reason': self.blocking_reason(),
        }


def compute_reserve(available: float, cfg) -> float:
    """
    الاحتياطي النقدي (القسم 8): الأكبر بين نسبة ومبلغ ثابت.

    «لا تُنفِق 100% من المتاح لمجرد أن صيغة المخاطرة تسمح.» الاحتياطي
    يُقتطَع **قبل** أي حساب حجم، لا بعده — فالاقتطاع اللاحق يسمح
    للصيغة بأن ترى مالاً لا يجوز إنفاقه.
    """
    pct = max(0.0, float(getattr(cfg, 'min_cash_reserve_pct', 0.0)))
    flat = max(0.0, float(getattr(cfg, 'min_cash_reserve_quote', 0.0)))
    return max(available * pct / 100.0, flat)
