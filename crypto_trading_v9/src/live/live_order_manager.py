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
                 health=None):
        self.db = db
        self.client = client
        self.orders = base_order_manager       # OrderManager الموجود — لا تكرار منطق
        self.cfg = live_cfg
        self.health = health
        self.portfolio = PortfolioRisk(PortfolioConfig(
            max_total_exposure_pct=live_cfg.limits.max_total_exposure_pct,
            max_cluster_exposure_pct=live_cfg.limits.max_cluster_exposure_pct))

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
        الحجم بحدود Live، ثم تقريبه على قواعد المنصة **الفعلية**.

        النسخة السابقة كانت تستخدم حد أدنى مفترَض ($10) بدل قراءة
        LOT_SIZE / MIN_NOTIONAL الحقيقيين من المنصة، ولا تُقرِّب الكمية
        على step_size إطلاقاً — أمر بكمية غير مقرَّبة يُرفَض من بينانس
        بخطأ -1013، أو أسوأ، يُنفَّذ بتقريب المنصة الخاص فيُغيّر الحجم
        الفعلي عن المخطَّط بصمت.
        """
        lim = self.cfg.limits
        entry, stop = signal.entry_reference_price, signal.stop_loss
        if not entry or not stop or stop >= entry:
            return {'qty': 0, 'notional': 0, 'reason': 'وقف أو دخول غير صالح'}

        risk_amount = equity * lim.max_risk_per_trade_pct / 100
        risk_per_unit = entry - stop
        qty_by_risk = risk_amount / risk_per_unit
        notional = min(qty_by_risk * entry, lim.max_position_notional)
        available = equity - lim.min_reserve_quote
        notional = max(0.0, min(notional, available))
        if notional <= 0:
            return {'qty': 0, 'notional': 0, 'reason': 'لا رصيد متاح بعد الاحتياطي'}

        try:
            rules = self.client.rules(signal.symbol)
        except Exception as e:
            # لا نخمّن حداً أدنى بديلاً — الفشل في قراءة قواعد المنصة
            # يمنع الحجم بدل المتابعة برقم غير مُتحقَّق منه
            return {'qty': 0, 'notional': 0,
                   'reason': f'تعذّر قراءة قواعد المنصة: {type(e).__name__}'}

        raw_qty = notional / entry
        qty = self.client.round_qty(signal.symbol, raw_qty)
        notional = qty * entry

        min_notional = rules.get('min_notional', 10.0)
        min_qty = rules.get('min_qty', 0.0)
        if qty < min_qty:
            return {'qty': 0, 'notional': 0,
                   'reason': f'الكمية {qty} أقل من min_qty={min_qty}'}
        if notional < min_notional:
            return {'qty': 0, 'notional': 0,
                   'reason': f'قيمة الصفقة ${notional:.2f} أقل من '
                             f'min_notional=${min_notional}'}
        return {'qty': qty, 'notional': notional, 'reason': 'ok'}

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
