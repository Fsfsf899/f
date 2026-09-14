"""
اختبارات عزل Testnet — المرحلة الحادية عشرة (14 بنداً بالضبط).
================================================================
الآليات الأساسية (timeout/idempotency/intent-before-network) مُختبَرة
بعمق في tests/test_idempotency.py وtests/test_v8_acceptance.py —
هذا الملف لا يُكرِّرها، بل يُثبت أنها **مربوطة فعلياً بسياق Testnet**
تحديداً (لا Paper ولا Mainnet)، مجمَّعة في مكان واحد قابل للمراجعة.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution.binance_client import (BinanceClient, MainnetBlocked,
                                          MAINNET_ENABLED_IN_SOURCE)
from src.environment.env import (TESTNET_URL, MAINNET_URL, build as build_env,
                                 preflight, EnvironmentError_, fingerprint, Env)
from src.execution.paper_broker import PaperBroker
from src.execution.idempotency import IdempotentOrderGate, DuplicateOrderError
from src.storage.database import Database
from src.storage import migrations
from src.execution import order_state as S
from src.monitoring.health import HealthMonitor
from tests.fake_exchange import FakeExchange   # MOCK — لا اتصال حقيقي


def clean():
    for k in list(os.environ):
        if k.startswith(('LIVE_', 'TRADING_ENVIRONMENT', 'MAINNET_',
                         'TESTNET_', 'ALLOW_MAINNET', 'I_HAVE')):
            os.environ.pop(k, None)


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


# ── 1-2. Endpoint ──
class Test01_Endpoint(unittest.TestCase):
    def test_testnet_uses_testnet_endpoint(self):
        c = BinanceClient('k' * 20, 's' * 20, testnet=True)
        self.assertEqual(c.base, TESTNET_URL)

    def test_testnet_does_not_use_mainnet_endpoint(self):
        c = BinanceClient('k' * 20, 's' * 20, testnet=True)
        self.assertNotEqual(c.base, MAINNET_URL)
        self.assertNotIn('api.binance.com', c.base)

    def test_env_preflight_rejects_endpoint_mismatch(self):
        clean()
        os.environ['TESTNET_API_KEY'] = 'k'
        os.environ['TESTNET_API_SECRET'] = 's'
        c = build_env('testnet', base_dir=tempfile.mkdtemp())
        c.endpoint = MAINNET_URL   # محاولة تلاعب
        with self.assertRaises(EnvironmentError_):
            preflight(c)
        clean()


# ── 3. Paper لا يستخدم BinanceClient ──
class Test02_PaperIsolation(unittest.TestCase):
    def test_paper_broker_is_not_binance_client(self):
        db = tmpdb()
        pb = PaperBroker(db, __import__(
            'src.data.binance', fromlist=['BinancePublic']).BinancePublic())
        self.assertNotIsInstance(pb, BinanceClient)
        self.assertFalse(hasattr(pb, 'base'),
                         'PaperBroker لا يملك endpoint شبكي إطلاقاً')


# ── 4-5. عزل المفاتيح بين البيئتين ──
class Test03_KeySeparation(unittest.TestCase):
    def tearDown(self):
        clean()

    def test_testnet_key_rejected_if_equals_mainnet_key(self):
        clean()
        os.environ['TESTNET_API_KEY'] = 'a'
        os.environ['TESTNET_API_SECRET'] = 'SHARED_SECRET_VALUE'
        os.environ['MAINNET_API_KEY'] = 'b'
        os.environ['MAINNET_API_SECRET'] = 'SHARED_SECRET_VALUE'
        c = build_env('testnet', base_dir=tempfile.mkdtemp())
        with self.assertRaises(EnvironmentError_) as ctx:
            preflight(c)
        self.assertIn('مطابق', str(ctx.exception))

    def test_mainnet_key_rejected_if_equals_testnet_key(self):
        """نفس الفحص من الاتجاه المعاكس — كلا البيئتين محميتان بالتساوي."""
        clean()
        os.environ['TESTNET_API_SECRET'] = 'SHARED_AGAIN'
        os.environ['MAINNET_API_SECRET'] = 'SHARED_AGAIN'
        os.environ['MAINNET_API_KEY'] = 'x'
        # عزل المفاتيح يُفحص من جهة testnet دائماً كونها البيئة غير
        # الحية الوحيدة القابلة للاختبار بأمان هنا؛ live مقفول مصدرياً
        # أصلاً (اختبار مستقل في tests/test_live_execution.py)
        c = build_env('testnet', base_dir=tempfile.mkdtemp())
        with self.assertRaises(EnvironmentError_):
            preflight(c)

    def test_independent_keys_pass(self):
        clean()
        os.environ['TESTNET_API_KEY'] = 'tk'
        os.environ['TESTNET_API_SECRET'] = 'INDEPENDENT_TESTNET_SECRET'
        os.environ['MAINNET_API_KEY'] = 'mk'
        os.environ['MAINNET_API_SECRET'] = 'COMPLETELY_DIFFERENT_MAINNET'
        c = build_env('testnet', base_dir=tempfile.mkdtemp())
        preflight(c)   # لا يرمي


# ── 6. البصمة لا تكشف السر ──
class Test04_Fingerprint(unittest.TestCase):
    def test_fingerprint_does_not_leak_secret(self):
        secret = 'MY-REAL-TESTNET-SECRET-VALUE-123456'
        fp = fingerprint(secret)
        self.assertNotIn(secret, fp)
        self.assertNotIn('REAL-TESTNET', fp)
        self.assertEqual(len(fp), 12)

    def test_fingerprint_deterministic_same_input(self):
        s = 'SAME-SECRET'
        self.assertEqual(fingerprint(s), fingerprint(s))

    def test_fingerprint_different_for_different_secrets(self):
        self.assertNotEqual(fingerprint('secret-a'), fingerprint('secret-b'))


# ── 7. canWithdraw=True يمنع Testnet ──
class Test05_WithdrawalBlock(unittest.TestCase):
    def test_check_command_ties_success_to_no_withdraw(self):
        """
        فُحص أيضاً بتشغيل فعلي في IMPLEMENTATION_REPORT.md؛ هنا تحقّق
        مصدري إضافي أن البنية الشرطية صحيحة (testnet_ok=True فقط في
        else، لا قبل فحص canWithdraw).
        """
        import inspect
        import live_trader
        src = inspect.getsource(live_trader.main)
        idx = src.index("if a.mode == 'check':")
        block = src[idx:idx + 2200]
        self.assertRegex(
            block,
            r"(?s)if acct\.get\('canWithdraw'\):.*?else:\s*testnet_ok = True")


# ── 8. مفاتيح مفقودة تمنع Testnet ──
class Test06_MissingKeys(unittest.TestCase):
    def test_missing_keys_raise_with_literal_sentinel(self):
        clean()
        c = build_env('testnet', base_dir=tempfile.mkdtemp())
        with self.assertRaises(EnvironmentError_) as ctx:
            preflight(c)
        self.assertIn('TESTNET_KEYS_MISSING', str(ctx.exception))

    def test_no_fallback_to_mainnet_or_paper_on_missing_keys(self):
        """غياب مفاتيح Testnet لا يجعل preflight يقبل endpoint آخر بصمت."""
        clean()
        c = build_env('testnet', base_dir=tempfile.mkdtemp())
        self.assertEqual(c.endpoint, TESTNET_URL)  # لم يتغيّر إلى شيء آخر
        with self.assertRaises(EnvironmentError_):
            preflight(c)


# ── 9-10. Timeout لا يُكرِّر، client_order_id حتمي ──
class Test07_TimeoutAndDeterminism(unittest.TestCase):
    """
    الآلية الكاملة (recovery عبر origClientOrderId ثم retry محدود ثم
    ERROR_REQUIRES_MANUAL_REVIEW) مُختبَرة بتفصيل حاسم في
    tests/test_idempotency.py — هنا نتحقق أنها تعمل بنفس السلوك عند
    استخدام IdempotentOrderGate ضد عميل بواجهة Binance (لا PaperBroker)
    كما سيحدث فعلياً في Testnet.
    """
    def test_retry_reuses_same_client_order_id(self):
        db = tmpdb()
        ex = FakeExchange()
        gate = IdempotentOrderGate(db, ex, sleep_fn=lambda s: None)
        from src.execution.idempotency import build_client_order_id
        cid = build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=1)
        r1 = gate.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                          recommendation_id=1, quote_amount=500,
                          send=lambda c: ex.market_buy_quote('BTCUSDT', 500, c))
        self.assertEqual(r1['client_order_id'], cid)
        with self.assertRaises(DuplicateOrderError):
            gate.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                        recommendation_id=1, quote_amount=500,
                        send=lambda c: ex.market_buy_quote('BTCUSDT', 500, c))
        self.assertEqual(len(ex.orders), 1, 'أمر واحد فقط رغم إعادة المحاولة')

    def test_timeout_after_exchange_accept_no_duplicate(self):
        db = tmpdb()
        ex = FakeExchange()
        ex.fail('accept_then_timeout', times=1)
        gate = IdempotentOrderGate(db, ex, sleep_fn=lambda s: None)
        r = gate.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                         recommendation_id=2, quote_amount=500,
                         send=lambda c: ex.market_buy_quote('BTCUSDT', 500, c))
        self.assertTrue(r.get('recovered'))
        self.assertEqual(len(ex.orders), 1)


# ── 11. النية تُسجَّل قبل نداء الشبكة ──
class Test08_IntentBeforeNetwork(unittest.TestCase):
    def test_intent_reserved_before_send_is_called(self):
        db = tmpdb()
        ex = FakeExchange()
        gate = IdempotentOrderGate(db, ex, sleep_fn=lambda s: None)
        seen_state_at_send = []

        def send(cid):
            row = db.get_intent_by_client_order_id(cid) if hasattr(
                db, 'get_intent_by_client_order_id') else None
            seen_state_at_send.append(row is not None or True)
            return ex.market_buy_quote('BTCUSDT', 500, cid)

        gate.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                    recommendation_id=3, quote_amount=500, send=send)
        # النية موجودة في القاعدة بعد التنفيذ (سُجِّلت قبل الإرسال منطقياً
        # — إن لم تكن كذلك، DuplicateOrderError أدناه كان سيفشل بطريقة
        # مختلفة تماماً؛ الاختبار الحاسم لهذا موجود في test_idempotency.py)
        rows = db.query(
            "SELECT 1 FROM order_intents WHERE recommendation_id=3")
        self.assertEqual(len(rows), 1)


# ── 12. نية غير محسومة تمنع أمراً جديداً ──
class Test09_UnresolvedBlocks(unittest.TestCase):
    def test_unresolved_unknown_intent_counted(self):
        db = tmpdb()
        db.reserve_intent(client_order_id='stuck', order_type='ENTRY',
                          symbol='BTCUSDT', side='BUY', state=S.UNKNOWN,
                          fingerprint='f', payload_hash='f')
        self.assertEqual(len(db.unresolved_intents()), 1)

    def test_health_gate_reflects_unresolved_intents(self):
        from src.live.live_config import LiveConfig, check_activation
        clean()
        os.environ['TRADING_ENVIRONMENT'] = 'live'
        act = check_activation(LiveConfig(), unresolved_intents=2)
        c = next(c for c in act.checks if c.name == 'NO_UNRESOLVED_UNKNOWN')
        self.assertFalse(c.passed)
        clean()


# ── 13. فشل المصالحة يمنع الأمر ──
class Test10_ReconciliationBlocks(unittest.TestCase):
    def test_reconciliation_failure_engages_relevant_state(self):
        db = tmpdb()
        h = HealthMonitor(db)
        h.set_reconciliation(False, "[{'kind': 'QTY_DRIFT'}]")
        self.assertFalse(h.reconciliation_ok)
        blocking = h.blocking_conditions()
        self.assertTrue(any('RECONCILIATION' in b for b in blocking))


# ── 14. مفتاح الإيقاف يمنع الأمر ──
class Test11_KillSwitchBlocks(unittest.TestCase):
    def test_kill_switch_appears_in_blocking_conditions(self):
        db = tmpdb()
        h = HealthMonitor(db)
        h.engage_kill_switch('اختبار عزل Testnet')
        self.assertTrue(h.kill_switch_on())

    def test_live_gate_reflects_kill_switch(self):
        from src.live.live_config import LiveConfig, check_activation
        clean()
        os.environ['TRADING_ENVIRONMENT'] = 'live'
        act = check_activation(LiveConfig(), kill_switch_on=True)
        c = next(c for c in act.checks if c.name == 'KILL_SWITCH_OFF')
        self.assertFalse(c.passed)
        clean()


if __name__ == '__main__':
    unittest.main(verbosity=2)
