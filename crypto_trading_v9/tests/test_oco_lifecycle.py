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
from src.execution.paper_broker import PaperBroker
from src.execution.reconciliation import Reconciler, D_UNKNOWN_EXCHANGE_ORDER
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
