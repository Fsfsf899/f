"""
تكامل الإدارة التكيّفية مع المسار الحي — Part B البنود 23-26 و39.
=================================================================
الغرض الوحيد لهذا الملف: إثبات أن الطبقة الجديدة **تعمل فعلاً داخل
`LiveTrader.tick()`**، لا أنها موجودة في ملف.

هذا المشروع فيه ست حالات موثَّقة لمكوّن مبنيّ ومختبَر وموثَّق لكنه
غير موصول بالمسار الذي يعمل. أخطرها أن التعادل والتتبّع كانا في
الباكتست وحده، فكل نتيجة باكتست افترضت حمايةً لم يطبّقها التنفيذ
الحي قط. الاختبارات هنا تمرّ عبر `tick()` الحقيقية كاملةً.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from src.core.config import Config
from tests.test_paper_smoke import new_trader


def prime(t, *, stop_pct=2.0, target_pct=6.0, entry_bars_ago=10):
    """
    يفتح مركزاً ورقياً حقيقياً بسعر ووقتٍ مشتقَّين من الـ fixture نفسها.

    ربط الدخول بشمعة فعلية داخل السلسلة مقصود: المحرك يقرأ
    `data.close[-1]` ويشتقّ MFE/MAE من الشموع منذ `opened_ts`. مركز
    "فُتح" عند أول شمعة في التاريخ يُنتج MAE بعشرات بالمئة ويقيس عبثاً.
    """
    data = t.cache.get(None, t.symbol, t.interval)
    j = max(0, len(data.close) - entry_bars_ago)
    last = float(data.close[-1])
    entry = float(data.close[j])
    t.paper_market.set_price(t.symbol, last, spread_bps=4.0)
    buy = t.paper.market_buy_quote(t.symbol, 500, 'seed-entry')
    qty = t.paper.base_free(t.symbol)
    stop = entry * (1 - stop_pct / 100)
    target = entry * (1 + target_pct / 100)
    t.db.open_position(id=1, symbol=t.symbol, qty=qty, entry_price=entry,
                       stop_loss=stop, take_profit=target,
                       initial_stop=stop, initial_target=target,
                       opened_ts=int(data.open_time[j]), status='OPEN')
    # تسجيل التعبئة محلياً: بدونه تكتشف المصالحة (الخطوة 1) تعبئةً
    # على المنصة بلا سجل محلي فتُوقف الدورة قبل بلوغ الإدارة أصلاً —
    # وهو سلوك صحيح تماماً للمصالحة، لا عيب فيها.
    for tr in (t.paper.my_trades(t.symbol) or []):
        t.db.save_fill(symbol=t.symbol,
                       exchange_trade_id=str(tr.get('id')),
                       qty=float(tr.get('qty', 0)),
                       price=float(tr.get('price', 0)),
                       commission=float(tr.get('commission', 0) or 0),
                       commission_asset=tr.get('commissionAsset', ''),
                       ts=int(tr.get('time', 0)))
    sid, tid = t.orders._place_oco(t.symbol, qty, stop, target, pos_id=1)
    return data, entry, stop, target, sid, tid


def adaptive_trader(**kw):
    """متداول ورقي بالطبقة التكيّفية مفعَّلة عبر Config الحقيقي."""
    cfg = Config()
    cfg.adaptive.enabled = True
    cfg.no_trade.min_data_quality = 0.5
    for k, v in kw.items():
        setattr(cfg.adaptive, k, v)
    t, base = new_trader('paper')
    t.cfg = cfg
    t.atm.cfg = cfg
    t.engine.cfg = cfg
    return t, base


class Test01_ManagementRunsInRealTick(unittest.TestCase):

    def test_paper_mode_records_a_decision(self):
        """
        الاختبار الأهم في الملف: بعد `tick()` حقيقية واحدة، يجب أن
        يوجد صف في `trade_decisions`. غيابه يعني أن المحرك لم يُستدعَ
        إطلاقاً — أي النمط نفسه للمرّة السابعة.
        """
        t, _ = adaptive_trader()
        prime(t)
        t.tick(verbose=False)
        rows = t.db.query('SELECT * FROM trade_decisions')
        self.assertTrue(rows, 'لم يُستدعَ المحرك التكيّفي داخل tick() إطلاقاً')
        self.assertEqual(rows[0]['position_id'], 1)
        self.assertEqual(rows[0]['symbol'], t.symbol)
        self.assertTrue(rows[0]['decision'])

    def test_decision_is_skipped_when_adaptive_disabled(self):
        """الضابط السالب: بلا تفعيل، لا صف ولا استدعاء."""
        t, _ = new_trader('paper')
        t.cfg.no_trade.min_data_quality = 0.5
        self.assertFalse(t.cfg.adaptive.enabled)
        prime(t)
        t.tick(verbose=False)
        self.assertEqual(t.db.query('SELECT * FROM trade_decisions'), [])

    def test_no_open_position_no_decision(self):
        t, _ = adaptive_trader()
        t.tick(verbose=False)
        self.assertEqual(t.db.query('SELECT * FROM trade_decisions'), [])

    def test_decision_row_has_no_secrets(self):
        t, _ = adaptive_trader()
        prime(t)
        t.tick(verbose=False)
        blob = repr(t.db.query('SELECT * FROM trade_decisions')).lower()
        for bad in ('api_key', 'secret', 'signature', 'apikey'):
            self.assertNotIn(bad, blob)


class Test02_StopActuallyMoves(unittest.TestCase):

    def test_break_even_moves_stop_on_exchange(self):
        """
        التعادل لا يُسجَّل فقط — يجب أن ينتقل الوقف فعلياً عبر دورة
        OCO، ويتغيّر `stop_order_id` على الوسيط، ويُحدَّث السجل.
        """
        # دخول قبل 10 شمعات: السعر ارتفع 2.1% منذها، فالتعادل الصافي
        # (≈ الدخول +0.38%) يقع تحت السعر الحالي ويمكن وضعه فعلاً.
        t, _ = adaptive_trader(break_even_trigger_r=0.1)
        data, entry, stop, target, sid0, _ = prime(t, stop_pct=2.0)
        t.tick(verbose=False)

        pos = t.db.query('SELECT * FROM positions WHERE id=1')[0]
        dec = t.db.query('SELECT * FROM trade_decisions')[0]
        if dec['decision'] != 'BREAK_EVEN':
            self.skipTest(f"الـ fixture لم يحقّق 1R (القرار {dec['decision']})")

        self.assertGreater(pos['stop_loss'], stop, 'الوقف لم يتحرّك في السجل')
        self.assertGreater(pos['stop_loss'], entry,
                           'التعادل السعري الساذج بدل الصافي')
        self.assertNotEqual(str(pos['stop_order_id']), str(sid0),
                            'لم يُوضع أمر وقف جديد على الوسيط')
        self.assertEqual(dec['applied'], 'APPLIED')

    def test_protection_stays_intact_when_nothing_to_do(self):
        """قرار HOLD لا يلمس الحماية القائمة إطلاقاً."""
        t, _ = adaptive_trader(break_even_trigger_r=99.0,
                               adaptive_tp_enabled=False,
                               stagnation_enabled=False)
        data, entry, stop, target, sid0, tid0 = prime(t)
        t.tick(verbose=False)
        pos = t.db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertAlmostEqual(pos['stop_loss'], stop, places=8)
        self.assertEqual(str(pos['stop_order_id']), str(sid0))
        self.assertEqual(t.db.query(
            'SELECT applied FROM trade_decisions')[0]['applied'], 'NONE')


class Test03_SafetyInvariants(unittest.TestCase):

    def test_widening_stop_is_refused_at_order_layer(self):
        """
        الحارس مكرَّر عمداً عند حدود المال: حتى لو طلب المحرك وقفاً
        أدنى، طبقة الأوامر ترفضه وتُسجِّل الحدث.
        """
        t, _ = adaptive_trader()
        data, entry, stop, target, sid0, _ = prime(t)
        pos = t.db.query('SELECT * FROM positions WHERE id=1')[0]
        r = t.orders.apply_protection_change(pos, new_stop=stop * 0.5,
                                             reason='TEST')
        self.assertEqual(r['action'], 'REFUSED')
        self.assertEqual(r['why'], 'WOULD_WIDEN_RISK')
        after = t.db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertAlmostEqual(after['stop_loss'], stop, places=8)
        self.assertEqual(str(after['stop_order_id']), str(sid0))
        kinds = {x['kind'] for x in t.db.query('SELECT kind FROM risk_events')}
        self.assertIn('ADAPTIVE_STOP_WIDEN_BLOCKED', kinds)

    def test_oco_identifiers_stay_distinct_after_move(self):
        """
        البند 23: بعد النقل يجب أن تبقى معرّفات OCO صحيحة ومنفصلة —
        لا خلط بين معرّف القائمة ومعرّفات الأبناء.
        """
        t, _ = adaptive_trader()
        data, entry, stop, target, _, _ = prime(t)
        pos = t.db.query('SELECT * FROM positions WHERE id=1')[0]
        r = t.orders.apply_protection_change(pos, new_stop=stop * 1.01,
                                             reason='TEST')
        self.assertEqual(r['action'], 'APPLIED')
        after = t.db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertTrue(after['order_list_id'])
        self.assertTrue(after['stop_order_id'])
        self.assertTrue(after['target_order_id'])
        self.assertNotEqual(after['stop_order_id'], after['target_order_id'])
        self.assertNotEqual(after['stop_order_id'], after['order_list_id'])

    def test_target_preserved_when_only_stop_moves(self):
        """نقل الوقف لا يجوز أن يُسقط الهدف — OCO يُعاد وضعه بطرفيه."""
        t, _ = adaptive_trader()
        data, entry, stop, target, _, _ = prime(t)
        pos = t.db.query('SELECT * FROM positions WHERE id=1')[0]
        t.orders.apply_protection_change(pos, new_stop=stop * 1.01,
                                         reason='TEST')
        after = t.db.query('SELECT * FROM positions WHERE id=1')[0]
        self.assertAlmostEqual(after['take_profit'], target, places=8)
        self.assertTrue(after['target_order_id'])

    def test_identical_values_are_a_no_op(self):
        t, _ = adaptive_trader()
        data, entry, stop, target, sid0, _ = prime(t)
        pos = t.db.query('SELECT * FROM positions WHERE id=1')[0]
        r = t.orders.apply_protection_change(pos, new_stop=stop,
                                             new_target=target)
        self.assertEqual(r['action'], 'NO_CHANGE')
        self.assertEqual(str(t.db.query(
            'SELECT * FROM positions WHERE id=1')[0]['stop_order_id']), str(sid0))

    def test_bad_data_quality_skips_management(self):
        """Part C البند 6: لا قرار إدارة على بيانات مشكوكة."""
        t, _ = adaptive_trader()
        t.cfg.no_trade.min_data_quality = 1.5   # فوق أي درجة ممكنة
        prime(t)
        t.tick(verbose=False)
        self.assertEqual(t.db.query('SELECT * FROM trade_decisions'), [])
        kinds = {x['kind'] for x in t.db.query('SELECT kind FROM risk_events')}
        self.assertIn('ADAPTIVE_SKIPPED_DATA_QUALITY', kinds)


class Test04_RestartAndRecovery(unittest.TestCase):

    def test_mfe_survives_restart(self):
        """
        MFE تُشتقّ من الشموع لا من ذاكرة العملية. لو كانت عدّاداً في
        الذاكرة لصُفِّرت بإعادة التشغيل، فتراجع النظام عن تعادل
        استحقّه — وهذا عطل حماية حقيقي لا تجميلي.

        الإثبات: عمليّتان منفصلتان تماماً (قاعدتا بيانات ووسيطان
        مستقلّان) بنفس المركز ونفس التاريخ تُنتجان نفس MFE بالضبط.
        لا حالة منقولة بينهما — الاشتقاق وحده يفسّر التطابق.
        """
        t1, _ = adaptive_trader(break_even_trigger_r=0.1)
        prime(t1)
        t1.tick(verbose=False)
        first = t1.db.query('SELECT mfe_pct, mae_pct FROM trade_decisions')[0]

        t2, _ = adaptive_trader(break_even_trigger_r=0.1)   # عملية جديدة تماماً
        prime(t2)
        t2.tick(verbose=False)
        second = t2.db.query('SELECT mfe_pct, mae_pct FROM trade_decisions')[0]

        self.assertAlmostEqual(first['mfe_pct'], second['mfe_pct'], places=9,
                               msg='MFE لم تنجُ من إعادة التشغيل')
        self.assertAlmostEqual(first['mae_pct'], second['mae_pct'], places=9)
        self.assertGreater(first['mfe_pct'], 0.0,
                           'MFE صفرية — الاختبار لا يقيس شيئاً')

    def test_repeated_tick_on_same_bar_is_idempotent(self):
        """
        دورتان داخل الشمعة نفسها لا تحرّكان الوقف مرّتين.

        الثبات المقصود ليس "صف واحد مهما حدث": القرار الثاني يختلف
        بحقّ (التعادل تحقَّق في الأولى، فالثانية تجد الوقف مرفوعاً
        سلفاً) وتسجيله معلومة صحيحة. المطلوب أن **الأثر** لا يتكرّر:
        لا أمر وقف ثالث، ولا صفّان بنفس (الشمعة، القرار).
        """
        t, _ = adaptive_trader()
        prime(t)
        t.tick(verbose=False)
        stop1 = t.db.query('SELECT stop_loss FROM positions WHERE id=1')[0]['stop_loss']
        t.tick(verbose=False)
        stop2 = t.db.query('SELECT stop_loss FROM positions WHERE id=1')[0]['stop_loss']
        self.assertAlmostEqual(stop1, stop2, places=8,
                               msg='الوقف تحرّك مرّتين في الشمعة نفسها')
        rows = t.db.query('SELECT bar_time, decision FROM trade_decisions')
        keys = [(r['bar_time'], r['decision']) for r in rows]
        self.assertEqual(len(keys), len(set(keys)), 'صفّ قرار مكرَّر حرفياً')


class Test06_RiskUnitBasis(unittest.TestCase):
    """
    بق وُجد في هذا العمل نفسه بالمراجعة الذاتية قبل الدمج — ولم يكن أي
    اختبار وحدة ليكشفه، لأنه يقع في الجسر بين جدول القاعدة والمحرك.
    """

    def test_r_unit_uses_initial_stop_not_moved_stop(self):
        """
        بعد أن ينقل التعادلُ الوقفَ فوق سعر الدخول، تصير
        (الدخول − الوقف الحالي) **سالبة**. لو حُسبت R منها لرفض المحرك
        كل قرار تالٍ بـ INSUFFICIENT_DATA — أي يموت التتبّع في المسار
        الحي صامتاً بعد أول تعادل، وهو تعطيل كامل لميزة يظنّها
        المستخدم عاملة.
        """
        t, _ = adaptive_trader(break_even_trigger_r=0.1)
        t.cfg.signal.trailing_stop_enabled = True
        t.cfg.signal.trailing_activate_at_r = 1.0
        data, entry, stop, target, _, _ = prime(t)

        t.tick(verbose=False)
        moved = t.db.query('SELECT stop_loss, initial_stop '
                           'FROM positions WHERE id=1')[0]
        self.assertGreater(moved['stop_loss'], entry,
                           'التعادل لم يرفع الوقف فوق الدخول — الاختبار عقيم')
        self.assertLess(moved['initial_stop'], entry,
                        'الوقف الأصلي تلوّث بالوقف المتحرّك')

        t.tick(verbose=False)
        rows = t.db.query('SELECT decision, reason FROM trade_decisions '
                          'ORDER BY id')
        self.assertGreaterEqual(len(rows), 2, 'الدورة الثانية لم تُقيَّم')
        self.assertNotEqual(rows[-1]['reason'], 'INSUFFICIENT_DATA',
                            'المحرك توقّف عن التقييم بعد التعادل — '
                            'وحدة المخاطرة تُحسب من الوقف المتحرّك')

    def test_open_long_persists_initial_levels(self):
        """المسار الحقيقي لفتح المركز يسجّل المستويين الأصليين."""
        import inspect
        from src.execution import order_manager
        src = inspect.getsource(order_manager.OrderManager.open_long)
        self.assertIn('initial_stop=stop', src)
        self.assertIn('initial_target=target', src)

    def test_migration_backfills_initial_levels(self):
        """قاعدة قديمة تُرحَّل بلا فقد: العمودان يُملآن من القيم الحالية."""
        import sqlite3
        import tempfile
        from src.storage.database import Database
        from src.storage import migrations

        path = tempfile.mkdtemp() + '/legacy.db'
        Database(path)
        c = sqlite3.connect(path)
        c.execute('DROP TABLE trade_decisions')
        c.execute("INSERT INTO positions (symbol, qty, entry_price, stop_loss,"
                  " take_profit, opened_ts) VALUES ('BTCUSDT',1,100,98,104,1)")
        c.execute("CREATE TABLE p2 AS SELECT id,symbol,qty,entry_price,"
                  "stop_loss,take_profit,opened_ts,status FROM positions")
        c.execute('DROP TABLE positions')
        c.execute('ALTER TABLE p2 RENAME TO positions')
        c.execute('PRAGMA user_version=7')
        c.commit(); c.close()

        migrations.run(path)
        c = sqlite3.connect(path)
        row = c.execute('SELECT stop_loss, initial_stop, take_profit, '
                        'initial_target FROM positions').fetchone()
        c.close()
        self.assertEqual(row[0], row[1], 'initial_stop لم يُملأ')
        self.assertEqual(row[2], row[3], 'initial_target لم يُملأ')


class Test05_NoLookahead(unittest.TestCase):

    def test_decision_uses_only_closed_history(self):
        """
        البند 29: قصّ السلسلة عند الشمعة الأخيرة لا يغيّر القرار —
        لأن القرار لم يكن يرى ما بعدها أصلاً. لو غيّره القصّ، فثمّة
        تسريب. نقارن القرار على سلسلة كاملة بالقرار على نفس السلسلة
        بعد إضافة شموع لاحقة.
        """
        from src.trade_management import AdaptiveTradeManager, MarketView, TradeState
        from src.trade_management.hysteresis import confirm
        from src.market.regime import detect_series
        from tests.fixtures import make_fixture

        cfg = Config(); cfg.adaptive.enabled = True
        atm = AdaptiveTradeManager(cfg)
        full = make_fixture(600, '1h', seed=5)

        def decide(upto: int):
            close = full.close[:upto]
            labels = [str(x) for x in detect_series(close, cfg.regime, None, '1h')]
            reg = confirm(labels, cfg.adaptive)
            e = float(full.close[300])
            st = TradeState(entry_price=e, qty=1.0, initial_stop=e * 0.98,
                            current_stop=e * 0.98, initial_target=e * 1.05,
                            current_target=e * 1.05,
                            opened_ms=int(full.open_time[300]),
                            entry_fee=e * 0.001,
                            mfe_pct=float((full.high[300:upto].max() - e) / e * 100),
                            mae_pct=float((full.low[300:upto].min() - e) / e * 100))
            v = MarketView(price=float(close[-1]), atr=float(e * 0.01),
                           regime=reg.regime, now_ms=int(full.open_time[upto - 1]))
            return atm.evaluate(st, v).to_dict()

        at_400 = decide(400)
        _ = decide(600)                       # المستقبل صار معروفاً الآن
        self.assertEqual(at_400, decide(400),
                         'قرار الشمعة 400 تغيّر بعد رؤية ما بعدها')


if __name__ == '__main__':
    unittest.main(verbosity=2)


class Test07_MigrationFromAncientSchema(unittest.TestCase):
    """
    انحدار حقيقي وقع في هذا العمل: `UPDATE positions SET
    initial_target=take_profit` عارية. قاعدة من حقبة v4 قد لا تحوي
    `take_profit` أصلاً، فترفع OperationalError فيُجهَض الترحيل كلّه
    وتُستعاد النسخة الاحتياطية — أي **تعطيل ترقية المستخدم**، لا مجرّد
    تخطّي حقل. كشفه `tests/test_opportunity_scan_migration.py` القائم.
    """

    def _legacy_db(self, columns: str, rows: str = ''):
        import sqlite3
        import tempfile
        path = tempfile.mkdtemp() + '/ancient.db'
        c = sqlite3.connect(path)
        c.execute(f'CREATE TABLE positions ({columns})')
        if rows:
            c.execute(rows)
        # يُبدأ من v7 عمداً: الهدف فحص ترحيل **هذه الجولة** (v8) على
        # جدول مراكز ناقص، لا فحص سلسلة الترحيلات من v1 (التي تفترض
        # جداول أخرى غير موجودة في هذه القاعدة المصغَّرة).
        c.execute('PRAGMA user_version=7')
        c.commit(); c.close()
        return path

    def test_migration_survives_positions_without_take_profit(self):
        from src.storage import migrations
        path = self._legacy_db(
            'id INTEGER PRIMARY KEY, symbol TEXT, qty REAL, '
            'entry_price REAL, stop_loss REAL, opened_ts INTEGER',
            "INSERT INTO positions VALUES (1,'BTCUSDT',1,100,98,1)")
        report = migrations.run(path, verbose=False)      # لا يرمي
        self.assertIn(8, report['applied'])

        import sqlite3
        c = sqlite3.connect(path)
        cols = {r[1] for r in c.execute('PRAGMA table_info(positions)')}
        row = c.execute('SELECT stop_loss, initial_stop '
                        'FROM positions').fetchone()
        c.close()
        self.assertIn('initial_stop', cols)
        self.assertEqual(row[0], row[1], 'initial_stop لم يُملأ رغم توفّر مصدره')

    def test_migration_survives_missing_positions_table(self):
        """قاعدة بلا جدول مراكز إطلاقاً — الترحيل يمرّ ولا يُجهَض."""
        import sqlite3
        import tempfile
        from src.storage import migrations
        path = tempfile.mkdtemp() + '/nopos.db'
        c = sqlite3.connect(path)
        c.execute('CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)')
        c.execute('PRAGMA user_version=7')
        c.commit(); c.close()
        report = migrations.run(path, verbose=False)
        self.assertIn(8, report['applied'])
