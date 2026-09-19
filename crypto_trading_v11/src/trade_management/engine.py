"""
محرك إدارة الصفقة التكيّفية — Part B، البنود 9-22.

محرك **واحد** يستدعيه الباكتست والتنفيذ الحي بنفس المدخلات فيُخرج
نفس القرار. هذا ليس ترفاً معمارياً: المشروع اكتشف ست مرّات مكوّناً
مبنيّاً ومختبَراً لكنه غير موصول بالمسار الذي يعمل فعلاً، ومرّة
واحدة (التعادل) مطبَّقاً في الباكتست وحده فتفرّق سلوك البيئتين بصمت.

قواعد غير قابلة للتفاوض (Part C البندان 15 و16):
  • الوقف لا يتّسع أبداً. أي مرشّح ≤ الوقف الحالي يُهمَل.
  • هذه الطبقة لا تُلغي طبقة الطوارئ ولا OCO ولا محرّك المخاطرة —
    تُستدعى بعدها، وتقترح فقط.
  • التعادل تعادل **صافٍ**: بعد الرسوم والانزلاق والفرق السعري.
    `stop = entry` ليس تعادلاً، بل خسارة مضمونة بمقدار التكاليف.
"""
from typing import List, Optional, Sequence

from ..core.config import AdaptiveConfig, Config
from ..backtest.costs import CostModel
from ..market.regime import (TRENDING_BULL, RANGING, LOW_VOL, HIGH_VOL,
                             TRANSITION, UNKNOWN)
from .hysteresis import confirm
from .types import (BREAK_EVEN, ExitDecision, HOLD, MarketView, NO_ACTION,
                    R_DISABLED, R_INSUFFICIENT, R_NOT_STAGNANT, R_NO_ATR,
                    R_MOVE_TOO_SMALL, R_RR_GUARD, R_STRONG_TREND,
                    R_TARGET_BELOW_PX,
                    R_WOULD_WIDEN, REDUCE_TARGET, STAGNATION_EXIT,
                    TRAILING_STOP, TradeState)

# الحالات التي يُسمح فيها بتقريب الهدف (البند 6)
RANGE_LIKE = {RANGING, LOW_VOL, TRANSITION}


