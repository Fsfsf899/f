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

PRIORITY = (BREAKOUT_RETEST, BREAKOUT, PULLBACK, BASELINE)


class EntryRouter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.baseline = BaselineEntryModel(cfg)
        self.breakout = BreakoutEntryModel(cfg) if cfg.breakout.enabled else None
        self.pullback = PullbackEntryModel(cfg) if cfg.pullback.enabled else None

    def evaluate(self, data, idx: int, context: Dict[str, Any]) -> EntrySignal:
        candidates: Dict[str, EntrySignal] = {}

        if self.breakout is not None:
            bo = self.breakout.evaluate(data, idx, context)
            if self.cfg.breakout.retest_enabled:
                bo = process_breakout_retest(data, idx, context, bo)
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
