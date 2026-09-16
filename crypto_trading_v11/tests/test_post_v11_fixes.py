"""
اختبارات انحدار لإصلاحات ما بعد V11 — كل اختبار هنا **يفشل** على
الشجرة قبل الإصلاح، لا يمرّ بالصدفة.

الدرس المطبَّق من CRITICAL_BTC_WIRING_REGRESSION_REPORT.md: الاختبار
المعزول لا يُثبت أن النظام الحقيقي يعمل. اختبارات التشغيل هنا تمرّ عبر
`LiveTrader.tick()` الحقيقية، واختبارات سطر الأوامر تمرّ عبر
`main()` الحقيقية بـ argv مُزيَّف — لا استدعاءً مباشراً بمعزل.
"""
import io
import os
import sys
import contextlib
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_trader as LT
from tests.fixtures import make_fixture
from test_multi_asset_integration import _make_trader, MultiSymbolFixtureCache


class Test01_SymbolSurvivesRestart(unittest.TestCase):
    """
    البق: `self.symbol` يُضبَط من `envcfg.symbol` في الـ constructor
    ولا يُستعاد من المركز المفتوح، فإعادة التشغيل بمركز على رمز فائز
    سابق تجعل كل `tick()` تُدير الرمز الافتراضي — بما فيها
    `_settle_paper_trigger()` التي تستعلم `WHERE symbol=?`. عملياً:
    وقف وهدف المركز المفتوح لا يُسوَّيان أبداً بعد إعادة التشغيل.

    الاختبار الموجود في الحزمة يفتح المركز على `BTCUSDT` وهو
    `envcfg.symbol` نفسه، فيمرّ بشكل فارغ ولا يلمس الحالة الخطرة.
    """

    def _trader_with_open_position_on(self, held: str):
        symbols = ['BTCUSDT', 'ETHUSDT']
        d = {s: make_fixture(600, '4h', seed=500 + i, symbol=s, kind='up')
            for i, s in enumerate(symbols)}
        t = _make_trader(scan_symbols=symbols, symbol='BTCUSDT')
        t.cache = MultiSymbolFixtureCache(d)
        t.paper_market.set_price('BTCUSDT', 50000.0, spread_bps=4.0)
        t.paper_market.set_price('ETHUSDT', 3000.0, spread_bps=4.0)
        t.db.open_position(id=1, symbol=held, qty=0.5, entry_price=3000.0,
                           opened_ts=1, status='OPEN')
        return t

    def test_restart_adopts_symbol_of_open_position(self):
        t = self._trader_with_open_position_on('ETHUSDT')
        self.assertEqual(t.symbol, 'BTCUSDT', 'الحالة الابتدائية غير متوقَّعة')
        t.tick(verbose=False)
        self.assertEqual(t.symbol, 'ETHUSDT',
                         'الدورة تُدير رمزاً غير رمز المركز المفتوح — '
                         'وقف/هدف المركز لن يُسوَّيا أبداً')

    def test_adoption_is_recorded_not_silent(self):
        t = self._trader_with_open_position_on('ETHUSDT')
        t.tick(verbose=False)
        ev = t.db.query("SELECT * FROM system_events WHERE kind='SYMBOL_ADOPTED'")
        self.assertTrue(ev, 'تبنّي الرمز حدث بصمت — لا أثر في السجل')

    def test_same_symbol_needs_no_adoption_event(self):
        t = self._trader_with_open_position_on('BTCUSDT')
        t.tick(verbose=False)
        self.assertEqual(t.symbol, 'BTCUSDT')
        ev = t.db.query("SELECT * FROM system_events WHERE kind='SYMBOL_ADOPTED'")
        self.assertEqual(ev, [], 'حدث تبنٍّ رغم أن الرمز لم يتغيَّر')

    def test_multi_symbol_open_positions_stop_the_cycle(self):
        """
        دورة واحدة لا تستطيع حماية أكثر من رمز — المرور بصمت يترك
        مركزاً بلا حراسة، وهو أخطر من التوقف الصريح.
        """
        t = self._trader_with_open_position_on('ETHUSDT')
        t.db.open_position(id=2, symbol='BTCUSDT', qty=0.01, entry_price=50000.0,
                           opened_ts=2, status='OPEN')
        r = t.tick(verbose=False)
        self.assertEqual(r['status'], 'MULTI_SYMBOL_POSITIONS')
        self.assertEqual(r['symbols'], ['BTCUSDT', 'ETHUSDT'])

    def test_single_symbol_mode_is_untouched(self):
        """scan_symbols=None ⇒ لا تبنٍّ ولا سلوك جديد إطلاقاً."""
        t = _make_trader(scan_symbols=None, symbol='BTCUSDT')
        d = {'BTCUSDT': make_fixture(600, '4h', seed=500, symbol='BTCUSDT',
                                     kind='up')}
        t.cache = MultiSymbolFixtureCache(d)
        t.paper_market.set_price('BTCUSDT', 50000.0, spread_bps=4.0)
        t.db.open_position(id=1, symbol='ETHUSDT', qty=0.5, entry_price=3000.0,
                           opened_ts=1, status='OPEN')
        t.tick(verbose=False)
        self.assertEqual(t.symbol, 'BTCUSDT',
                         'وضع الرمز الواحد تغيَّر سلوكه — لا يجوز')


