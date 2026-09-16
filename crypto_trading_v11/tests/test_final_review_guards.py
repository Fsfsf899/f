"""
حُرَّاس المراجعة النهائية.

يوثّقان حالتين قائمتين في الشجرة ويمنعان تغيّرهما بصمت.
"""
import io
import os
import sys
import contextlib
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_trader as LT


class Test01_LiveLimitsNotWired(unittest.TestCase):
    """
    أخطر ما وجدته المراجعة النهائية: `LiveOrderManager` وحدوده الصارمة
    (`LiveLimits`) مبنيّة ومُختبَرة، و**غير موصولة بأي مسار إنتاج**.
    `LiveTrader` يبني `OrderManager` العادي في كل الأوضاع بما فيها
    `live`. الفارق: مخاطرة 1.00% بدل 0.25%، وسقف 25% من الحقوق بدل $25
    مطلقاً، وتعرّض 60% بدل 10%.

    Mainnet مقفول ببوابة الجاهزية فالعيب كامن لا نشط — لكن الاعتماد على
    قفل واحد لعيب بهذا الأثر لا يكفي.
    """

    def test_live_order_manager_is_still_not_wired(self):
        """
        حارس توثيقي: يسقط عند وصل `LiveOrderManager` — وهو **نجاح** لا
        فشل، وإشارة إلى أن الحاجز الصريح في `main()` صار زائداً ويمكن
        رفعه مع هذا الاختبار.
        """
        with open(LT.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('LiveOrderManager(', src,
                         'وُصِل LiveOrderManager — احذف الحاجز وهذا الاختبار')

    def test_live_mode_refuses_without_explicit_acknowledgement(self):
        os.environ.pop('I_ACCEPT_LIVE_LIMITS_NOT_WIRED', None)
        with open(LT.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('LIVE_LIMITS_NOT_WIRED', src,
                      'الحاجز الصريح اختفى من مسار live')
        self.assertIn('I_ACCEPT_LIVE_LIMITS_NOT_WIRED', src)

    def test_live_limits_are_materially_stricter(self):
        """لو تقاربت الحدود لصار الحاجز بلا معنى — نُثبت أنه ذو أثر."""
        from src.live.live_config import LiveLimits
        from src.core.config import Config
        lim, cfg = LiveLimits(), Config()
        self.assertLess(lim.max_risk_per_trade_pct,
                        cfg.risk.risk_per_trade_pct,
                        'حدود Live لم تعد أضيق — راجع المبرّر')
        self.assertLess(lim.max_total_exposure_pct, 60.0)


class Test02_FailureCountersAreProcessLocal(unittest.TestCase):
    """
    ملاحظة موثَّقة لا بق: عدّادا `api_failures`/`order_failures` في
    `HealthMonitor` في الذاكرة فقط، فيصفران عند إعادة التشغيل. الحماية
    الفعلية محفوظة (مفتاح الإيقاف في `kv`، والأحداث في `risk_events`،
    وسقف إعادات التشغيل في `watchdog`)، فهذه طبقة إضافية لا أساسية.
    """

    def test_counters_reset_on_new_instance(self):
        import tempfile
        from src.storage.database import Database
        from src.monitoring.health import HealthMonitor
        from src.core.config import Config
        path = os.path.join(tempfile.mkdtemp(), 'h.db')
        db = Database(path)
        h1 = HealthMonitor(db, Config().risk)
        for _ in range(6):
            h1.record_api_failure('x')
        self.assertGreaterEqual(h1.api_failures, 6)
        h2 = HealthMonitor(db, Config().risk)
        self.assertEqual(h2.api_failures, 0,
                         'صار العدّاد محفوظاً — حدّث هذا التوثيق')

    def test_kill_switch_does_persist(self):
        """المهم أن الحماية الأساسية تنجو، وهي تنجو."""
        import tempfile
        from src.storage.database import Database
        from src.monitoring.health import HealthMonitor
        from src.core.config import Config
        path = os.path.join(tempfile.mkdtemp(), 'k.db')
        db = Database(path)
        HealthMonitor(db, Config().risk).engage_kill_switch('اختبار')
        self.assertTrue(HealthMonitor(db, Config().risk).kill_switch_on(),
                        'مفتاح الإيقاف لم ينجُ من إعادة التشغيل')


if __name__ == '__main__':
    unittest.main(verbosity=2)
