"""
اختبارات التنبيهات والمراقب المستمر — بلا شبكة حقيقية إطلاقاً.
كل "إرسال" هنا محاكى عبر نقل (transport) أو مُشغِّل (runner) مُحقَن.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.monitoring.notify import (Notifier, TelegramBackend, WebhookBackend,
                                   NotifyResult)
from src.monitoring.health import HealthMonitor
from src.storage.database import Database
from watchdog import Watchdog, WatchdogConfig, build_cmd


def tmpdb():
    return Database(os.path.join(tempfile.mkdtemp(), 't.db'))


class FakeTransport:
    """يلتقط كل نداء بدل إرساله فعلياً."""
    def __init__(self, fail: bool = False):
        self.calls = []
        self.fail = fail

    def __call__(self, url, method, headers, data):
        self.calls.append({'url': url, 'method': method, 'headers': headers,
                           'data': data})
        if self.fail:
            raise ConnectionError('محاكاة انقطاع')
        return 200


# ══ 1. بلا مزوّد = no-op آمن ══
class Test01_NoOpDefault(unittest.TestCase):
    def test_empty_notifier_never_sends(self):
        n = Notifier()
        r = n.notify('عنوان', 'نص')
        self.assertFalse(r.sent)
        self.assertEqual(r.reason, 'NO_BACKEND_CONFIGURED')

    def test_from_env_empty_is_noop(self):
        for k in ('NOTIFY_TELEGRAM_BOT_TOKEN', 'NOTIFY_TELEGRAM_CHAT_ID',
                 'NOTIFY_WEBHOOK_URL'):
            os.environ.pop(k, None)
        n = Notifier.from_env()
        self.assertFalse(n.configured)
        self.assertFalse(n.notify('x', 'y').sent)

    def test_default_health_monitor_notifier_is_noop(self):
        """HealthMonitor بلا notifier صريح يجب ألا يحاول أي اتصال شبكة."""
        db = tmpdb()
        h = HealthMonitor(db)
        h.engage_kill_switch('اختبار')   # يجب ألا يرمي ولا يتصل بأي شبكة
        self.assertTrue(h.kill_switch_on())


# ══ 2. الإرسال عبر نقل محاكى ══
class Test02_Sending(unittest.TestCase):
    def test_telegram_backend_builds_correct_request(self):
        t = FakeTransport()
        n = Notifier([TelegramBackend('TOKEN123', 'CHAT456')], transport=t)
        r = n.notify('عنوان', 'محتوى', severity='HIGH')
        self.assertTrue(r.sent)
        self.assertEqual(len(t.calls), 1)
        self.assertIn('TOKEN123', t.calls[0]['url'])
        self.assertIn(b'CHAT456', t.calls[0]['data'])
        self.assertIn(b'HIGH', t.calls[0]['data'])

    def test_webhook_backend_sends_json(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://hooks.example/x')], transport=t)
        r = n.notify('X', 'Y')
        self.assertTrue(r.sent)
        self.assertEqual(t.calls[0]['url'], 'https://hooks.example/x')

    def test_multiple_backends_all_called(self):
        t = FakeTransport()
        n = Notifier([TelegramBackend('t', 'c'), WebhookBackend('https://h/x')],
                    transport=t)
        n.notify('X', 'Y', force=True)
        self.assertEqual(len(t.calls), 2)

    def test_backend_failure_does_not_raise(self):
        """فشل الإرسال يُعاد كنتيجة — لا يُرفع كاستثناء أبداً."""
        t = FakeTransport(fail=True)
        n = Notifier([WebhookBackend('https://h/x')], transport=t)
        try:
            r = n.notify('X', 'Y')
        except Exception as e:
            self.fail(f'notify() رفع استثناءً: {e}')
        self.assertFalse(r.sent)
        self.assertEqual(r.reason, 'ALL_BACKENDS_FAILED')

    def test_partial_failure_still_counts_as_sent(self):
        good = FakeTransport()

        def mixed(url, method, headers, data):
            if url == 'https://b':
                raise TimeoutError('bad')
            good.calls.append(url)
            return 200

        n = Notifier([WebhookBackend('https://a'), WebhookBackend('https://b')],
                    transport=mixed)
        r = n.notify('X', 'Y')
        self.assertTrue(r.sent)   # نجح واحد على الأقل
        oks = [d['ok'] for d in r.details]
        self.assertIn(True, oks); self.assertIn(False, oks)


# ══ 3. التبريد ومنع إغراق التنبيهات ══
class Test03_Cooldown(unittest.TestCase):
    def test_repeated_same_key_suppressed(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://h/x')], cooldown_s=100, transport=t)
        n.notify('X', '1', key='same')
        r2 = n.notify('X', '2', key='same')
        self.assertFalse(r2.sent)
        self.assertEqual(r2.reason, 'COOLDOWN')
        self.assertEqual(len(t.calls), 1)

    def test_different_keys_independent(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://h/x')], cooldown_s=100, transport=t)
        n.notify('A', '1', key='a')
        r = n.notify('B', '1', key='b')
        self.assertTrue(r.sent)
        self.assertEqual(len(t.calls), 2)

    def test_force_bypasses_cooldown(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://h/x')], cooldown_s=999, transport=t)
        n.notify('X', '1', key='k')
        r = n.notify('X', '2', key='k', force=True)
        self.assertTrue(r.sent)
        self.assertEqual(len(t.calls), 2)

    def test_cooldown_expires(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://h/x')], cooldown_s=0.05, transport=t)
        n.notify('X', '1', key='k')
        time.sleep(0.06)
        r = n.notify('X', '2', key='k')
        self.assertTrue(r.sent)


# ══ 4. حجب الأسرار ══
class Test04_Redaction(unittest.TestCase):
    def test_secret_redacted_from_body(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://h/x')], transport=t)
        n.notify('عطل', 'فشل الاتصال signature=deadbeefcafe1234 apiKey=abc123456789')
        body = t.calls[0]['data'].decode()
        self.assertNotIn('deadbeefcafe1234', body)


# ══ 5. from_env ══
class Test05_FromEnv(unittest.TestCase):
    def tearDown(self):
        for k in ('NOTIFY_TELEGRAM_BOT_TOKEN', 'NOTIFY_TELEGRAM_CHAT_ID',
                 'NOTIFY_WEBHOOK_URL', 'NOTIFY_COOLDOWN_SECONDS'):
            os.environ.pop(k, None)

    def test_telegram_configured_from_env(self):
        os.environ['NOTIFY_TELEGRAM_BOT_TOKEN'] = 'tok'
        os.environ['NOTIFY_TELEGRAM_CHAT_ID'] = 'chat'
        n = Notifier.from_env()
        self.assertTrue(n.configured)
        self.assertIsInstance(n.backends[0], TelegramBackend)

    def test_partial_telegram_env_ignored(self):
        os.environ['NOTIFY_TELEGRAM_BOT_TOKEN'] = 'tok'
        n = Notifier.from_env()
        self.assertFalse(n.configured, 'نصف إعداد لا يجب أن يُفعِّل Telegram')

    def test_custom_cooldown_from_env(self):
        os.environ['NOTIFY_WEBHOOK_URL'] = 'https://h/x'
        os.environ['NOTIFY_COOLDOWN_SECONDS'] = '42'
        n = Notifier.from_env()
        self.assertEqual(n.cooldown_s, 42.0)


# ══ 6. تكامل HealthMonitor ══
class Test06_HealthIntegration(unittest.TestCase):
    def test_kill_switch_triggers_notification(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://h/x')], transport=t)
        db = tmpdb()
        h = HealthMonitor(db, notifier=n)
        h.engage_kill_switch('اختبار حقيقي')
        self.assertEqual(len(t.calls), 1)
        self.assertIn(b'CRITICAL', t.calls[0]['data'])

    def test_forced_release_triggers_critical_notification(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://h/x')], transport=t)
        db = tmpdb()
        h = HealthMonitor(db, notifier=n)
        h.set_reconciliation(False, 'x')
        h.engage_kill_switch('y')
        t.calls.clear()
        h.release_kill_switch(force=True)
        self.assertEqual(len(t.calls), 1)
        self.assertIn(b'CRITICAL', t.calls[0]['data'])

    def test_notifier_failure_does_not_block_kill_switch(self):
        """فشل التنبيه لا يمنع تفعيل المفتاح — الأولوية لإيقاف التداول."""
        n = Notifier([WebhookBackend('https://h/x')],
                    transport=FakeTransport(fail=True))
        db = tmpdb()
        h = HealthMonitor(db, notifier=n)
        h.engage_kill_switch('سبب')
        self.assertTrue(h.kill_switch_on())   # المفتاح فُعِّل رغم فشل التنبيه

    def test_repeated_api_failures_notify_once_due_to_cooldown(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://h/x')], cooldown_s=300, transport=t)
        db = tmpdb()
        h = HealthMonitor(db, notifier=n)
        for _ in range(h.max_api_failures + 2):
            h.record_api_failure('عطل متكرر')
        # كل استدعاءات engage_kill_switch بعد الأول تستخدم نفس المفتاح
        # kill_switch فتُبرَّد — لا عاصفة تنبيهات من حالة واحدة متكررة
        self.assertEqual(len(t.calls), 1)


# ══ 7. المراقب المستمر ══
class Test07_Watchdog(unittest.TestCase):
    def test_clean_exit_stops_immediately(self):
        wd = Watchdog(['x'], runner=lambda cmd: 0, sleep_fn=lambda s: None)
        self.assertEqual(wd.run_forever(), 0)

    def test_restarts_on_crash(self):
        calls = []
        def runner(cmd):
            calls.append(1)
            return 0 if len(calls) >= 3 else 1
        wd = Watchdog(['x'], runner=runner, sleep_fn=lambda s: None)
        self.assertEqual(wd.run_forever(), 0)
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_max_restarts(self):
        wd = Watchdog(['x'], runner=lambda cmd: 1, sleep_fn=lambda s: None,
                      config=WatchdogConfig(max_restarts=3))
        self.assertEqual(wd.run_forever(), 1)

    def test_backoff_is_exponential_and_capped(self):
        wd = Watchdog(['x'], config=WatchdogConfig(
            base_backoff_s=2.0, max_backoff_s=20.0))
        vals = [wd.backoff_for(i) for i in range(1, 8)]
        self.assertEqual(vals[0], 2.0)
        self.assertEqual(vals[1], 4.0)
        self.assertEqual(vals[2], 8.0)
        self.assertTrue(all(v <= 20.0 for v in vals))
        self.assertEqual(vals[-1], 20.0)

    def test_old_restarts_pruned_outside_window(self):
        wd = Watchdog(['x'], config=WatchdogConfig(window_s=1.0, max_restarts=100))
        wd._restarts = [time.time() - 10]   # قديم — خارج النافذة
        wd._prune(time.time())
        self.assertEqual(len(wd._restarts), 0)

    def test_notifies_on_start_restart_and_giveup(self):
        t = FakeTransport()
        n = Notifier([WebhookBackend('https://h/x')], cooldown_s=0, transport=t)
        wd = Watchdog(['x'], notifier=n, runner=lambda cmd: 1,
                      sleep_fn=lambda s: None,
                      config=WatchdogConfig(max_restarts=2))
        wd.run_forever()
        bodies = b''.join(c['data'] for c in t.calls)
        self.assertIn(b'Watchdog Started', bodies)
        self.assertIn(b'Restarting', bodies)
        self.assertIn(b'Giving Up', bodies)

    def test_runner_exception_treated_as_crash_not_propagated(self):
        def boom(cmd):
            raise OSError('تعذّر التشغيل')
        wd = Watchdog(['x'], runner=boom, sleep_fn=lambda s: None,
                      config=WatchdogConfig(max_restarts=1))
        try:
            rc = wd.run_forever()
        except Exception as e:
            self.fail(f'run_forever رفع استثناءً بدل التعامل معه: {e}')
        self.assertEqual(rc, 1)

    def test_max_iterations_guard_for_tests(self):
        wd = Watchdog(['x'], runner=lambda cmd: 1, sleep_fn=lambda s: None,
                      config=WatchdogConfig(max_restarts=999))
        self.assertEqual(wd.run_forever(max_iterations=3), 2)

    def test_build_cmd_uses_correct_python_and_script(self):
        cmd = build_cmd('paper', ['--symbol', 'ETHUSDT'])
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith('live_trader.py'))
        self.assertEqual(cmd[2], 'paper')
        self.assertEqual(cmd[3:], ['--symbol', 'ETHUSDT'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
