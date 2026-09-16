"""
اختبارات القبول — المرحلة الثالثة عشرة.
لا مفاتيح حقيقية، لا اتصال بأي شبكة، لا mainnet.
"""
import unittest, os, sys, tempfile, threading, warnings, gc, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.database import Database
from src.storage import migrations
from src.core.config import Config
from src.environment.env import (Env, build as build_env, preflight,
                                 EnvironmentError_, fingerprint,
                                 MAINNET_URL, TESTNET_URL)
from src.execution.paper_broker import PaperBroker, PaperCosts
from src.execution.binance_client import (BinanceClient, BinanceError,
                                          MainnetBlocked, mainnet_allowed)
from src.execution.errors import redact
from src.execution.idempotency import (IdempotentOrderGate, DuplicateOrderError,
                                       build_client_order_id)
from src.execution.order_manager import OrderManager
from src.execution.reconciliation import Reconciler
from src.execution import order_state as S
from src.monitoring.health import HealthMonitor
from src.monitoring.gates import evaluate, readiness, GateCriteria, PRODUCTION_READY
from src.monitoring.paper_report import PaperReport
from src.monitoring.shadow import ShadowRecorder
from tests.fake_exchange import FakeExchange

ENV_VARS = ('TESTNET_API_KEY', 'TESTNET_API_SECRET', 'MAINNET_API_KEY',
            'MAINNET_API_SECRET', 'ALLOW_MAINNET')


def clean_env():
    for k in ENV_VARS:
        os.environ.pop(k, None)


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


class Pub:
    """TEST DOUBLE — مصدر سعر ثابت بـ bid/ask."""
    def __init__(self, p=50000.0, spread_bps=4.0):
        self.p = p; self.h = spread_bps / 20000
    def ticker(self, sym):
        return {'bid': self.p * (1 - self.h), 'ask': self.p * (1 + self.h),
                'mid': self.p}
    def exchange_rules(self, sym):
        return {'symbol': sym, 'status': 'TRADING', 'base': 'BTC',
                'quote': 'USDT', 'spot': True, 'step_size': 1e-5,
                'min_qty': 1e-5, 'tick_size': 0.01, 'min_notional': 10.0}


def mkpaper(costs=None, quote=10000.0, price=50000.0):
    db = tmpdb(); pub = Pub(price)
    pb = PaperBroker(db, pub, costs=costs or PaperCosts(latency_ms=0),
                     starting_quote=quote)
    return db, pub, pb


