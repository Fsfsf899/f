"""
محرك اختيار أفضل فرصة — الأقسام 78-98 من متطلبات V11.

طبقة توصية فوق SignalEngine الموجود، لا بديل عنه ولا تكرار لمنطقه:
يستدعي SignalEngine.evaluate() (الذي يستدعي NoTradeEngine.check()
داخلياً) لكل رمز، ويستخدم قرار BUY/WAIT/NO_TRADE وأسبابه **كما هي**
لتحديد الأهلية — لا إعادة تنفيذ فحوص الأهلية من الصفر هنا. ثم يُسجِّل
درجة رقمية للمؤهَّلين فقط، ويختار الأفضل.

⚠️ لا يُصرِّح بصفقة بنفسه (البند 97 الصريح: "Ranking chooses the best
opportunity. It does NOT authorize a trade by itself."). السلطة
النهائية تبقى لـ RiskGuard/PositionSizer/OrderManager القائمة —
هذه الوحدة تُغذِّيها بترشيح، لا تتجاوزها.

⚠️ نطاق هذا التنفيذ: طبقة الترتيب الأساسية (78-82، 84، 88، 91، 95،
97). سجل التدقيق المُخزَّن في القاعدة (93) يُنفَّذ عند المستدعي
(`LiveTrader._scan_and_select_symbol` عبر `save_opportunity_scan`)، لا
هنا. غير مُنفَّذ بعد: تكامل اللوحة (85-86، 96)، نقطة API مخصَّصة
(92)، باكتست طبقة الاختيار نفسها (94)، أداء تاريخي لكل أصل (87) —
موثَّق صراحة، لا مخفياً.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..signals.engine import SignalEngine, Signal, BUY
from ..risk.portfolio import PortfolioRisk

SUPPORTED_ASSETS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT']


@dataclass
class ScoreWeights:
    """
    أوزان قابلة للتهيئة وموثَّقة — البند 79: "The exact weighting must
    be configurable and documented." القيم أدناه اجتهادية موثَّقة
    كبداية معقولة، لا "الصحيحة الوحيدة" — القسم 94 (باكتست طبقة
    الاختيار، غير مُنفَّذ هنا) هو الطريق الصحيح لمعايرتها لاحقاً على
    بيانات حقيقية، لا التخمين.

    كل مكوّن مبني على حقل حقيقي من Signal الناتج فعلياً عن SignalEngine
    — لا قيمة عشوائية، لا تفضيل مكتوب لرمز بعينه (البند 79 الصريح).
    """
    signal_quality: float = 0.25   # Signal.score الخام مُطبَّعاً [0,1]
    confidence: float = 0.15       # Signal.confidence
    probability: float = 0.15      # calibrated_probability إن وُجد فقط
    risk_reward: float = 0.20      # net_risk_reward (أو risk_reward)
    stop_quality: float = 0.10     # مسافة الوقف كنسبة من السعر
    data_quality: float = 0.10     # Signal.data_quality
    btc_context: float = 0.05      # Signal.btc_context (LOW/MEDIUM/HIGH خطورة)

    def normalized(self, has_probability: bool) -> Dict[str, float]:
        """
        لا صفر صامت عند غياب probability — وزنها يُعاد توزيعه نسبياً
        على بقية المكوّنات بدل إسقاطه من المجموع الكلي بصمت.
        """
        d = {'signal_quality': self.signal_quality, 'confidence': self.confidence,
            'probability': self.probability if has_probability else 0.0,
            'risk_reward': self.risk_reward, 'stop_quality': self.stop_quality,
            'data_quality': self.data_quality, 'btc_context': self.btc_context}
        if not has_probability and self.probability > 0:
            others = [k for k in d if k != 'probability']
            total_others = sum(d[k] for k in others)
            if total_others > 0:
                for k in others:
                    d[k] += self.probability * (d[k] / total_others)
        s = sum(d.values())
        return {k: (v / s if s > 0 else 0.0) for k, v in d.items()}


@dataclass
class OpportunityScore:
    symbol: str
    eligible: bool
    decision: str
    score: float = 0.0
    rejection_reasons: List[str] = field(default_factory=list)
    signal: Optional[Signal] = None
    components: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol, 'eligible': self.eligible,
            'decision': self.decision, 'score': round(self.score, 2),
            'rejection_reasons': list(self.rejection_reasons),
            'components': {k: round(v, 4) for k, v in self.components.items()},
            'signal': self.signal.to_dict() if self.signal else None,
        }


def _score_signal(sig: Signal, weights: ScoreWeights) -> Dict[str, float]:
    has_prob = sig.calibrated_probability is not None
    w = weights.normalized(has_prob)

    signal_quality = max(0.0, min(1.0, sig.score / 10.0))
    confidence = max(0.0, min(1.0, sig.confidence))
    rr = sig.net_risk_reward if sig.net_risk_reward is not None else sig.risk_reward
    rr_component = max(0.0, min(1.0, (rr - 1.0) / 2.0)) if rr is not None else 0.0
    stop_pct = (abs(sig.entry - sig.stop_loss) / sig.entry * 100
               if sig.entry and sig.stop_loss else None)
    # وقف أضيق = مخاطرة أقل لكل وحدة، لكن أضيق جداً = هشّ وعرضة
    # للتفعيل بضجيج السوق — نُكافئ الاقتراب من نطاق معقول (~1.5%)، لا
    # الأضيق مطلقاً.
    stop_component = (max(0.0, min(1.0, 1.0 - abs(stop_pct - 1.5) / 3.0))
                      if stop_pct is not None else 0.0)
    data_quality = max(0.0, min(1.0, sig.data_quality))
    btc_component = {'LOW': 1.0, 'MEDIUM': 0.5}.get(sig.btc_context, 0.0)

    parts = {
        'signal_quality': signal_quality * w['signal_quality'],
        'confidence': confidence * w['confidence'],
        'risk_reward': rr_component * w['risk_reward'],
        'stop_quality': stop_component * w['stop_quality'],
        'data_quality': data_quality * w['data_quality'],
        'btc_context': btc_component * w['btc_context'],
    }
    if has_prob:
        parts['probability'] = float(sig.calibrated_probability) * w['probability']
    return parts


def evaluate_opportunity(symbol: str, engine: SignalEngine, data, idx: int, *,
                         data_quality: float, btc_ctx=None,
                         account_state: Optional[Dict] = None,
                         weights: Optional[ScoreWeights] = None) -> OpportunityScore:
    """
    يقيّم رمزاً واحداً. القسم 80: الأهلية عبر SignalEngine.evaluate()
    (الذي يستدعي NoTradeEngine.check() داخلياً) — نفس منطق الأهلية
    المُستخدَم في التداول أحادي الرمز بالضبط، لا نسخة موازية.
    """
    weights = weights or ScoreWeights()
    sig = engine.evaluate(data, idx, data_quality=data_quality,
                          account_state=account_state, btc_ctx=btc_ctx)

    if sig.decision != BUY:
        return OpportunityScore(symbol=symbol, eligible=False,
                                decision=sig.decision,
                                rejection_reasons=list(sig.reasons), signal=sig)

    parts = _score_signal(sig, weights)
    total = sum(parts.values()) * 100
    return OpportunityScore(symbol=symbol, eligible=True, decision=sig.decision,
                            score=total, signal=sig, components=parts)


@dataclass
class RankingResult:
    decision: str                          # 'BUY' | 'NO_TRADE' — القسم 92
    selected_symbol: Optional[str] = None
    opportunities: List[OpportunityScore] = field(default_factory=list)
    reason: str = ''
    tie_break_applied: bool = False

    def to_dict(self) -> Dict[str, Any]:
        sel = next((o for o in self.opportunities if o.symbol == self.selected_symbol),
                  None) if self.selected_symbol else None
        sig = sel.signal if sel else None
        return {
            'selected_symbol': self.selected_symbol, 'decision': self.decision,
            'score': round(sel.score, 2) if sel else None,
            'confidence': round(sig.confidence, 4) if sig else None,
            'entry': sig.entry if sig else None,
            'stop': sig.stop_loss if sig else None,
            'target': sig.take_profit if sig else None,
            'risk_reward': (sig.net_risk_reward if sig and sig.net_risk_reward
                           is not None else (sig.risk_reward if sig else None)),
            'reason': self.reason,
            'tie_break_applied': self.tie_break_applied,
            'alternatives': [o.to_dict() for o in self.opportunities
                            if o.symbol != self.selected_symbol],
        }


def scan_and_rank(symbols: List[str], *, engines: Dict[str, SignalEngine],
                  data_by_symbol: Dict, idx_by_symbol: Dict[str, int],
                  data_quality_by_symbol: Dict[str, float],
                  btc_ctx=None, account_state: Optional[Dict] = None,
                  weights: Optional[ScoreWeights] = None,
                  portfolio: Optional[PortfolioRisk] = None,
                  equity: float = 0.0,
                  open_notional: Optional[Dict[str, float]] = None,
                  price_series: Optional[Dict] = None,
                  notional_estimates: Optional[Dict[str, float]] = None,
                  notional_fn=None,
                  already_has_open_position: bool = False) -> RankingResult:
    """
    البنود 78-84، 91، 97: يفحص كل الرموز، يستبعد غير المؤهَّل (عبر
    NoTradeEngine الموجود أصلاً)، يُرتِّب المؤهَّلين بدرجة رقمية
    حتمية، يختار الأفضل — أو NO_TRADE صراحة إن لم يتأهل أحد (البند
    82: "There must be NO forced selection... NO 'best of bad
    choices'"). **لا يُصرِّح بصفقة بنفسه.**

    `notional_estimates`: قيمة مركز مُقدَّرة حقيقية لكل رمز (يُفترَض
    حسابها عبر PositionSizer الكنسي نفسه من طرف المستدعي — لا مصدر
    حجم موازٍ هنا). **بلا هذه القيمة، فحص تعرّض المحفظة على المرشَّح
    الفائز يُتجاوَز صراحةً** (لا يُستبدَل بصفر ضمني — صفر كان يعني
    عملياً "أي حد تعرّض يمر دائماً"، وهذا فشل أخطر من تخطّي الفحص
    بوضوح موثَّق).

    `notional_fn`: بديل عملي عن `notional_estimates` عندما لا يستطيع
    المستدعي حساب الحجم مسبقاً — الحجم يحتاج `entry`/`stop` وهما لا
    يُعرفان إلا بعد التقييم الذي يجري **داخل** هذه الدالة. دالة
    `(symbol, Signal) -> float` تُستدعى كسولاً للمرشَّح المفحوص فقط،
    ويجب أن تُفوِّض إلى PositionSizer الكنسي نفسه — لا أن تُعيد
    تنفيذ منطق حجم موازٍ. تُستخدَم فقط إن غاب `notional_estimates`.
    """
    # البند 84: أولوية مركز واحد — لا مسح جديد يفتح مركزاً ثانياً
    if already_has_open_position:
        return RankingResult(decision='NO_TRADE', reason='POSITION_ALREADY_OPEN')

    opportunities: List[OpportunityScore] = []
    for sym in symbols:
        eng = engines.get(sym)
        data = data_by_symbol.get(sym)
        if eng is None or data is None:
            opportunities.append(OpportunityScore(
                symbol=sym, eligible=False, decision='NO_TRADE',
                rejection_reasons=['INVALID_MARKET_DATA']))
            continue
        idx = idx_by_symbol.get(sym)
        dq = data_quality_by_symbol.get(sym, 0.0)
        opportunities.append(evaluate_opportunity(
            sym, eng, data, idx, data_quality=dq, btc_ctx=btc_ctx,
            account_state=account_state, weights=weights))

    eligible = [o for o in opportunities if o.eligible]
    if not eligible:
        return RankingResult(decision='NO_TRADE', opportunities=opportunities,
                             reason='NO_ELIGIBLE_OPPORTUNITY')

    # البند 91: ترتيب حتمي — الدرجة أولاً تنازلياً، ثم الرمز أبجدياً
    # عند التعادل. قاعدة موثَّقة صراحة هنا، لا عشوائية بأي شكل.
    eligible.sort(key=lambda o: (-o.score, o.symbol))
    tie = len(eligible) > 1 and abs(eligible[0].score - eligible[1].score) < 1e-9
    best = eligible[0]

    # البند 88: فحص الارتباط/التعرّض النهائي على المرشَّح الأفضل فقط.
    # لا يُنفَّذ إلا إن توفَّر تقدير حجم حقيقي — تمرير صفر ضمني كان
    # سيجعل الفحص "يمر دائماً" بصرف النظر عن الحد المُعرَّف، وهذا
    # أسوأ من تخطّيه بوضوح.
    if portfolio is not None and equity > 0 and (notional_estimates or notional_fn):
        while True:
            if notional_estimates:
                est = notional_estimates.get(best.symbol)
            else:
                est = notional_fn(best.symbol, best.signal)
            if est is None or est <= 0:
                break
            exp = portfolio.evaluate(equity=equity, open_notional=open_notional or {},
                                     new_symbol=best.symbol, new_notional=est,
                                     price_series=price_series)
            if exp.allowed:
                break
            best.eligible = False
            best.rejection_reasons.extend(exp.reasons)
            eligible = [o for o in eligible if o.symbol != best.symbol]
            if not eligible:
                return RankingResult(decision='NO_TRADE', opportunities=opportunities,
                                     reason='PORTFOLIO_RISK_EXCEEDED')
            best = eligible[0]

    return RankingResult(decision='BUY', selected_symbol=best.symbol,
                         opportunities=opportunities,
                         reason='BEST_ELIGIBLE_OPPORTUNITY', tie_break_applied=tie)
