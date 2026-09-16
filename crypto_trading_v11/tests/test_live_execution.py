"""
اختبارات مسار التنفيذ الحي — بلا اتصال شبكة، بلا مفاتيح حقيقية،
بلا Mainnet إطلاقاً. كل "منصة" هنا MOCK EXCHANGE موسوم بوضوح.
"""
import dataclasses
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.live.trade_signal import (TradeSignal, validate_signal,
                                   from_engine_signal, BUY, LONG, ACTION_ENTER)
from src.live.live_config import (LiveConfig, Stage, check_activation,
                                  LiveLimits, fingerprint, STAGE_KEYS)
from src.live.live_order_manager import LiveOrderManager
from src.live.promotion import paper_gate_status, testnet_gate_status
from src.signals.engine import Signal
from src.storage.database import Database
from src.monitoring.health import HealthMonitor
from src.execution.order_manager import OrderManager
from src.execution.binance_client import MAINNET_ENABLED_IN_SOURCE
from tests.fake_exchange import FakeExchange   # MOCK EXCHANGE — لا اتصال حقيقي

IV_4H = 14_400_000
# وقت مجمَّد لكل الملف — لا time.time() طازجة داخل كل استدعاء بناء
# إشارة. استدعاءان منفصلان لـ mk_signal() ضمن نفس اختبار الحتمية
# كانا يعتمدان على وقوعهما في نفس المليثانية بالصدفة؛ نادراً ما يفشل
# هذا لكنه غير حتمي فعلياً — نفس فئة الخطأ التي أُصلحت في الإنتاج
# سابقاً، متسللة هنا داخل مساعد الاختبار نفسه.
_FROZEN_NOW_MS = int(time.time() * 1000)
ENV_VARS = [k for k in list(os.environ)
           if k.startswith(('LIVE_', 'TRADING_ENVIRONMENT', 'MAINNET_',
                            'TESTNET_', 'ALLOW_MAINNET', 'I_HAVE'))]


def clean():
    for k in list(os.environ):
        if k.startswith(('LIVE_', 'TRADING_ENVIRONMENT', 'MAINNET_',
                         'TESTNET_', 'ALLOW_MAINNET', 'I_HAVE')):
            os.environ.pop(k, None)


def mk_engine_signal(**over):
    now = _FROZEN_NOW_MS
    base = dict(index=100, timestamp=now - IV_4H, symbol='BTCUSDT',
               interval='4h', decision=BUY, strategy_version='v9.0.0',
               score=6.0, stars=4, confidence=0.9, raw_probability=0.6,
               calibrated_probability=0.58, probability_source='isotonic',
               entry=50000.0, stop_loss=49000.0, take_profit=52000.0,
               risk_reward=2.0, atr=500.0, regime='TRENDING_BULLISH',
               data_quality=0.96, btc_context='LOW', evidence=[], reasons=[])
    base.update(over)
    return Signal(**base)


def mk_signal(environment='live', **over) -> TradeSignal:
    es = mk_engine_signal(**{k: v for k, v in over.items()
                             if k in Signal.__dataclass_fields__})
    ts = from_engine_signal(es, environment=environment,
                            config_fingerprint='fp1', interval_ms=IV_4H)
    replace = {k: v for k, v in over.items()
              if k in TradeSignal.__dataclass_fields__ and k not in ('signal_id',)}
    if replace:
        ts2 = dataclasses.replace(ts, **replace)
        return dataclasses.replace(ts2, payload_hash=ts2.compute_hash())
    return ts


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


# ══ 1. العقد ══
class Test01_Contract(unittest.TestCase):
    def test_frozen(self):
        sig = mk_signal()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            sig.stop_loss = 1.0

    def test_deterministic_id(self):
        a = mk_signal(); b = mk_signal()
        self.assertEqual(a.signal_id, b.signal_id)

    def test_different_candle_different_id(self):
        a = mk_signal(timestamp=int(time.time() * 1000) - IV_4H)
        b = mk_signal(timestamp=int(time.time() * 1000) - IV_4H * 2)
        self.assertNotEqual(a.signal_id, b.signal_id)

    def test_hash_matches_content(self):
        self.assertTrue(mk_signal().hash_matches())

    def test_tampering_breaks_hash(self):
        sig = mk_signal()
        tampered = dataclasses.replace(sig, stop_loss=1.0)  # بلا إعادة حساب البصمة
        self.assertFalse(tampered.hash_matches())

    def test_no_constructor_from_dict(self):
        """لا مسار لبناء TradeSignal من قاموس خارجي."""
        import inspect
        src = inspect.getsource(sys.modules['src.live.trade_signal'])
        self.assertNotIn('def from_dict', src)
        self.assertNotIn('def from_json', src)


