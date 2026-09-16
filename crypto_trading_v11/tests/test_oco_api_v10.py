"""
اختبارات V10 — الانتقال من `/api/v3/order/oco` المهجورة إلى
`/api/v3/orderList/oco` الحالية. الادعاء تحقَّق منه فعلياً عبر بحث
حقيقي في توثيق بينانس الرسمي قبل أي تعديل — راجع الرد الذي يسبق هذا
الملف في المحادثة للمصادر.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution.binance_client import BinanceClient
from src.execution.oco import parse_oco_response, OCOResult


def mkclient():
    c = BinanceClient('k' * 20, 's' * 20, testnet=True)

    def fake_req(method, path, params=None, signed=False):
        if path == '/api/v3/exchangeInfo':
            return {'symbols': [{
                'symbol': params.get('symbol', 'BTCUSDT'), 'status': 'TRADING',
                'baseAsset': 'BTC', 'quoteAsset': 'USDT',
                'filters': [
                    {'filterType': 'LOT_SIZE', 'stepSize': '0.00001',
                     'minQty': '0.00001'},
                    {'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},
                    {'filterType': 'MIN_NOTIONAL', 'minNotional': '10.0'},
                    {'filterType': 'NOTIONAL', 'minNotional': '10.0'}]}]}
        return {'orderListId': 1}

    c._req = MagicMock(side_effect=fake_req)
    return c


# ══ 1. النقطة الصحيحة، لا المهجورة ══
class Test01_CorrectEndpoint(unittest.TestCase):
    def test_oco_sell_uses_new_endpoint_not_deprecated_one(self):
        c = mkclient()
        c.oco_sell('BTCUSDT', 0.01, 52000, 49000, 48900, 'cid1')
        args, kwargs = c._req.call_args
        method, path = args[0], args[1]
        self.assertEqual(method, 'POST')
        self.assertEqual(path, '/api/v3/orderList/oco')
        self.assertNotEqual(path, '/api/v3/order/oco',
                            'لا يزال يستخدم النقطة المهجورة رسمياً')

    def test_request_uses_above_below_contract_not_deprecated_params(self):
        c = mkclient()
        c.oco_sell('BTCUSDT', 0.01, 52000, 49000, 48900, 'cid1')
        params = c._req.call_args[0][2]
        # العقد الجديد
        self.assertEqual(params['aboveType'], 'LIMIT_MAKER')
        self.assertEqual(params['belowType'], 'STOP_LOSS_LIMIT')
        self.assertIn('abovePrice', params)
        self.assertIn('belowStopPrice', params)
        self.assertIn('belowPrice', params)
        self.assertEqual(params['belowTimeInForce'], 'GTC')
        # العقد القديم المهجور يجب ألا يظهر
        self.assertNotIn('stopLimitPrice', params)
        self.assertNotIn('stopLimitTimeInForce', params)

    def test_explicit_child_client_ids_sent_not_guessed(self):
        """
        لا اعتماد على تخمين لاحقة — القسم 3 من متطلبات V10: نُرسل
        aboveClientOrderId/belowClientOrderId صراحةً.
        """
        c = mkclient()
        c.oco_sell('BTCUSDT', 0.01, 52000, 49000, 48900, 'cid1',
                  above_client_id='cid1-TARGET', below_client_id='cid1-STOP')
        params = c._req.call_args[0][2]
        self.assertEqual(params['aboveClientOrderId'], 'cid1-TARGET')
        self.assertEqual(params['belowClientOrderId'], 'cid1-STOP')
        self.assertEqual(params['listClientOrderId'], 'cid1')
        # الثلاثة معرّفات مختلفة — لا خلط بين مستوى القائمة والأبناء
        self.assertNotEqual(params['listClientOrderId'],
                            params['aboveClientOrderId'])
        self.assertNotEqual(params['listClientOrderId'],
                            params['belowClientOrderId'])


# ══ 2. إلغاء واستعلام القائمة ══
class Test02_CancelAndQuery(unittest.TestCase):
    def test_cancel_oco_uses_order_list_endpoint(self):
        c = mkclient()
        c.cancel_oco('BTCUSDT', order_list_id='9001')
        args = c._req.call_args[0]
        self.assertEqual(args[0], 'DELETE')
        self.assertEqual(args[1], '/api/v3/orderList')
        self.assertEqual(args[2]['orderListId'], '9001')

    def test_cancel_oco_requires_an_identifier(self):
        c = mkclient()
        with self.assertRaises(ValueError):
            c.cancel_oco('BTCUSDT')

    def test_query_oco_by_list_client_id(self):
        c = mkclient()
        c.query_oco(list_client_order_id='lcid1')
        args = c._req.call_args[0]
        self.assertEqual(args[0], 'GET')
        self.assertEqual(args[1], '/api/v3/orderList')
        self.assertEqual(args[2]['origClientOrderId'], 'lcid1')


# ══ 3. OCOResult — القسم 5 ══
class Test03_OCOResultModel(unittest.TestCase):
    REALISTIC = {
        'orderListId': 9001, 'listClientOrderId': 'oco1',
        'listStatusType': 'EXEC_STARTED', 'listOrderStatus': 'EXECUTING',
        'symbol': 'BTCUSDT',
        'orders': [{'symbol': 'BTCUSDT', 'orderId': 50002, 'clientOrderId': 'CID-S'},
                  {'symbol': 'BTCUSDT', 'orderId': 50001, 'clientOrderId': 'CID-T'}],
        'orderReports': [
            {'symbol': 'BTCUSDT', 'orderId': 50002, 'clientOrderId': 'CID-S',
             'type': 'STOP_LOSS_LIMIT', 'status': 'NEW'},
            {'symbol': 'BTCUSDT', 'orderId': 50001, 'clientOrderId': 'CID-T',
             'type': 'LIMIT_MAKER', 'status': 'NEW'}],
    }

    def test_valid_fixture_parses_correctly(self):
        r = parse_oco_response(self.REALISTIC, expected_symbol='BTCUSDT')
        self.assertTrue(r.valid)
        self.assertTrue(r.fully_protected)
        self.assertEqual(r.order_list_id, '9001')
        self.assertEqual(r.stop_order_id, '50002')
        self.assertEqual(r.target_order_id, '50001')
        self.assertEqual(r.stop_client_order_id, 'CID-S')
        self.assertEqual(r.target_client_order_id, 'CID-T')
        self.assertNotEqual(r.stop_order_id, r.target_order_id)

    def test_empty_response_invalid(self):
        r = parse_oco_response({})
        self.assertFalse(r.valid)
        self.assertFalse(r.fully_protected)

    def test_missing_order_list_id_invalid(self):
        bad = dict(self.REALISTIC); bad.pop('orderListId')
        r = parse_oco_response(bad)
        self.assertFalse(r.valid)

    def test_missing_one_child_invalid(self):
        bad = dict(self.REALISTIC)
        bad['orderReports'] = [self.REALISTIC['orderReports'][0]]
        bad['orders'] = [self.REALISTIC['orders'][0]]
        r = parse_oco_response(bad)
        self.assertFalse(r.valid)
        self.assertFalse(r.fully_protected)

    def test_same_id_for_both_children_invalid(self):
        """أهم اختبار — يمنع رجوع خلل خلط orderListId مع معرّفات الأبناء."""
        bad = {
            'orderListId': 9001, 'listClientOrderId': 'oco1', 'symbol': 'BTCUSDT',
            'orderReports': [
                {'symbol': 'BTCUSDT', 'orderId': 9001, 'clientOrderId': 'CID-S',
                 'type': 'STOP_LOSS_LIMIT', 'status': 'NEW'},
                {'symbol': 'BTCUSDT', 'orderId': 9001, 'clientOrderId': 'CID-T',
                 'type': 'LIMIT_MAKER', 'status': 'NEW'}]}
        r = parse_oco_response(bad)
        self.assertFalse(r.valid)
        self.assertIn('خلط', r.error)

    def test_symbol_mismatch_invalid(self):
        r = parse_oco_response(self.REALISTIC, expected_symbol='ETHUSDT')
        self.assertFalse(r.valid)

    def test_null_order_id_invalid(self):
        bad = dict(self.REALISTIC)
        bad['orderReports'] = [
            {**self.REALISTIC['orderReports'][0], 'orderId': None},
            self.REALISTIC['orderReports'][1]]
        r = parse_oco_response(bad)
        self.assertFalse(r.valid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
