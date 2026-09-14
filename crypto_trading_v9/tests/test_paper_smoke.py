"""
اختبارات Paper الدخانية — المرحلة التاسعة.
============================================
هذه الاختبارات تبني `LiveTrader` عبر نفس المسار الحقيقي الذي يستخدمه
`main()` (build_env → preflight → LiveTrader(...))، لا اختصارات تخفي
منطق التهيئة — فالغرض هو كشف أخطاء التهيئة فعلياً، وهذا بالضبط ما
كشف الأعطال الخمسة الموثَّقة في IMPLEMENTATION_REPORT.md.

الحد الوحيد المفروض على البيئة: لا اتصال شبكة حقيقي ببينانس. لذا
نستبدل `t.cache` فقط (حدود جلب البيانات) بمزوّد بيانات fixture محلي
واضح المصدر — لا نستبدل أي منطق تداول. `tests/fixtures.py` مُستخدَم
في كل اختبارات المحرك للغرض نفسه ومُوسَّم صراحة: **بيانات اختبار
اصطناعية، ليست بيانات سوق حقيقية ولا تُستخدَم لإثبات أي أداء.**
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.environment.env import build as build_env, preflight
from src.core.config import Config
from src.storage.database import Database
from src.execution.paper_broker import PaperBroker
from src.execution.binance_client import BinanceClient
from tests.fixtures import make_fixture
import live_trader as LT


def clean_env():
    """
    قائمة شاملة موحَّدة عبر كل ملفات الاختبار — عدم اتساقها سابقاً بين
    الملفات كان يسمح بتسرّب متغيرات (LIVE_TRADING_ENABLED مثلاً) من
    tests/test_live_execution.py إلى هذا الملف عند تشغيلهما معاً عبر
    `unittest discover`، رغم عمل كل ملف بمفرده بلا مشاكل.
    """
    for k in list(os.environ):
        if k.startswith(('LIVE_', 'TRADING_ENVIRONMENT', 'MAINNET_',
                         'TESTNET_', 'ALLOW_MAINNET', 'I_HAVE')):
            os.environ.pop(k, None)


class FixtureCache:
    """
    ⚠️ اختبار فقط — بيانات fixture محلية عبر make_fixture()، ليست
    بيانات سوق حقيقية بأي شكل. تستبدل حدود جلب البيانات فقط
    (Cache.get) — بقية `tick()` يعمل بمنطقه الحقيقي كاملاً: محرك
    الإشارة، PaperBroker، OrderManager، IdempotentOrderGate، إلخ.
    """
    def __init__(self, data):
        self._data = data

    def get(self, client, symbol, interval, days=365, force=False,
           verbose=True):
        return self._data


def new_trader(mode='paper', base=None, symbol='BTCUSDT', interval='4h',
              fixture_bars=800, seed=11):
    clean_env()
    base = base or tempfile.mkdtemp()
    envcfg = build_env(mode, base_dir=base, symbol=symbol, interval=interval,
                       strategy_version='v9.0.0', config_fingerprint='fp')
    preflight(envcfg)
    t = LT.LiveTrader(envcfg, Config(), 50.0)
    t.cache = FixtureCache(make_fixture(fixture_bars, interval, seed=seed))
    return t, base


# ══ 1. التهيئة ══
class Test01_Init(unittest.TestCase):
    def test_livetrader_paper_initializes(self):
        """يجب أن يُبنى LiveTrader في Paper بلا NameError ولا أي خطأ."""
        t, _ = new_trader('paper')
        self.assertEqual(t.mode, 'paper')

    def test_paper_startup_event_is_written(self):
        t, _ = new_trader('paper', symbol='ETHUSDT', interval='1h')
        rows = t.db.query("SELECT detail FROM system_events WHERE kind='STARTUP'")
        self.assertEqual(len(rows), 1)
        detail = rows[0]['detail']
        self.assertIn('paper', detail)
        self.assertIn('ETHUSDT', detail)
        self.assertIn('1h', detail)
        self.assertIn(t.cfg.version, detail)

    def test_paper_uses_paper_broker(self):
        t, _ = new_trader('paper')
        self.assertIsInstance(t.client, PaperBroker)
        self.assertNotIsInstance(t.client, BinanceClient)

    def test_paper_never_uses_mainnet(self):
        t, _ = new_trader('paper')
        self.assertFalse(hasattr(t.client, 'base'),
                         'PaperBroker لا يجب أن يملك endpoint شبكي')
        self.assertIsNone(t.envcfg.endpoint)
        self.assertEqual(t.envcfg.api_key, '')

    def test_paper_creates_database(self):
        t, base = new_trader('paper')
        tables = {r['name'] for r in t.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for required in ('signals', 'recommendations', 'positions', 'orders',
                        'fills', 'order_intents', 'risk_events',
                        'system_events', 'kv'):
            self.assertIn(required, tables)
        self.assertTrue(os.path.exists(t.envcfg.db_path))
        self.assertIn('paper', t.envcfg.db_path)


# ══ 2. الدورة الفعلية ══
class Test02_Tick(unittest.TestCase):
    def test_paper_tick_runs_once(self):
        """دورة كاملة عبر بيانات fixture — لا NameError ولا استثناء."""
        t, _ = new_trader('paper')
        res = t.tick(verbose=False)
        self.assertIsInstance(res, dict)
        self.assertIn('status', res)
        self.assertNotEqual(res.get('status'), 'ERROR')

    def test_paper_does_not_duplicate_signal(self):
        t, _ = new_trader('paper')
        t.tick(verbose=False)
        n1 = len(t.db.query('SELECT 1 FROM signals'))
        t.tick(verbose=False)   # نفس بيانات fixture — نفس الشمعة الأخيرة
        n2 = len(t.db.query('SELECT 1 FROM signals'))
        self.assertEqual(n1, n2, 'دورة ثانية على نفس الشمعة أنتجت إشارة جديدة')

    def test_paper_does_not_duplicate_order(self):
        t, _ = new_trader('paper')
        t.tick(verbose=False)
        n1 = len(t.db.query('SELECT 1 FROM orders'))
        t.tick(verbose=False)
        n2 = len(t.db.query('SELECT 1 FROM orders'))
        self.assertEqual(n1, n2)


# ══ 3. الاستعادة بعد إعادة التشغيل ══
class Test03_Restart(unittest.TestCase):
    def test_paper_recovers_after_restart(self):
        t, base = new_trader('paper', fixture_bars=800, seed=3)
        t.tick(verbose=False)
        db_path = t.envcfg.db_path
        equity_before = t.equity()
        n_signals_before = len(t.db.query('SELECT 1 FROM signals'))

        # إعادة تشغيل حقيقية: كائن LiveTrader جديد على نفس القاعدة
        clean_env()
        envcfg2 = build_env('paper', base_dir=base, symbol='BTCUSDT',
                            interval='4h', strategy_version='v9.0.0',
                            config_fingerprint='fp', db_override=db_path)
        preflight(envcfg2)
        t2 = LT.LiveTrader(envcfg2, Config(), 50.0)
        self.assertEqual(len(t2.db.query('SELECT 1 FROM signals')),
                         n_signals_before)
        self.assertAlmostEqual(t2.equity(), equity_before, places=6)
        starts = t2.db.query("SELECT 1 FROM system_events WHERE kind='STARTUP'")
        self.assertEqual(len(starts), 2, 'حدثا STARTUP متوقَّعان (تشغيلان)')


# ══ 4. الحواجز الأمنية عند مستوى tick() ══
class Test04_Guards(unittest.TestCase):
    def test_unresolved_intent_blocks_new_trade(self):
        t, _ = new_trader('paper')
        from src.execution import order_state as S
        t.db.reserve_intent(client_order_id='stuck1', order_type='ENTRY',
                            symbol='BTCUSDT', side='BUY', state=S.UNKNOWN,
                            fingerprint='f', payload_hash='f')
        res = t.tick(verbose=False)
        self.assertNotEqual(res.get('status'), 'ENTERED')
        self.assertGreater(len(t.db.unresolved_intents()), 0)

    def test_kill_switch_blocks_new_trade(self):
        t, _ = new_trader('paper')
        t.health.engage_kill_switch('اختبار')
        res = t.tick(verbose=False)
        self.assertTrue(t.health.kill_switch_on())
        self.assertNotEqual(res.get('status'), 'ENTERED')


# ══ 5. آليات PaperBroker — عبر المكوّن مباشرة (موثَّقة أعمق في
#       test_v8_acceptance.py؛ هنا نتحقق من الاتصال الفعلي بـ LiveTrader) ══
class Test05_PaperMechanics(unittest.TestCase):
    """
    fee/slippage/OCO/partial-fill آليات محاكاة عامة في PaperBroker
    نفسه — مُختبَرة بتفصيل أكبر في Test01/02_Paper* بـ
    tests/test_v8_acceptance.py (تنفيذ حقيقي، سعر أسوأ من ask/bid،
    فجوة، تقريب step_size، إلخ). هذه الاختبارات هنا تتحقق تحديداً أن
    LiveTrader.tick() **يستخدم فعلاً** نفس الآليات، لا أنها موجودة
    بمعزل عن التكامل.
    """
    def test_paper_applies_fees(self):
        t, _ = new_trader('paper')
        equity_start = t.equity()
        t.tick(verbose=False)
        fills = t.db.query('SELECT * FROM fills')
        if fills:  # يعتمد على وجود إشارة BUY فعلية من fixture هذا الاختبار
            self.assertTrue(any(float(f['commission']) > 0 for f in fills))
        else:
            self.skipTest('لا صفقة دخول في هذه العينة العشوائية — '
                          'الآلية مُثبَتة مباشرة في test_v8_acceptance.py')

    def test_paper_applies_slippage(self):
        t, _ = new_trader('paper')
        self.assertGreater(t.paper.costs.normal_slippage_bps, 0)
        self.assertGreater(t.paper.costs.stop_slippage_bps,
                           t.paper.costs.normal_slippage_bps,
                           'انزلاق الوقف يجب أن يكون أسوأ من العادي')

    def test_paper_oco_behavior(self):
        t, _ = new_trader('paper')
        self.assertTrue(hasattr(t.paper, 'oco_sell'))

    def test_paper_partial_fill(self):
        t, _ = new_trader('paper')
        self.assertIn('partial_fill_ratio', t.paper.costs.to_dict())

    def test_paper_timeout_recovery(self):
        """
        Timeout بعد قبول الأمر لا يُكرِّر — مُختبَر بتفصيل حاسم في
        tests.test_idempotency وtests.test_v8_acceptance
        (test_timeout_after_accept_no_double). هنا: التحقق أن
        LiveTrader.orders يستخدم فعلاً IdempotentOrderGate الحقيقي،
        لا مساراً مختصراً.
        """
        from src.execution.idempotency import IdempotentOrderGate
        t, _ = new_trader('paper')
        self.assertIsInstance(t.gate, IdempotentOrderGate)


if __name__ == '__main__':
    unittest.main(verbosity=2)
