"""
خطة التحجيم — «كم أدخل؟ ولماذا هذا المبلغ؟»
=============================================
الأقسام 13 و25 و34 من مواصفة V11 FINAL.

هذه الوحدة **لا تحسب حجماً**. تستدعي `PositionSizer` الكنسي و
`AccountSnapshot` وتُغلِّف نتيجتهما في شكل واحد قابل للعرض والتخزين.
القسم 25 صريح: «This value must come from the backend PositionSizer and
real account state, not from frontend arithmetic» — فلا رقم هنا يُشتق
بحساب موازٍ، وكل حقل إمّا منقول من نتيجة المحجِّم أو مشتقّ منها بعملية
عرض بحتة.

الخطة تُخزَّن في القاعدة ليقرأها لوحة المتابعة: اللوحة تتصل بقاعدة
**للقراءة فقط وبلا مفاتيح API**، فلا يمكنها سؤال بينانس بنفسها — ولا
يجوز أن تستطيع. المتداول يحسب، واللوحة تعرض.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .state import AccountSnapshot


@dataclass
class SizingPlan:
    """كل ما يحتاجه القسمان 13 و25، محسوباً في الخلفية."""
    symbol: str = ''
    decision: str = 'NO_TRADE'          # ENTER | NO_TRADE
    reason: str = ''
    account: Dict[str, Any] = field(default_factory=dict)

    # المخاطرة
    base_risk_pct: float = 0.0
    effective_risk_pct: float = 0.0
    max_risk_amount: float = 0.0

    # الإشارة
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    stop_distance_pct: Optional[float] = None
    risk_per_unit: Optional[float] = None
    risk_reward: Optional[float] = None

    # الكمية
    raw_quantity: float = 0.0
    final_quantity: float = 0.0
    position_value: float = 0.0

    # التكاليف
    estimated_entry_fee: float = 0.0
    estimated_exit_fee: float = 0.0
    slippage_bps: float = 0.0
    spread_bps: float = 0.0
    estimated_max_loss: float = 0.0

    # الحدود
    max_position_value: float = 0.0
    capped_by_balance: bool = False
    remaining_available: Optional[float] = None
    actual_risk_pct: float = 0.0

    explanation: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items()}
        return d


def _r(x, n=8):
    return None if x is None else round(float(x), n)


def build_sizing_plan(*, symbol: str, sizer, account: AccountSnapshot,
                      cfg, signal=None, rules: Optional[Dict] = None,
                      max_notional: float = 0.0,
                      consecutive_losses: int = 0) -> SizingPlan:
    """
    يبني الخطة من حالة الحساب والإشارة الحقيقيتين.

    `signal=None` وضع مشروع تماماً: «كم أستطيع أن أدخل؟» سؤال له جواب
    حتى بلا إشارة قائمة — يُعرَض عندها الرصيد والاحتياطي والمخاطرة
    القصوى، ويكون القرار NO_TRADE بسبب NO_SIGNAL. هذا أصدق من إخفاء
    اللوحة أو ملئها بأصفار بلا تفسير.
    """
    rules = rules or {}
    plan = SizingPlan(symbol=symbol, account=account.to_dict())
    plan.base_risk_pct = float(cfg.risk.risk_per_trade_pct)
    plan.slippage_bps = float(cfg.costs.slippage_bps_entry)
    plan.spread_bps = float(cfg.costs.spread_bps)

    block = account.blocking_reason()
    if block:
        plan.reason = block
        plan.explanation = _explain(plan, account, cfg, blocked=block)
        return plan

    plan.max_risk_amount = _r(account.usable_equity
                              * plan.base_risk_pct / 100.0, 6)
    plan.max_position_value = _r(account.usable_equity
                                 * cfg.risk.max_position_notional_pct / 100.0, 6)

    if signal is None or not signal.entry or not signal.stop_loss:
        plan.reason = 'NO_SIGNAL'
        plan.explanation = _explain(plan, account, cfg, blocked='NO_SIGNAL')
        return plan

    plan.entry = _r(signal.entry)
    plan.stop = _r(signal.stop_loss)
    plan.target = _r(getattr(signal, 'take_profit', None))
    plan.risk_reward = _r(getattr(signal, 'net_risk_reward', None)
                          or getattr(signal, 'risk_reward', None), 4)
    plan.stop_distance_pct = _r((signal.entry - signal.stop_loss)
                                / signal.entry * 100, 4)

    fee_rate = float(cfg.costs.taker_fee)
    sz = sizer.calculate(
        equity=account.usable_equity, entry=signal.entry, stop=signal.stop_loss,
        stars=getattr(signal, 'stars', 3), consecutive_losses=consecutive_losses,
        account=account, step_size=rules.get('step_size'),
        min_qty=rules.get('min_qty', 0.0),
        min_notional=rules.get('min_notional', 10.0), fee_rate=fee_rate)

    plan.effective_risk_pct = _r(sz.get('risk_pct', 0.0), 4)
    plan.risk_per_unit = _r(sz.get('effective_risk_per_unit'))
    plan.capped_by_balance = bool(sz.get('capped_by_balance'))
    plan.actual_risk_pct = _r(sz.get('actual_risk_pct', 0.0), 4)
    # الكمية الخام قبل أي تقريب أو تحديد — للمقارنة في العرض (القسم 13)
    rpu = sz.get('effective_risk_per_unit') or 0.0
    plan.raw_quantity = _r((account.usable_equity * plan.effective_risk_pct / 100.0)
                           / rpu, 8) if rpu > 0 else 0.0

    if sz.get('blocked') or sz.get('notional', 0.0) <= 0:
        plan.reason = sz.get('reason', 'SIZING_BLOCKED')
        plan.explanation = _explain(plan, account, cfg, blocked=plan.reason)
        return plan

    notional = min(float(sz['notional']), max_notional) if max_notional > 0 \
        else float(sz['notional'])
    if notional < float(sz['notional']):
        # سقف التشغيل (--max-notional) أضيق من نتيجة المحجِّم
        plan.final_quantity = _r(notional / signal.entry)
        plan.capped_by_balance = plan.capped_by_balance or False
    else:
        plan.final_quantity = _r(sz['qty'])

    plan.position_value = _r(notional, 6)
    plan.estimated_entry_fee = _r(notional * fee_rate, 6)
    plan.estimated_exit_fee = _r(notional * fee_rate, 6)
    plan.estimated_max_loss = _r(
        (plan.final_quantity or 0.0) * (signal.entry - signal.stop_loss)
        + plan.estimated_entry_fee + plan.estimated_exit_fee, 6)
    plan.remaining_available = _r(
        account.available_balance - notional - plan.estimated_entry_fee, 6)
    plan.decision = 'ENTER'
    plan.reason = 'OK'
    plan.explanation = _explain(plan, account, cfg)
    return plan


def _explain(plan: SizingPlan, account: AccountSnapshot, cfg,
             blocked: Optional[str] = None) -> List[str]:
    """
    «لماذا هذا المبلغ؟» بلغة بسيطة (القسم 25).

    كل سطر يشرح رقماً **ظاهراً في الخطة نفسها**، فلا يقول النص شيئاً
    لا تؤكّده الحقول. عند المنع، يُشرح المانع بدل عرض أصفار بلا سبب.
    """
    q = account.quote_asset
    out: List[str] = []

    if blocked == 'ACCOUNT_BALANCE_UNAVAILABLE':
        return ['تعذّر قراءة رصيد الحساب من المنصة، فلا يمكن تحديد حجم آمن.',
                'النظام يمتنع عن التداول بدل التخمين.']
    if blocked == 'ACCOUNT_BALANCE_STALE':
        return [f'آخر قراءة للرصيد عمرها {account.age_seconds:.0f} ثانية — '
                f'أقدم من الحد المسموح.',
                'الرصيد قد يكون تغيّر، فلا يُبنى عليه قرار حجم.']
    if blocked == 'ACCOUNT_RESTRICTED':
        return ['مفتاح الـ API لا يملك صلاحية التداول على هذا الحساب.']

    out.append(f'رصيدك المتاح فعلاً: {account.available_balance:,.2f} {q}'
               + (f' (ومحجوز في أوامر قائمة: {account.locked_balance:,.2f})'
                  if account.locked_balance > 0 else ''))
    if account.reserve_amount > 0:
        out.append(f'يُحتجَز {account.reserve_amount:,.2f} {q} كاحتياطي لا '
                   f'يُنفَق، فيبقى {account.usable_equity:,.2f} {q} للاستخدام.')

    if blocked == 'INSUFFICIENT_USABLE_BALANCE':
        out.append('لا يتبقّى شيء بعد الاحتياطي — لا دخول.')
        return out
    if blocked == 'NO_SIGNAL':
        out.append(f'أقصى ما يُخاطَر به لو ظهرت فرصة: '
                   f'{plan.max_risk_amount:,.2f} {q} '
                   f'({plan.base_risk_pct:.2f}% من المستخدَم).')
        out.append('لا توجد إشارة دخول صالحة الآن — لا صفقة.')
        return out

    out.append(f'النظام مستعد للمخاطرة بـ {plan.effective_risk_pct:.2f}% '
               f'من المستخدَم، أي {plan.max_risk_amount:,.2f} {q} كحدّ أعلى.')
    if plan.stop_distance_pct is not None:
        out.append(f'الوقف يبعد {plan.stop_distance_pct:.2f}% عن الدخول '
                   f'({plan.entry:,.2f} ← {plan.stop:,.2f}).')
    if plan.risk_per_unit:
        out.append(f'بعد إضافة الرسوم والانزلاق والفارق السعري، الخسارة لكل '
                   f'وحدة {plan.risk_per_unit:,.2f} {q} — أكبر من مسافة الوقف '
                   f'الاسمية، ولهذا الكمية أصغر مما قد تتوقّع.')

    if blocked == 'EXCHANGE_MINIMUM_EXCEEDS_RISK_LIMIT':
        out.append('الحد الأدنى لأمر المنصة أكبر مما تسمح به مخاطرتك '
                   'المسموحة — ولا يُقرَّب المبلغ لأعلى لإرضاء المنصة.')
        out.append('النتيجة: لا صفقة.')
        return out
    if blocked == 'ACTUAL_RISK_EXCEEDS_LIMIT':
        out.append('بعد التقريب لكمية صالحة على المنصة، صارت المخاطرة الفعلية '
                   'أعلى من الحد المسموح — لا صفقة.')
        return out
    if blocked:
        out.append(f'مُنع الدخول: {blocked}')
        return out

    if plan.capped_by_balance:
        out.append('المبلغ خُفِّض ليبقى ضمن رصيدك المتاح بعد الاحتياطي '
                   'والرسوم — لا اقتراض ولا رافعة.')
    out.append(f'المبلغ النهائي: {plan.position_value:,.2f} {q} '
               f'({plan.final_quantity} وحدة).')
    out.append(f'أسوأ خسارة متوقَّعة لو ضُرب الوقف: '
               f'{plan.estimated_max_loss:,.2f} {q} '
               f'(شاملة رسوم الدخول والخروج).')
    if plan.remaining_available is not None:
        out.append(f'يتبقّى في حسابك بعد الأمر: '
                   f'{plan.remaining_available:,.2f} {q}.')
    return out