class AdaptiveTradeManager:
    """
    لا يملك حالة بين الاستدعاءات. كل قرار يُشتقّ من `TradeState`
    و`MarketView` وحدهما، فالنتيجة قابلة لإعادة الإنتاج حرفياً
    (Part C البند 14) وناجية من إعادة التشغيل بلا ذاكرة مخفية.
    """

    def __init__(self, cfg: Optional[Config] = None,
                 costs: Optional[CostModel] = None):
        self.cfg = cfg or Config()
        self.costs = costs or CostModel(self.cfg.costs)

    # ────────────────────────── التعادل الصافي ──────────────────────────
    def net_breakeven_stop(self, state: TradeState, vol_mult: float = 1.0,
                           buffer_pct: Optional[float] = None) -> float:
        """
        أدنى سعر **تفعيل** وقف يضمن ربحاً صافياً ≥ الهامش المطلوب.

        يُشتقّ معامل التنفيذ من `CostModel.sell()` نفسه بدل إعادة
        كتابة صيغة الرسوم/الانزلاق هنا — فلو تغيّر نموذج التكاليف
        يوماً تحرّك هذا الحساب معه تلقائياً، ولا يمكن أن يتباعدا.
        """
        acfg = self.cfg.adaptive
        buf = acfg.break_even_buffer_pct if buffer_pct is None else buffer_pct
        # التنفيذ عند سعر مرجعي 1.0 وكمية 1.0 ⇒ العائد الصافي لكل وحدة سعر
        probe = self.costs.sell(1.0, 1.0, vol_mult, is_stop=True)
        net_per_unit_price = probe.notional - probe.fee      # = k·(1−f)
        if net_per_unit_price <= 0:
            return float('inf')
        qty = max(state.qty, 1e-12)
        need = (state.entry_price * qty + state.entry_fee
                + buf / 100.0 * state.entry_price * qty)
        return need / (net_per_unit_price * qty)

    # ─────────────────────────── الهدف التكيّفي ──────────────────────────
    def _adaptive_target(self, state: TradeState, view: MarketView,
                         regime: str) -> Optional[tuple]:
        """
        يُرجع (هدف_جديد، سبب) أو None إن لم يكن التعديل مبرَّراً.
        لا يرفع الهدف أبداً — الرفع يزيد احتمال عدم التنفيذ ويخالف
        نيّة البند 6 (تقريب الهدف في السوق الضعيف).
        """
        acfg = self.cfg.adaptive
        if not acfg.adaptive_tp_enabled or state.target_adjusted:
            return None

        # البند 8: اتجاه صاعد قوي ⇒ لا تقريب. دع الصفقة تستفيد من الحركة
        # واكتفِ بالتعادل والتتبّع.
        if regime == TRENDING_BULL:
            return None

        candidates: List[tuple] = []

        # (أ) سوق عرضي/ضعيف ⇒ نطاق هدف أقرب قابل للضبط (البند 6)
        if regime in RANGE_LIKE:
            candidates.append((state.entry_price * (1 + acfg.range_tp_max_pct / 100.0),
                               'RANGE_TP'))

        # (ب) مقاومة موثوقة قبل الهدف الأصلي (البند 7) — بهامش ATR لا نسبة
        # ثابتة، فالمسافة المعقولة قبل مستوى ما تتبع تقلّب السوق نفسه.
        if (view.resistance is not None and view.atr > 0
                and view.resistance < state.current_target):
            lvl = view.resistance - acfg.resistance_buffer_atr * view.atr
            candidates.append((lvl, 'RESISTANCE'))

        if not candidates:
            return None

        new_t, why = min(candidates, key=lambda c: c[0])
        if new_t >= state.current_target:
            return None                                   # لا تقريب فعلي

        # لا يهبط الهدف تحت الحد الأدنى المسموح للنطاق
        floor_t = state.entry_price * (1 + acfg.range_tp_min_pct / 100.0)
        if new_t < floor_t:
            new_t = floor_t
            if new_t >= state.current_target:
                return None
        return new_t, why

    def _target_is_viable(self, state: TradeState, view: MarketView,
                          new_target: float) -> tuple:
        """
        البند 20 + Part C البند 2: هدف معدَّل لا يُقبل إلا إذا بقي
        منطقياً بعد التكاليف. الفحص بالعائد/المخاطرة **الصافي**، لا
        الاسمي، وبنفس نموذج التكاليف المستخدَم في التنفيذ.
        """
        acfg = self.cfg.adaptive
        if new_target <= view.price:
            return False, R_TARGET_BELOW_PX
        # هدف تحت نقطة التعادل الصافية = خسارة مضمونة مهما بدا ربحاً اسمياً
        be = self.net_breakeven_stop(state, buffer_pct=0.0)
        if new_target <= be:
            return False, R_TARGET_BELOW_PX

        qty = max(state.qty, 1e-12)
        win = self.costs.sell(new_target, qty, 1.0, is_stop=False)
        lose = self.costs.sell(state.current_stop, qty, 1.0, is_stop=True)
        cost_in = state.entry_price * qty + state.entry_fee
        net_win = (win.notional - win.fee) - cost_in
        net_loss = cost_in - (lose.notional - lose.fee)
        if net_loss <= 0:
            return True, 'RISK_ALREADY_ZERO'     # الوقف فوق التعادل أصلاً
        rr = net_win / net_loss
        if rr < acfg.min_adaptive_rr:
            return False, R_RR_GUARD
        return True, f'NET_RR={rr:.2f}'

    # ─────────────────────────── حارس الركود ────────────────────────────
    def _stagnation(self, state: TradeState, view: MarketView,
                    regime: str) -> Optional[str]:
        """
        الركود ليس مرور الوقت وحده (البند 16). يشترط اجتماع: انقضاء
        المدة، وضعف أقصى ربح عائم، وضعف التقدّم الحالي.
        """
        acfg = self.cfg.adaptive
        if not acfg.stagnation_enabled or not state.opened_ms or not view.now_ms:
            return None
        hours = (view.now_ms - state.opened_ms) / 3_600_000.0
        if hours < acfg.max_trade_duration_hours:
            return None

        progress = (view.price - state.entry_price) / state.entry_price * 100.0
        if state.mfe_pct >= acfg.min_mfe_pct:
            return None
        if progress >= acfg.min_progress_pct:
            return None

        # البند 17: لا إغلاق أعمى. اتجاه صاعد مؤكَّد يُبقي الصفقة.
        if regime == TRENDING_BULL:
            return R_STRONG_TREND
        return 'STAGNANT'

    # ──────────────────────────── القرار الموحَّد ─────────────────────────
    def evaluate(self, state: TradeState, view: MarketView,
                 regime_labels: Optional[Sequence[str]] = None,
                 vol_mult: float = 1.0) -> ExitDecision:
        """
        الأولوية القطعية (البند 22)، بعد طبقة الطوارئ وOCO دائماً:
          ١. حماية التعادل   ٢. التتبّع   ٣. تقريب الهدف   ٤. الركود   ٥. إبقاء
        """
        acfg: AdaptiveConfig = self.cfg.adaptive
        if not acfg.enabled:
            return ExitDecision(HOLD, reason=R_DISABLED,
                                reason_ar='الإدارة التكيّفية معطَّلة')

        # حالة السوق المثبَّتة — أو UNKNOWN صراحةً عند نقص البيانات.
        # البند 4: ممنوع افتراض حالة بلا بيانات كافية.
        if regime_labels:
            conf = confirm(regime_labels, acfg)
            regime, regime_bars = conf.regime, conf.bars_in_regime
        else:
            regime, regime_bars = (view.regime or UNKNOWN), 0

        audit = {'regime': regime, 'regime_bars': regime_bars,
                 'regime_confidence': round(view.regime_confidence, 4),
                 'price': view.price, 'atr': view.atr,
                 'mfe_pct': round(state.mfe_pct, 4),
                 'mae_pct': round(state.mae_pct, 4),
                 'current_stop': state.current_stop,
                 'current_target': state.current_target}

        r_unit = state.r_unit
        if r_unit <= 0:
            return ExitDecision(HOLD, reason=R_INSUFFICIENT, audit=audit,
                                reason_ar='وحدة مخاطرة غير صالحة')

        new_stop: Optional[float] = None
        stop_reason = ''

        # ١) التعادل الصافي (البند 9)
        if acfg.break_even_enabled and not state.breakeven_done:
            reached = (state.mfe_pct / 100.0 * state.entry_price
                       >= acfg.break_even_trigger_r * r_unit)
            if reached:
                be = self.net_breakeven_stop(state, vol_mult)
                audit['net_breakeven_stop'] = round(be, 8)
                audit['naive_breakeven'] = state.entry_price
                # لا يُنقل الوقف إلى التعادل إن كان ذلك سيضعه فوق السعر
                # الحالي — أمر وقف يُفعَّل فوراً ترفضه المنصة، والخروج
                # الفوري قرار مختلف تماماً لا يملكه هذا المحرك.
                if be > state.current_stop and be < view.price:
                    new_stop, stop_reason = be, BREAK_EVEN

        # ٢) التتبّع بـ ATR (البنود 11-14) — يستخدم إعدادات
        # `SignalConfig` القائمة، بلا إعداد مكرّر (البند 37).
        scfg = self.cfg.signal
        if scfg.trailing_stop_enabled and view.atr > 0:
            activated = (state.mfe_pct / 100.0 * state.entry_price
                         >= scfg.trailing_activate_at_r * r_unit)
            if activated:
                cand = view.price - scfg.trailing_atr_mult * view.atr
                audit['trailing_candidate'] = round(cand, 8)
                base = new_stop if new_stop is not None else state.current_stop
                if cand > base and cand < view.price:
                    new_stop, stop_reason = cand, TRAILING_STOP
        elif scfg.trailing_stop_enabled and view.atr <= 0:
            audit['trailing_skipped'] = R_NO_ATR

        # الحارس المطلق (Part C البند 16): لا اتّساع للمخاطرة، أبداً.
        if new_stop is not None and new_stop <= state.current_stop:
            audit['stop_rejected'] = R_WOULD_WIDEN
            new_stop, stop_reason = None, ''

        # عتبة الجدوى: نقل الوقف ليس مجانياً — إلغاء OCO ووضع آخر،
        # ومعهما نافذة انكشاف. تحسّن أصغر من العتبة لا يستحق الثمن.
        if new_stop is not None:
            gain = (new_stop - state.current_stop) / state.entry_price * 100
            audit['stop_gain_pct'] = round(gain, 6)
            if gain < acfg.min_stop_move_pct:
                audit['stop_rejected'] = R_MOVE_TOO_SMALL
                new_stop, stop_reason = None, ''

        # ٣) الهدف التكيّفي (البنود 6 و7 و19 و20)
        new_target: Optional[float] = None
        tgt = self._adaptive_target(state, view, regime)
        if tgt is not None:
            cand_t, why = tgt
            ok, rr_reason = self._target_is_viable(state, view, cand_t)
            audit['target_candidate'] = round(cand_t, 8)
            audit['target_source'] = why
            audit['target_check'] = rr_reason
            if ok:
                new_target = cand_t
            else:
                audit['target_rejected'] = rr_reason

        # ٤) الركود (البنود 15-18)
        stag = self._stagnation(state, view, regime)
        if stag == 'STAGNANT':
            audit['stagnation'] = 'STAGNANT'
            hrs = (view.now_ms - state.opened_ms) / 3_600_000.0
            return ExitDecision(
                STAGNATION_EXIT, new_stop=new_stop, new_target=new_target,
                reason='STAGNATION', audit=audit,
                reason_ar=(f'الصفقة مفتوحة {hrs:.1f} ساعة دون تقدّم: أقصى ربح '
                           f'عائم {state.mfe_pct:.2f}% < {acfg.min_mfe_pct}%، '
                           f'والتقدّم الحالي '
                           f'{(view.price - state.entry_price) / state.entry_price * 100:.2f}%'
                           f' < {acfg.min_progress_pct}%'))
        if stag == R_STRONG_TREND:
            audit['stagnation'] = R_STRONG_TREND

        # ٥) صياغة القرار
        if new_stop is None and new_target is None:
            return ExitDecision(HOLD, reason=NO_ACTION, audit=audit,
                                reason_ar='لا تعديل مبرَّر على الوقف أو الهدف')

        decision = stop_reason or REDUCE_TARGET
        parts = []
        if new_stop is not None:
            move = (new_stop - state.current_stop) / state.entry_price * 100
            if stop_reason == BREAK_EVEN:
                parts.append(f'الوقف إلى التعادل الصافي {new_stop:.8g} '
                             f'(يغطّي الرسوم والانزلاق، لا سعر الدخول وحده)')
            else:
                parts.append(f'الوقف يتبع السعر إلى {new_stop:.8g} '
                             f'({scfg.trailing_atr_mult}×ATR تحت '
                             f'{view.price:.8g})')
            parts.append(f'تحسّن الحماية {move:+.2f}%')
        if new_target is not None:
            parts.append(f'الهدف من {state.current_target:.8g} إلى '
                         f'{new_target:.8g} ({audit.get("target_source")}) '
                         f'— الحالة {regime}')
        return ExitDecision(decision, new_stop=new_stop, new_target=new_target,
                            reason=stop_reason or 'ADAPTIVE_TARGET',
                            reason_ar='؛ '.join(parts), audit=audit)
