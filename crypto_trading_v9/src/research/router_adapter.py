"""
محوّل EntryRouter → واجهة SignalEngine — للمقارنة عبر BacktestEngine
الحقيقي دون تعديل BacktestEngine نفسه (بلا منطق مكرَّر أو مسار
تنفيذ ثانٍ). يُستخدَم للبحث فقط؛ ليس جزءاً من مسار Live/Paper.
"""
from typing import Optional

from ..signals.engine import Signal, BUY, WAIT, SignalEngine
from ..signals.entry import EntryRouter, BASELINE
from ..signals.entry.breakout import BreakoutEntryModel, process_breakout_retest

class RouterAsSignalEngine:
    """
    يحقّق نفس واجهة `SignalEngine.evaluate()` التي يتوقعها
    `BacktestEngine`، لكن القرار الفعلي يمر عبر `EntryRouter` —
    فيشمل Breakout/Pullback إن فُعِّلا في `cfg`، بنفس بوابات المخاطر
    المشتركة تماماً (لا تكرار، لا مسار مختصر).
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.router = EntryRouter(cfg)
        self._underlying = self.router.baseline.engine  # لإعادة استخدام prepare()

    def prepare(self, data):
        return self._underlying.prepare(data)

    def evaluate(self, data, idx: int, *, data_quality: float = 1.0,
                prep=None, **kw) -> Signal:
        context = {'prep': prep or self.prepare(data),
                  'data_quality': data_quality, 'account_state': kw.get(
                      'account_state', {})}
        es = self.router.evaluate(data, idx, context)

        decision = BUY if es.eligible else WAIT
        return Signal(
            index=idx, timestamp=int(data.open_time[idx]), symbol=data.symbol,
            interval=data.interval, decision=decision,
            strategy_version=self.cfg.version, score=es.score,
            stars=max(1, min(5, round(es.score))),
            confidence=es.confidence or 0.0,
            raw_probability=None, calibrated_probability=None,
            entry=es.entry_price, stop_loss=es.stop_loss,
            take_profit=es.take_profit, risk_reward=es.net_risk_reward,
            net_risk_reward=es.net_risk_reward,
            atr=(es.diagnostics.get('raw_signal', {}).get('atr')
                if es.setup_type == BASELINE
                else (prep['ind']['atr'][idx] if prep else None)),
            regime=es.diagnostics.get('regime', 'UNKNOWN'),
            data_quality=data_quality,
            evidence=list(es.reasons), reasons=list(es.rejection_reasons))


class IsolatedModelAdapter:
    """
    مقارنة صارمة (البند 5 من هذه المهمة) تتطلب عزل كل نموذج عن Baseline
    فعلياً، لا فقط عن طريق أولوية EntryRouter — لأن BacktestEngine
    يُقيِّم إشارة جديدة فقط عندما `pos is None`، وBaseline يُشغِّل كل
    نافذة تقييم متاحة إن كان مفعَّلاً معه، فيُخفي أي أثر لـ
    Breakout/Pullback بالكامل (موثَّق في STRATEGY_EXPERIMENTS.md —
    92 صفقة متطابقة تماماً بغض النظر عن تفعيلهما). هذا المحوّل يُقيِّم
    **نموذجاً واحداً فقط**، بلا سقوط لـ Baseline إطلاقاً — WAIT صريح
    إن لم يكن مؤهَّلاً، لا تفويض له.

    ⚠️ Retest يتطلب `db` فعلية ليعمل — بدونها `process_breakout_retest`
    لا يُستدعى إطلاقاً (خطأ اكتُشف ذاتياً: أول نسخة من هذا المحوّل كانت
    تتجاوزه بصمت فتعطي نفس نتائج breakout العادي، لا صفراً كما ظُنَّ
    ولا نتائج Retest الحقيقية — قياس مضلِّل، أُصلح بتمرير db إلزامياً
    عند retest_enabled=True).
    """

    def __init__(self, cfg, model, db=None, symbol='BTCUSDT', interval='1h',
                interval_ms: int = 3_600_000):
        self.cfg = cfg
        self.model = model
        self.db = db
        self.symbol, self.interval, self.interval_ms = symbol, interval, interval_ms
        self._underlying = SignalEngine(cfg)
        self._is_breakout_retest = (isinstance(model, BreakoutEntryModel)
                                    and cfg.breakout.retest_enabled)
        if self._is_breakout_retest and db is None:
            raise ValueError(
                'retest_enabled=True يتطلب db فعلية — بدونها القياس مضلِّل '
                '(process_breakout_retest لا يُستدعى إطلاقاً)')

    def prepare(self, data):
        return self._underlying.prepare(data)

    def evaluate(self, data, idx: int, *, data_quality: float = 1.0,
                prep=None, **kw) -> Signal:
        p = prep or self.prepare(data)
        context = {'prep': p, 'data_quality': data_quality,
                  'account_state': kw.get('account_state', {})}
        es = self.model.evaluate(data, idx, context)
        if self._is_breakout_retest:
            ctx2 = {**context, 'db': self.db, 'symbol': self.symbol,
                    'interval': self.interval, 'cfg': self.cfg,
                    'interval_ms': self.interval_ms}
            es = process_breakout_retest(data, idx, ctx2, es)
        decision = BUY if es.eligible else WAIT
        return Signal(
            index=idx, timestamp=int(data.open_time[idx]), symbol=data.symbol,
            interval=data.interval, decision=decision,
            strategy_version=self.cfg.version, score=es.score,
            stars=max(1, min(5, round(max(es.score, 0)))),
            confidence=es.confidence or 0.0,
            entry=es.entry_price, stop_loss=es.stop_loss,
            take_profit=es.take_profit, risk_reward=es.net_risk_reward,
            net_risk_reward=es.net_risk_reward,
            atr=(p['ind']['atr'][idx] if p else None),
            regime=es.diagnostics.get('regime', 'UNKNOWN'),
            data_quality=data_quality,
            evidence=list(es.reasons), reasons=list(es.rejection_reasons))
