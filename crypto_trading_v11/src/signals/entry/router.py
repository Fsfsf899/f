"""
EntryRouter — البند 3. أولوية ثابتة موثَّقة، لا اختيار بالنتيجة
المستقبلية أبداً (كل نموذج يُقيَّم بمعزل عن الآخر عند نفس idx فقط).

الأولوية (الأعلى أولاً):
  1. BREAKOUT_RETEST مؤكَّد هذه اللحظة تحديداً
  2. BREAKOUT مباشر (إن كان retest معطَّلاً أو لم يُفعَّل)
  3. PULLBACK
  4. BASELINE
  5. NO_TRADE (تعارض أو عدم أهلية الجميع)

النماذج المعطَّلة (enabled=False) لا تُستدعى فعلياً — استدعاؤها آمن
(تُرجع رفضاً فورياً بسبب *_DISABLED) لكن تخطّيها أوضح وأسرع.
"""
from typing import Any, Dict, List, Optional

from .base import EntrySignal, BASELINE, BREAKOUT, BREAKOUT_RETEST, PULLBACK, NO_TRADE
from .baseline import BaselineEntryModel
from .breakout import BreakoutEntryModel, process_breakout_retest
from .pullback import PullbackEntryModel
from ...data.types import INTERVAL_MS

PRIORITY = (BREAKOUT_RETEST, BREAKOUT, PULLBACK, BASELINE)


class EntryRouter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.baseline = BaselineEntryModel(cfg)
        self.breakout = BreakoutEntryModel(cfg) if cfg.breakout.enabled else None
        self.pullback = PullbackEntryModel(cfg) if cfg.pullback.enabled else None

    def _retest_context(self, data, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        ⚠️ إصلاح (تدقيق ما بعد V11): `process_breakout_retest()` تقرأ
        `context['cfg']` و`context['symbol']` و`context['interval']`،
        لكن `RouterAsSignalEngine` — الغلاف الذي يستخدمه
        `live_trader.py` حصراً — لم يكن يضع أياً منها. فتفعيل
        `BREAKOUT_RETEST_ENABLED=1` كان يرفع `KeyError: 'cfg'` في كل
        دورة. الحلقة في `run()` تبتلع الاستثناء وتسجّله `TICK_ERROR`
        وتتابع، فالنظام يبدو حيّاً بينما **لا يتخذ أي قرار إطلاقاً**.

        `IsolatedModelAdapter` (مسار البحث) كان يبني السياق كاملاً،
        فالميزة تعمل حيث تُختبَر وتتعطّل حيث تعمل — نفس نمط انحدار
        ربط BTC الموثَّق في CRITICAL_BTC_WIRING_REGRESSION_REPORT.md.

        الحل هنا لا عند كل مستدعٍ: الموجِّه يملك `cfg`، والرمز والفريم
        موجودان في `data` نفسها. ما يمرّره المستدعي صراحةً له الأسبقية.
        """
        return {**context,
                'cfg': context.get('cfg') or self.cfg,
                'symbol': context.get('symbol') or data.symbol,
                'interval': context.get('interval') or data.interval,
                'interval_ms': (context.get('interval_ms')
                                or INTERVAL_MS.get(data.interval, 0))}

    def evaluate(self, data, idx: int, context: Dict[str, Any]) -> EntrySignal:
        candidates: Dict[str, EntrySignal] = {}

        if self.breakout is not None:
            bo = self.breakout.evaluate(data, idx, context)
            if self.cfg.breakout.retest_enabled:
                bo = process_breakout_retest(
                    data, idx, self._retest_context(data, context), bo)
                if bo.setup_type == BREAKOUT_RETEST:
                    candidates[BREAKOUT_RETEST] = bo
                elif bo.eligible:
                    candidates[BREAKOUT] = bo
            elif bo.eligible:
                candidates[BREAKOUT] = bo

        if self.pullback is not None:
            pb = self.pullback.evaluate(data, idx, context)
            if pb.eligible:
                candidates[PULLBACK] = pb

        base = self.baseline.evaluate(data, idx, context)
        if base.eligible:
            candidates[BASELINE] = base

        for setup_type in PRIORITY:
            if setup_type in candidates:
                chosen = candidates[setup_type]
                chosen.diagnostics.setdefault('router', {})
                chosen.diagnostics['router'] = {
                    'candidates_eligible': list(candidates.keys()),
                    'chosen': setup_type, 'priority_order': list(PRIORITY)}
                return chosen

        # لا نموذج مؤهَّل — أعد أكثر تشخيص إفادة (baseline يحمل الأدلة الأغنى)
        # لكن setup_type النهائي يعكس قرار الموجِّه: لا تداول.
        base.diagnostics.setdefault('router', {})
        base.diagnostics['router'] = {'candidates_eligible': [],
                                      'priority_order': list(PRIORITY)}
        base.setup_type = NO_TRADE
        return base
