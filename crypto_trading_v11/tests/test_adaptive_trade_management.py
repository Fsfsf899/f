"""
اختبارات إدارة الصفقة التكيّفية — Part B البند 39.
==================================================
كل اختبار هنا يُشغِّل المنطق الحقيقي. لا محاكاة لقرار المحرك، ولا
تأكيد على "أن الدالة موجودة" — المشروع فيه ست حالات موثَّقة لمكوّن
مبنيّ ومختبَر لكنه غير موصول، ولن يضيف هذا الملف سابعة.

البيانات الاصطناعية هنا لتشغيل الآلية فقط، لا لإثبات أي أداء.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from src.core.config import AdaptiveConfig, Config
from src.backtest.costs import CostModel
from src.market.regime import (detect_at, HIGH_VOL, RANGING, TRENDING_BULL,
                               UNKNOWN)
from src.trade_management import (AdaptiveTradeManager, MarketView, TradeState,
                                  confirm, confirm_series)
from src.trade_management.types import (BREAK_EVEN, HOLD, REDUCE_TARGET,
                                        STAGNATION_EXIT, TRAILING_STOP)


def cfg_on(**kw) -> Config:
    """إعداد بالطبقة التكيّفية مفعَّلة — الافتراضي معطَّل عمداً."""
    c = Config()
    c.adaptive.enabled = True
    for k, v in kw.items():
        setattr(c.adaptive, k, v)
    return c


def open_trade(cfg: Config, entry_ideal=100.0, qty=10.0,
               stop_pct=2.0, target_pct=4.0) -> TradeState:
    """مركز مفتوح بسعر تنفيذ ورسوم حقيقية من نموذج التكاليف نفسه."""
    costs = CostModel(cfg.costs)
    buy = costs.buy(entry_ideal, qty)
    e = buy.price
    return TradeState(entry_price=e, qty=qty,
                      initial_stop=e * (1 - stop_pct / 100),
                      current_stop=e * (1 - stop_pct / 100),
                      initial_target=e * (1 + target_pct / 100),
                      current_target=e * (1 + target_pct / 100),
                      entry_fee=buy.fee, opened_ms=1_700_000_000_000)


# ══════════════════ 1. حالة السوق والتهدئة ══════════════════
class Test01_Regime(unittest.TestCase):

    @staticmethod
    def _series(kind, n=400):
        rng = np.random.default_rng(3)
        if kind == 'up':
            return 100 * np.cumprod(1 + rng.normal(0.0035, 0.004, n))
        if kind == 'range':
            return 100 + np.sin(np.arange(n) / 9) * 2.0 + rng.normal(0, 0.15, n)
        if kind == 'wild':
            return 100 * np.cumprod(1 + rng.normal(0.0, 0.09, n))
        return np.full(n, 100.0)

    def test_trending_up_detection(self):
        c = Config()
        close = self._series('up')
        adx = np.full(len(close), 45.0)
        r = detect_at(close, len(close) - 1, c.regime, adx, '1h')
        self.assertEqual(r.regime, TRENDING_BULL)
        self.assertTrue(r.long_friendly)

    def test_ranging_detection(self):
        c = Config()
        close = self._series('range')
        adx = np.full(len(close), 12.0)
        r = detect_at(close, len(close) - 1, c.regime, adx, '1h')
        self.assertIn(r.regime, (RANGING, 'LOW_VOLATILITY', 'TRANSITION'))
        self.assertFalse(r.long_friendly)

    def test_weak_trend_detection(self):
        """ميل موجود لكن ADX ضعيف ⇒ لا يُصنَّف اتجاهاً."""
        c = Config()
        close = self._series('up')
        weak = np.full(len(close), 8.0)      # دون adx_trend_min
        r = detect_at(close, len(close) - 1, c.regime, weak, '1h')
        self.assertNotEqual(r.regime, TRENDING_BULL)

    def test_high_volatility_detection(self):
        c = Config()
        close = self._series('wild')
        adx = np.full(len(close), 30.0)
        r = detect_at(close, len(close) - 1, c.regime, adx, '1h')
        self.assertEqual(r.regime, HIGH_VOL)
        self.assertFalse(r.long_friendly)

    def test_insufficient_data_fail_closed(self):
        """البند 4: ممنوع افتراض حالة بلا بيانات كافية."""
        c = Config()
        short = np.full(5, 100.0)
        r = detect_at(short, 4, c.regime, None, '1h')
        self.assertEqual(r.regime, UNKNOWN)
        self.assertEqual(r.confidence, 0.0)
        self.assertFalse(r.long_friendly)

    def test_hysteresis_rejects_brief_flip(self):
        """وميض شمعتين لا يقلب الحالة (Part C البند 1)."""
        a = AdaptiveConfig(regime_confirmation_candles=3, regime_min_duration=3)
        labels = [RANGING] * 10 + [TRENDING_BULL] * 2 + [RANGING] * 5
        self.assertEqual(confirm(labels, a).regime, RANGING)

    def test_hysteresis_adopts_sustained_change(self):
        a = AdaptiveConfig(regime_confirmation_candles=3, regime_min_duration=3)
        labels = [RANGING] * 10 + [TRENDING_BULL] * 6
        self.assertEqual(confirm(labels, a).regime, TRENDING_BULL)

    def test_hysteresis_is_causal(self):
        """
        الحكم عند i لا يتغيّر بإضافة شمعات بعده — شرط انعدام النظر
        للأمام، ويُفحص على كل بادئة لا على الطرف وحده.
        """
        a = AdaptiveConfig(regime_confirmation_candles=3, regime_min_duration=2)
        labels = ([RANGING] * 7 + [TRENDING_BULL] * 4 + [HIGH_VOL] * 5
                  + [RANGING] * 6)
        full = confirm_series(labels, a)
        for i in range(1, len(labels) + 1):
            self.assertEqual(confirm_series(labels[:i], a)[-1], full[i - 1],
                             f'حكم الشمعة {i - 1} تغيّر بمعرفة المستقبل')

    def test_hysteresis_matches_single_call(self):
        """`confirm` و`confirm_series` مشية واحدة — لا يجوز أن يتباعدا."""
        a = AdaptiveConfig(regime_confirmation_candles=2, regime_min_duration=4)
        labels = [RANGING, RANGING, TRENDING_BULL, TRENDING_BULL, HIGH_VOL,
                  HIGH_VOL, HIGH_VOL, RANGING, RANGING, RANGING]
        for i in range(1, len(labels) + 1):
            self.assertEqual(confirm(labels[:i], a).regime,
                             confirm_series(labels[:i], a)[-1])


# ══════════════════ 2. التعادل الصافي ══════════════════
class Test02_BreakEven(unittest.TestCase):

    def test_break_even_activation(self):
        cfg = cfg_on(break_even_trigger_r=1.0)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        r_pct = st.r_unit / st.entry_price * 100

        below = atm.evaluate(st, MarketView(price=st.entry_price * 1.005,
                                            atr=1.0, now_ms=st.opened_ms))
        st.mfe_pct = r_pct * 0.5
        below = atm.evaluate(st, MarketView(price=st.entry_price * 1.005,
                                            atr=1.0, now_ms=st.opened_ms))
        self.assertEqual(below.decision, HOLD, 'تعادل قبل تحقق 1R')

        st.mfe_pct = r_pct * 1.2
        above = atm.evaluate(st, MarketView(price=st.entry_price * 1.03,
                                            atr=1.0, now_ms=st.opened_ms))
        self.assertEqual(above.decision, BREAK_EVEN)
        self.assertGreater(above.new_stop, st.current_stop)

    def test_break_even_includes_costs(self):
        """
        البند 9: `stop = entry` ليس تعادلاً. الوقف المحسوب يجب أن
        يكون **أعلى** من سعر الدخول بما يغطي الرسوم والانزلاق.
        """
        cfg = cfg_on()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        be = atm.net_breakeven_stop(st)
        self.assertGreater(be, st.entry_price,
                           'التعادل الصافي يجب أن يفوق سعر الدخول')

    def test_break_even_actually_breaks_even(self):
        """
        الاختبار الحاسم: نفّذ الخروج فعلياً عند الوقف المحسوب بنفس
        نموذج التكاليف، وتحقّق أن الربح الصافي ≥ 0 — وأن الطريقة
        الساذجة تخسر.
        """
        cfg = cfg_on(break_even_buffer_pct=0.05)
        costs = CostModel(cfg.costs)
        atm = AdaptiveTradeManager(cfg, costs)
        st = open_trade(cfg)
        cost_in = st.entry_price * st.qty + st.entry_fee

        def net(trigger):
            f = costs.sell(trigger, st.qty, 1.0, is_stop=True)
            return (f.notional - f.fee) - cost_in

        self.assertGreaterEqual(net(atm.net_breakeven_stop(st)), 0.0,
                                'التعادل الصافي خرج بخسارة')
        self.assertLess(net(st.entry_price), 0.0,
                        'stop=entry يجب أن يخسر — وإلا فنموذج التكاليف معطّل')

    def test_tiny_improvement_does_not_churn_orders(self):
        """
        نقطة التعادل الصافية تتبع انزلاق الوقف، وهو يتبع تقلّب الشمعة،
        فتتذبذب ~٠٫٣٪ من سعر الدخول بين شمعة وأخرى. بلا عتبة جدوى
        يُترجَم كل فرق موجب ضئيل إلى إلغاء OCO ووضع آخر — استهلاك
        لحدود المعدّل، ونافذة انكشاف جديدة، مقابل حماية لا تُذكر.
        """
        cfg = cfg_on(break_even_trigger_r=0.1, min_stop_move_pct=0.10)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 5.0
        v = MarketView(price=st.entry_price * 1.03, atr=0.5,
                       now_ms=st.opened_ms)

        first = atm.evaluate(st, v, vol_mult=1.0)
        self.assertEqual(first.decision, BREAK_EVEN)
        st.current_stop = first.new_stop

        # شمعة أكثر تقلّباً ⇒ تعادل أعلى قليلاً، لكن دون العتبة
        second = atm.evaluate(st, v, vol_mult=1.5)
        self.assertIsNone(second.new_stop, 'أمر جديد لمكسب تافه')
        self.assertEqual(second.audit.get('stop_rejected'),
                         'MOVE_BELOW_THRESHOLD')
        self.assertLess(second.audit['stop_gain_pct'], 0.10)

    def test_threshold_still_allows_real_improvement(self):
        """الضابط المقابل: تحسّن معتبر يمرّ — العتبة ليست تعطيلاً."""
        cfg = cfg_on(break_even_trigger_r=0.1, min_stop_move_pct=0.10)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 5.0
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.03, atr=0.5,
                                        now_ms=st.opened_ms))
        self.assertEqual(d.decision, BREAK_EVEN)
        self.assertGreater(d.audit['stop_gain_pct'], 0.10)

    def test_break_even_never_widens_risk(self):
        """وقف حاليّ أعلى من التعادل ⇒ لا تراجع (Part C البند 16)."""
        cfg = cfg_on()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 10.0
        st.current_stop = st.entry_price * 1.03      # مؤمَّن سلفاً فوق التعادل
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.05, atr=1.0,
                                        now_ms=st.opened_ms))
        if d.new_stop is not None:
            self.assertGreater(d.new_stop, st.current_stop)

    def test_break_even_not_placed_above_price(self):
        """وقف فوق السعر الحالي ترفضه المنصة — لا يُقترح أصلاً."""
        cfg = cfg_on()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 10.0
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.0001, atr=1.0,
                                        now_ms=st.opened_ms))
        if d.new_stop is not None:
            self.assertLess(d.new_stop, st.entry_price * 1.0001)


# ══════════════════ 3. التتبّع ══════════════════
class Test03_Trailing(unittest.TestCase):

    @staticmethod
    def _cfg():
        c = cfg_on(break_even_enabled=False)
        c.signal.trailing_stop_enabled = True
        c.signal.trailing_atr_mult = 2.0
        c.signal.trailing_activate_at_r = 1.0
        return c

    def test_atr_trailing(self):
        cfg = self._cfg()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = st.r_unit / st.entry_price * 100 * 1.5
        px, atr = st.entry_price * 1.04, 0.5
        d = atm.evaluate(st, MarketView(price=px, atr=atr, now_ms=st.opened_ms))
        self.assertEqual(d.decision, TRAILING_STOP)
        self.assertAlmostEqual(d.new_stop, px - 2.0 * atr, places=8)

    def test_trailing_only_moves_forward(self):
        cfg = self._cfg()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = st.r_unit / st.entry_price * 100 * 3
        st.current_stop = st.entry_price * 1.03      # تتبّع سابق رفع الوقف
        # السعر تراجع ⇒ المرشّح أدنى من الوقف الحالي
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.01, atr=0.5,
                                        now_ms=st.opened_ms))
        self.assertIsNone(d.new_stop, 'التتبّع وسّع المخاطرة')

    def test_trailing_does_not_widen_stop(self):
        """فحص شامل: أي سعر/ATR، لا يخرج وقف أدنى من الحالي."""
        cfg = self._cfg()
        atm = AdaptiveTradeManager(cfg)
        for mult in (0.5, 1.0, 2.0, 5.0):
            for up in (1.0, 1.02, 1.05, 1.20):
                st = open_trade(cfg)
                st.mfe_pct = st.r_unit / st.entry_price * 100 * 3
                cfg.signal.trailing_atr_mult = mult
                d = atm.evaluate(st, MarketView(price=st.entry_price * up,
                                                atr=1.0, now_ms=st.opened_ms))
                if d.new_stop is not None:
                    self.assertGreater(d.new_stop, st.current_stop,
                                       f'mult={mult} up={up}')

    def test_trailing_respects_risk(self):
        """الوقف المتتبِّع لا يوضع فوق السعر الحالي أبداً."""
        cfg = self._cfg()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 50.0
        px = st.entry_price * 1.10
        d = atm.evaluate(st, MarketView(price=px, atr=0.0001, now_ms=st.opened_ms))
        if d.new_stop is not None:
            self.assertLess(d.new_stop, px)

    def test_trailing_inactive_without_atr(self):
        cfg = self._cfg()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 50.0
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.05, atr=0.0,
                                        now_ms=st.opened_ms))
        self.assertIsNone(d.new_stop)


# ══════════════════ 4. الهدف التكيّفي ══════════════════
class Test04_AdaptiveTP(unittest.TestCase):

    def test_range_market_reduces_target(self):
        # وقف ضيّق عمداً: هدف 1.5% مقابل وقف 2% عائد/مخاطرة 0.54 —
        # وحارس البند 20 يرفضه بحق. المقاس هنا آلية التقريب نفسها.
        cfg = cfg_on(break_even_enabled=False, range_tp_max_pct=1.5)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg, stop_pct=0.8, target_pct=4.0)
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.005, atr=0.5,
                                        regime=RANGING, now_ms=st.opened_ms))
        self.assertEqual(d.decision, REDUCE_TARGET)
        self.assertLess(d.new_target, st.current_target)

    def test_strong_trend_preserves_target(self):
        """البند 8: لا تقريب هدف في اتجاه صاعد."""
        cfg = cfg_on(break_even_enabled=False)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg, target_pct=4.0)
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.01, atr=0.5,
                                        regime=TRENDING_BULL,
                                        now_ms=st.opened_ms))
        self.assertIsNone(d.new_target)

    def test_resistance_adjustment(self):
        cfg = cfg_on(break_even_enabled=False, resistance_buffer_atr=0.25)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg, stop_pct=0.8, target_pct=6.0)
        res, atr = st.entry_price * 1.03, 0.4
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.005, atr=atr,
                                        regime=RANGING, resistance=res,
                                        now_ms=st.opened_ms))
        self.assertIsNotNone(d.new_target)
        self.assertLessEqual(d.new_target, res, 'الهدف تجاوز المقاومة')

    def test_rr_guard(self):
        """البند 20: هدف يهبط بالعائد/المخاطرة تحت الحد يُرفض."""
        cfg = cfg_on(break_even_enabled=False, min_adaptive_rr=5.0,
                     range_tp_min_pct=0.2, range_tp_max_pct=0.3)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg, stop_pct=3.0, target_pct=6.0)
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.001, atr=0.4,
                                        regime=RANGING, now_ms=st.opened_ms))
        self.assertIsNone(d.new_target)
        self.assertIn('target_rejected', d.audit)

    def test_fee_aware_target(self):
        """هدف تحت نقطة التعادل الصافية مرفوض مهما بدا ربحاً اسمياً."""
        cfg = cfg_on(break_even_enabled=False, range_tp_min_pct=0.01,
                     range_tp_max_pct=0.02, min_adaptive_rr=0.0)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg, target_pct=5.0)
        be = atm.net_breakeven_stop(st, buffer_pct=0.0)
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.0001, atr=0.3,
                                        regime=RANGING, now_ms=st.opened_ms))
        if d.new_target is not None:
            self.assertGreater(d.new_target, be)
        else:
            self.assertIn('target_rejected', d.audit)

    def test_wide_stop_blocks_target_reduction(self):
        """
        النتيجة العملية لحارس البند 20، مُقاسة لا مفترضة: مع وقف 2%
        يصبح هدف 1.5% عائداً/مخاطرة 0.54، فيُرفض التقريب ويبقى الهدف
        الأصلي. أي أن الهدف التكيّفي **لا يعمل** إلا بعد أن تنكمش
        المخاطرة (وقف ضيّق أصلاً، أو تعادل رفع الوقف). هذا سلوك
        مقصود، ومُوثَّق هنا كي لا يُقرأ يوماً على أنه عطل.
        """
        cfg = cfg_on(break_even_enabled=False, range_tp_max_pct=1.5)
        atm = AdaptiveTradeManager(cfg)
        wide = open_trade(cfg, stop_pct=2.0, target_pct=4.0)
        d = atm.evaluate(wide, MarketView(price=wide.entry_price * 1.005,
                                          atr=0.5, regime=RANGING,
                                          now_ms=wide.opened_ms))
        self.assertIsNone(d.new_target)
        self.assertEqual(d.audit.get('target_rejected'), 'RR_BELOW_MIN')

    def test_breakeven_stop_unlocks_target_reduction(self):
        """
        المقابل: بعد أن يرفع التعادل الوقف، تنكمش المخاطرة فيصبح
        الهدف الأقرب مقبولاً بنفس الإعداد بالضبط.
        """
        cfg = cfg_on(break_even_enabled=False, range_tp_max_pct=1.5)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg, stop_pct=2.0, target_pct=4.0)
        st.current_stop = atm.net_breakeven_stop(st)     # التعادل تحقّق
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.008, atr=0.5,
                                        regime=RANGING, now_ms=st.opened_ms))
        self.assertIsNotNone(d.new_target)
        self.assertLess(d.new_target, st.current_target)

    def test_target_never_raised(self):
        cfg = cfg_on(break_even_enabled=False, range_tp_max_pct=9.0)
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg, target_pct=2.0)
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.001, atr=0.3,
                                        regime=RANGING, now_ms=st.opened_ms))
        if d.new_target is not None:
            self.assertLess(d.new_target, st.current_target)


# ══════════════════ 5. الركود ══════════════════
class Test05_Stagnation(unittest.TestCase):

    @staticmethod
    def _cfg():
        return cfg_on(break_even_enabled=False, max_trade_duration_hours=10.0,
                      min_progress_pct=0.30, min_mfe_pct=0.50)

    def test_timeout_guard(self):
        cfg = self._cfg()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 0.1
        early = atm.evaluate(st, MarketView(
            price=st.entry_price, atr=0.3, regime=RANGING,
            now_ms=st.opened_ms + 5 * 3_600_000))
        self.assertNotEqual(early.decision, STAGNATION_EXIT)

        late = atm.evaluate(st, MarketView(
            price=st.entry_price, atr=0.3, regime=RANGING,
            now_ms=st.opened_ms + 20 * 3_600_000))
        self.assertEqual(late.decision, STAGNATION_EXIT)

    def test_stagnation_detection(self):
        cfg = self._cfg()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 0.05
        d = atm.evaluate(st, MarketView(
            price=st.entry_price * 1.0005, atr=0.3, regime=RANGING,
            now_ms=st.opened_ms + 30 * 3_600_000))
        self.assertEqual(d.decision, STAGNATION_EXIT)
        self.assertTrue(d.is_exit)
        self.assertIn('ساعة', d.reason_ar)

    def test_stagnation_requires_low_progress(self):
        """البند 16: مرور الوقت وحده لا يكفي."""
        cfg = self._cfg()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 3.0                   # ربح عائم جيّد تحقّق فعلاً
        d = atm.evaluate(st, MarketView(
            price=st.entry_price * 1.02, atr=0.3, regime=RANGING,
            now_ms=st.opened_ms + 40 * 3_600_000))
        self.assertNotEqual(d.decision, STAGNATION_EXIT)

    def test_stagnation_does_not_exit_strong_trend(self):
        """البند 17: لا إغلاق أعمى داخل اتجاه صاعد مؤكَّد."""
        cfg = self._cfg()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 0.05
        d = atm.evaluate(st, MarketView(
            price=st.entry_price, atr=0.3, regime=TRENDING_BULL,
            now_ms=st.opened_ms + 40 * 3_600_000))
        self.assertNotEqual(d.decision, STAGNATION_EXIT)
        self.assertEqual(d.audit.get('stagnation'), 'STRONG_TREND_HOLD')


# ══════════════════ 6. البوابات العامة ══════════════════
class Test06_Gates(unittest.TestCase):

    def test_disabled_by_default(self):
        """إضافة سلوكية لا تُفعَّل بلا قرار صريح."""
        self.assertFalse(Config().adaptive.enabled)
        atm = AdaptiveTradeManager(Config())
        st = open_trade(Config())
        st.mfe_pct = 50.0
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.1, atr=1.0))
        self.assertEqual(d.decision, HOLD)
        self.assertIsNone(d.new_stop)
        self.assertIsNone(d.new_target)

    def test_invalid_r_unit_holds(self):
        cfg = cfg_on()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.initial_stop = st.entry_price * 1.5      # وقف فوق الدخول = فاسد
        d = atm.evaluate(st, MarketView(price=st.entry_price, atr=1.0))
        self.assertEqual(d.decision, HOLD)
        self.assertIsNone(d.new_stop)

    def test_decision_is_deterministic(self):
        """Part C البند 14: نفس المدخلات ⇒ نفس القرار حرفياً."""
        cfg = cfg_on()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 5.0
        v = MarketView(price=st.entry_price * 1.03, atr=0.5, regime=RANGING,
                       now_ms=st.opened_ms)
        a = atm.evaluate(st, v).to_dict()
        b = atm.evaluate(st, v).to_dict()
        self.assertEqual(a, b)

    def test_audit_has_no_secrets(self):
        """البند 36: سجل القرار لا يحمل أي سرّ."""
        cfg = cfg_on()
        atm = AdaptiveTradeManager(cfg)
        st = open_trade(cfg)
        st.mfe_pct = 5.0
        d = atm.evaluate(st, MarketView(price=st.entry_price * 1.03, atr=0.5,
                                        now_ms=st.opened_ms))
        blob = repr(d.to_dict()).lower()
        for bad in ('api_key', 'secret', 'signature', 'apikey', 'token'):
            self.assertNotIn(bad, blob)


if __name__ == '__main__':
    unittest.main(verbosity=2)


# ══════════════════ ٧. تبريد ما بعد الخروج ══════════════════
class Test07_PostExitCooldown(unittest.TestCase):
    """
    Part C البند ٤. بعد وقف أو خروج ركود، الشرط الذي أخرجنا غالباً ما
    يزال قائماً — الدخول فوراً يعيد إنتاج الصفقة الخاسرة نفسها.
    """

    @staticmethod
    def _guard(bars=3, reasons=('STOP_LOSS', 'STAGNATION_EXIT')):
        from src.core.config import RiskConfig
        from src.risk.risk_guard import RiskGuard
        return RiskGuard(RiskConfig(post_exit_cooldown_bars=bars,
                                    post_exit_cooldown_reasons=reasons))

    def test_disabled_by_default(self):
        from src.core.config import RiskConfig
        self.assertEqual(RiskConfig().post_exit_cooldown_bars, 0)

    def test_cooldown_counts_down_in_bars(self):
        g = self._guard(3)
        g.record_trade(-5.0, exit_reason='STOP_LOSS', exit_ms=1000)
        self.assertEqual(g.cooldown_remaining_bars(1000, 100), 3)
        self.assertEqual(g.cooldown_remaining_bars(1100, 100), 2)
        self.assertEqual(g.cooldown_remaining_bars(1300, 100), 0)
        self.assertEqual(g.cooldown_remaining_bars(9999, 100), 0)

    def test_take_profit_is_not_punished(self):
        """الخروج بالهدف ليس إشارة سوء — لا يستحق عقوبة انتظار."""
        g = self._guard(5)
        g.record_trade(10.0, exit_reason='TAKE_PROFIT', exit_ms=1000)
        self.assertEqual(g.cooldown_remaining_bars(1000, 100), 0)

    def test_stagnation_exit_triggers_cooldown(self):
        g = self._guard(4)
        g.record_trade(-1.0, exit_reason='STAGNATION_EXIT', exit_ms=1000)
        self.assertEqual(g.cooldown_remaining_bars(1000, 100), 4)

    def test_zero_bars_never_blocks(self):
        g = self._guard(0)
        g.record_trade(-5.0, exit_reason='STOP_LOSS', exit_ms=1000)
        self.assertEqual(g.cooldown_remaining_bars(1000, 100), 0)

    def test_survives_restart_through_db(self):
        """
        التبريد حالة مخاطرة، فيجب أن ينجو من إعادة التشغيل — وإلا كان
        تجاوزه بإعادة تشغيل العملية.
        """
        import tempfile
        from src.core.config import RiskConfig
        from src.risk.risk_guard import RiskGuard
        from src.storage.database import Database

        db = Database(tempfile.mkdtemp() + '/r.db')
        cfg = RiskConfig(post_exit_cooldown_bars=3)
        g1 = RiskGuard(cfg, db=db)
        g1.record_trade(-5.0, trade_id='t1', exit_reason='STOP_LOSS',
                        exit_ms=1000)
        g2 = RiskGuard(cfg, db=db)                       # عملية جديدة
        self.assertEqual(g2.last_exit_reason, 'STOP_LOSS')
        self.assertEqual(g2.cooldown_remaining_bars(1000, 100), 3)

    def test_backtest_applies_cooldown_and_keeps_equity_intact(self):
        """
        الاختبار الحاسم: التبريد يعمل داخل الباكتست فعلاً، **ولا يُفسد
        منحنى الحقوق**. أول تنفيذ استعمل `continue` فتخطّى سطر
        `equity[i + 1]` — ما كان سيترك حقوق كل شمعة مبرَّدة على القيمة
        الابتدائية، فيُفسد الانخفاض الأقصى وكل ما يُشتقّ منه.
        """
        import numpy as np
        from tests.fixtures import make_fixture
        from src.backtest.engine import BacktestEngine

        data = make_fixture(3000, '1h', seed=11)

        def run(bars):
            c = Config()
            c.signal.min_score = 1.0
            c.no_trade.min_data_quality = 0.5
            c.no_trade.require_btc_ok = False
            c.adaptive.enabled = True
            c.risk.post_exit_cooldown_bars = bars
            return BacktestEngine(c, initial_capital=10000).run(
                data, data_quality=0.95)

        off, on = run(0), run(10)
        self.assertEqual(off.rejections.get('POST_EXIT_COOLDOWN', 0), 0)
        self.assertGreater(on.rejections.get('POST_EXIT_COOLDOWN', 0), 0,
                           'التبريد لم يمنع أي تقييم — غير موصول')
        self.assertLess(len(on.trades), len(off.trades),
                        'التبريد لم يقلّل الصفقات')
        # منحنى الحقوق: لا شمعة بعد أول صفقة بقيت على القيمة الابتدائية
        first_exit = min(t['exit_index'] for t in on.trades)
        after = np.asarray(on.equity[first_exit + 2:], dtype=float)
        self.assertFalse(np.any(after == 10000.0),
                         'حقوق شمعة مبرَّدة بقيت على القيمة الابتدائية')