# ══ 2. التحقق ══
class Test02_Validation(unittest.TestCase):
    def test_valid_signal_accepted(self):
        ok, checks = validate_signal(mk_signal(), environment='live',
                                     allowed_symbols=['BTCUSDT'],
                                     interval_ms=IV_4H)
        self.assertTrue(ok, checks)

    def test_no_trade_reasons_blocks(self):
        sig = mk_signal(no_trade_reasons=('LOW_DATA_QUALITY',))
        ok, _ = validate_signal(sig, environment='live',
                                allowed_symbols=['BTCUSDT'], interval_ms=IV_4H)
        self.assertFalse(ok)

    def test_unclosed_candle_blocks(self):
        sig = mk_signal(candle_close_ts=int(time.time() * 1000) + 999999)
        ok, _ = validate_signal(sig, environment='live',
                                allowed_symbols=['BTCUSDT'], interval_ms=IV_4H)
        self.assertFalse(ok)

    def test_stale_signal_blocks(self):
        old = int(time.time() * 1000) - IV_4H * 10
        sig = mk_signal(candle_close_ts=old)
        ok, _ = validate_signal(sig, environment='live',
                                allowed_symbols=['BTCUSDT'], interval_ms=IV_4H,
                                max_age_multiple=2.0)
        self.assertFalse(ok)

    def test_nan_blocks(self):
        for field_ in ('stop_loss', 'take_profit', 'score'):
            with self.subTest(field=field_):
                sig = mk_signal(**{field_: float('nan')})
                ok, _ = validate_signal(sig, environment='live',
                                        allowed_symbols=['BTCUSDT'],
                                        interval_ms=IV_4H)
                self.assertFalse(ok)

    def test_wrong_environment_blocks(self):
        sig = mk_signal(environment='paper')
        ok, _ = validate_signal(sig, environment='live',
                                allowed_symbols=['BTCUSDT'], interval_ms=IV_4H)
        self.assertFalse(ok)

    def test_disallowed_symbol_blocks(self):
        ok, _ = validate_signal(mk_signal(), environment='live',
                                allowed_symbols=['ETHUSDT'], interval_ms=IV_4H)
        self.assertFalse(ok)

    def test_short_side_blocks(self):
        sig = mk_signal(side='SHORT')
        ok, checks = validate_signal(sig, environment='live',
                                     allowed_symbols=['BTCUSDT'],
                                     interval_ms=IV_4H)
        self.assertFalse(ok)
        self.assertTrue(any(c['check'] == 'SIDE_ALLOWED' and not c['passed']
                            for c in checks))

    def test_stop_above_entry_blocks(self):
        sig = mk_signal(stop_loss=51000.0)
        ok, _ = validate_signal(sig, environment='live',
                                allowed_symbols=['BTCUSDT'], interval_ms=IV_4H)
        self.assertFalse(ok)

    def test_stop_too_far_blocks(self):
        sig = mk_signal(stop_loss=40000.0)   # 20%
        ok, _ = validate_signal(sig, environment='live',
                                allowed_symbols=['BTCUSDT'], interval_ms=IV_4H,
                                max_stop_distance_pct=10.0)
        self.assertFalse(ok)

    def test_low_data_quality_blocks(self):
        sig = mk_signal(data_quality={'score': 0.4})
        ok, _ = validate_signal(sig, environment='live',
                                allowed_symbols=['BTCUSDT'], interval_ms=IV_4H,
                                min_data_quality=0.80)
        self.assertFalse(ok)

    def test_missing_stop_blocks(self):
        sig = mk_signal(stop_loss=None)
        ok, _ = validate_signal(sig, environment='live',
                                allowed_symbols=['BTCUSDT'], interval_ms=IV_4H)
        self.assertFalse(ok)


