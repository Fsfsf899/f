"""
مدير الأوامر الحي — المراحل 5-9.
=================================
يستقبل TradeSignal مجمَّداً فقط. يحسب الحجم بحدود Live الصارمة،
يتحقق أن المركز يبقى ضمن سقف الانكشاف الكلي والعنقودي، يمرّ عبر
IdempotentOrderGate الموجود (لا تكرار)، ثم OCO للحماية.

كل خطوة تُسجَّل في audit_log — القرار، لا فقط النتيجة.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .trade_signal import TradeSignal, validate_signal, ACCEPT_CHECKS
from .live_config import LiveConfig, LiveLimits, check_activation
from ..execution.idempotency import DuplicateOrderError
from ..execution.order_state import BLOCKING
from ..risk.portfolio import PortfolioRisk, PortfolioConfig


@dataclass
class LiveDecision:
    signal_id: str
    outcome: str                    # EXECUTED | REJECTED | BLOCKED | ERROR
    reason: str = ''
    checks: List[Dict] = field(default_factory=list)
    pre_trade_checks: List[Dict] = field(default_factory=list)
    order_result: Optional[Dict] = None
    sized_notional: Optional[float] = None
    sized_qty: Optional[float] = None

    def to_dict(self) -> Dict:
        return {'signal_id': self.signal_id, 'outcome': self.outcome,
                'reason': self.reason, 'checks': self.checks,
                'pre_trade_checks': self.pre_trade_checks,
                'order_result': self.order_result,
                'sized_notional': self.sized_notional,
                'sized_qty': self.sized_qty}


class LiveOrderManager:
    """
    يُستخدم فقط عند stage.real_money. كل استدعاء يبدأ بإعادة تقييم
    البوابة الكاملة — نتيجة سابقة لا تُعتمد لاحقاً (لا cache على القرار).
    """

    def __init__(self, db, client, base_order_manager, live_cfg: LiveConfig,
                 health=None, cost_cfg=None):
        self.db = db
        self.client = client
        self.orders = base_order_manager       # OrderManager الموجود — لا تكرار منطق
        self.cfg = live_cfg
        self.health = health
        self.portfolio = PortfolioRisk(PortfolioConfig(
            max_total_exposure_pct=live_cfg.limits.max_total_exposure_pct,
            max_cluster_exposure_pct=live_cfg.limits.max_cluster_exposure_pct))
        # القسم 3 من متطلبات V11 Final Patch: مصدر واحد لحساب الحجم —
        # PositionSizer الكنسي نفسه المستخدَم في الباكتست، لا صيغة
        # مستقلة هنا. cost_cfg اختياري (افتراضي CostConfig() المحايدة)
        # كي لا يُغيَّر توقيع البناء لكل استدعاء قديم.
        from ..core.config import CostConfig
        from ..backtest.costs import CostModel
        from ..risk.position_sizing import PositionSizer
        self.cost_model = CostModel(cost_cfg or CostConfig())
        self._PositionSizer = PositionSizer

    def _gate_snapshot(self) -> Dict:
        """يقرأ حالة حية من القاعدة والمنصة — لا قيمة مخزَّنة."""
        try:
            acct = self.client.account()
        except Exception as e:
            self.db.risk_event('LIVE_ACCOUNT_UNREADABLE', 'CRITICAL', str(e)[:200])
            acct = None
        try:
            offset = self.client.sync_time()
        except Exception:
            offset = None
        unresolved = len(self.db.unresolved_intents())
        kill = bool(self.db.get_kv('kill_switch', False))
        recon_ok = self.db.get_kv('last_reconciliation_ok')
        db_ok = True
        try:
            self.db.query('SELECT 1')
        except Exception:
            db_ok = False
        return dict(account_info=acct, unresolved_intents=unresolved,
                   reconciliation_ok=recon_ok, kill_switch_on=kill,
                   db_healthy=db_ok, clock_offset_ms=offset)

    def preflight(self, *, paper_gate_passed: Optional[bool] = None,
                 testnet_gate_passed: Optional[bool] = None):
        snap = self._gate_snapshot()
        act = check_activation(self.cfg, paper_gate_passed=paper_gate_passed,
                               testnet_gate_passed=testnet_gate_passed, **snap)
        self.db.system_event('LIVE_GATE_EVALUATED',
                             f"allowed={act.allowed} failures={act.failures}")
        return act

    def submit(self, signal: TradeSignal, *, equity: float,
              current_open_notional: Dict[str, float],
              price_series: Optional[Dict] = None,
              paper_gate_passed: Optional[bool] = None,
              testnet_gate_passed: Optional[bool] = None,
              recommendation_id: Optional[int] = None) -> LiveDecision:
        """
        المسار الوحيد لتحويل إشارة إلى أمر حي. كل رفض مُسجَّل بسببه.
        """
        d = LiveDecision(signal.signal_id, 'BLOCKED')

        # 1) البوابة أولاً — تُعاد كاملة، لا اختصار
        act = self.preflight(paper_gate_passed=paper_gate_passed,
                             testnet_gate_passed=testnet_gate_passed)
        if not act.allowed:
            d.reason = f"LIVE_GATE_CLOSED: {','.join(act.failures)}"
            self._audit(signal, d)
            return d

        # 2) عقد الإشارة
        ok, checks = validate_signal(
            signal, environment=self.cfg.stage.value,
            allowed_symbols=self.cfg.allowed_symbols,
            interval_ms=self._interval_ms(signal.timeframe),
            max_age_multiple=self.cfg.limits.max_signal_age_multiple)
        d.checks = checks
        if not ok:
            d.outcome = 'REJECTED'
            d.reason = 'SIGNAL_CONTRACT_FAILED: ' + ','.join(
                c['check'] for c in checks if not c['passed'])
            self._audit(signal, d)
            return d

        # 3) حجم بحدود Live — لا حدود الاستراتيجية العامة
        sized = self._size(signal, equity)
        if sized['qty'] <= 0:
            d.outcome = 'REJECTED'
            d.reason = sized['reason']
            self._audit(signal, d)
            return d
        d.sized_notional, d.sized_qty = sized['notional'], sized['qty']

        # 4) انكشاف المحفظة والعنقود
        pf = self.portfolio.evaluate(
            equity=equity, open_notional=current_open_notional,
            new_symbol=signal.symbol, new_notional=sized['notional'],
            price_series=price_series)
        if not pf.allowed:
            d.outcome = 'REJECTED'
            d.reason = 'PORTFOLIO_LIMIT: ' + '; '.join(pf.reasons)
            self._audit(signal, d)
            return d

        # 5) البوابة الأخيرة قبل الشبكة — الأقرب زمنياً للتنفيذ
        act2 = self.preflight(paper_gate_passed=paper_gate_passed,
                              testnet_gate_passed=testnet_gate_passed)
        if not act2.allowed:
            d.outcome = 'BLOCKED'
            d.reason = f"LIVE_GATE_CLOSED_AT_SUBMIT: {','.join(act2.failures)}"
            self._audit(signal, d)
            return d

        # 5.5) سلسلة الفحوص الحقيقية لـ OrderManager — كانت مُتجاوَزة بالكامل
        # عبر _passthrough_check() في نسخة سابقة (ثغرة اكتُشفت بمراجعة عدائية،
        # انظر AUDIT). هذه السلسلة مستقلة عن بوابة Live: تتحقق من الرصيد
        # الفعلي على المنصة، LOT_SIZE/MIN_NOTIONAL الحقيقيين، الانكشاف من
        # القاعدة، والحد اليومي — بيانات لا تملكها بوابة Live لأنها لا تتصل
        # بالمنصة ولا تقرأ القاعدة بنفس الطريقة.
        dq_score = (signal.data_quality or {}).get('score', 0.0)
        try:
            chk = self.orders.pre_trade(
                symbol=signal.symbol, notional=sized['notional'],
                entry=signal.entry_reference_price, stop=signal.stop_loss,
                signal_bar_time=signal.candle_close_ts,
                interval_ms=self._interval_ms(signal.timeframe) or 0,
                data_quality=float(dq_score or 0.0), equity=equity,
                day_key=self._day_key())
        except Exception as e:
            d.outcome = 'ERROR'
            d.reason = f'PRE_TRADE_CHECK_ERROR: {type(e).__name__}: {str(e)[:180]}'
            self._audit(signal, d)
            return d
        d.pre_trade_checks = chk.checks
        if not chk.passed:
            d.outcome = 'REJECTED'
            d.reason = 'PRE_TRADE_FAILED: ' + ','.join(chk.failures)
            self._audit(signal, d)
            return d

        # 6) التنفيذ عبر OrderManager الموجود — نفس البوابة الحتمية
        try:
            res = self.orders.open_long(
                symbol=signal.symbol, notional=sized['notional'],
                entry_ref=signal.entry_reference_price,
                stop=signal.stop_loss, target=signal.take_profit,
                recommendation_id=recommendation_id, check=chk)
        except DuplicateOrderError as e:
            d.outcome = 'REJECTED'
            d.reason = f'DUPLICATE: {e}'
            self._audit(signal, d)
            return d
        except Exception as e:
            d.outcome = 'ERROR'
            d.reason = f'{type(e).__name__}: {str(e)[:200]}'
            if self.health:
                self.health.engage_kill_switch(f'خطأ تنفيذ حي: {d.reason}')
            self.db.risk_event('LIVE_ORDER_ERROR', 'CRITICAL', d.reason,
                               signal.symbol)
            self._audit(signal, d)
            return d

        if res is None:
            d.outcome = 'REJECTED'
            d.reason = 'ORDER_MANAGER_REFUSED'
        else:
            d.outcome = 'EXECUTED'
            d.order_result = res
            self.db.system_event(
                'LIVE_ORDER_EXECUTED',
                f"{signal.signal_id} {signal.symbol} qty={res.get('qty')}")
        self._audit(signal, d)
        return d

    def _size(self, signal: TradeSignal, equity: float) -> Dict:
        """
        الحجم عبر `PositionSizer` الكنسي الوحيد (القسم 3 من متطلبات
        V11 Final Patch) — لا صيغة مخاطرة مستقلة هنا بعد الآن.

        كانت النسخة السابقة تحسب `risk_per_unit` من المسافة الخام
        (entry-stop) فقط — بلا رسوم، بلا انزلاق، بلا سبريد — وبلا أي
        من معدِّلات المخاطرة (النجوم، سلاسل الخسائر، معدل الربح) التي
        يُطبِّقها `PositionSizer.calculate()` الكنسي المُستخدَم في
        الباكتست. الأثر: حجم أكبر فعلياً مما تقرِّره الاستراتيجية نفسها
        للمُدخلات نفسها — وأخطر ما فيه: بعد سلسلة خسائر متتالية، حيث
        الكنسي يُخفِّض المخاطرة حتى للنصف، وكانت النسخة القديمة هنا
        تتجاهل ذلك كلياً وتستمر بالحجم الكامل.
        """
        lim = self.cfg.limits
        entry, stop = signal.entry_reference_price, signal.stop_loss
        if not entry or not stop or stop >= entry:
            return {'qty': 0, 'notional': 0, 'reason': 'وقف أو دخول غير صالح'}

        # حدود Live أضيق عمداً من حدود الاستراتيجية العامة — نبني
        # RiskConfig من LiveLimits تحديداً. max_position_notional_pct=100
        # لأن الحد المطلق (بالعملة، لا بالنسبة) يُطبَّق أدناه صراحةً
        # كخطوة منفصلة مدقَّقة، لا مدموجاً داخل الصيغة الكنسية.
        from ..core.config import RiskConfig
        live_risk_cfg = RiskConfig(
            risk_per_trade_pct=lim.max_risk_per_trade_pct,
            min_risk_per_trade_pct=0.0,
            max_risk_per_trade_pct=lim.max_risk_per_trade_pct,
            max_position_notional_pct=100.0)

        stars = max(1, min(5, round(signal.score))) if signal.score else 3
        consecutive_losses = self._live_consecutive_losses()

        sizer = self._PositionSizer(live_risk_cfg, self.cost_model)
        raw = sizer.calculate(
            equity=equity, entry=entry, stop=stop, stars=stars,
            consecutive_losses=consecutive_losses,
            recent_win_rate=None,       # غير متاح هنا فعلياً — لا نفتعله
            regime_confidence=1.0,      # محايد — لا معلومة نظام إضافية هنا
            step_size=None, min_notional=0.0)  # التقريب/الحد الأدنى أدناه صراحةً

        detail: Dict[str, Any] = {
            'account_equity': equity, 'base_risk_pct': live_risk_cfg.risk_per_trade_pct,
            'effective_risk_pct': raw.get('risk_pct'),
            'stars_modifier_input': stars,
            'consecutive_losses_modifier_input': consecutive_losses,
            'entry_price': entry, 'stop_price': stop,
            'raw_stop_distance': entry - stop,
            'effective_risk_per_unit': raw.get('effective_risk_per_unit'),
            'cost_overhead_pct': raw.get('cost_overhead_pct'),
            'raw_quantity': raw.get('qty'),
        }

        if raw['qty'] <= 0:
            return {'qty': 0, 'notional': 0, 'reason': raw['reason'],
                    'sizing_detail': detail}

        notional = min(raw['notional'], lim.max_position_notional)
        available = equity - lim.min_reserve_quote
        notional = max(0.0, min(notional, available))
        detail['max_position_notional_cap'] = lim.max_position_notional
        detail['available_balance_constraint'] = available
        if notional <= 0:
            return {'qty': 0, 'notional': 0, 'reason': 'لا رصيد متاح بعد الاحتياطي',
                    'sizing_detail': detail}

        try:
            rules = self.client.rules(signal.symbol)
        except Exception as e:
            # لا نخمّن حداً أدنى بديلاً — الفشل في قراءة قواعد المنصة
            # يمنع الحجم بدل المتابعة برقم غير مُتحقَّق منه
            return {'qty': 0, 'notional': 0,
                   'reason': f'تعذّر قراءة قواعد المنصة: {type(e).__name__}',
                   'sizing_detail': detail}

        raw_qty = notional / entry
        qty = self.client.round_qty(signal.symbol, raw_qty)
        notional = qty * entry
        detail['exchange_rounded_quantity'] = qty
        detail['position_notional'] = notional

        min_notional = rules.get('min_notional', 10.0)
        min_qty = rules.get('min_qty', 0.0)
        if qty < min_qty:
            return {'qty': 0, 'notional': 0,
                   'reason': f'الكمية {qty} أقل من min_qty={min_qty}',
                   'sizing_detail': detail}
        if notional < min_notional:
            return {'qty': 0, 'notional': 0,
                   'reason': f'قيمة الصفقة ${notional:.2f} أقل من '
                             f'min_notional=${min_notional}',
                   'sizing_detail': detail}

        # القسم 3: "After exchange rounding, recalculate the ACTUAL
        # estimated risk. If actual risk exceeds the allowed risk
        # because of rounding: NO TRADE." — لا نزيد الحجم لإرضاء حداً
        # أدنى للمنصة أبداً؛ هنا نتحقق من الاتجاه المعاكس: هل التقريب
        # لأعلى (نحو step_size/min_notional) رفع المخاطرة الفعلية فوق
        # المسموح؟
        eff = sizer.effective_risk_per_unit(entry, stop)
        actual_risk_amount = qty * eff
        max_allowed_risk = equity * live_risk_cfg.risk_per_trade_pct / 100
        detail['estimated_fees'] = qty * (entry + stop) * self.cost_model.cfg.eff_taker()
        detail['estimated_slippage'] = qty * (
            entry * self.cost_model.cfg.slippage_bps_entry / 10000
            + stop * self.cost_model.cfg.slippage_bps_stop / 10000)
        detail['estimated_spread_cost'] = qty * (entry + stop) * (
            self.cost_model.cfg.spread_bps / 20000)
        detail['estimated_max_loss'] = actual_risk_amount
        detail['risk_pct_of_equity'] = (actual_risk_amount / equity * 100
                                        if equity > 0 else None)
        detail['max_allowed_risk_amount'] = max_allowed_risk
        if signal.take_profit:
            detail['expected_reward'] = qty * (signal.take_profit - entry)
        detail['net_risk_reward'] = signal.risk_reward

        if actual_risk_amount > max_allowed_risk * 1.01:   # هامش تقريب دقيق ضئيل
            detail['sizing_decision'] = 'REJECTED'
            detail['rejection_reason'] = 'EXCHANGE_MINIMUM_EXCEEDS_RISK_LIMIT'
            return {'qty': 0, 'notional': 0,
                   'reason': 'EXCHANGE_MINIMUM_EXCEEDS_RISK_LIMIT',
                   'sizing_detail': detail}

        detail['sizing_decision'] = 'ACCEPTED'
        return {'qty': qty, 'notional': notional, 'reason': 'ok',
                'sizing_detail': detail}

    def _live_consecutive_losses(self) -> int:
        """
        يقرأ نفس حالة المخاطرة المُستمرَّة التي يستخدمها RiskGuard —
        لا عدّاد مستقل ثانٍ (نفس مبدأ توحيد المصدر في القسم 3، مطبَّق
        هنا على معدِّل الحجم لا الحجم نفسه فقط).
        """
        try:
            raw = self.db.get_kv('risk_state')
            return int(raw.get('consecutive_losses', 0)) if raw else 0
        except Exception:
            return 0

    @staticmethod
    def _interval_ms(tf: str) -> Optional[int]:
        table = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
                 '1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000}
        return table.get(tf)

    @staticmethod
    def _day_key() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def _audit(self, signal: TradeSignal, decision: LiveDecision):
        """يسجّل القرار كاملاً — ليس فقط EXECUTED/REJECTED."""
        self.db.risk_event(
            f'LIVE_DECISION_{decision.outcome}',
            'CRITICAL' if decision.outcome == 'ERROR' else 'INFO',
            f"{signal.signal_id} {signal.symbol}: {decision.reason}"[:400],
            signal.symbol)
