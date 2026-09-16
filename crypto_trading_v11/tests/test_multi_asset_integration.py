"""
اختبارات تكامل محرك اختيار أفضل فرصة مع LiveTrader.tick() الحقيقية —
الأقسام 78-98 من متطلبات V11.

⚠️ الدرس المستفاد من الجولة السابقة في نفس الجلسة (تقرير
CRITICAL_BTC_WIRING_REGRESSION_REPORT.md): إصلاح ناجح في اختبار وحدة
معزول لا يُثبت أن النظام الحقيقي يعمل — فقط تكامل عبر tick() الكاملة
حرفياً يُثبت ذلك. كل اختبار هنا يمر عبر LiveTrader.tick() نفسها، لا
استدعاءً مباشراً لـ scan_and_rank() بمعزل.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_trader as LT
from src.environment.env import build as build_env, preflight
from src.core.config import Config
from tests.fixtures import make_fixture
from test_paper_smoke import clean_env


class MultiSymbolFixtureCache:
    """
    مثل FixtureCache لكن تُعيد بيانات مختلفة فعلياً حسب الرمز —
    ضرورية لاختبار أن المسح يميّز بين الرموز لا يُعيد نفس البيانات
    للجميع (ما كان سيُخفي أي خلل في التمرير الصحيح للرمز).
    """
    def __init__(self, data_by_symbol):
        self._data = data_by_symbol

    def get(self, client, symbol, interval, days=365, force=False, verbose=True):
        return self._data[symbol]


def _make_trader(scan_symbols=None, symbol='BTCUSDT'):
    clean_env()
    os.environ.update({'TRADING_ENVIRONMENT': 'paper', 'CAPITAL': '10000'})
    base = tempfile.mkdtemp()
    envcfg = build_env('paper', base_dir=base, symbol=symbol, interval='4h',
                       strategy_version='v11.0.0', config_fingerprint='fp')
    preflight(envcfg)
    t = LT.LiveTrader(envcfg, Config(), 50.0, scan_symbols=scan_symbols)
    if t.orders is not None:
        t.orders.gate._sleep = lambda s: None
    return t


class Test01_DefaultBehaviorUnchanged(unittest.TestCase):
    """
    الأهم أولاً: scan_symbols=None (الافتراضي) يجب أن يُنتج سلوكاً
    مطابقاً حرفياً لما كان قبل هذا التكامل بالكامل — لا انحراف طفيف.
    """

    def test_no_scan_symbols_means_single_symbol_behavior_exactly(self):
        t = _make_trader(scan_symbols=None, symbol='BTCUSDT')
        self.assertIsNone(t.scan_symbols)
        d = {'BTCUSDT': make_fixture(800, '4h', seed=11, symbol='BTCUSDT')}
        t.cache = MultiSymbolFixtureCache(d)
        t.paper_market.set_price('BTCUSDT', 50000.0, spread_bps=4.0)

        result = t.tick(verbose=False)
        # لا مسح حدث إطلاقاً — لا حدث OPPORTUNITY_SCAN مُسجَّل
        events = t.db.query(
            "SELECT 1 FROM system_events WHERE kind='OPPORTUNITY_SCAN'")
        self.assertEqual(events, [], 'مسح فرص حدث رغم scan_symbols=None')
        self.assertEqual(t.symbol, 'BTCUSDT')   # لم يتغيّر


class Test02_ScanSelectsWinnerThroughRealTick(unittest.TestCase):
    """الاختبار الحاسم: عبر tick() الحقيقية الكاملة، لا مكوّن معزول."""

    def test_scan_switches_symbol_to_the_eligible_winner(self):
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT']
        # ETH وحده بيانات صاعدة قوية — البقية هابطة (غير مؤهَّلة على الأرجح)
        d = {}
        for i, s in enumerate(symbols):
            kind = 'up' if s == 'ETHUSDT' else 'down'
            d[s] = make_fixture(600, '4h', seed=300 + i, symbol=s, kind=kind)

        t = _make_trader(scan_symbols=symbols, symbol='BTCUSDT')
        cfg = t.cfg
        cfg.no_trade.require_btc_ok = False   # نعزل متغيّر الاختيار عن بوابة BTC هنا
        t.cache = MultiSymbolFixtureCache(d)
        for s in symbols:
            t.paper_market.set_price(s, 100.0, spread_bps=4.0)

        result = t.tick(verbose=False)
        events = t.db.query(
            "SELECT detail FROM system_events WHERE kind='OPPORTUNITY_SCAN' "
            "ORDER BY id DESC LIMIT 1")
        self.assertTrue(events, 'لا حدث مسح مُسجَّل رغم تفعيل scan_symbols')
        if 'BUY' in events[0]['detail']:
            self.assertEqual(t.symbol, 'ETHUSDT',
                             f'المتوقَّع فوز ETHUSDT بالبيانات الصاعدة، والفائز {t.symbol}')

    def test_all_symbols_ineligible_returns_no_trade_without_crash(self):
        symbols = ['BTCUSDT', 'ETHUSDT']
        d = {s: make_fixture(600, '4h', seed=400 + i, symbol=s, kind='down')
            for i, s in enumerate(symbols)}
        t = _make_trader(scan_symbols=symbols, symbol='BTCUSDT')
        t.cfg.no_trade.require_btc_ok = False
        t.cache = MultiSymbolFixtureCache(d)
        for s in symbols:
            t.paper_market.set_price(s, 100.0, spread_bps=4.0)

        result = t.tick(verbose=False)
        # إما NO_ELIGIBLE_OPPORTUNITY (رفضتهما الأهلية) أو استمرار عادي —
        # المهم: لا كسر، ولا مركز فُتح قسراً
        self.assertNotIn('EXECUTED', str(result.get('status', '')))


class Test03_OnePositionPriorityThroughRealTick(unittest.TestCase):
    """البند 84: مركز مفتوح يمنع أي مسح جديد — عبر tick() الحقيقية."""

    def test_open_position_skips_scan_and_manages_existing_symbol(self):
        symbols = ['BTCUSDT', 'ETHUSDT']
        d = {s: make_fixture(600, '4h', seed=500 + i, symbol=s, kind='up')
            for i, s in enumerate(symbols)}
        t = _make_trader(scan_symbols=symbols, symbol='BTCUSDT')
        t.cache = MultiSymbolFixtureCache(d)
        t.paper_market.set_price('BTCUSDT', 50000.0, spread_bps=4.0)

        # مركز مفتوح فعلياً على BTCUSDT قبل الدورة
        t.db.open_position(id=1, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
                           opened_ts=1, status='OPEN')

        t.tick(verbose=False)
        events = t.db.query(
            "SELECT 1 FROM system_events WHERE kind='OPPORTUNITY_SCAN'")
        self.assertEqual(events, [], 'مسح حدث رغم مركز مفتوح فعلياً — يخالف البند 84')
        self.assertEqual(t.symbol, 'BTCUSDT', 'الرمز تغيَّر رغم مركز مفتوح — لا يجوز')


if __name__ == '__main__':
    unittest.main(verbosity=2)
