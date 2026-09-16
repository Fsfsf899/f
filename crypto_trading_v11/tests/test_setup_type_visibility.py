"""
رصد نموذج الدخول — ثغرة وجدتها المراجعة النهائية.

`Signal` كان بلا حقل `setup_type` إطلاقاً، فالمسار الحيّ لا يستطيع
الإجابة عن «أي نموذج أنتج هذه الصفقة؟». والأسوأ أن أي قياس يقرأ
`getattr(sig, 'setup_type', 'baseline')` كان يُرجع `baseline` لكل شيء
**بصمت** — فتبدو النماذج الأخرى معطَّلة وهي تعمل.

قياس فعلي قبل الإصلاح: 110 إشارة ظهرت كلها BASELINE، بينما 46 منها
كانت BREAKOUT أو PULLBACK.
"""
import os
import sys
import tempfile
import unittest
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.signals.engine import Signal
from src.research.router_adapter import RouterAsSignalEngine
from src.storage.database import Database
from src.core.config import Config
from tests.fixtures import make_fixture


def _cfg(bo=False, pb=False):
    c = Config()
    c.signal.min_score = 1.0
    c.no_trade.min_data_quality = 0.5
    c.no_trade.require_btc_ok = False
    c.breakout.enabled = bo
    c.pullback.enabled = pb
    return c


class Test01_SignalCarriesSetupType(unittest.TestCase):

    def test_field_exists(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(Signal)}
        self.assertIn('setup_type', names,
                      'Signal بلا setup_type — لا يمكن رصد النموذج')

    def test_default_is_baseline(self):
        self.assertEqual(Signal(0, 0, 'X', '1h', 'WAIT', 'v').setup_type,
                         'BASELINE')

    def test_to_dict_includes_it(self):
        d = Signal(0, 0, 'X', '1h', 'WAIT', 'v').to_dict()
        self.assertIn('setup_type', d)


class Test02_RouterPropagatesRealModel(unittest.TestCase):
    """الإثبات على المحرك: النماذج تظهر بأسمائها لا كلها baseline."""

    @classmethod
    def setUpClass(cls):
        cls.d = make_fixture(2000, '1h', seed=42, kind='mixed')

    def _tally(self, **flags):
        eng = RouterAsSignalEngine(_cfg(**flags))
        k = collections.Counter()
        for i in range(500, 2000):
            s = eng.evaluate(self.d, i, data_quality=0.95)
            if s.decision == 'BUY':
                k[s.setup_type] += 1
        return k

    def test_breakout_is_visible_when_enabled(self):
        k = self._tally(bo=True)
        self.assertGreater(k.get('BREAKOUT', 0), 0,
                           'BREAKOUT لا يظهر رغم تفعيله — الثغرة عادت')

    def test_pullback_is_visible_when_enabled(self):
        k = self._tally(pb=True)
        self.assertGreater(k.get('PULLBACK', 0), 0,
                           'PULLBACK لا يظهر رغم تفعيله — الثغرة عادت')

    def test_default_is_baseline_only(self):
        k = self._tally()
        self.assertEqual(set(k), {'BASELINE'},
                         f'نموذج غير متوقَّع بالإعداد الافتراضي: {dict(k)}')

    def test_enabling_models_changes_the_mix(self):
        """
        الحارس الجوهري: لو تطابق التوزيعان لعاد الوهم القديم — «تفعيل
        النماذج لا يُغيِّر شيئاً».
        """
        self.assertNotEqual(dict(self._tally()), dict(self._tally(bo=True, pb=True)),
                            'تفعيل النماذج لم يُغيِّر التوزيع — راجع الموجِّه')


class Test03_Persistence(unittest.TestCase):
    """بلا تخزين يستحيل لاحقاً تقييم النماذج من تشغيل حقيقي."""

    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), 's.db'))

    def test_setup_type_is_stored_and_read_back(self):
        sig = Signal(0, 1_700_000_000_000, 'BTCUSDT', '4h', 'BUY', 'v11.0.0')
        sig.setup_type = 'PULLBACK'
        self.db.save_signal(sig.to_dict())
        row = self.db.query('SELECT setup_type FROM signals ORDER BY id DESC '
                            'LIMIT 1')
        self.assertEqual(row[0]['setup_type'], 'PULLBACK')

    def test_column_exists_in_schema(self):
        cols = {r['name'] for r in self.db.query('PRAGMA table_info(signals)')}
        self.assertIn('setup_type', cols)

    def test_migration_adds_the_column(self):
        """قاعدة قديمة تُرحَّل بلا فقدان بيانات."""
        import sqlite3
        from src.storage import migrations
        path = os.path.join(tempfile.mkdtemp(), 'old.db')
        Database(path)
        c = sqlite3.connect(path)
        c.execute('ALTER TABLE signals DROP COLUMN setup_type')
        c.execute('PRAGMA user_version=6')
        c.commit()
        c.close()
        rep = migrations.run(path, verbose=False)
        self.assertIn(7, rep['applied'])
        cols = {r['name'] for r in Database(path).query(
            'PRAGMA table_info(signals)')}
        self.assertIn('setup_type', cols)


if __name__ == '__main__':
    unittest.main(verbosity=2)
