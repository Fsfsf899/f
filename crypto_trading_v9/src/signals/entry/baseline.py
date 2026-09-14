"""
Baseline Entry — غلاف رقيق حول SignalEngine الموجود، بلا إعادة كتابة
منطقه. الغرض: توحيد الشكل (EntrySignal) عبر كل النماذج، لا تغيير
سلوك Baseline بحرف واحد.
"""
from typing import Any, Dict

from .base import EntrySignal, BASELINE, SubScores
from ..engine import SignalEngine, BUY


class BaselineEntryModel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.engine = SignalEngine(cfg)

    def evaluate(self, data, idx: int, context: Dict[str, Any]) -> EntrySignal:
        prep = context.get('prep') or self.engine.prepare(data)
        sig = self.engine.evaluate(
            data, idx, data_quality=context.get('data_quality', 1.0),
            account_state=context.get('account_state'),
            btc_ctx=context.get('btc_ctx'), spread_bps=context.get('spread_bps'),
            prep=prep)

        eligible = sig.decision == BUY
        sub = SubScores(
            trend_score=sig.score * 0.3, momentum_score=sig.score * 0.2,
            structure_score=sig.score * 0.3, risk_score=sig.score * 0.2,
            execution_score=0.0)
        return EntrySignal(
            setup_type=BASELINE, eligible=eligible, score=sig.score,
            confidence=sig.confidence,
            entry_price=sig.entry if eligible else None,
            stop_loss=sig.stop_loss if eligible else None,
            take_profit=sig.take_profit if eligible else None,
            net_risk_reward=sig.net_risk_reward,
            reasons=list(sig.evidence) if eligible else [],
            rejection_reasons=list(sig.reasons) if not eligible else [],
            diagnostics={'raw_signal': sig.to_dict()}, sub_scores=sub)
