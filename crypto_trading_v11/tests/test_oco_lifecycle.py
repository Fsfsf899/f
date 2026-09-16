"""
اختبارات إصلاح دورة حياة OCO الحرج — القسمان 13/14/56.A.
==========================================================
الخلل المُصلَح: orderListId كان يُخزَّن في كلا stop_order_id
وtarget_order_id — لا وسيلة للتمييز بين الأمرين الفرعيين، وهذا كان
يمنع Reconciliation من العمل بشكل صحيح ضد استجابة بينانس حقيقية،
ويجعل استعلام حالة الوقف يستعلم عن *القائمة* لا الأمر الفرعي.

fixture واقعي مطابق للمثال المطلوب بالضبط:
    orderListId = 9001
    target: orderId=50001, clientOrderId=CID-T
    stop:   orderId=50002, clientOrderId=CID-S
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.database import Database
from src.core.config import Config
from src.execution.order_manager import OrderManager
from src.execution.binance_client import BinanceError
from src.execution.paper_broker import PaperBroker
from src.execution.reconciliation import (Reconciler, D_UNKNOWN_EXCHANGE_ORDER,
                                          D_TARGET_MISSING)
from src.execution.idempotency import IdempotentOrderGate
from src.monitoring.health import HealthMonitor
from src.data.binance import BinancePublic
from tests.fake_exchange import FakeExchange


class Pub:
    """TEST DOUBLE — مصدر سعر ثابت، لا اتصال شبكة حقيقي."""
    def __init__(self, p=50000.0, spread_bps=4.0):
        self.p = p; self.h = spread_bps / 20000

    def ticker(self, sym):
        return {'bid': self.p * (1 - self.h), 'ask': self.p * (1 + self.h),
               'mid': self.p}

    def exchange_rules(self, sym):
        return {'symbol': sym, 'status': 'TRADING', 'base': 'BTC',
               'quote': 'USDT', 'spot': True, 'step_size': 1e-5,
               'min_qty': 1e-5, 'tick_size': 0.01, 'min_notional': 10.0}


def mkpaper():
    db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
    pub = Pub()
    pb = PaperBroker(db, pub, starting_quote=10000.0)
    return db, pub, pb


def realistic_oco_client(monkeypatch_id=True):
    """عميل زائف يُرجع بالضبط المثال المطلوب في المستند."""
    class RealisticOCOClient:
        def oco_sell(self, symbol, qty, target_price, stop_price,
                    stop_limit_price, client_id):
            return {
                'orderListId': 9001, 'listClientOrderId': client_id,
                'listStatusType': 'EXEC_STARTED', 'listOrderStatus': 'EXECUTING',
                'symbol': symbol,
                'orders': [
                    {'symbol': symbol, 'orderId': 50002, 'clientOrderId': 'CID-S'},
                    {'symbol': symbol, 'orderId': 50001, 'clientOrderId': 'CID-T'}],
                'orderReports': [
                    {'symbol': symbol, 'orderId': 50002, 'clientOrderId': 'CID-S',
                     'type': 'STOP_LOSS_LIMIT', 'side': 'SELL', 'status': 'NEW',
                     'stopPrice': f'{stop_price:.2f}', 'price': f'{stop_limit_price:.2f}'},
                    {'symbol': symbol, 'orderId': 50001, 'clientOrderId': 'CID-T',
                     'type': 'LIMIT_MAKER', 'side': 'SELL', 'status': 'NEW',
                     'price': f'{target_price:.2f}'}],
            }
    return RealisticOCOClient()


# ══ 1. إنشاء OCO بمعرّفات منفصلة حقيقية ══
class Test01_DistinctIDs(unittest.TestCase):
    def test_paper_broker_produces_distinct_child_ids(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        qty = pb.base_free('BTCUSDT')
        r = pb.oco_sell('BTCUSDT', qty, 52000, 49000, 48900, 'oco1')
        stop_id = r['orderReports'][0]['orderId']
        target_id = r['orderReports'][1]['orderId']
        self.assertNotEqual(stop_id, target_id)
        self.assertEqual(r['orderReports'][0]['type'], 'STOP_LOSS_LIMIT')
        self.assertEqual(r['orderReports'][1]['type'], 'LIMIT_MAKER')

    def test_fake_exchange_produces_distinct_child_ids(self):
        ex = FakeExchange()
        r = ex.oco_sell('BTCUSDT', 0.01, 52000, 49000, 48900, 'oco1')
        stop_id = r['orderReports'][0]['orderId']
        target_id = r['orderReports'][1]['orderId']
        self.assertNotEqual(stop_id, target_id)
        self.assertIn('orderListId', r)

    def test_order_manager_stores_distinct_ids_in_position(self):
        db, pub, pb = mkpaper()
        db.open_position(id=1, symbol='BTCUSDT', qty=0.02, entry_price=50000.0,
                         opened_ts=1)
        pb.market_buy_quote('BTCUSDT', 2000, 'seed')
        om = OrderManager(db, pb, Config(), HealthMonitor(db))
        om.gate._sleep = lambda s: None
        sid, tid = om._place_oco('BTCUSDT', 0.02, 49000, 52000, pos_id=1)
        self.assertIsNotNone(sid)
        self.assertNotEqual(sid, tid)
        row = db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertEqual(row['stop_order_id'], sid)
        self.assertEqual(row['target_order_id'], tid)
        self.assertIsNotNone(row['order_list_id'])
        self.assertNotIn(row['order_list_id'], (sid, tid))
        self.assertTrue(row['stop_client_order_id'])
        self.assertTrue(row['target_client_order_id'])
        self.assertNotEqual(row['stop_client_order_id'], row['target_client_order_id'])


# ══ 2. الاستمرارية عبر Restart ══
class Test02_Persistence(unittest.TestCase):
    def test_ids_survive_restart(self):
        db, pub, pb = mkpaper()
        db_path = db.path
        db.open_position(id=1, symbol='BTCUSDT', qty=0.02, entry_price=50000.0,
                         opened_ts=1)
        pb.market_buy_quote('BTCUSDT', 2000, 'seed')
        om = OrderManager(db, pb, Config(), HealthMonitor(db))
        om.gate._sleep = lambda s: None
        sid, tid = om._place_oco('BTCUSDT', 0.02, 49000, 52000, pos_id=1)

        db2 = Database(db_path)
        row = db2.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertEqual(row['stop_order_id'], sid)
        self.assertEqual(row['target_order_id'], tid)


# ══ 3. إلغاء الأخ عند تنفيذ أحد الطرفين — الاتجاهان ══
class Test03_SiblingCancellation(unittest.TestCase):
    def test_stop_fill_cancels_target(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        qty = pb.base_free('BTCUSDT')
        pb.oco_sell('BTCUSDT', qty, 60000, 49000, 48900, 'oco1')
        stop_o = pb._orders['oco1-STOP']
        pb._execute(stop_o, 48800.0, qty, reason='test')
        pb._cancel_sibling(stop_o)
        target_o = pb._orders['oco1-TARGET']
        self.assertEqual(target_o['status'], 'CANCELED')

    def test_target_fill_cancels_stop(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        qty = pb.base_free('BTCUSDT')
        pb.oco_sell('BTCUSDT', qty, 52000, 49000, 48900, 'oco1')
        target_o = pb._orders['oco1-TARGET']
        pb._execute(target_o, 52000.0, qty, reason='test')
        pb._cancel_sibling(target_o)
        stop_o = pb._orders['oco1-STOP']
        self.assertEqual(stop_o['status'], 'CANCELED')

    def test_sibling_cancellation_does_not_double_release_balance(self):
        db, pub, pb = mkpaper()
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        qty = pb.base_free('BTCUSDT')
        pb.oco_sell('BTCUSDT', qty, 52000, 49000, 48900, 'oco1')
        stop_o = pb._orders['oco1-STOP']
        pb._execute(stop_o, 48800.0, qty, reason='test')
        pb._cancel_sibling(stop_o)
        # بعد تنفيذ الوقف بالكامل وإلغاء الهدف، يجب أن يكون رصيد BTC
        # صفراً تماماً — balances() تستبعد الأصول الصفرية، فغياب
        # المفتاح هنا هو الإثبات الصحيح لعدم وجود تحرير مزدوج (لو
        # حدث تحرير مزدوج، locked كان سيظهر بقيمة متبقية زائفة).
        b = pb.balances().get('BTC', {'free': 0.0, 'locked': 0.0})
        self.assertAlmostEqual(b['locked'], 0.0, places=6)


# ══ 4. استجابة مشوَّهة = فشل حماية صريح، لا نجاح مصطنع ══
class Test04_MalformedResponse(unittest.TestCase):
    def test_missing_order_reports_blocks_and_kills(self):
        class BrokenClient:
            def oco_sell(self, *a, **kw):
                return {'orderListId': 123}   # بلا orderReports إطلاقاً

        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        db.open_position(id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
                         opened_ts=1)
        h = HealthMonitor(db)
        om = OrderManager(db, BrokenClient(), Config(), h)
        om.gate._sleep = lambda s: None
        sid, tid = om._place_oco('BTCUSDT', 0.01, 49000, 52000, pos_id=1)
        self.assertIsNone(sid)
        self.assertIsNone(tid)
        self.assertTrue(h.kill_switch_on(),
                        'استجابة مشوَّهة يجب أن تُفعِّل مفتاح الإيقاف — لا حماية غير مؤكَّدة')
        rows = db.query(
            "SELECT 1 FROM risk_events WHERE kind='OCO_RESPONSE_MALFORMED'")
        self.assertEqual(len(rows), 1)

    def test_null_order_id_never_stored_as_string_none(self):
        class NullIdClient:
            def oco_sell(self, symbol, qty, target_price, stop_price,
                        stop_limit_price, client_id):
                return {
                    'orderListId': 5, 'listClientOrderId': client_id,
                    'orders': [], 'orderReports': [
                        {'symbol': symbol, 'orderId': None,
                         'clientOrderId': 'x-STOP', 'type': 'STOP_LOSS_LIMIT'},
                        {'symbol': symbol, 'orderId': 999,
                         'clientOrderId': 'x-TARGET', 'type': 'LIMIT_MAKER'}]}

        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        db.open_position(id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
                         opened_ts=1)
        h = HealthMonitor(db)
        om = OrderManager(db, NullIdClient(), Config(), h)
        om.gate._sleep = lambda s: None
        sid, tid = om._place_oco('BTCUSDT', 0.01, 49000, 52000, pos_id=1)
        self.assertIsNone(sid)
        self.assertTrue(h.kill_switch_on())
        row = db.query('SELECT stop_order_id FROM positions WHERE id=1')[0]
        self.assertNotEqual(row['stop_order_id'], 'None',
                            'لا يجوز تخزين النص "None" كمعرّف أمر')


# ══ 5. لا يُصنَّف طرف OCO شرعي كأمر مجهول ══
class Test05_ReconciliationRecognizesOCOChildren(unittest.TestCase):
    def test_oco_children_not_flagged_unknown(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        db.reserve_intent(client_order_id='parent-cid', order_type='STOP',
                          symbol='BTCUSDT', side='SELL', state='ACKED',
                          fingerprint='f', payload_hash='f')
        ex = FakeExchange(base_free=0.01, quote_free=0.0)

        class ExWithOpenOrders(FakeExchange):
            def open_orders(self, symbol=None):
                return [
                    {'clientOrderId': 'parent-cid-STOP', 'orderId': 111,
                     'type': 'STOP_LOSS_LIMIT'},
                    {'clientOrderId': 'parent-cid-TARGET', 'orderId': 112,
                     'type': 'LIMIT_MAKER'}]

            def rules(self, symbol):
                return {'symbol': symbol, 'base': 'BTC', 'quote': 'USDT',
                       'min_notional': 10.0, 'step_size': 0.00001,
                       'min_qty': 0.00001, 'tick_size': 0.01}

            def balances(self):
                return {'BTC': {'free': 0.01, 'locked': 0.0}}

            def price(self, symbol):
                return 50000.0

        ex2 = ExWithOpenOrders()
        gate = IdempotentOrderGate(db, ex2)
        r = Reconciler(db, ex2, gate=gate).run(['BTCUSDT'])
        unknown = [d for d in r.discrepancies if d['kind'] == D_UNKNOWN_EXCHANGE_ORDER]
        self.assertEqual(unknown, [],
                         f'أطراف OCO شرعية صُنِّفت مجهولة: {unknown}')

    def test_positive_real_ids_recognized_without_suffix_convention(self):
        """
        إيجابي — القسم E من متطلبات V11: التعرُّف عبر orderListId/
        orderId/clientOrderId الحقيقية المخزَّنة في positions، لا عبر
        تخمين لاحقة الاسم. معرّفات العميل هنا **لا تتبع** اصطلاح
        `-STOP`/`-TARGET` إطلاقاً عمداً — لو كان الربط لا يزال يعتمد
        على اللاحقة فقط، هذا الاختبار سيفشل.
        """
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        db.reserve_intent(client_order_id='list-abc123', order_type='STOP',
                          symbol='BTCUSDT', side='SELL', state='ACKED',
                          fingerprint='f', payload_hash='f')
        db.open_position(
            id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
            opened_ts=1, status='OPEN',
            order_list_id='9999', list_client_order_id='list-abc123',
            stop_order_id='555001', stop_client_order_id='xK9m2-A',
            target_order_id='555002', target_client_order_id='xK9m2-B')

        class ExWithOpenOrders(FakeExchange):
            def open_orders(self, symbol=None):
                return [
                    {'clientOrderId': 'xK9m2-A', 'orderId': 555001,
                     'orderListId': 9999, 'type': 'STOP_LOSS_LIMIT'},
                    {'clientOrderId': 'xK9m2-B', 'orderId': 555002,
                     'orderListId': 9999, 'type': 'LIMIT_MAKER'}]

            def rules(self, symbol):
                return {'symbol': symbol, 'base': 'BTC', 'quote': 'USDT',
                       'min_notional': 10.0, 'step_size': 0.00001,
                       'min_qty': 0.00001, 'tick_size': 0.01}

            def balances(self):
                return {'BTC': {'free': 0.01, 'locked': 0.0}}

            def price(self, symbol):
                return 50000.0

        ex2 = ExWithOpenOrders()
        gate = IdempotentOrderGate(db, ex2)
        r = Reconciler(db, ex2, gate=gate).run(['BTCUSDT'])
        unknown = [d for d in r.discrepancies if d['kind'] == D_UNKNOWN_EXCHANGE_ORDER]
        self.assertEqual(unknown, [],
                         f'طرفا OCO بمعرّفات حقيقية بلا لاحقة صُنِّفا مجهولين: {unknown}')

    def test_recently_closed_position_still_recognizes_sibling_in_flight(self):
        """
        نافذة انتقال إلغاء الأخ — مركز أُغلق للتو (منذ دقائق)، وطرف OCO
        الآخر لا يزال يظهر على المنصة لحظياً (قبل تأكيد الإلغاء). يجب
        ألا يُصنَّف مجهولاً لمجرد أن المركز لم يعد "OPEN".
        """
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        db.reserve_intent(client_order_id='list-xyz', order_type='STOP',
                          symbol='BTCUSDT', side='SELL', state='ACKED',
                          fingerprint='f', payload_hash='f')
        now_ms = int(__import__('time').time() * 1000)
        db.open_position(
            id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
            opened_ts=now_ms - 3600_000, closed_ts=now_ms - 60_000,
            status='CLOSED',
            order_list_id='7777', list_client_order_id='list-xyz',
            stop_order_id='888001', stop_client_order_id='qZ-S',
            target_order_id='888002', target_client_order_id='qZ-T')

        class ExWithOpenOrders(FakeExchange):
            def open_orders(self, symbol=None):
                # الهدف نُفِّذ (لم يعد على المنصة)، الوقف لم يُلغَ بعد فعلياً
                return [{'clientOrderId': 'qZ-S', 'orderId': 888001,
                        'orderListId': 7777, 'type': 'STOP_LOSS_LIMIT'}]

            def rules(self, symbol):
                return {'symbol': symbol, 'base': 'BTC', 'quote': 'USDT',
                       'min_notional': 10.0, 'step_size': 0.00001,
                       'min_qty': 0.00001, 'tick_size': 0.01}

            def balances(self):
                return {'BTC': {'free': 0.0, 'locked': 0.0}}

            def price(self, symbol):
                return 50000.0

        ex2 = ExWithOpenOrders()
        gate = IdempotentOrderGate(db, ex2)
        r = Reconciler(db, ex2, gate=gate).run(['BTCUSDT'])
        unknown = [d for d in r.discrepancies if d['kind'] == D_UNKNOWN_EXCHANGE_ORDER]
        self.assertEqual(unknown, [],
                         f'أخ OCO لمركز أُغلق منذ دقائق صُنِّف مجهولاً: {unknown}')

    def test_negative_unrelated_order_still_flagged_unknown(self):
        """
        سلبي — القسم E: أمر غريب تماماً، لا علاقة له بأي نية أو مركز
        محلي، يجب أن يبقى `UNKNOWN_EXCHANGE_ORDER` رغم كل مسارات
        الربط الجديدة — لا تساهل زائد يُخفي أوامر فعلياً مجهولة.
        """
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        db.reserve_intent(client_order_id='list-abc123', order_type='STOP',
                          symbol='BTCUSDT', side='SELL', state='ACKED',
                          fingerprint='f', payload_hash='f')
        db.open_position(
            id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
            opened_ts=1, status='OPEN',
            order_list_id='9999', list_client_order_id='list-abc123',
            stop_order_id='555001', stop_client_order_id='xK9m2-A',
            target_order_id='555002', target_client_order_id='xK9m2-B')

        class ExWithOpenOrders(FakeExchange):
            def open_orders(self, symbol=None):
                return [
                    # الطرفان الشرعيان
                    {'clientOrderId': 'xK9m2-A', 'orderId': 555001,
                     'orderListId': 9999, 'type': 'STOP_LOSS_LIMIT'},
                    {'clientOrderId': 'xK9m2-B', 'orderId': 555002,
                     'orderListId': 9999, 'type': 'LIMIT_MAKER'},
                    # أمر غريب تماماً — لا علاقة له بأي شيء محلي
                    {'clientOrderId': 'manual-order-from-app', 'orderId': 777777,
                     'orderListId': -1, 'type': 'LIMIT'}]

            def rules(self, symbol):
                return {'symbol': symbol, 'base': 'BTC', 'quote': 'USDT',
                       'min_notional': 10.0, 'step_size': 0.00001,
                       'min_qty': 0.00001, 'tick_size': 0.01}

            def balances(self):
                return {'BTC': {'free': 0.01, 'locked': 0.0}}

            def price(self, symbol):
                return 50000.0

        ex2 = ExWithOpenOrders()
        gate = IdempotentOrderGate(db, ex2)
        r = Reconciler(db, ex2, gate=gate).run(['BTCUSDT'])
        unknown = [d for d in r.discrepancies if d['kind'] == D_UNKNOWN_EXCHANGE_ORDER]
        self.assertEqual(len(unknown), 1, f'يجب رصد أمر مجهول واحد فقط: {unknown}')
        self.assertEqual(unknown[0]['client_order_id'], 'manual-order-from-app')


# ══ 6. عدم استخدام مفاتيح Mainnet مطلقاً في هذه الاختبارات ══
class Test06_NoNetworkNoMainnet(unittest.TestCase):
    def test_no_real_network_calls(self):
        """كل الاختبارات أعلاه تعمل بلا اتصال شبكة — FakeExchange/PaperBroker فقط."""
        from src.execution.binance_client import MAINNET_ENABLED_IN_SOURCE
        self.assertFalse(MAINNET_ENABLED_IN_SOURCE)




# ══ 7. لوحة المراقبة تعرض حالة الحماية صراحة (البند 33) ══
class Test07_DashboardProtectionStatus(unittest.TestCase):
    def test_positions_query_exposes_protected_and_oco_status(self):
        import sys as _sys
        sys_path_added = os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), 'dashboard', 'backend')
        if sys_path_added not in _sys.path:
            _sys.path.insert(0, sys_path_added)
        from dashboard.backend.queries import DashboardQueries
        from dashboard.backend.readonly_db import ReadOnlyDB

        db_path = os.path.join(tempfile.mkdtemp(), 't.db')
        write_db = Database(db_path)
        write_db.open_position(id=1, symbol='BTCUSDT', qty=0.01,
                               entry_price=50000.0, stop_loss=49000.0,
                               opened_ts=1, status='OPEN',
                               order_list_id='9001', stop_order_id='50002',
                               stop_client_order_id='CID-S',
                               target_order_id='50001',
                               target_client_order_id='CID-T')

        rodb = ReadOnlyDB(db_path)
        q = DashboardQueries(rodb)
        rows = q.positions('OPEN')
        self.assertEqual(len(rows), 1)
        p = rows[0]
        self.assertTrue(p['protected'])
        self.assertEqual(p['oco_status']['order_list_id'], '9001')
        self.assertEqual(p['oco_status']['stop_order_id'], '50002')
        self.assertEqual(p['oco_status']['target_order_id'], '50001')
        self.assertNotEqual(p['oco_status']['stop_order_id'],
                            p['oco_status']['target_order_id'])


# ══ 8. تعافي OCO بعد حالة ملتبسة — القسم D من متطلبات V11 ══
class Test08_OCORecoveryAfterUncertainState(unittest.TestCase):
    """
    اكتُشف عند العمل على القسم D: `IdempotentOrderGate.recover_one()`
    كان يستعلم دائماً عبر `/api/v3/order` (أمر مفرد) بمعرّف النية —
    لكن معرّف نية OCO يُرسَل للمنصة كـ `listClientOrderId`، لا
    `clientOrderId` لأي أمر مفرد. النتيجة: OCO ناجحة فعلياً على
    المنصة كانت ستُصنَّف `ABORTED` خطأً عند أي حالة ملتبسة (Timeout
    بعد القبول، انقطاع بعد الإرسال) — وإعادة الإرسال بعدها تعني OCO
    **مكرَّرة فعلياً** على حساب حقيقي.
    """

    def test_recover_one_finds_oco_via_list_not_single_order(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        pub = Pub()
        pb = PaperBroker(db, pub, starting_quote=10000.0)
        pb.market_buy_quote('BTCUSDT', 1000, 'seed')
        qty = pb.base_free('BTCUSDT')

        gate = IdempotentOrderGate(db, pb)
        intent = gate.reserve(order_type='STOP', symbol='BTCUSDT', side='SELL',
                              position_id=1, version=1, qty=qty,
                              stop_price=49000, limit_price=52000)
        cid = intent.client_order_id

        # نُحاكي دورة send_loop الحقيقية: RESERVED → IN_FLIGHT → UNKNOWN
        # (كأن استثناءً ملتبساً حدث بعد وصول الطلب فعلياً للمنصة)
        gate._transition(cid, 'IN_FLIGHT', reason='محاكاة محاولة')
        gate._transition(cid, 'UNKNOWN', reason='محاكاة انقطاع بعد القبول')

        # لكن OCO **وصلت فعلاً ونُفِّذت** على المنصة بنفس معرّف القائمة
        pb.oco_sell('BTCUSDT', qty, 52000, 49000, 48900, cid,
                   below_client_id=f'{cid}-STOP', above_client_id=f'{cid}-TARGET')

        rec = gate.recover_one(gate.get(cid))
        self.assertNotEqual(rec['state'], 'ABORTED',
                            'OCO ناجحة فعلياً صُنِّفت ABORTED — ستُعاد إرسالها مكرَّرة')
        self.assertIn(rec['state'], ('OPEN', 'CONFIRMED', 'FILLED'))

    def test_recover_one_still_aborts_when_truly_never_sent(self):
        """لا OCO فعلياً — recover_one يجب أن يبقى ABORTED كالسابق تماماً."""
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        pub = Pub()
        pb = PaperBroker(db, pub, starting_quote=10000.0)
        gate = IdempotentOrderGate(db, pb)
        intent = gate.reserve(order_type='STOP', symbol='BTCUSDT', side='SELL',
                              position_id=2, version=1, qty=0.01,
                              stop_price=49000, limit_price=52000)
        cid = intent.client_order_id
        gate._transition(cid, 'IN_FLIGHT', reason='محاكاة محاولة')
        gate._transition(cid, 'UNKNOWN', reason='محاكاة انقطاع قبل الوصول')
        # لا استدعاء oco_sell إطلاقاً هنا — لم يصل شيء فعلياً للمنصة

        rec = gate.recover_one(gate.get(cid))
        self.assertEqual(rec['state'], 'ABORTED')

    def test_recovered_oco_all_done_maps_to_filled(self):
        """قائمة OCO انتهت (طرف نُفِّذ، الآخر أُلغي) ⇒ FILLED لا OPEN."""
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        pub = Pub()
        pb = PaperBroker(db, pub, starting_quote=10000.0)
        pb.market_buy_quote('BTCUSDT', 1000, 'seed')
        qty = pb.base_free('BTCUSDT')
        gate = IdempotentOrderGate(db, pb)
        intent = gate.reserve(order_type='STOP', symbol='BTCUSDT', side='SELL',
                              position_id=3, version=1, qty=qty,
                              stop_price=49000, limit_price=52000)
        cid = intent.client_order_id
        gate._transition(cid, 'IN_FLIGHT', reason='محاكاة')
        gate._transition(cid, 'UNKNOWN', reason='محاكاة')
        pb.oco_sell('BTCUSDT', qty, 52000, 49000, 48900, cid,
                   below_client_id=f'{cid}-STOP', above_client_id=f'{cid}-TARGET')
        # الهدف نُفِّذ فعلياً، الوقف أُلغي تلقائياً (سلوك OCO الطبيعي)
        target_o = pb._orders[f'{cid}-TARGET']
        pb._execute(target_o, 52000.0, qty, reason='test')
        pb._cancel_sibling(target_o)

        rec = gate.recover_one(gate.get(cid))
        self.assertEqual(rec['state'], 'FILLED')


# ══ 9. التحقق من طرفَي OCO كليهما — لا الوقف فقط (فجوة اكتُشفت) ══
class Test09_TargetSideVerification(unittest.TestCase):
    """
    القسم 5 من reconciliation.py كان يفحص حيوية الوقف فقط. اختفاء
    الهدف بصمت بينما الوقف لا يزال حياً (إلغاء يدوي خارجي، أو انحراف
    آخر) لم يكن يُرصَد إطلاقاً — المركز يبدو "محمياً" رغم فقدان جانب
    الربح فعلياً.
    """

    def _base_position(self, db):
        db.reserve_intent(client_order_id='list-1', order_type='STOP',
                          symbol='BTCUSDT', side='SELL', state='ACKED',
                          fingerprint='f', payload_hash='f')
        db.open_position(
            id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
            opened_ts=1, status='OPEN',
            order_list_id='1', list_client_order_id='list-1',
            stop_order_id='201', stop_client_order_id='list-1-STOP',
            target_order_id='202', target_client_order_id='list-1-TARGET')

    def test_both_sides_live_no_discrepancy(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        self._base_position(db)

        class Ex(FakeExchange):
            def open_orders(self, symbol=None):
                return [{'clientOrderId': 'list-1-STOP', 'orderId': 201,
                        'orderListId': 1, 'type': 'STOP_LOSS_LIMIT'},
                       {'clientOrderId': 'list-1-TARGET', 'orderId': 202,
                        'orderListId': 1, 'type': 'LIMIT_MAKER'}]

            def rules(self, symbol):
                return {'symbol': symbol, 'base': 'BTC', 'quote': 'USDT',
                       'min_notional': 10.0, 'step_size': 0.00001,
                       'min_qty': 0.00001, 'tick_size': 0.01}

            def balances(self):
                return {'BTC': {'free': 0.01, 'locked': 0.0}}

            def price(self, symbol):
                return 50000.0

        ex = Ex()
        r = Reconciler(db, ex, gate=IdempotentOrderGate(db, ex)).run(['BTCUSDT'])
        target_disc = [d for d in r.discrepancies if d['kind'] == D_TARGET_MISSING]
        self.assertEqual(target_disc, [])

    def test_target_vanished_while_stop_alive_flagged(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        self._base_position(db)

        class Ex(FakeExchange):
            def open_orders(self, symbol=None):
                return [{'clientOrderId': 'list-1-STOP', 'orderId': 201,
                        'orderListId': 1, 'type': 'STOP_LOSS_LIMIT'}]
                # لا الهدف — اختفى دون بديل

            def rules(self, symbol):
                return {'symbol': symbol, 'base': 'BTC', 'quote': 'USDT',
                       'min_notional': 10.0, 'step_size': 0.00001,
                       'min_qty': 0.00001, 'tick_size': 0.01}

            def balances(self):
                return {'BTC': {'free': 0.01, 'locked': 0.0}}

            def price(self, symbol):
                return 50000.0

        ex = Ex()
        r = Reconciler(db, ex, gate=IdempotentOrderGate(db, ex)).run(['BTCUSDT'])
        target_disc = [d for d in r.discrepancies if d['kind'] == D_TARGET_MISSING]
        self.assertEqual(len(target_disc), 1)
        self.assertEqual(target_disc[0]['position_id'], 1)

    def test_target_filled_while_stop_still_live_flagged_distinctly(self):
        """أخطر حالة: إلغاء الأخ لم يحدث — الهدف نُفِّذ لكن الوقف باقٍ حياً."""
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        self._base_position(db)

        class Ex(FakeExchange):
            def open_orders(self, symbol=None):
                return [{'clientOrderId': 'list-1-STOP', 'orderId': 201,
                        'orderListId': 1, 'type': 'STOP_LOSS_LIMIT'}]

            def order_status(self, symbol, order_id):
                if str(order_id) == '202':
                    return {'orderId': 202, 'status': 'FILLED'}
                return super().order_status(symbol, order_id)

            def rules(self, symbol):
                return {'symbol': symbol, 'base': 'BTC', 'quote': 'USDT',
                       'min_notional': 10.0, 'step_size': 0.00001,
                       'min_qty': 0.00001, 'tick_size': 0.01}

            def balances(self):
                return {'BTC': {'free': 0.01, 'locked': 0.0}}

            def price(self, symbol):
                return 50000.0

        ex = Ex()
        r = Reconciler(db, ex, gate=IdempotentOrderGate(db, ex)).run(['BTCUSDT'])
        target_disc = [d for d in r.discrepancies if d['kind'] == D_TARGET_MISSING]
        self.assertEqual(len(target_disc), 1)
        self.assertEqual(target_disc[0]['exchange_status'], 'FILLED')
        self.assertIn('إلغاء الأخ لم يحدث', target_disc[0]['action'])


# ══ 10. خروج جزئي — القسمان D/G من متطلبات V11 ══
class Test10_PartialExit(unittest.TestCase):
    """
    `_record_exit()` كانت تُغلق المركز بالكامل دائماً بصرف النظر عن
    الكمية المُنفَّذة الفعلية. أمر STOP_LOSS_LIMIT المُفعَّل يستقر
    كأمر Limit حي — قابل للتنفيذ الجزئي فعلياً على أي منصة حقيقية،
    بصرف النظر عن إعدادات المحاكاة المحلية. تنفيذ جزئي كان سيُغلق
    المركز بكمية أصغر من الحقيقية، تاركاً الباقي بلا حماية وبلا تتبّع
    إطلاقاً (الأخ يُلغى فور أي تنفيذ، جزئياً كان أم كاملاً).
    """

    def _make_om(self, db):
        h = HealthMonitor(db)
        pub = Pub()
        pb = PaperBroker(db, pub, starting_quote=10000.0)
        return OrderManager(db, pb, Config(), h), h, pb

    def test_full_exit_still_closes_position_as_before(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        om, h, pb = self._make_om(db)
        db.open_position(id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
                         opened_ts=1, status='OPEN')
        r = {'orderId': 999, 'status': 'FILLED',
            'fills': [{'qty': '0.01', 'price': '52000', 'commission': '0.5',
                      'commissionAsset': 'USDT'}]}
        om._record_exit(db.query('SELECT * FROM positions WHERE id=1')[0],
                        'BTCUSDT', r, 'TAKE_PROFIT')
        pos = db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertEqual(pos['status'], 'CLOSED')
        self.assertFalse(h.kill_switch_on())

    def test_partial_exit_does_not_close_position(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        om, h, pb = self._make_om(db)
        db.open_position(id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
                         opened_ts=1, status='OPEN',
                         stop_order_id='s1', target_order_id='t1')
        r = {'orderId': 999, 'status': 'PARTIALLY_FILLED',
            'fills': [{'qty': '0.004', 'price': '49000', 'commission': '0.2',
                      'commissionAsset': 'USDT'}]}
        om._record_exit(db.query('SELECT * FROM positions WHERE id=1')[0],
                        'BTCUSDT', r, 'STOP_LOSS')
        pos = db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertEqual(pos['status'], 'OPEN', 'خروج جزئي أغلق المركز بالكامل خطأً')
        self.assertAlmostEqual(pos['sold_qty'], 0.004, places=6)
        # الحماية أُزيلت — لا يبقى مرجع لأوامر أُلغيت فعلياً
        self.assertIsNone(pos['stop_order_id'])
        self.assertIsNone(pos['target_order_id'])
        self.assertTrue(h.kill_switch_on(),
                        'خروج جزئي بلا حماية للباقي يجب أن يوقف التداول')

    def test_partial_exit_logs_critical_risk_event(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        om, h, pb = self._make_om(db)
        db.open_position(id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
                         opened_ts=1, status='OPEN')
        r = {'orderId': 999, 'status': 'PARTIALLY_FILLED',
            'fills': [{'qty': '0.004', 'price': '49000', 'commission': '0.2',
                      'commissionAsset': 'USDT'}]}
        om._record_exit(db.query('SELECT * FROM positions WHERE id=1')[0],
                        'BTCUSDT', r, 'STOP_LOSS')
        rows = db.query(
            "SELECT 1 FROM risk_events WHERE kind='PARTIAL_EXIT_UNPROTECTED_REMAINDER'")
        self.assertEqual(len(rows), 1)

    def test_two_stage_partial_exit_accumulates_correctly_then_closes(self):
        """المرحلة الأولى جزئية، الثانية تُكمِل المركز — PnL يتراكم صح."""
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        om, h, pb = self._make_om(db)
        db.open_position(id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
                         opened_ts=1, status='OPEN')

        r1 = {'orderId': 1, 'status': 'PARTIALLY_FILLED',
             'fills': [{'qty': '0.004', 'price': '52000', 'commission': '0.1',
                       'commissionAsset': 'USDT'}]}
        om._record_exit(db.query('SELECT * FROM positions WHERE id=1')[0],
                        'BTCUSDT', r1, 'TAKE_PROFIT')
        pos_mid = db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertEqual(pos_mid['status'], 'OPEN')
        pnl1 = (52000 - 50000) * 0.004 - 0.1

        r2 = {'orderId': 2, 'status': 'FILLED',
             'fills': [{'qty': '0.006', 'price': '52500', 'commission': '0.15',
                       'commissionAsset': 'USDT'}]}
        om._record_exit(pos_mid, 'BTCUSDT', r2, 'TAKE_PROFIT')
        pos_final = db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertEqual(pos_final['status'], 'CLOSED')
        pnl2 = (52500 - 50000) * 0.006 - 0.15
        self.assertAlmostEqual(pos_final['realized_pnl'], pnl1 + pnl2, places=6)
        self.assertAlmostEqual(pos_final['sold_qty'], 0.01, places=6)

    def test_exit_qty_exceeding_remaining_is_capped_not_trusted_blindly(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        om, h, pb = self._make_om(db)
        db.open_position(id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
                         opened_ts=1, status='OPEN', sold_qty=0.008)
        r = {'orderId': 999, 'status': 'FILLED',
            'fills': [{'qty': '0.01', 'price': '52000', 'commission': '0.1',
                      'commissionAsset': 'USDT'}]}   # 0.01 لكن المتبقي 0.002 فقط
        om._record_exit(db.query('SELECT * FROM positions WHERE id=1')[0],
                        'BTCUSDT', r, 'TAKE_PROFIT')
        rows = db.query(
            "SELECT 1 FROM risk_events WHERE kind='EXIT_QTY_EXCEEDS_POSITION'")
        self.assertEqual(len(rows), 1)
        self.assertTrue(h.kill_switch_on())


# ══ 11. حارس التكرار في PaperBroker.oco_sell() كان كوداً ميتاً ══
class Test11_OCODuplicateGuardActuallyFires(unittest.TestCase):
    """
    الفحص القديم كان `if client_id in self._orders` — لكن `client_id`
    (معرّف القائمة) لا يُخزَّن أبداً كمفتاح في `self._orders` (المفاتيح
    دائماً `{cid}-STOP`/`{cid}-TARGET`). النتيجة: الحارس لم يكن يعمل
    إطلاقاً — أي تكرار كان "يُرفَض" صدفة فقط عبر خطأ رصيد غير مرتبط
    (الرصيد مُقفَل من المحاولة الأولى)، لا حارساً موثوقاً. لو توفَّر
    رصيد كافٍ صدفة لمحاولتين، التكرار كان يمر بصمت.
    """

    def test_duplicate_cid_rejected_with_correct_error_not_balance_error(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        pub = Pub()
        pb = PaperBroker(db, pub, starting_quote=10000.0)
        pb.market_buy_quote('BTCUSDT', 1000, 'b1')
        qty = pb.base_free('BTCUSDT')
        pb.oco_sell('BTCUSDT', qty, 52000, 49000, 48900, 'dup-cid')
        with self.assertRaises(BinanceError) as ctx:
            pb.oco_sell('BTCUSDT', qty, 52000, 49000, 48900, 'dup-cid')
        self.assertIn('Duplicate', str(ctx.exception),
                      'رُفض التكرار برسالة خاطئة (رصيد) لا رسالة تكرار صريحة')

    def test_duplicate_still_rejected_even_with_ample_balance(self):
        """
        الاختبار الحاسم: رصيد وفير كافٍ لعدة محاولات — لو كان الحارس
        القديم لا يزال معطَّلاً، هذا التكرار كان سيمر بصمت (لا خطأ
        رصيد يُخفيه هذه المرة).
        """
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        pub = Pub()
        pb = PaperBroker(db, pub, starting_quote=1_000_000.0)
        pb.market_buy_quote('BTCUSDT', 900_000, 'b1')
        qty = pb.base_free('BTCUSDT')
        pb.oco_sell('BTCUSDT', qty * 0.3, 52000, 49000, 48900, 'dup-cid2')
        with self.assertRaises(BinanceError) as ctx:
            pb.oco_sell('BTCUSDT', qty * 0.3, 52000, 49000, 48900, 'dup-cid2')
        self.assertIn('Duplicate', str(ctx.exception))


# ══ 12. تركيب خروج جزئي + إعادة حماية تلقائية موجودة أصلاً ══
class Test12_PartialExitComposesWithExistingAutoRecovery(unittest.TestCase):
    """
    اكتُشف أثناء توثيق "لا إعادة حماية تلقائية بعد" في التقرير: هذا
    غير دقيق تماماً — `guard_stops()` (آلية موجودة مسبقاً، غير
    مرتبطة بعمل هذه الجولة) تُعيد وضع وقف مفرد تلقائياً في الدورة
    التالية إن وجدت `stop_order_id=None`، **حتى مع تفعيل مفتاح
    الإيقاف** — لأن مفتاح الإيقاف يمنع صفقات جديدة فقط (عبر فحص
    `pre_trade()`)، لا إجراءات الحماية لمراكز قائمة. النتيجة الفعلية
    بعد خروج جزئي: المتبقي يُعاد حمايته تلقائياً بوقف فردي (بلا هدف)
    خلال دورة واحدة، لا "بلا حماية حتى تدخل يدوي" كما وُصف سابقاً
    بتشاؤم زائد.
    """

    def test_stop_auto_replaced_after_partial_exit_clears_protection(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        pub = Pub()
        pb = PaperBroker(db, pub, starting_quote=10000.0)
        om = OrderManager(db, pb, Config(), h)

        pb.market_buy_quote('BTCUSDT', 1000, 'seed')
        qty = pb.base_free('BTCUSDT')
        db.open_position(id=1, symbol='BTCUSDT', qty=qty, entry_price=50000.0,
                         opened_ts=1, status='OPEN', stop_loss=48000.0)

        # خروج جزئي يُزيل مراجع الحماية ويُفعِّل مفتاح الإيقاف
        r = {'orderId': 999, 'status': 'PARTIALLY_FILLED',
            'fills': [{'qty': str(qty * 0.4), 'price': '49000',
                      'commission': '0.1', 'commissionAsset': 'USDT'}]}
        om._record_exit(db.query('SELECT * FROM positions WHERE id=1')[0],
                        'BTCUSDT', r, 'STOP_LOSS')
        pos = db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertIsNone(pos['stop_order_id'])
        self.assertTrue(h.kill_switch_on())

        # الدورة التالية: guard_stops() يُعيد الحماية تلقائياً
        actions = om.guard_stops()
        pos2 = db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertIsNotNone(pos2['stop_order_id'],
                             'لم تُعَد الحماية تلقائياً رغم توفّر آلية guard_stops()')
        self.assertTrue(h.kill_switch_on(),
                        'مفتاح الإيقاف يجب أن يبقى مُفعَّلاً — لا هدف مُعاد بعد، '
                        'قرار بشري لا يزال مطلوباً')


# ══ 13. استمرارية كاملة عبر انقطاع فعلي — كائنات Python جديدة تماماً ══
class Test13_FullRestartRecovery(unittest.TestCase):
    """
    القسم D: "restart recovery". يُحاكي انقطاعاً حقيقياً — الجلسة
    الثانية تبني `Database`/`PaperBroker`/`OrderManager` جديدة تماماً
    (لا مشاركة أي كائن Python مع الجلسة الأولى)، متصلة بنفس ملف
    القاعدة فقط، تماماً كما يحدث عند إعادة تشغيل العملية فعلياً.
    """

    def test_oco_protection_survives_and_no_duplicate_after_restart(self):
        db_path = os.path.join(tempfile.mkdtemp(), 't.db')

        # ═ الجلسة 1 ═
        db1 = Database(db_path)
        h1 = HealthMonitor(db1)
        pb1 = PaperBroker(db1, Pub(), starting_quote=10000.0)
        om1 = OrderManager(db1, pb1, Config(), h1)
        pb1.market_buy_quote('BTCUSDT', 1000, 'entry')
        qty = pb1.base_free('BTCUSDT')
        db1.open_position(id=1, symbol='BTCUSDT', qty=qty, entry_price=50000.0,
                          opened_ts=1, status='OPEN')
        sid, tid = om1._place_oco('BTCUSDT', qty, 49000, 52000, pos_id=1)
        self.assertIsNotNone(sid)

        # ═ انقطاع كامل — لا كائن مشترك مع الجلسة 1 إطلاقاً ═
        db2 = Database(db_path)
        h2 = HealthMonitor(db2)
        pb2 = PaperBroker(db2, Pub(), starting_quote=10000.0)
        om2 = OrderManager(db2, pb2, Config(), h2)

        # الأوامر استُعيدت فعلياً من القاعدة، لا من ذاكرة الجلسة 1
        stop_cids = [cid for cid, o in pb2._orders.items()
                    if str(o['orderId']) == str(sid)]
        target_cids = [cid for cid, o in pb2._orders.items()
                       if str(o['orderId']) == str(tid)]
        self.assertEqual(len(stop_cids), 1, 'أمر الوقف لم يُستعَد بعد إعادة التشغيل')
        self.assertEqual(len(target_cids), 1, 'أمر الهدف لم يُستعَد بعد إعادة التشغيل')
        self.assertEqual(pb2._orders[stop_cids[0]]['status'], 'NEW')

        actions = om2.guard_stops()
        self.assertEqual(actions, [], 'guard_stops أنشأ إجراءً رغم أن الحماية سليمة فعلاً')
        pos2 = db2.query(
            'SELECT stop_order_id, target_order_id FROM positions WHERE id=1')[0]
        self.assertEqual(pos2['stop_order_id'], sid, 'الوقف تغيَّر أو تكرَّر بعد إعادة التشغيل')
        self.assertEqual(pos2['target_order_id'], tid)

        gate2 = IdempotentOrderGate(db2, pb2)
        r = Reconciler(db2, pb2, gate=gate2).run(['BTCUSDT'])
        oco_related = [d for d in r.discrepancies
                       if d['kind'] in (D_UNKNOWN_EXCHANGE_ORDER, D_TARGET_MISSING)]
        self.assertEqual(oco_related, [],
                         f'أطراف OCO سليمة صُنِّفت خطأً بعد إعادة التشغيل: {oco_related}')


# ══ 14. كود ميت في _settle_paper_trigger لمراكز OCO — موثَّق لا مخفي ══
class Test14_SettlePaperTriggerOCOPath(unittest.TestCase):
    """
    اكتُشف: حلقة "إلغاء الأمر المقابل" في `_settle_paper_trigger()`
    تبحث عن نية بنوع `TARGET` منفصلة — تصميم يسبق توحيد OCO. `_place_oco()`
    يُنشئ نية واحدة فقط بنوع `STOP` لكامل القائمة؛ `place_target()`
    (المُنشئة الوحيدة لنوع TARGET) غير مُستدعاة من مسار التداول الفعلي
    إطلاقاً. النتيجة: هذه الحلقة **لا تُنفَّذ جسمها أبداً** لمراكز
    OCO — تتبّع النية يكتمل بالكامل عبر تحويل النية الوحيدة (STOP)
    إلى FILLED مباشرة، لا عبر هذه الحلقة.
    """

    def test_target_intent_never_exists_for_oco_positions(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        pb = PaperBroker(db, Pub(), starting_quote=10000.0)
        om = OrderManager(db, pb, Config(), h)
        pb.market_buy_quote('BTCUSDT', 1000, 'entry')
        qty = pb.base_free('BTCUSDT')
        db.open_position(id=1, symbol='BTCUSDT', qty=qty, entry_price=50000.0,
                         opened_ts=1, status='OPEN')
        om._place_oco('BTCUSDT', qty, 49000, 52000, pos_id=1)

        # لا نية TARGET منفصلة إطلاقاً — نية واحدة فقط بنوع STOP لكل OCO
        target_intents = db.active_intents('TARGET', 1)
        self.assertEqual(target_intents, [],
                         'وُجدت نية TARGET منفصلة — الافتراض المُوثَّق في الكود خاطئ')
        stop_intents = db.query(
            "SELECT COUNT(*) as n FROM order_intents WHERE position_id=1")
        self.assertEqual(stop_intents[0]['n'], 1,
                         'يجب نية واحدة فقط لكامل قائمة OCO')

    def test_intent_transitions_to_filled_without_the_dead_loop(self):
        """تتبّع النية يكتمل بالكامل بلا اعتماد على الحلقة الميتة."""
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        pb = PaperBroker(db, Pub(), starting_quote=10000.0)
        om = OrderManager(db, pb, Config(), h)
        pb.market_buy_quote('BTCUSDT', 1000, 'entry')
        qty = pb.base_free('BTCUSDT')
        db.open_position(id=1, symbol='BTCUSDT', qty=qty, entry_price=50000.0,
                         opened_ts=1, status='OPEN')
        om._place_oco('BTCUSDT', qty, 49000, 52000, pos_id=1)
        cid = db.query(
            'SELECT client_order_id FROM order_intents WHERE position_id=1')[0][
                'client_order_id']
        db.transition_order_state(cid, 'FILLED', reason='محاكاة تفعيل الوقف',
                                  actor='test')
        state = db.query(
            'SELECT state FROM order_intents WHERE client_order_id=?', (cid,))[0]
        self.assertEqual(state['state'], 'FILLED')


# ══ 15. الإصلاح الأخطر: _settle_paper_trigger كانت تفشل بصمت تام ══
class Test15_SettlePaperTriggerCriticalFix(unittest.TestCase):
    """
    اكتُشف: `_settle_paper_trigger()` كانت تبحث عن المركز عبر
    `get_intent_by_client_order_id(trig['cid'])` — لكن `trig['cid']`
    معرّف **الطرف الفرعي** (`{list_cid}-STOP`/`-TARGET`)، بينما
    `order_intents` يخزِّن نية واحدة فقط لكامل OCO بمعرّف **القائمة**.
    النتيجة: أي تفعيل وقف أو هدف في Paper كان يُبحث عنه بمعرّف خاطئ
    فتفشل الدالة من أول سطر **بصمت تام** — لا إغلاق مركز، لا PnL،
    لا تحديث RiskGuard. المركز يبقى "OPEN" في القاعدة للأبد رغم
    إغلاقه فعلياً على المنصة المحاكاة. لم يكتشفه أي اختبار سابق لأن
    لا اختبار كان يُنفِّذ المسار الكامل (evaluate_pending → trigger
    حقيقي → _settle_paper_trigger) بأسعار تُفعِّل الوقف فعلياً.
    """

    def _setup(self, db_path=None):
        db = Database(db_path or os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        from src.risk.risk_guard import RiskGuard
        guard = RiskGuard(db=db)
        pub = Pub()
        pb = PaperBroker(db, pub, starting_quote=10000.0)
        om = OrderManager(db, pb, Config(), h, risk_guard=guard)
        return db, h, guard, pb, om

    def test_stop_trigger_actually_closes_position(self):
        db, h, guard, pb, om = self._setup()
        pb.market_buy_quote('BTCUSDT', 1000, 'entry')
        qty = pb.base_free('BTCUSDT')
        db.open_position(id=1, symbol='BTCUSDT', qty=qty, entry_price=50000.0,
                         opened_ts=1, status='OPEN')
        om._place_oco('BTCUSDT', qty, 49000, 52000, pos_id=1)

        import live_trader as LT

        class FakeTrader:
            symbol = 'BTCUSDT'
            _settle_paper_trigger = LT.LiveTrader._settle_paper_trigger

            def __init__(self, db, paper, orders):
                self.db, self.paper, self.orders = db, paper, orders

        pub2 = Pub(p=48990.0)   # سعر أسفل الوقف (49000) يُفعِّله
        pb2 = PaperBroker(db, pub2, starting_quote=10000.0)
        pb2._load()   # يستعيد نفس الأوامر من القاعدة بسعر جديد يُفعِّل الزناد
        triggers = pb2.evaluate_pending('BTCUSDT')
        self.assertTrue(triggers, 'لم يُفعَّل الوقف رغم أن السعر تحته')

        ft = FakeTrader(db, pb2, om)
        for t in triggers:
            ft._settle_paper_trigger(t)

        pos = db.query('SELECT status, realized_pnl FROM positions WHERE id=1')[0]
        self.assertEqual(pos['status'], 'CLOSED',
                         'المركز لم يُغلَق رغم تفعيل الوقف فعلياً — الخلل الحرِج لم يُصلَح')
        self.assertIsNotNone(pos['realized_pnl'])
        self.assertLess(pos['realized_pnl'], 0, 'خسارة متوقَّعة من الوقف')

    def test_target_trigger_actually_closes_position(self):
        db, h, guard, pb, om = self._setup()
        pb.market_buy_quote('BTCUSDT', 1000, 'entry')
        qty = pb.base_free('BTCUSDT')
        db.open_position(id=1, symbol='BTCUSDT', qty=qty, entry_price=50000.0,
                         opened_ts=1, status='OPEN')
        om._place_oco('BTCUSDT', qty, 49000, 52000, pos_id=1)

        import live_trader as LT

        class FakeTrader:
            symbol = 'BTCUSDT'
            _settle_paper_trigger = LT.LiveTrader._settle_paper_trigger

            def __init__(self, db, paper, orders):
                self.db, self.paper, self.orders = db, paper, orders

        pub2 = Pub(p=52100.0)   # سعر فوق الهدف (52000) يُفعِّله
        pb2 = PaperBroker(db, pub2, starting_quote=10000.0)
        pb2._load()
        triggers = pb2.evaluate_pending('BTCUSDT')
        self.assertTrue(triggers, 'لم يُفعَّل الهدف رغم أن السعر فوقه')

        ft = FakeTrader(db, pb2, om)
        for t in triggers:
            ft._settle_paper_trigger(t)

        pos = db.query('SELECT status, realized_pnl FROM positions WHERE id=1')[0]
        self.assertEqual(pos['status'], 'CLOSED')
        self.assertGreater(pos['realized_pnl'], 0, 'ربح متوقَّع من الهدف')

    def test_risk_guard_updates_on_stop_trigger(self):
        db, h, guard, pb, om = self._setup()
        pb.market_buy_quote('BTCUSDT', 1000, 'entry')
        qty = pb.base_free('BTCUSDT')
        db.open_position(id=1, symbol='BTCUSDT', qty=qty, entry_price=50000.0,
                         opened_ts=1, status='OPEN')
        om._place_oco('BTCUSDT', qty, 49000, 52000, pos_id=1)

        import live_trader as LT

        class FakeTrader:
            symbol = 'BTCUSDT'
            _settle_paper_trigger = LT.LiveTrader._settle_paper_trigger

            def __init__(self, db, paper, orders):
                self.db, self.paper, self.orders = db, paper, orders

        pub2 = Pub(p=48990.0)
        pb2 = PaperBroker(db, pub2, starting_quote=10000.0)
        pb2._load()
        triggers = pb2.evaluate_pending('BTCUSDT')
        ft = FakeTrader(db, pb2, om)
        for t in triggers:
            ft._settle_paper_trigger(t)

        self.assertEqual(guard.consecutive_losses, 1,
                         'RiskGuard لم يُحدَّث بعد خسارة فعلية — عداد الخسائر المتتالية معطَّل')


# ══ 16. اكتشاف تنفيذ الهدف على منصة حقيقية — كان غائباً تماماً ══
class Test16_GuardStopsDetectsTargetFill(unittest.TestCase):
    """
    اكتُشف: `guard_stops()` (مسار Live/Testnet عبر استعلام دوري، لا
    `evaluate_pending()` الخاصة بـ Paper) كانت تتحقق من جانب **الوقف
    فقط**. لا فحص مطابق لتنفيذ **الهدف** إطلاقاً. على منصة حقيقية،
    تفعيل الهدف يُلغي الوقف تلقائياً (سلوك OCO الحقيقي) — فيبدو
    للفحص القديم أن "الوقف مفقود لا مُنفَّذ"، ويحاول **وضع وقف جديد
    لمركز أُغلق بالفعل بالكامل عبر الهدف**. أُصلح بإضافة فحص مطابق
    للهدف، موحَّد مع الوقف عبر `_record_exit()` نفسها (لا
    `_settle_after_stop()` المنفصلة والأقل أماناً سابقاً).
    """

    def test_target_fill_detected_and_closes_position_not_replaces_stop(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        db.open_position(
            id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
            opened_ts=1, status='OPEN', stop_loss=49000.0,
            stop_order_id='201', stop_client_order_id='list1-STOP',
            target_order_id='202', target_client_order_id='list1-TARGET',
            order_list_id='1', list_client_order_id='list1')

        class ExWithFilledTarget(FakeExchange):
            def price(self, symbol):
                return 52050.0

            def order_status(self, symbol, order_id):
                if str(order_id) == '201':
                    return {'status': 'CANCELED', 'orderId': 201}
                if str(order_id) == '202':
                    return {'status': 'FILLED', 'orderId': 202,
                            'executedQty': '0.01', 'cummulativeQuoteQty': '520.50',
                            'price': '52050.0'}
                raise Exception('unexpected order_id')

        ex = ExWithFilledTarget()
        om = OrderManager(db, ex, Config(), h)
        actions = om.guard_stops()

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['action'], 'TARGET_FILLED_SETTLED')
        self.assertNotEqual(actions[0]['action'], 'STOP_REPLACED',
                            'كان سيحاول وضع وقف جديد لمركز أُغلق بالفعل عبر الهدف')

        pos = db.query('SELECT status, realized_pnl FROM positions WHERE id=1')[0]
        self.assertEqual(pos['status'], 'CLOSED')
        self.assertAlmostEqual(pos['realized_pnl'], 20.5, places=2)

    def test_stop_fill_still_detected_via_unified_path(self):
        """التوحيد مع _record_exit() لم يكسر مسار الوقف الأصلي."""
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        db.open_position(
            id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
            opened_ts=1, status='OPEN', stop_loss=49000.0,
            stop_order_id='201', stop_client_order_id='list1-STOP',
            target_order_id='202', target_client_order_id='list1-TARGET',
            order_list_id='1', list_client_order_id='list1')

        class ExWithFilledStop(FakeExchange):
            def price(self, symbol):
                return 48900.0

            def order_status(self, symbol, order_id):
                if str(order_id) == '201':
                    return {'status': 'FILLED', 'orderId': 201,
                            'executedQty': '0.01', 'cummulativeQuoteQty': '489.00',
                            'price': '48900.0'}
                raise Exception('unexpected order_id')

        ex = ExWithFilledStop()
        om = OrderManager(db, ex, Config(), h)
        actions = om.guard_stops()
        self.assertEqual(actions[0]['action'], 'STOP_FILLED_SETTLED')
        pos = db.query('SELECT status, realized_pnl FROM positions WHERE id=1')[0]
        self.assertEqual(pos['status'], 'CLOSED')
        self.assertLess(pos['realized_pnl'], 0)


# ══ 17. _exit_via_gate كانت لا تزال تستخدم المسار القديم رغم تعليق خاطئ ══
class Test17_ExitViaGateUsesUnifiedRecordExit(unittest.TestCase):
    """
    اكتُشف أثناء تصحيح `guard_stops()`: `_exit_via_gate()` (تُستدعى من
    `close_long()`/`_emergency_exit()`) كانت **لا تزال** تستدعي
    `_settle_after_stop()` القديمة عند اكتشاف أن الوقف نُفِّذ فعلاً قبل
    محاولة بيع إشارة/طارئ — رغم أن تعليقاً سابقاً في نفس الجلسة ادّعى
    خطأً أنها "لم تعد تُستدعى من أي مسار". صُحِّح كلا الأمرين: التعليق
    الآن دقيق، و`_exit_via_gate()` توحَّدت مع `guard_stops()` عبر
    `_record_exit()` نفسها.
    """

    def test_exit_via_gate_detects_already_filled_stop_via_unified_path(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        db.open_position(
            id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
            opened_ts=1, status='OPEN', stop_loss=49000.0,
            stop_order_id='201', stop_client_order_id='list1-STOP',
            target_order_id='202', target_client_order_id='list1-TARGET')

        class ExStopAlreadyFilled(FakeExchange):
            def order_status(self, symbol, order_id):
                if str(order_id) == '201':
                    return {'status': 'FILLED', 'orderId': 201,
                            'executedQty': '0.01', 'cummulativeQuoteQty': '489.00',
                            'price': '48900.0'}
                raise Exception('unexpected order_id')

            def base_free(self, symbol):
                return 0.0   # الوقف باع الكل فعلاً — لا رصيد متبقٍ للبيع مجدداً

        ex = ExStopAlreadyFilled()
        om = OrderManager(db, ex, Config(), h)
        pos = db.query('SELECT * FROM positions WHERE id=1')[0]
        result = om.close_long(pos, reason='SIGNAL_EXIT')

        self.assertTrue(result, 'يجب أن يُعتبَر الخروج ناجحاً — الوقف أنجزه بالفعل')
        pos2 = db.query('SELECT status, realized_pnl FROM positions WHERE id=1')[0]
        self.assertEqual(pos2['status'], 'CLOSED')
        self.assertLess(pos2['realized_pnl'], 0)


# ══ 18. حافة أضيق: النية الأصلية عالقة بعد إغلاق Live/Testnet ══
class Test18_ListIntentStateUpdatesOnLiveExit(unittest.TestCase):
    """
    حافة أضيق اكتُشفت بمراجعة إضافية: `_settle_paper_trigger()` (Paper)
    تُحدِّث نية القائمة الأصلية إلى FILLED صراحةً. `guard_stops()`/
    `_exit_via_gate()` (Live/Testnet) كانتا لا تفعلان ذلك — النية تبقى
    عالقة عند حالتها السابقة (CONFIRMED مثلاً) رغم إغلاق المركز فعلياً.
    ليس بقاً خطراً (لا حظر تداول، لا تصادم معرّفات) لكنه عدم اتساق
    حقيقي بين المسارين، وقد يُربك أي تدقيق مستقبلي على `order_intents`.
    """

    def _setup_intent_and_position(self, db):
        db.reserve_intent(client_order_id='list1', order_type='STOP',
                          symbol='BTCUSDT', side='SELL', state='RESERVED',
                          fingerprint='f', payload_hash='f')
        db.transition_order_state('list1', 'IN_FLIGHT', reason='محاكاة', actor='test')
        db.transition_order_state('list1', 'CONFIRMED', reason='محاكاة قبول',
                                  actor='test')
        db.open_position(
            id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
            opened_ts=1, status='OPEN', stop_loss=49000.0,
            stop_order_id='201', stop_client_order_id='list1-STOP',
            target_order_id='202', target_client_order_id='list1-TARGET',
            order_list_id='1', list_client_order_id='list1')

    def test_list_intent_transitions_to_filled_on_stop_fill(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        self._setup_intent_and_position(db)

        class ExStopFilled(FakeExchange):
            def order_status(self, symbol, order_id):
                if str(order_id) == '201':
                    return {'status': 'FILLED', 'orderId': 201,
                            'executedQty': '0.01', 'cummulativeQuoteQty': '489.00',
                            'price': '48900.0'}
                raise Exception('unexpected')

            def price(self, symbol):
                return 48900.0

        om = OrderManager(db, ExStopFilled(), Config(), h)
        om.guard_stops()
        intent = db.query(
            'SELECT state FROM order_intents WHERE client_order_id=?', ('list1',))[0]
        self.assertEqual(intent['state'], 'FILLED',
                         'النية الأصلية بقيت عالقة رغم إغلاق المركز فعلياً')

    def test_list_intent_transitions_to_filled_on_target_fill(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        self._setup_intent_and_position(db)

        class ExTargetFilled(FakeExchange):
            def order_status(self, symbol, order_id):
                if str(order_id) == '201':
                    return {'status': 'CANCELED', 'orderId': 201}
                if str(order_id) == '202':
                    return {'status': 'FILLED', 'orderId': 202,
                            'executedQty': '0.01', 'cummulativeQuoteQty': '520.50',
                            'price': '52050.0'}
                raise Exception('unexpected')

            def price(self, symbol):
                return 52050.0

        om = OrderManager(db, ExTargetFilled(), Config(), h)
        om.guard_stops()
        intent = db.query(
            'SELECT state FROM order_intents WHERE client_order_id=?', ('list1',))[0]
        self.assertEqual(intent['state'], 'FILLED')


# ══ 19. حافة أضيق زيادة: النية لا تُوسَم FILLED قبل التأكد من الإغلاق الكامل ══
class Test19_IntentStateReflectsActualOutcome(unittest.TestCase):
    """
    اكتُشفت أثناء مراجعة الإصلاح السابق (Test18) نفسه: كان يُحدِّث
    النية إلى `FILLED` **قبل** معرفة نتيجة `_record_exit()` الفعلية —
    "أمر الوقف بحالة FILLED" لا يعني بالضرورة "المركز أُغلق بالكامل"
    إن كانت كمية الأمر نفسه أصغر من كمية المركز المتبقية (حافة نادرة:
    أمر بكمية أقل من كامل المركز). النتيجة: كان يُمكن أن تُصنَّف النية
    `FILLED` (حالة نهائية — لا انتقال آخر مسموح) بينما المركز لا يزال
    `OPEN` فعلياً بعد خروج جزئي — تناقض حقيقي. أُصلح بترتيب الاستدعاء:
    `_record_exit()` أولاً، ثم فحص حالة المركز الناتجة فعلياً لتحديد
    `FILLED` أو `PARTIALLY_FILLED` بدقة.
    """

    def test_partial_order_fill_marks_intent_partially_filled_not_filled(self):
        db = Database(os.path.join(tempfile.mkdtemp(), 't.db'))
        h = HealthMonitor(db)
        db.reserve_intent(client_order_id='list1', order_type='STOP',
                          symbol='BTCUSDT', side='SELL', state='RESERVED',
                          fingerprint='f', payload_hash='f')
        db.transition_order_state('list1', 'IN_FLIGHT', reason='m', actor='t')
        db.transition_order_state('list1', 'CONFIRMED', reason='m', actor='t')
        db.open_position(
            id=1, symbol='BTCUSDT', qty=0.02, entry_price=50000.0,
            opened_ts=1, status='OPEN', stop_loss=49000.0,
            stop_order_id='201', stop_client_order_id='list1-STOP',
            target_order_id='202', target_client_order_id='list1-TARGET',
            order_list_id='1', list_client_order_id='list1')

        class ExStopFilledPartialQty(FakeExchange):
            def order_status(self, symbol, order_id):
                if str(order_id) == '201':
                    # الأمر نفسه FILLED لكن بكمية أصغر من المركز الكامل
                    return {'status': 'FILLED', 'orderId': 201,
                            'executedQty': '0.008', 'cummulativeQuoteQty': '391.20',
                            'price': '48900.0'}
                raise Exception('unexpected')

            def price(self, symbol):
                return 48900.0

        om = OrderManager(db, ExStopFilledPartialQty(), Config(), h)
        om.guard_stops()

        pos = db.query('SELECT status, sold_qty FROM positions WHERE id=1')[0]
        intent = db.query(
            'SELECT state FROM order_intents WHERE client_order_id=?', ('list1',))[0]

        self.assertEqual(pos['status'], 'OPEN', 'خروج جزئي أغلق المركز خطأً')
        self.assertAlmostEqual(pos['sold_qty'], 0.008, places=6)
        self.assertEqual(intent['state'], 'PARTIALLY_FILLED',
                         'النية صُنِّفت FILLED رغم أن المركز لا يزال OPEN فعلياً — '
                         'تناقض بين حالتين لنفس العملية')
        self.assertNotEqual(intent['state'], 'FILLED')


if __name__ == '__main__':
    unittest.main(verbosity=2)
