"""
اختبارات إصلاحات دورة التدقيق هذه — كل واحد وُلد من تشغيل فعلي كشف
عطلاً حقيقياً، لا من افتراض نظري.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.database import Database
from src.monitoring.health import HealthMonitor
from src.monitoring.gates import evaluate, GateCriteria


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


# ══ 1. NameError في LiveTrader.__init__ ══
class Test01_StartupNameError(unittest.TestCase):
    """
    اكتُشف بتشغيل حقيقي: python3 live_trader.py monitor كان يرمي
    NameError في كل استدعاء — self.mode/self.symbol/self.interval كانت
    مكتوبة بلا self. لم يلتقطه أي اختبار سابق لأن لا اختبار بنى
    LiveTrader عبر المسار الحقيقي (main() → LiveTrader(...)) من قبل.
    """

    def test_source_uses_self_attributes_not_bare_locals(self):
        import inspect
        import live_trader
        src = inspect.getsource(live_trader.LiveTrader.__init__)
        self.assertIn("self.mode} {self.symbol} {self.interval}", src)

    def test_livetrader_constructs_without_crash(self):
        """هذا الاختبار بالذات كان سيفشل بـ NameError قبل الإصلاح."""
        import tempfile as _t
        from src.environment.env import build as build_env
        from src.core.config import Config
        import live_trader as LT

        base = _t.mkdtemp()
        for k in list(os.environ):
            if k.startswith(('LIVE_', 'TRADING_ENVIRONMENT', 'MAINNET_',
                             'TESTNET_', 'ALLOW_MAINNET', 'I_HAVE')):
                os.environ.pop(k, None)
        envcfg = build_env('monitor', base_dir=base, symbol='BTCUSDT',
                           interval='4h', strategy_version='v9.0.0',
                           config_fingerprint='fp')
        t = LT.LiveTrader(envcfg, Config(), 50.0)
        self.assertEqual(t.mode, 'monitor')
        rows = t.db.query("SELECT detail FROM system_events WHERE kind='STARTUP'")
        self.assertEqual(len(rows), 1)
        self.assertIn('monitor', rows[0]['detail'])
        self.assertIn('BTCUSDT', rows[0]['detail'])
        self.assertIn('4h', rows[0]['detail'])


# ══ 2. تمييز تعذّر المصالحة عن الاختلاف الحقيقي ══
class Test02_ReconciliationUnavailableVsMismatch(unittest.TestCase):
    """
    اكتُشف بتشغيل paper --once فعلياً: انقطاع شبكة عابر أثناء المصالحة
    كان يُسجَّل RECONCILIATION_MISMATCH/CRITICAL — نفس الحدث الذي تحسبه
    بوابة الترقية (no_reconciliation_mismatch). تشغيل طويل حقيقي فيه
    انقطاع شبكة واحد كان سيُسقِط البوابة ظلماً رغم عدم وجود أي اختلاف
    فعلي في المراكز.
    """

    def test_unavailable_does_not_log_as_mismatch(self):
        db = tmpdb()
        h = HealthMonitor(db)
        h.set_reconciliation(False, 'تعذّر جلب السعر: HTTP 403', unavailable=True)
        kinds = [r['kind'] for r in db.query('SELECT kind FROM risk_events')]
        self.assertIn('RECONCILIATION_UNAVAILABLE', kinds)
        self.assertNotIn('RECONCILIATION_MISMATCH', kinds)

    def test_genuine_mismatch_still_logs_as_mismatch(self):
        """السلوك القديم يبقى كما هو للاختلاف الحقيقي — لا رجعة هنا."""
        db = tmpdb()
        h = HealthMonitor(db)
        h.set_reconciliation(False, "[{'kind': 'QTY_DRIFT'}]")
        kinds = [r['kind'] for r in db.query('SELECT kind FROM risk_events')]
        self.assertIn('RECONCILIATION_MISMATCH', kinds)

    def test_unavailable_severity_is_warning_not_critical(self):
        db = tmpdb()
        HealthMonitor(db).set_reconciliation(False, 'x', unavailable=True)
        row = db.query('SELECT severity FROM risk_events')[0]
        self.assertEqual(row['severity'], 'WARNING')

    def test_gate_not_penalized_by_transient_unavailability(self):
        """
        هذا الاختبار الحاسم: نفس السيناريو الذي كسر البوابة قبل
        الإصلاح — انقطاعات شبكة متكررة أثناء تشغيل طويل — يجب ألا
        يُسقِط بوابة الترقية بعد الإصلاح.
        """
        db = tmpdb()
        h = HealthMonitor(db)
        for _ in range(5):
            h.set_reconciliation(False, 'تعذّر جلب السعر', unavailable=True)
        h.set_reconciliation(True)   # ثم عادت الشبكة
        g = evaluate(db, 'paper', GateCriteria(max_reconciliation_mismatches=0),
                    tests_passed=True)
        self.assertNotIn('no_reconciliation_mismatch', g.failures)

    def test_backward_compatible_default_unaffected(self):
        """استدعاء بلا unavailable= يبقى بالسلوك الأصلي — كل الاستدعاءات
        القديمة في بقية الاختبارات يجب ألا تتأثر."""
        db = tmpdb()
        h = HealthMonitor(db)
        h.set_reconciliation(False, 'اختلاف قديم الصياغة')
        kinds = [r['kind'] for r in db.query('SELECT kind FROM risk_events')]
        self.assertIn('RECONCILIATION_MISMATCH', kinds)


# ══ 3. أوامر CLI لا تدّعي نجاحاً كاذباً ══
class Test03_HonestExitCodes(unittest.TestCase):
    def test_check_command_source_ties_exit_to_testnet_result(self):
        """check لم يعد يُرجع 0 دائماً بغض النظر عمّا طبعه."""
        import inspect
        import live_trader
        src = inspect.getsource(live_trader.main)
        # الجزء الخاص بوضع check يجب أن يحسب testnet_ok لا أن يُرجع 0 ثابتاً
        idx = src.index("if a.mode == 'check':")
        block = src[idx:idx + 2000]
        self.assertIn('testnet_ok', block)
        self.assertNotIn('return 0\n\n    if a.mode in', block)

    def test_testnet_keys_missing_sentinel_is_literal(self):
        from src.environment.env import build as build_env, preflight, EnvironmentError_
        import tempfile as _t
        for k in ('TESTNET_API_KEY', 'TESTNET_API_SECRET'):
            os.environ.pop(k, None)
        c = build_env('testnet', base_dir=_t.mkdtemp())
        with self.assertRaises(EnvironmentError_) as ctx:
            preflight(c)
        self.assertIn('TESTNET_KEYS_MISSING', str(ctx.exception))

    def test_data_unavailable_sentinel_present_in_tick_handler(self):
        import inspect
        import live_trader
        src = inspect.getsource(live_trader.LiveTrader.tick)
        self.assertIn('DATA_UNAVAILABLE', src)

    def test_check_command_blocks_on_can_withdraw(self):
        """canWithdraw=True يجب ألا يجعل testnet_ok=True — مصدرياً."""
        import inspect
        import live_trader
        src = inspect.getsource(live_trader.main)
        idx = src.index("if a.mode == 'check':")
        block = src[idx:idx + 2200]
        self.assertIn("if acct.get('canWithdraw')", block)
        self.assertRegex(
            block,
            r"(?s)if acct\.get\('canWithdraw'\):.*?else:\s*testnet_ok = True")


# ══ 4. أخطاء أخرى اكتُشفت بتشغيل كل وضع فعلياً ══
class Test04_RuntimeCoverageBugs(unittest.TestCase):
    """
    كل اختبار هنا يقابل خللاً حقيقياً لم يكتشفه أي اختبار سابق لأن لا
    اختبار سابق شغّل main() عبر مسار CLI الفعلي لكل فرع على حدة.
    """

    def test_shadow_mode_has_icon_entry(self):
        """KeyError: 'shadow' كان يمنع تشغيل shadow --once بالكامل."""
        import re
        import live_trader
        import inspect
        src = inspect.getsource(live_trader.LiveTrader.run)
        m = re.search(r"icon = \{(.*?)\}\[self\.mode\]", src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("'shadow'", m.group(1))

    def test_no_local_database_import_shadows_module_global(self):
        """
        UnboundLocalError كان يكسر report/health/gate/intents/readiness
        بالكامل بسبب `from ... import Database` محلي داخل فرع
        live-status فقط — وهذا يجعل الاسم محلياً لكامل main() في
        بايثون بصرف النظر عن أي فرع تنفَّذ فعلاً.
        """
        import inspect
        import live_trader
        src = inspect.getsource(live_trader.main)
        self.assertNotIn('from src.storage.database import Database', src,
                         'استيراد محلي لـ Database عاد إلى الملف — '
                         'سيكسر كل فروع main() الأخرى مجدداً')

    def test_report_health_gate_intents_readiness_do_not_crash(self):
        """اختبار حاسم: كانت هذه الأوامر الخمسة تنهار جميعاً بنفس السبب."""
        import subprocess
        import tempfile as _t
        base = _t.mkdtemp()
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(os.path.dirname(here), 'live_trader.py')
        env = dict(os.environ)
        for cmd in (['paper', '--once'],
                    ['report', '--env', 'paper'], ['health', '--env', 'paper'],
                    ['gate', '--env', 'paper'], ['intents', '--env', 'paper'],
                    ['readiness']):
            r = subprocess.run(
                [sys.executable, script] + cmd, cwd=base, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertNotIn('UnboundLocalError', r.stdout + r.stderr,
                             f'{cmd}: {r.stderr[-300:]}')
            self.assertNotIn('KeyError', r.stdout + r.stderr,
                             f'{cmd}: {r.stderr[-300:]}')

    def test_recon_paper_does_not_require_binance_keys(self):
        """
        Paper لا يتصل ببينانس إطلاقاً — طلب مفاتيح API له خطأ تصميمي،
        ليس فحصاً أمنياً صحيحاً. المصالحة يجب أن تكون ضد PaperBroker.
        """
        import inspect
        import live_trader
        src = inspect.getsource(live_trader.main)
        idx = src.index("if a.mode == 'recon':")
        block = src[idx:idx + 1200]
        self.assertIn('PaperBroker', block)
        self.assertIn('Env.PAPER', block)

    def test_recon_shadow_monitor_short_circuit_without_network(self):
        import subprocess
        import tempfile as _t
        base = _t.mkdtemp()
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(os.path.dirname(here), 'live_trader.py')
        r = subprocess.run(
            [sys.executable, script, 'recon', '--env', 'shadow'],
            cwd=base, env=dict(os.environ), capture_output=True, text=True,
            timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertIn('لا شيء للمصالحة', r.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