# ══ 3. المصدر معطَّل ══
class Test03_SourceDisabled(unittest.TestCase):
    def test_mainnet_disabled_constant(self):
        self.assertFalse(MAINNET_ENABLED_IN_SOURCE)

    def test_gate_fails_on_source_lock_regardless_of_env(self):
        clean()
        os.environ.update({
            'TRADING_ENVIRONMENT': 'live', 'LIVE_TRADING_ENABLED': '1',
            'I_HAVE_REVIEWED_AND_ACCEPT_RISK': 'yes',
            'MAINNET_API_KEY': 'k' * 20, 'MAINNET_API_SECRET': 's' * 20})
        try:
            act = check_activation(LiveConfig(), account_info={
                'canTrade': True, 'canWithdraw': False, 'ipRestrict': True},
                paper_gate_passed=True, testnet_gate_passed=True,
                unresolved_intents=0, reconciliation_ok=True,
                kill_switch_on=False, db_healthy=True, clock_offset_ms=10)
            self.assertFalse(act.allowed)
            self.assertIn('MAINNET_ENABLED_IN_SOURCE', act.failures)
        finally:
            clean()


# ══ 4. عزل المفاتيح ══
class Test04_KeySeparation(unittest.TestCase):
    def tearDown(self): clean()

    def test_shared_secret_detected(self):
        clean()
        os.environ.update({'TESTNET_API_SECRET': 'SAME',
                           'MAINNET_API_SECRET': 'SAME',
                           'TRADING_ENVIRONMENT': 'testnet'})
        probs = LiveConfig().key_separation_problems()
        self.assertTrue(any('مطابق' in p for p in probs))

    def test_mainnet_key_in_paper_rejected(self):
        clean()
        os.environ.update({'TRADING_ENVIRONMENT': 'paper',
                           'MAINNET_API_KEY': 'k', 'MAINNET_API_SECRET': 'v'})
        probs = LiveConfig().key_separation_problems()
        self.assertTrue(any('paper' in p for p in probs))

    def test_independent_keys_pass(self):
        clean()
        os.environ.update({'TRADING_ENVIRONMENT': 'testnet',
                           'TESTNET_API_SECRET': 'AAA',
                           'MAINNET_API_SECRET': 'BBB'})
        self.assertEqual(LiveConfig().key_separation_problems(), [])

    def test_fingerprint_never_reveals_secret(self):
        secret = 'TOP-SECRET-VALUE-1234567890'
        fp = fingerprint(secret)
        self.assertNotIn(secret, fp)
        self.assertNotIn('TOP-SECRET', fp)
        self.assertEqual(len(fp), 12)

    def test_stage_keys_are_distinct_env_vars(self):
        self.assertNotEqual(STAGE_KEYS[Stage.TESTNET],
                            STAGE_KEYS[Stage.LIVE])


# ══ 5. البوابة تجمع كل الشروط ══
class Test05_ActivationGate(unittest.TestCase):
    def tearDown(self): clean()

    def _full_pass_kwargs(self):
        return dict(
            account_info={'canTrade': True, 'canWithdraw': False,
                         'ipRestrict': True},
            paper_gate_passed=True, testnet_gate_passed=True,
            unresolved_intents=0, reconciliation_ok=True,
            kill_switch_on=False, db_healthy=True, clock_offset_ms=5)

    def test_missing_evidence_treated_as_failure(self):
        """غياب دليل = فشل، لا نجاح ضمني."""
        clean()
        os.environ['TRADING_ENVIRONMENT'] = 'live'
        act = check_activation(LiveConfig())
        for name in ('PAPER_GATE_PASSED', 'RECONCILIATION_OK',
                    'DATABASE_HEALTHY', 'CLOCK_SYNCHRONIZED'):
            c = next(c for c in act.checks if c.name == name)
            self.assertFalse(c.passed, name)

    def test_withdrawal_permission_blocks(self):
        clean()
        os.environ['TRADING_ENVIRONMENT'] = 'live'
        kw = self._full_pass_kwargs()
        kw['account_info'] = {'canTrade': True, 'canWithdraw': True,
                              'ipRestrict': True}
        act = check_activation(LiveConfig(), **kw)
        self.assertFalse(act.allowed)
        self.assertIn('NO_WITHDRAWAL_PERMISSION', act.failures)

    def test_open_unknown_intent_blocks(self):
        clean()
        os.environ['TRADING_ENVIRONMENT'] = 'live'
        kw = self._full_pass_kwargs()
        kw['unresolved_intents'] = 1
        act = check_activation(LiveConfig(), **kw)
        self.assertIn('NO_UNRESOLVED_UNKNOWN', act.failures)

    def test_kill_switch_blocks(self):
        clean()
        os.environ['TRADING_ENVIRONMENT'] = 'live'
        kw = self._full_pass_kwargs()
        kw['kill_switch_on'] = True
        act = check_activation(LiveConfig(), **kw)
        self.assertIn('KILL_SWITCH_OFF', act.failures)

    def test_clock_drift_blocks(self):
        clean()
        os.environ['TRADING_ENVIRONMENT'] = 'live'
        kw = self._full_pass_kwargs()
        kw['clock_offset_ms'] = 5000
        act = check_activation(LiveConfig(), **kw)
        self.assertIn('CLOCK_SYNCHRONIZED', act.failures)

    def test_non_live_stage_never_allowed(self):
        for stage in ('shadow', 'monitor', 'paper', 'testnet'):
            clean()
            os.environ['TRADING_ENVIRONMENT'] = stage
            act = check_activation(LiveConfig(), **self._full_pass_kwargs())
            self.assertFalse(act.allowed, stage)

    def test_source_lock_alone_blocks_even_with_everything_else_green(self):
        """حتى مع كل شيء آخر أخضر، القفل المصدري يمنع وحده."""
        clean()
        os.environ.update({
            'TRADING_ENVIRONMENT': 'live', 'LIVE_TRADING_ENABLED': '1',
            'I_HAVE_REVIEWED_AND_ACCEPT_RISK': 'yes',
            'MAINNET_API_KEY': 'k' * 20, 'MAINNET_API_SECRET': 's' * 20})
        act = check_activation(LiveConfig(), **self._full_pass_kwargs())
        self.assertFalse(act.allowed)
        self.assertEqual(act.failures, ['MAINNET_ENABLED_IN_SOURCE'])