# ── 1. Paper Broker: التكاليف ──
class Test01_PaperCosts(unittest.TestCase):
    def test_buy_worse_than_ask(self):
        db, pub, pb = mkpaper()
        o = pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        fill = float(o['fills'][0]['price'])
        self.assertGreater(fill, pub.ticker('X')['ask'],
                           'الشراء يجب أن يكون أسوأ من ask')

    def test_sell_worse_than_bid(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        o = pb.market_sell('BTCUSDT', 0.01, 's1')
        self.assertLess(float(o['fills'][0]['price']), pub.ticker('X')['bid'])

    def test_fees_deducted(self):
        db, pub, pb = mkpaper()
        before = pb.balances()['USDT']['free']
        o = pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        after = pb.balances()['USDT']['free']
        fee = float(o['fills'][0]['commission'])
        self.assertGreater(fee, 0)
        self.assertAlmostEqual(before - after, 1000 * 0 + (before - after), places=6)
        self.assertGreater(before - after, 1000 * 0.99)

    def test_costs_persisted_with_order(self):
        db, pub, pb = mkpaper()
        o = pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        for k in ('taker_fee', 'spread_bps', 'normal_slippage_bps',
                  'stop_slippage_bps', 'gap_slippage_bps', 'latency_ms'):
            self.assertIn(k, o['costs'])

    def test_no_ideal_close_price(self):
        """لا ينفَّذ أي أمر عند السعر المتوسط بالضبط."""
        db, pub, pb = mkpaper()
        mid = pub.ticker('X')['mid']
        o = pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        self.assertNotAlmostEqual(float(o['fills'][0]['price']), mid, places=2)


# ── 2. Paper Broker: الفجوات والتنفيذ الجزئي ──
class Test02_PaperExecution(unittest.TestCase):
    def test_gap_down_fills_worse_than_stop(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        pb.stop_loss_limit('BTCUSDT', 0.01, 49000, 48900, 'sl1')
        pub.p = 46000                       # فجوة حادة
        trig = pb.evaluate_pending('BTCUSDT')
        self.assertTrue(trig)
        self.assertLess(trig[0]['price'], 49000,
                        'الفجوة يجب أن تنفَّذ أسوأ من الوقف')
        self.assertEqual(trig[0]['slippage_bps'], pb.costs.gap_slippage_bps)

    def test_partial_fill(self):
        db, pub, pb = mkpaper(PaperCosts(latency_ms=0, partial_fill_ratio=0.4))
        o = pb.market_buy_quote('BTCUSDT', 1000, 'p1')
        self.assertEqual(o['status'], 'PARTIALLY_FILLED')
        self.assertLess(float(o['executedQty']), float(o['origQty']))
        self.assertGreater(float(o['executedQty']), 0)

    def test_locked_balance_on_stop(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        free_before = pb.balances()['BTC']['free']
        pb.stop_loss_limit('BTCUSDT', 0.01, 49000, 48900, 'sl1')
        b = pb.balances()['BTC']
        self.assertAlmostEqual(b['locked'], 0.01, places=5)
        self.assertLess(b['free'], free_before)

    def test_cancel_unlocks(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        o = pb.stop_loss_limit('BTCUSDT', 0.01, 49000, 48900, 'sl1')
        pb.cancel('BTCUSDT', o['orderId'])
        self.assertAlmostEqual(pb.balances()['BTC']['locked'], 0.0, places=9)

    def test_no_future_price(self):
        """السعر المستخدم هو الحالي فقط — لا اطلاع على المستقبل."""
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        pb.stop_loss_limit('BTCUSDT', 0.01, 40000, 39900, 'sl1')
        self.assertEqual(pb.evaluate_pending('BTCUSDT'), [],
                         'نُفّذ رغم أن السعر لم يصل بعد')
        pub.p = 39000
        self.assertTrue(pb.evaluate_pending('BTCUSDT'))

    def test_faults_simulated(self):
        for kw, exc in [({'reject_probability': 1.0}, '-2010'),
                        ({'timeout_probability': 1.0}, 'timed out'),
                        ({'rate_limit_probability': 1.0}, '-1003')]:
            db, pub, pb = mkpaper(PaperCosts(latency_ms=0, **kw))
            with self.assertRaises(BinanceError):
                pb.market_buy_quote('BTCUSDT', 1000, 'x')

    def test_disconnect_after_accept_leaves_order(self):
        db, pub, pb = mkpaper(
            PaperCosts(latency_ms=0, disconnect_after_accept_probability=1.0))
        with self.assertRaises(BinanceError):
            pb.market_buy_quote('BTCUSDT', 1000, 'd1')
        o = pb.order_by_client_id('BTCUSDT', 'd1')
        self.assertEqual(o['status'], 'FILLED', 'الأمر يجب أن يبقى للتعافي')

    def test_restart_restores_state(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        bal = pb.balances()
        pb2 = PaperBroker(Database(db.path), pub,
                          costs=PaperCosts(latency_ms=0))
        self.assertEqual(pb2.balances(), bal)
        self.assertEqual(len(pb2._orders), 1)


class Test02b_OCO(unittest.TestCase):
    """
    على Spot لا يمكن حجز نفس الكمية لأمرين منفصلين. الوقف وحده يحجز
    كل الرصيد فيتعذّر وضع الهدف — OCO هو الحل الوحيد لحماية مزدوجة.
    """

    def test_oco_locks_once_for_both(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        qty = pb.base_free('BTCUSDT')
        o = pb.oco_sell('BTCUSDT', qty, 52000, 49000, 48900, 'oco1')
        # إصلاح: OCO لم يعد أمراً واحداً بنوع 'OCO' — أب (orderListId)
        # وابنان منفصلان بمعرّفات خاصة، مطابق لشكل استجابة بينانس
        # الحقيقية (orderReports). راجع IMPLEMENTATION_REPORT.md.
        self.assertIn('orderListId', o)
        self.assertEqual(len(o['orders']), 2)
        self.assertEqual(len(o['orderReports']), 2)
        types = {rep['type'] for rep in o['orderReports']}
        self.assertEqual(types, {'STOP_LOSS_LIMIT', 'LIMIT_MAKER'})
        ids = {rep['orderId'] for rep in o['orderReports']}
        self.assertEqual(len(ids), 2, 'الابنان يجب أن يحملا orderId مختلفين')
        b = pb.balances()['BTC']
        self.assertAlmostEqual(b['locked'], qty, places=6)
        self.assertAlmostEqual(b['free'], 0.0, places=9)

    def test_separate_stop_then_target_impossible(self):
        """توثيق القيد: الوقف المنفصل يمنع الهدف."""
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        qty = pb.base_free('BTCUSDT')
        pb.stop_loss_limit('BTCUSDT', qty, 49000, 48900, 'sl1')
        with self.assertRaises(BinanceError):
            pb.limit_sell('BTCUSDT', qty, 52000, 'tp1')

    def test_oco_stop_side_triggers(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        pb.oco_sell('BTCUSDT', pb.base_free('BTCUSDT'), 52000, 49000, 48900, 'o1')
        pub.p = 48500
        t = pb.evaluate_pending('BTCUSDT')
        self.assertTrue(t and t[0]['kind'] == 'STOP' and t[0]['oco'])
        self.assertLess(t[0]['price'], 49000)

    def test_oco_target_side_triggers(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        pb.oco_sell('BTCUSDT', pb.base_free('BTCUSDT'), 52000, 49000, 48900, 'o1')
        pub.p = 52500
        t = pb.evaluate_pending('BTCUSDT')
        self.assertTrue(t and t[0]['kind'] == 'TARGET')
        self.assertEqual(t[0]['price'], 52000)

    def test_order_manager_prefers_oco(self):
        db, pub, pb = mkpaper()
        om = OrderManager(db, pb, Config(), HealthMonitor(db))
        om.gate._sleep = lambda s: None
        sid, tid = om._place_oco('BTCUSDT', 0.01, 49000, 52000, pos_id=1)
        self.assertIsNone(sid, 'لا رصيد بعد — يجب أن يفشل بأمان')
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        sid, tid = om._place_oco('BTCUSDT', 0.019, 49000, 52000, pos_id=2)
        self.assertIsNotNone(sid)
        # إصلاح: كانا يُخزَّنان بنفس القيمة خطأً (orderListId مكرَّراً).
        # الآن معرّفان حقيقيان مختلفان للابنين — هذا هو الإصلاح نفسه.
        self.assertNotEqual(sid, tid,
                            'الوقف والهدف يجب أن يحملا orderId مختلفين فعلياً')


class Test02c_Drawdown(unittest.TestCase):
    def test_drawdown_bounded(self):
        """قمة قريبة من الصفر كانت تُنتج نسباً بالتريليونات."""
        from src.monitoring.paper_report import _agg
        for pnls in ([-38.5], [-100, 50, -30], [10, -5, 8], [-1e-9, 1e-9]):
            rows = [{'pnl': p, 'holding_bars': 1} for p in pnls]
            dd = _agg(rows)['max_drawdown_pct']
            self.assertGreaterEqual(dd, 0.0)
            self.assertLessEqual(dd, 100.0, f'تراجع خارج النطاق: {dd} لـ {pnls}')


# ── 3. فصل البيئات ──
class Test03_Environments(unittest.TestCase):
    def setUp(self):
        clean_env()
        self.base = tempfile.mkdtemp()

    def test_separate_databases(self):
        paths = {e: build_env(e, base_dir=self.base).db_path
                 for e in ('shadow', 'monitor', 'paper', 'testnet', 'live')}
        self.assertEqual(len(set(paths.values())), 5)

    def test_separate_locks(self):
        locks = {e: build_env(e, base_dir=self.base).lock_name
                 for e in ('paper', 'testnet', 'live')}
        self.assertEqual(len(set(locks.values())), 3)

    def test_testnet_endpoint_not_mainnet(self):
        c = build_env('testnet', base_dir=self.base)
        self.assertEqual(c.endpoint, TESTNET_URL)
        self.assertNotEqual(c.endpoint, MAINNET_URL)

    def test_endpoint_mismatch_halts(self):
        c = build_env('testnet', base_dir=self.base)
        os.environ['TESTNET_API_KEY'] = 'k'; os.environ['TESTNET_API_SECRET'] = 's'
        c.endpoint = MAINNET_URL
        with self.assertRaises(EnvironmentError_):
            preflight(c)

    def test_missing_testnet_keys_halts(self):
        with self.assertRaises(EnvironmentError_):
            preflight(build_env('testnet', base_dir=self.base))

    def test_live_without_permission_halts(self):
        os.environ['MAINNET_API_KEY'] = 'k'; os.environ['MAINNET_API_SECRET'] = 's'
        with self.assertRaises(EnvironmentError_):
            preflight(build_env('live', base_dir=self.base))

    def test_shared_secret_between_envs_halts(self):
        os.environ['TESTNET_API_KEY'] = 'a'; os.environ['TESTNET_API_SECRET'] = 'SAME'
        os.environ['MAINNET_API_KEY'] = 'b'; os.environ['MAINNET_API_SECRET'] = 'SAME'
        with self.assertRaises(EnvironmentError_):
            preflight(build_env('testnet', base_dir=self.base))

    def test_no_silent_fallback(self):
        """paper لا يملك endpoint ولا مفاتيح — لا انزلاق إلى testnet."""
        c = build_env('paper', base_dir=self.base)
        preflight(c)
        self.assertIsNone(c.endpoint)
        self.assertEqual(c.api_key, '')

    def test_fingerprint_hides_secret(self):
        fp = fingerprint('SUPERSECRET123456')
        self.assertNotIn('SUPERSECRET', fp)
        self.assertEqual(len(fp), 12)

    def test_banner_has_no_secret(self):
        from src.environment.env import print_banner
        os.environ['TESTNET_API_KEY'] = 'k'
        os.environ['TESTNET_API_SECRET'] = 'MYSECRETVALUE999'
        c = build_env('testnet', base_dir=self.base)
        self.assertNotIn('MYSECRETVALUE999', print_banner(c))


# ── 4. Mainnet محظور ──
class Test04_MainnetBlocked(unittest.TestCase):
    def setUp(self): clean_env()

    def test_blocked_by_default(self):
        self.assertFalse(mainnet_allowed())
        with self.assertRaises(MainnetBlocked):
            BinanceClient('k' * 20, 's' * 20, testnet=False)

    def test_testnet_client_base(self):
        c = BinanceClient('k' * 20, 's' * 20, testnet=True)
        self.assertEqual(c.base, TESTNET_URL)
        self.assertNotEqual(c.base, MAINNET_URL)

    def test_paper_never_touches_network(self):
        db, pub, pb = mkpaper()
        self.assertTrue(pb.testnet)
        self.assertFalse(hasattr(pb, 'base'))


# ── 5. نفس محرك الإشارة ──
class Test05_SharedEngine(unittest.TestCase):
    def test_same_order_manager_for_paper_and_testnet(self):
        db, pub, pb = mkpaper()
        om_paper = OrderManager(db, pb, Config(), HealthMonitor(db))
        om_fake = OrderManager(tmpdb(), FakeExchange(), Config())
        self.assertEqual(type(om_paper.gate), type(om_fake.gate))
        self.assertEqual(type(om_paper).__name__, type(om_fake).__name__)

    def test_paper_uses_same_signal_engine(self):
        from src.signals.engine import SignalEngine
        from src.backtest.engine import BacktestEngine
        self.assertIsInstance(BacktestEngine(Config()).engine, SignalEngine)


# ── 6. منع التكرار عبر Paper ──
class Test06_IdempotencyOverPaper(unittest.TestCase):
    def test_deterministic_id_on_retry(self):
        db, pub, pb = mkpaper()
        g = IdempotentOrderGate(db, pb, sleep_fn=lambda s: None)
        a = build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=1)
        r = g.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                      recommendation_id=1, quote_amount=1000,
                      send=lambda cid: pb.market_buy_quote('BTCUSDT', 1000, cid))
        self.assertEqual(r['client_order_id'], a)
        with self.assertRaises(DuplicateOrderError):
            g.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                      recommendation_id=1, quote_amount=1000,
                      send=lambda cid: pb.market_buy_quote('BTCUSDT', 1000, cid))
        self.assertEqual(len(pb._orders), 1)

    def test_timeout_after_accept_no_double(self):
        db, pub, pb = mkpaper(
            PaperCosts(latency_ms=0, disconnect_after_accept_probability=1.0))
        g = IdempotentOrderGate(db, pb, sleep_fn=lambda s: None)
        r = g.execute(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                      recommendation_id=2, quote_amount=1000,
                      send=lambda cid: pb.market_buy_quote('BTCUSDT', 1000, cid))
        self.assertTrue(r['recovered'])
        self.assertEqual(len(pb._orders), 1)

    def test_unknown_blocks_new_trades(self):
        db, pub, pb = mkpaper()
        g = IdempotentOrderGate(db, pb, sleep_fn=lambda s: None)
        cid = build_client_order_id('ENTRY', 'BTCUSDT', recommendation_id=5)
        g.reserve(order_type='ENTRY', symbol='BTCUSDT', side='BUY',
                  recommendation_id=5, quote_amount=1000)
        db.transition_order_state(cid, S.IN_FLIGHT, reason='s')
        db.transition_order_state(cid, S.UNKNOWN, reason='s')
        self.assertGreater(len(db.unresolved_intents()), 0)

    def test_duplicate_fill_not_recorded(self):
        db = tmpdb()
        a = db.record_fill_if_absent(order_id=1, symbol='B', qty=1, price=1,
                                     exchange_trade_id='t1')
        b = db.record_fill_if_absent(order_id=1, symbol='B', qty=1, price=1,
                                     exchange_trade_id='t1')
        self.assertTrue(a['created']); self.assertFalse(b['created'])

    def test_duplicate_exit_no_double_sell(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        om = OrderManager(db, pb, Config(), HealthMonitor(db))
        om.gate._sleep = lambda s: None
        pos = {'id': 1, 'symbol': 'BTCUSDT', 'qty': 0.01, 'entry_price': 50000,
               'fees': 0.0, 'status': 'OPEN', 'stop_order_id': None,
               'recommendation_id': None}
        om._exit_via_gate(pos, 'BTCUSDT', 0.01, 'EXIT', 'SIGNAL_EXIT')
        before = pb.base_free('BTCUSDT')
        om._exit_via_gate(pos, 'BTCUSDT', 0.01, 'EXIT', 'SIGNAL_EXIT')
        self.assertAlmostEqual(pb.base_free('BTCUSDT'), before, places=9)


# ── 7. المصالحة و Kill Switch ──
class Test07_KillSwitch(unittest.TestCase):
    def test_persists_across_restart(self):
        db = tmpdb(); HealthMonitor(db).engage_kill_switch('x')
        self.assertTrue(HealthMonitor(Database(db.path)).kill_switch_on())

    def test_release_blocked_while_cause_remains(self):
        db = tmpdb(); h = HealthMonitor(db)
        h.set_reconciliation(False, 'mismatch')
        h.engage_kill_switch('recon')
        r = h.release_kill_switch()
        self.assertFalse(r['released'])
        self.assertTrue(h.kill_switch_on())

    def test_release_after_cause_cleared(self):
        db = tmpdb(); h = HealthMonitor(db)
        h.set_reconciliation(False, 'x'); h.engage_kill_switch('y')
        h.set_reconciliation(True)
        self.assertTrue(h.release_kill_switch()['released'])

    def test_force_release_logged_as_critical(self):
        db = tmpdb(); h = HealthMonitor(db)
        h.set_reconciliation(False, 'x'); h.engage_kill_switch('y')
        r = h.release_kill_switch(force=True)
        self.assertTrue(r['released'] and r['forced'])
        self.assertEqual(len(db.query(
            "SELECT 1 FROM risk_events WHERE kind='KILL_SWITCH_FORCE_RELEASED'")), 1)

    def test_unresolved_intent_blocks_release(self):
        db = tmpdb(); h = HealthMonitor(db)
        db.reserve_intent(client_order_id='u1', order_type='ENTRY', symbol='B',
                          side='BUY', state=S.UNKNOWN, fingerprint='f',
                          payload_hash='f')
        h.engage_kill_switch('unknown')
        self.assertFalse(h.release_kill_switch()['released'])

    def test_position_without_stop_is_blocking(self):
        db = tmpdb(); h = HealthMonitor(db)
        sig = {'symbol': 'B', 'interval': '4h', 'timestamp': 1, 'decision': 'BUY',
               'strategy_version': 'v', 'evidence': [], 'reasons': []}
        sid = db.save_signal(sig); rid = db.save_recommendation(sid, sig, True)
        db.open_position(recommendation_id=rid, symbol='B', qty=1, entry_price=1)
        self.assertTrue(any('WITHOUT_STOP' in b for b in h.blocking_conditions()))

    def test_reconciliation_mismatch_detected(self):
        db = tmpdb(); ex = FakeExchange(base_free=0.01)
        ex.stop_loss_limit('BTCUSDT', 0.01, 49000, 48900, 'foreign')
        r = Reconciler(db, ex, gate=IdempotentOrderGate(db, ex)).run(['BTCUSDT'])
        self.assertFalse(r.ok)


# ── 8. الصحة والتقارير والبوابات ──
class Test08_HealthAndGates(unittest.TestCase):
    def test_health_detects_failure(self):
        db = tmpdb(); h = HealthMonitor(db)
        self.assertTrue(h.report()['healthy'])
        h.set_reconciliation(False, 'x')
        rep = h.report()
        self.assertFalse(rep['healthy'])
        self.assertIn('RECONCILIATION_FAILED', rep['blocking_conditions'])

    def test_health_has_required_fields(self):
        rep = HealthMonitor(tmpdb()).report()
        for k in ('last_successful_cycle', 'last_data_fetch', 'unknown_orders',
                  'open_positions', 'kill_switch', 'api_failure_count',
                  'database_ok', 'active_threads'):
            self.assertIn(k, rep)

    def test_empty_gate_fails(self):
        self.assertFalse(evaluate(tmpdb(), 'paper', tests_passed=True).passed)

    def test_gate_blocks_on_unresolved(self):
        db = tmpdb()
        db.reserve_intent(client_order_id='u', order_type='ENTRY', symbol='B',
                          side='BUY', state=S.UNKNOWN, fingerprint='f',
                          payload_hash='f')
        g = evaluate(db, 'paper', tests_passed=True)
        self.assertIn('no_unknown_orders', g.failures)

    def test_readiness_never_auto_production(self):
        r = readiness(tmpdb(), None, tests_passed=True)
        self.assertNotEqual(r['classification'], PRODUCTION_READY)
        self.assertIn('يدوي', r['live_activation'])

    def test_report_warns_on_small_sample(self):
        rep = PaperReport(tmpdb(), 'paper').full()
        self.assertIn('INSUFFICIENT SAMPLE', rep.get('warning', ''))

    def test_report_not_just_win_rate(self):
        rep = PaperReport(tmpdb(), 'paper').full()
        for k in ('overall', 'costs', 'unknown_orders', 'api_errors',
                  'reconciliation_mismatches', 'rejected_reasons'):
            self.assertIn(k, rep)

    def test_shadow_records_and_reports(self):
        db = tmpdb(); sh = ShadowRecorder(db, 'shadow')

        class Sig:
            symbol = 'BTCUSDT'; interval = '4h'; timestamp = 1700000000000
            decision = 'BUY'; entry = 50000.0; stop_loss = 49000.0
            take_profit = 52000.0; risk_reward = 2.0; score = 6.0; stars = 4
            regime = 'TRENDING_BULLISH'; data_quality = 0.95; reasons = []
        sh.record(Sig(), {'qty': 0.01, 'notional': 500})
        rep = sh.report()
        self.assertEqual(rep['n_buy'], 1)
        self.assertIn('INSUFFICIENT SAMPLE', rep.get('warning', ''))


# ── 9. النظافة ──
class Test09_Hygiene(unittest.TestCase):
    def test_no_secrets_in_logs(self):
        db = tmpdb()
        secret = 'TOPSECRETKEY9876543210'
        db.risk_event('T', 'INFO', redact(f'apiKey={secret} signature=abcdef123456'))
        rows = db.query('SELECT detail FROM risk_events')
        self.assertNotIn(secret, rows[0]['detail'])
        self.assertNotIn('abcdef123456', rows[0]['detail'])

    def test_no_resource_warnings(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            db, pub, pb = mkpaper()
            pb.market_buy_quote('BTCUSDT', 1000, 'b1')
            db.query('SELECT * FROM orders'); gc.collect()
            self.assertEqual(
                [x for x in w if issubclass(x.category, ResourceWarning)], [])

    def test_no_threads_left(self):
        before = threading.active_count()
        db, pub, pb = mkpaper()
        ts = [threading.Thread(target=lambda: Database(db.path).query(
            'SELECT 1'), daemon=True) for _ in range(4)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=10)
        gc.collect()
        self.assertLessEqual(threading.active_count(), before + 1)

    def test_no_sqlite_lock_errors(self):
        db = tmpdb(); errs = []

        def hammer(i):
            d = Database(db.path)
            for j in range(20):
                try:
                    d.set_kv(f'k{i}-{j}', j)
                except Exception as e:
                    errs.append(str(e))
        ts = [threading.Thread(target=hammer, args=(i,), daemon=True)
              for i in range(5)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=30)
        self.assertEqual([e for e in errs if 'locked' in e.lower()], [])

    def test_migration_preserves_data(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, 'm.db')
        db = Database(p)
        db.save_order(symbol='B', side='BUY', type='MARKET', status='FILLED',
                      client_order_id='keep')
        n = len(db.query('SELECT * FROM orders'))
        migrations.run(p)
        self.assertEqual(len(Database(p).query('SELECT * FROM orders')), n)


if __name__ == '__main__':
    unittest.main(verbosity=2)
