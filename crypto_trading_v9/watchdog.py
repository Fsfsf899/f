#!/usr/bin/env python3
"""
مراقب التشغيل المستمر — يعيد تشغيل live_trader.py عند الانهيار.
=================================================================
لا يُصلح أي عطل منطقي في المحرك — فقط يمنع توقف العملية بالكامل
بسبب استثناء غير متوقَّع أثناء تشغيل بلا إشراف لأيام. الأعطال
المتكررة تصل لسقف فيتوقف المراقب نفسه ويُنبِّه، بدل إعادة المحاولة
إلى ما لا نهاية بصمت.

⚠️ هذا لا يغيّر حالة بوابة Live بأي شكل. غرضه الوحيد جعل مرحلة
تجميع بيانات Paper/Testnet (١٤ يوماً فأكثر) قابلة للتنفيذ فعلياً
بلا شخص يراقب طرفية مفتوحة طوال الوقت.

الاستخدام:
    python3 watchdog.py paper
    python3 watchdog.py testnet --symbol ETHUSDT --interval 1h
"""
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from src.monitoring.notify import Notifier

Runner = Callable[[List[str]], int]
Sleeper = Callable[[float], None]


@dataclass
class WatchdogConfig:
    max_restarts: int = 10       # ضمن النافذة الزمنية أدناه
    window_s: float = 3600.0     # ساعة — انهيارات أقدم لا تُحتسَب
    base_backoff_s: float = 5.0
    max_backoff_s: float = 300.0


class Watchdog:
    def __init__(self, cmd: List[str], notifier: Optional[Notifier] = None,
                 config: Optional[WatchdogConfig] = None,
                 runner: Optional[Runner] = None,
                 sleep_fn: Optional[Sleeper] = None):
        self.cmd = cmd
        self.notifier = notifier or Notifier()
        self.cfg = config or WatchdogConfig()
        self._runner = runner or self._default_runner
        self._sleep = sleep_fn or time.sleep
        self._restarts: List[float] = []

    @staticmethod
    def _default_runner(cmd: List[str]) -> int:
        return subprocess.call(cmd)

    def _prune(self, now: float):
        self._restarts = [t for t in self._restarts if now - t < self.cfg.window_s]

    def backoff_for(self, attempt: int) -> float:
        """تصاعد أسّي بسقف — لا انتظار لانهائي ولا إعادة فورية متلاحقة."""
        return min(self.cfg.base_backoff_s * (2 ** max(0, attempt - 1)),
                  self.cfg.max_backoff_s)

    def run_forever(self, max_iterations: Optional[int] = None) -> int:
        """
        يُرجع 0 عند خروج نظيف (rc=0)، 1 عند بلوغ سقف الانهيارات،
        2 عند بلوغ max_iterations (اختباري فقط — لا يُستخدم في الإنتاج).
        """
        self.notifier.notify('▶️ Watchdog Started', ' '.join(self.cmd),
                             severity='INFO', key='watchdog_start')
        it = 0
        while max_iterations is None or it < max_iterations:
            it += 1
            try:
                rc = self._runner(self.cmd)
            except Exception as e:
                rc = -1
                self.notifier.notify('⚠️ Watchdog Runner Error',
                                     f'{type(e).__name__}: {str(e)[:200]}',
                                     severity='HIGH', key='watchdog_runner_error')

            if rc == 0:
                self.notifier.notify('⏹ Process Exited Cleanly', f'rc={rc}',
                                     severity='INFO', key='watchdog_clean_exit',
                                     force=True)
                return 0

            now = time.time()
            self._prune(now)
            self._restarts.append(now)
            n = len(self._restarts)

            if n > self.cfg.max_restarts:
                self.notifier.notify(
                    '🛑 Watchdog Giving Up', f'{n} انهياراً خلال '
                    f'{int(self.cfg.window_s)}ث — توقف المراقب. راجع السجلات '
                    f'يدوياً قبل إعادة التشغيل.', severity='CRITICAL',
                    key='watchdog_giveup', force=True)
                return 1

            delay = self.backoff_for(n)
            self.notifier.notify(
                '♻️ Process Crashed — Restarting',
                f'rc={rc} | محاولة {n}/{self.cfg.max_restarts} | '
                f'انتظار {delay:.0f}ث', severity='HIGH', key='watchdog_restart')
            self._sleep(delay)
        return 2


def build_cmd(mode: str, extra: List[str]) -> List[str]:
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    return [sys.executable, os.path.join(here, 'live_trader.py'), mode] + extra


def main() -> int:
    if len(sys.argv) < 2:
        print('الاستخدام: python3 watchdog.py <paper|testnet|monitor|shadow> '
              '[أي وسيطة أخرى تُمرَّر لـ live_trader.py]')
        return 2
    mode = sys.argv[1]
    extra = sys.argv[2:]
    cmd = build_cmd(mode, extra)
    wd = Watchdog(cmd, notifier=Notifier.from_env())
    print(f"مراقب مستمر لـ: {' '.join(cmd)}")
    print("Ctrl+C لإيقاف المراقب (لن يُعيد التشغيل بعدها)")
    try:
        return wd.run_forever()
    except KeyboardInterrupt:
        print('\n⏹️ أُوقف المراقب يدوياً')
        wd.notifier.notify('⏹ Watchdog Stopped', 'إيقاف يدوي', severity='INFO',
                           key='watchdog_manual_stop', force=True)
        return 0


if __name__ == '__main__':
    sys.exit(main())