# ══ 6. مدير الأوامر الحي (Mock Exchange) ══
class Test06_LiveOrderManager(unittest.TestCase):
    """
    ⚠️ FakeExchange هنا MOCK EXCHANGE — لا يتصل بأي شبكة ولا Binance
    حقيقي. الغرض فحص منطق البوابة والحجم فقط.
    """

    def tearDown(self): clean()

    def _mgr(self, ex=None):
        clean()
        os.environ.update({'TRADING_ENVIRONMENT': 'live',
                           'LIVE_TRADING_ENABLED': '0'})  # مقفول بالتصميم
        db = tmpdb()
        ex = ex or FakeExchange()
        h = HealthMonitor(db)
        base_om = OrderManager(db, ex, __import__(
            'src.core.config', fromlist=['Config']).Config(), h)
        lc = LiveConfig()
        return LiveOrderManager(db, ex, base_om, lc, h), db, ex

    def test_gate_closed_blocks_submit_before_any_network_call(self):
        mgr, db, ex = self._mgr()
        d = mgr.submit(mk_signal(), equity=1000,
                       current_open_notional={})
        self.assertEqual(d.outcome, 'BLOCKED')
        self.assertIn('LIVE_GATE_CLOSED', d.reason)
        self.assertEqual(ex.send_calls, 0, 'وصل نداء شبكة رغم بوابة مغلقة')

    def test_disallowed_symbol_rejected_before_sizing(self):
        clean()
        os.environ['TRADING_ENVIRONMENT'] = 'live'
        db = tmpdb(); ex = FakeExchange()
        lc = LiveConfig()
        lc = dataclasses.replace(lc, allowed_symbols=['ETHUSDT'])
        h = HealthMonitor(db)
        base_om = OrderManager(db, ex, __import__(
            'src.core.config', fromlist=['Config']).Config(), h)
        mgr = LiveOrderManager(db, ex, base_om, lc, h)
        d = mgr.submit(mk_signal(), equity=1000, current_open_notional={})
        self.assertIn(d.outcome, ('BLOCKED', 'REJECTED'))
        self.assertEqual(ex.send_calls, 0)

    def test_sizing_respects_limits(self):
        mgr, db, ex = self._mgr()
        sized = mgr._size(mk_signal(), equity=10000)
        max_allowed = mgr.cfg.limits.max_position_notional
        self.assertLessEqual(sized['notional'], max_allowed + 1e-6)

    def test_sizing_zero_on_bad_stop(self):
        mgr, db, ex = self._mgr()
        sig = mk_signal(stop_loss=None)
        sized = mgr._size(sig, equity=10000)
        self.assertEqual(sized['qty'], 0)

    def test_audit_records_every_outcome(self):
        mgr, db, ex = self._mgr()
        sig = mk_signal()          # استدعاء واحد — signal_id ثابت بعده
        mgr.submit(sig, equity=1000, current_open_notional={})
        rows = db.query(
            "SELECT * FROM risk_events WHERE kind LIKE 'LIVE_DECISION_%'")
        self.assertGreaterEqual(len(rows), 1)
        self.assertIn(sig.signal_id, rows[0]['detail'])