class Test02_ScanSymbolsReachableFromCLI(unittest.TestCase):
    """
    البق: محرك اختيار أفضل فرصة (78-98) كان **بلا أي مسار تشغيل** —
    `scan_symbols` وسيط constructor فقط، و `main()` تبني LiveTrader
    بلا تمريره. الميزة كانت حيّة في الاختبارات وميتة في الإنتاج.
    """

    def _parse(self, argv):
        """يشغّل main() الحقيقية ويلتقط ما بُني به LiveTrader."""
        captured = {}
        real = LT.LiveTrader

        class Spy(real):
            def __init__(self, envcfg, cfg, max_notional, scan_symbols=None):
                captured['scan_symbols'] = scan_symbols
                raise RuntimeError('stop-after-construction')

        old_argv = sys.argv
        sys.argv = argv
        LT.LiveTrader = Spy
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = LT.main()
        finally:
            LT.LiveTrader = real
            sys.argv = old_argv
        return rc, captured

    def setUp(self):
        from test_paper_smoke import clean_env
        clean_env()
        os.environ.update({'TRADING_ENVIRONMENT': 'paper', 'CAPITAL': '10000'})

    def test_flag_reaches_the_trader(self):
        _, cap = self._parse(['live_trader.py', 'paper', '--once',
                              '--scan-symbols', 'BTCUSDT,ETHUSDT'])
        self.assertEqual(cap.get('scan_symbols'), ['BTCUSDT', 'ETHUSDT'],
                         'العلَم لم يصل إلى LiveTrader — الميزة غير قابلة للتشغيل')

    def test_flag_normalizes_case_and_spaces(self):
        _, cap = self._parse(['live_trader.py', 'paper', '--once',
                              '--scan-symbols', ' btcusdt , solusdt '])
        self.assertEqual(cap.get('scan_symbols'), ['BTCUSDT', 'SOLUSDT'])

    def test_absence_keeps_single_symbol_behavior(self):
        _, cap = self._parse(['live_trader.py', 'paper', '--once'])
        self.assertIsNone(cap.get('scan_symbols'),
                          'الغياب غيَّر السلوك الافتراضي — لا يجوز')

    def test_unsupported_symbol_is_rejected_before_construction(self):
        rc, cap = self._parse(['live_trader.py', 'paper', '--once',
                               '--scan-symbols', 'BTCUSDT,DOGEUSDT'])
        self.assertEqual(rc, 1)
        self.assertNotIn('scan_symbols', cap,
                         'بُني المتداول رغم رمز غير مدعوم')

    def test_env_var_is_an_accepted_source(self):
        os.environ['SCAN_SYMBOLS'] = 'BTCUSDT,BNBUSDT'
        try:
            _, cap = self._parse(['live_trader.py', 'paper', '--once'])
        finally:
            os.environ.pop('SCAN_SYMBOLS', None)
        self.assertEqual(cap.get('scan_symbols'), ['BTCUSDT', 'BNBUSDT'])


class Test03_PortfolioExposureRunsInProduction(unittest.TestCase):
    """
    البق: البند 88 (فحص التعرّض/الارتباط على المرشَّح الفائز) مُنفَّذ
    ومُختبَر في الوحدة، لكن `live_trader.py` لم يكن يُمرِّر `portfolio`
    ولا تقدير حجم — فيُتخطّى الفحص بصمت في المسار الحيّ الوحيد.
    """

    def _trader(self):
        symbols = ['BTCUSDT', 'ETHUSDT']
        d = {s: make_fixture(600, '4h', seed=500 + i, symbol=s, kind='up')
            for i, s in enumerate(symbols)}
        t = _make_trader(scan_symbols=symbols, symbol='BTCUSDT')
        t.cache = MultiSymbolFixtureCache(d)
        for s, px in (('BTCUSDT', 50000.0), ('ETHUSDT', 3000.0)):
            t.paper_market.set_price(s, px, spread_bps=4.0)
        return t

    def test_portfolio_and_size_estimate_are_passed_through(self):
        seen = {}
        import src.selection.opportunity as OPP
        orig = OPP.scan_and_rank

        def spy(*a, **kw):
            seen.update(kw)
            return orig(*a, **kw)

        OPP.scan_and_rank = spy
        try:
            self._trader().tick(verbose=False)
        finally:
            OPP.scan_and_rank = orig
        self.assertIsNotNone(seen.get('portfolio'),
                             'فحص تعرّض المحفظة لا يعمل في الإنتاج')
        self.assertTrue(callable(seen.get('notional_fn')),
                        'لا تقدير حجم حقيقي — الفحص سيُتخطّى بصمت')
        self.assertGreater(seen.get('equity', 0), 0)

    def test_open_position_flag_reflects_reality_not_a_constant(self):
        import src.selection.opportunity as OPP
        seen = {}
        orig = OPP.scan_and_rank

        def spy(*a, **kw):
            seen.update(kw)
            return orig(*a, **kw)

        OPP.scan_and_rank = spy
        try:
            t = self._trader()
            t.tick(verbose=False)
        finally:
            OPP.scan_and_rank = orig
        self.assertIn('already_has_open_position', seen)
        self.assertFalse(seen['already_has_open_position'])

    def test_exposure_cap_can_actually_reject_the_winner(self):
        """
        الإثبات النهائي: سقف تعرّض منخفض جداً يجب أن يمنع الصفقة عبر
        المسار الحقيقي — لا أن يمر لأن الفحص مُتخطّى.
        """
        from src.risk.portfolio import PortfolioConfig
        t = self._trader()
        t.cfg.portfolio = PortfolioConfig(max_total_exposure_pct=0.0001,
                                          max_per_asset_pct=0.0001)
        r = t.tick(verbose=False)
        row = t.db.query("SELECT * FROM opportunity_scans ORDER BY id DESC LIMIT 1")
        if row and row[0].get('decision') == 'BUY':
            self.fail('سقف تعرّض ~0% ولم يمنع شيئاً — الفحص غير فعّال')
        self.assertNotEqual(r.get('status'), 'EXECUTED')


if __name__ == '__main__':
    unittest.main(verbosity=2)