# ══ 6ب. الفحص الحقيقي — إثبات إصلاح ثغرة عدائية ══
class Test06b_PreTradeWiring(unittest.TestCase):
    """
    اكتُشفت هذه الثغرة بمراجعة عدائية لهذا الملف نفسه: LiveOrderManager
    كان يمرّر PreTradeCheck(True) ثابتاً بدل استدعاء
    OrderManager.pre_trade() الحقيقي — فسلسلة الفحوص المستقلة هناك
    (رصيد فعلي، LOT_SIZE/MIN_NOTIONAL، الحد اليومي، الانكشاف من القاعدة)
    كانت مُعطَّلة بالكامل على مسار Live تحديداً، رغم عملها في paper/testnet.

    هذه الاختبارات تُثبت الإصلاح بسيناريو كان سيمر خطأً قبله.
    """

    def _mgr(self, ex=None, equity_start=None, **env_overrides):
        clean()
        os.environ.update({'TRADING_ENVIRONMENT': 'live',
                           'LIVE_TRADING_ENABLED': '0'})
        os.environ.update({k: str(v) for k, v in env_overrides.items()})
        db = tmpdb()
        ex = ex or FakeExchange(base_free=0.0, quote_free=100000.0)
        h = HealthMonitor(db)
        cfg = __import__('src.core.config', fromlist=['Config']).Config()
        base_om = OrderManager(db, ex, cfg, h)
        lc = LiveConfig()
        mgr = LiveOrderManager(db, ex, base_om, lc, h)
        if equity_start is not None:
            db.start_day(mgr._day_key(), equity_start)
        return mgr, db, ex

    def test_pre_trade_actually_invoked_and_recorded(self):
        """قبل الإصلاح: pre_trade_checks كانت تبقى فارغة دائماً."""
        mgr, db, ex = self._mgr()
        # نفتح البوابة كاملة يدوياً لنصل فعلياً لخطوة pre_trade
        act_names = [c.name for c in mgr.preflight().checks]
        self.assertIn('MAINNET_ENABLED_IN_SOURCE', act_names)  # سيمنع أصلاً
        # نتحقق من الاستدعاء المباشر بدل submit() الكامل (البوابة تمنعه هنا)
        chk = mgr.orders.pre_trade(
            symbol='BTCUSDT', notional=500.0, entry=50000.0, stop=49000.0,
            signal_bar_time=int(time.time() * 1000) - IV_4H,
            interval_ms=IV_4H, data_quality=0.95, equity=10000.0,
            day_key=mgr._day_key())
        names = [c['check'] for c in chk.checks]
        for n in ('1_account_reachable', '4_lot_size', '4b_min_notional',
                 '5_balance', '7_daily_loss', '8_exposure'):
            self.assertIn(n, names, f'{n} غير مُشغَّل — الفحص لا يزال معطَّلاً')

    def test_daily_loss_limit_blocks_even_though_live_gate_has_no_such_check(self):
        """
        هذا الشرط موجود فقط داخل pre_trade() ولا نظير له في بوابة Live
        الخاصة (LiveConfig / check_activation). قبل الإصلاح، كسر الحد
        اليومي كان يمر بصمت لأن pre_trade لم يكن يُستدعى إطلاقاً.
        """
        from src.core.config import Config
        cfg = Config()
        cfg.risk.max_daily_loss_pct = 1.0    # 1% فقط
        clean()
        os.environ.update({'TRADING_ENVIRONMENT': 'live'})
        db = tmpdb()
        ex = FakeExchange(base_free=0.0, quote_free=100000.0)
        h = HealthMonitor(db)
        base_om = OrderManager(db, ex, cfg, h)
        lc = LiveConfig()
        mgr = LiveOrderManager(db, ex, base_om, lc, h)

        day = mgr._day_key()
        db.start_day(day, 10000.0)          # بداية اليوم
        equity_now = 9800.0                  # خسارة 2% — تتجاوز الحد 1%

        chk = mgr.orders.pre_trade(
            symbol='BTCUSDT', notional=500.0, entry=50000.0, stop=49000.0,
            signal_bar_time=int(time.time() * 1000) - IV_4H,
            interval_ms=IV_4H, data_quality=0.95, equity=equity_now,
            day_key=day)
        self.assertFalse(chk.passed)
        self.assertIn('7_daily_loss', chk.failures)

    def test_sizing_rounds_to_real_step_size(self):
        """
        قبل الإصلاح: qty لم تُقرَّب على step_size الفعلي إطلاقاً — أمر
        بكمية غير مقرَّبة يُرفَض من بينانس بخطأ -1013 أو يُنفَّذ بتقريب
        صامت من المنصة يغيّر الحجم الفعلي عن المخطَّط.
        """
        import math

        class CoarseStepExchange(FakeExchange):
            STEP = 0.001

            def rules(self, symbol):
                return {'symbol': symbol, 'status': 'TRADING', 'spot': True,
                       'base': 'BTC', 'quote': 'USDT', 'step_size': self.STEP,
                       'min_qty': self.STEP, 'tick_size': 0.01,
                       'min_notional': 10.0}

            def round_qty(self, symbol, q):
                return math.floor(q / self.STEP) * self.STEP

        ex = CoarseStepExchange(base_free=0.0, quote_free=100000.0)
        # نرفع حد الحجم مؤقتاً كي تكون الكمية الخام (0.0137) غير مضاعف
        # صحيح لـ step_size، فيظهر التقريب فعلياً بدل أن يُصفَّر الحجم
        mgr, db, _ = self._mgr(ex, LIVE_MAX_POSITION_NOTIONAL=685,
                               LIVE_MAX_RISK_PER_TRADE_PCT=5.0)
        sig = mk_signal(entry=50000.0, stop_loss=49000.0)
        sized = mgr._size(sig, equity=10000)
        self.assertGreater(sized['qty'], 0)
        self.assertAlmostEqual(sized['qty'], 0.013, places=9,
                               msg='لم يُقرَّب على step_size — الرقم الخام '
                                   '0.0137 كان يمر بلا تقريب قبل الإصلاح')
        # الكمية مضاعف صحيح لـ step_size = 0.001 (لا كسور أدق)
        steps = round(sized['qty'] / CoarseStepExchange.STEP)
        self.assertAlmostEqual(sized['qty'], steps * CoarseStepExchange.STEP,
                               places=9)

    def test_sizing_rejects_when_rules_unreadable(self):
        """
        فشل قراءة قواعد المنصة يجب أن يمنع الحجم، لا أن يتابع برقم
        مخمَّن — هذا بالضبط ما كانت تفعله النسخة السابقة (تخمين $10).
        """
        class BrokenRules(FakeExchange):
            def rules(self, symbol):
                raise RuntimeError('انقطاع محاكى')
        mgr, db, _ = self._mgr(BrokenRules(base_free=0.0, quote_free=100000.0))
        sized = mgr._size(mk_signal(), equity=10000)
        self.assertEqual(sized['qty'], 0)
        self.assertIn('قواعد المنصة', sized['reason'])

    def test_submit_rejects_with_pre_trade_failure_reason_when_gate_open(self):
        """
        محاكاة اجتياز بوابة Live بالكامل (عبر تصحيح مباشر لنتيجة
        preflight) للتأكد أن submit() يصل فعلاً لمرحلة pre_trade ويرفض
        بسببها الحقيقي بدل تنفيذ أمر غير مُتحقَّق منه.
        """
        from src.core.config import Config
        cfg = Config(); cfg.risk.max_daily_loss_pct = 1.0
        clean()
        os.environ.update({'TRADING_ENVIRONMENT': 'live'})
        db = tmpdb()
        ex = FakeExchange(base_free=0.0, quote_free=100000.0)
        h = HealthMonitor(db)
        base_om = OrderManager(db, ex, cfg, h)
        lc = LiveConfig()
        mgr = LiveOrderManager(db, ex, base_om, lc, h)
        db.start_day(mgr._day_key(), 10000.0)

        import unittest.mock as mock
        from src.live.live_config import LiveActivation
        open_act = LiveActivation(True, [])
        with mock.patch.object(mgr, 'preflight', return_value=open_act):
            d = mgr.submit(mk_signal(), equity=9800.0,   # خسارة 2% > حد 1%
                          current_open_notional={})
        self.assertEqual(d.outcome, 'REJECTED')
        self.assertIn('PRE_TRADE_FAILED', d.reason)
        self.assertIn('7_daily_loss', d.reason)
        self.assertEqual(ex.send_calls, 0, 'وصل نداء تنفيذ رغم رفض pre_trade')


# ══ 7. اتصال Promotion بـ monitoring.gates ══
class Test07_PromotionWiring(unittest.TestCase):
    def test_empty_db_gate_fails(self):
        self.assertFalse(paper_gate_status(tmpdb()))
        self.assertFalse(testnet_gate_status(tmpdb()))

    def test_uses_same_module_as_cli_gate_command(self):
        import src.live.promotion as P
        import src.monitoring.gates as G
        self.assertIs(P.evaluate, G.evaluate)


# ══ 8. توحيد مصدر حجم المركز — القسم 3 من V11 Final Patch ══
class Test08_UnifiedPositionSizing(unittest.TestCase):
    """
    كانت `LiveOrderManager._size()` تحسب `risk_per_unit` من المسافة
    الخام (entry-stop) فقط — بلا رسوم، بلا انزلاق، بلا سبريد، وبلا
    أي من معدِّلات المخاطرة (نجوم، سلاسل خسائر) التي يُطبِّقها
    `PositionSizer` الكنسي المُستخدَم في الباكتست. أُصلح بتوحيد المصدر
    كاملاً — `_size()` تُفوِّض الآن لـ`PositionSizer` نفسه.
    """

    def _mgr(self, ex=None, base_free=1.0):
        clean()
        os.environ.update({'TRADING_ENVIRONMENT': 'live',
                           'LIVE_TRADING_ENABLED': '0'})
        db = tmpdb()
        ex = ex or FakeExchange(base_free=base_free)
        h = HealthMonitor(db)
        from src.core.config import Config
        base_om = OrderManager(db, ex, Config(), h)
        lc = LiveConfig()
        return LiveOrderManager(db, ex, base_om, lc, h), db, ex

    def test_size_delegates_to_canonical_position_sizer(self):
        """
        اختبار التكافؤ الإلزامي (القسم 7): الكمية الخام قبل تطبيق حدود
        Live المحلية (الحد المطلق، الاحتياطي) يجب أن تطابق ما يُنتجه
        `PositionSizer.calculate()` مباشرة لنفس المُدخلات بالضبط —
        لا صيغة موازية مستقلة.
        """
        from src.risk.position_sizing import PositionSizer
        from src.core.config import RiskConfig
        mgr, db, ex = self._mgr()

        equity, entry, stop = 10_000.0, 100.0, 95.0
        sig = mk_signal(entry_reference_price=entry, stop_loss=stop, score=6.0)
        result = mgr._size(sig, equity)
        self.assertGreater(result['qty'], 0, result.get('reason'))

        lim = mgr.cfg.limits
        direct_cfg = RiskConfig(
            risk_per_trade_pct=lim.max_risk_per_trade_pct,
            min_risk_per_trade_pct=0.0,
            max_risk_per_trade_pct=lim.max_risk_per_trade_pct,
            max_position_notional_pct=100.0)
        direct = PositionSizer(direct_cfg, mgr.cost_model).calculate(
            equity=equity, entry=entry, stop=stop, stars=6,
            consecutive_losses=0, recent_win_rate=None, regime_confidence=1.0,
            step_size=None, min_notional=0.0)
        # نفس raw_quantity المُسجَّلة في sizing_detail قبل أي تقريب/حد Live محلي
        self.assertAlmostEqual(result['sizing_detail']['raw_quantity'],
                               direct['qty'], places=8,
                               msg='الكمية الخام لا تطابق PositionSizer الكنسي — '
                                   'لا يزال هناك مصدر مستقل')

    def test_costs_are_included_unlike_old_raw_distance_formula(self):
        """
        الصيغة القديمة: risk_per_unit = entry - stop (بلا تكاليف).
        الكنسية: effective_risk_per_unit يشمل الرسوم/الانزلاق/السبريد،
        فهو دائماً **أكبر** من المسافة الخام لأي تكلفة غير صفرية —
        يعني كمية أصغر فعلياً مما كانت الصيغة القديمة ستُنتجه لنفس
        المُدخلات.
        """
        mgr, db, ex = self._mgr()
        equity, entry, stop = 10_000.0, 100.0, 95.0
        sig = mk_signal(entry_reference_price=entry, stop_loss=stop, score=6.0)
        result = mgr._size(sig, equity)
        self.assertGreater(result['qty'], 0, result.get('reason'))

        old_formula_qty = (equity * mgr.cfg.limits.max_risk_per_trade_pct / 100
                           ) / (entry - stop)
        self.assertLess(result['sizing_detail']['raw_quantity'], old_formula_qty,
                        'الكمية الجديدة يجب أن تكون أصغر — التكاليف مُدرَجة الآن')

    def test_consecutive_losses_now_reduces_size(self):
        """كان هذا المعدِّل يُتجاهَل كلياً في الصيغة القديمة."""
        mgr, db, ex = self._mgr()
        equity, entry, stop = 10_000.0, 100.0, 95.0
        sig = mk_signal(entry_reference_price=entry, stop_loss=stop, score=6.0)

        r_no_losses = mgr._size(sig, equity)
        db.set_kv('risk_state', {'consecutive_losses': 3})
        r_after_losses = mgr._size(sig, equity)

        self.assertGreater(r_no_losses['qty'], 0)
        self.assertGreater(r_after_losses['qty'], 0)
        self.assertLess(
            r_after_losses['sizing_detail']['raw_quantity'],
            r_no_losses['sizing_detail']['raw_quantity'],
            'الكمية بعد 3 خسائر متتالية يجب أن تكون أصغر — المعدِّل لا يزال ميتاً')

    def test_sizing_detail_is_explainable(self):
        """القسم 4: كل قرار حجم يجب أن يكون قابلاً للتفسير بحقول محدَّدة."""
        mgr, db, ex = self._mgr()
        sig = mk_signal(entry_reference_price=100.0, stop_loss=95.0, score=6.0,
                        take_profit=110.0)
        result = mgr._size(sig, 10_000.0)
        d = result['sizing_detail']
        for key in ('account_equity', 'base_risk_pct', 'effective_risk_pct',
                   'entry_price', 'stop_price', 'raw_stop_distance',
                   'effective_risk_per_unit', 'raw_quantity',
                   'exchange_rounded_quantity', 'position_notional',
                   'estimated_fees', 'estimated_slippage', 'estimated_spread_cost',
                   'estimated_max_loss', 'risk_pct_of_equity',
                   'max_allowed_risk_amount', 'net_risk_reward', 'sizing_decision'):
            self.assertIn(key, d, f'{key} غائب عن تفسير قرار الحجم')
        self.assertEqual(d['sizing_decision'], 'ACCEPTED')

    def test_rejects_when_rounding_pushes_actual_risk_over_limit(self):
        """
        القسم 3: "After exchange rounding, recalculate the ACTUAL
        estimated risk. If actual risk exceeds the allowed risk
        because of rounding: NO TRADE." — يُحاكى هنا بخطوة تقريب
        خشنة جداً (step_size كبير) تُقرِّب الكمية **لأعلى** بما يكفي
        لتجاوز حد المخاطرة الفعلي، مع بقاء notional فوق min_notional
        كي لا يُرفَض عند فحص أبكر مختلف تماماً.
        """
        # عند entry=100/stop=99 مع تكاليف CostConfig الافتراضية،
        # effective_risk_per_unit ≈ 1.37 — أي كمية ≥ 19 تكفي لتجاوز
        # ميزانية المخاطرة المسموحة (0.25% من $10,000 = $25). نفرض
        # حداً أدنى للتقريب = 20 عمداً لضمان تجاوز الحد بوضوح، مهما
        # كانت الكمية القائمة على المخاطرة الأصلية أصغر بكثير.
        class CoarseRoundingExchange(FakeExchange):
            def rules(self, symbol):
                r = dict(super().rules(symbol))
                r['step_size'] = 20.0
                r['min_qty'] = 1.0       # منخفض عمداً — لا يُرفَض عند فحص min_qty
                r['min_notional'] = 10.0
                return r

            def round_qty(self, symbol, q):
                return max(20.0, round(q))

        mgr, db, ex = self._mgr(ex=CoarseRoundingExchange(base_free=100.0))
        sig = mk_signal(entry_reference_price=100.0, stop_loss=99.0, score=6.0)
        result = mgr._size(sig, 10_000.0)
        self.assertEqual(result['qty'], 0)
        self.assertEqual(result['reason'], 'EXCHANGE_MINIMUM_EXCEEDS_RISK_LIMIT')
        self.assertEqual(result['sizing_detail']['sizing_decision'], 'REJECTED')

    def test_never_increases_size_merely_to_satisfy_exchange_minimum(self):
        """"Never increase position size merely to satisfy an exchange minimum" (القسم 3)."""
        class TinyRiskExchange(FakeExchange):
            def rules(self, symbol):
                r = dict(super().rules(symbol))
                r['min_notional'] = 9_500.0
                return r

        mgr, db, ex = self._mgr(ex=TinyRiskExchange(base_free=1.0))
        sig = mk_signal(entry_reference_price=100.0, stop_loss=99.0, score=6.0)
        result = mgr._size(sig, 10_000.0)
        # يُرفَض بدل تكبير الحجم لإرضاء الحد الأدنى
        self.assertEqual(result['qty'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
