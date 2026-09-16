"""
أدوات التزامن للاختبارات — دورة حياة كاملة لكل عامل.
=====================================================
السبب الجذري لتعليق مجموعة الاختبارات في v8:

    barrier = threading.Barrier(6)      # 6 أطراف
    ... 6 عمّال ينادون wait()
    barrier.wait()                       # الخيط الرئيسي = طرف سابع

أول ستة يعبرون فيُعاد ضبط الحاجز، والسابع ينتظر جيلاً جديداً لا يأتي.
الخيوط كانت غير daemon، فتنتظرها Python عند الخروج ⇒ تعليق.

كل عامل هنا له: startup · shutdown · timeout · cancellation · cleanup.
"""
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, List, Any, Optional

DEFAULT_TIMEOUT = 20.0


@dataclass
class WorkerOutcome:
    index: int
    result: Any = None
    error: Optional[BaseException] = None
    traceback_text: str = ''
    completed: bool = False
    duration_s: float = 0.0


class ConcurrentRunner:
    """
    يشغّل n عاملاً متزامنين بانطلاقة موحّدة وتنظيف مضمون.

    ضمانات:
      • عدد أطراف الحاجز = عدد العمّال بالضبط (الرئيسي ليس طرفاً)
      • كل الخيوط daemon — لا تمنع خروج المفسّر أبداً
      • مهلة على الحاجز وعلى الانضمام
      • الإلغاء يُبلَّغ عبر Event لا بالقتل
      • cleanup يعمل حتى عند الاستثناء
    """

    def __init__(self, n_workers: int, timeout: float = DEFAULT_TIMEOUT):
        assert n_workers >= 1
        self.n = n_workers
        self.timeout = timeout
        # الرئيسي ليس طرفاً — يُطلق عبر Event منفصل
        self._barrier = threading.Barrier(n_workers, timeout=timeout)
        self._go = threading.Event()
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.outcomes: List[WorkerOutcome] = []
        self._threads: List[threading.Thread] = []

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()

    def _wrap(self, idx: int, fn: Callable[[int], Any],
              setup: Optional[Callable[[int], Any]],
              cleanup: Optional[Callable[[int, Any], None]]):
        out = WorkerOutcome(index=idx)
        ctx = None
        t0 = time.monotonic()
        try:
            if setup is not None:
                ctx = setup(idx)
            # انتظار موحّد بمهلة — لا انتظار أبدي
            if not self._go.wait(timeout=self.timeout):
                out.error = TimeoutError('لم تصل إشارة الانطلاق')
                return
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                pass          # عامل تعثّر — نكمل بلا تعليق
            if self._cancel.is_set():
                return
            out.result = fn(idx) if ctx is None else fn(idx, ctx)
            out.completed = True
        except BaseException as e:
            out.error = e
            out.traceback_text = traceback.format_exc()
        finally:
            out.duration_s = time.monotonic() - t0
            try:
                if cleanup is not None:
                    cleanup(idx, ctx)
            except BaseException:
                pass
            with self._lock:
                self.outcomes.append(out)

    def run(self, fn: Callable, setup: Optional[Callable] = None,
            cleanup: Optional[Callable] = None) -> List[WorkerOutcome]:
        self._threads = [
            threading.Thread(target=self._wrap, args=(i, fn, setup, cleanup),
                             name=f'test-worker-{i}', daemon=True)
            for i in range(self.n)]
        for t in self._threads:
            t.start()
        self._go.set()

        deadline = time.monotonic() + self.timeout
        for t in self._threads:
            t.join(timeout=max(0.05, deadline - time.monotonic()))

        stuck = [t for t in self._threads if t.is_alive()]
        if stuck:
            self.cancel()
            self._barrier.abort()
            for t in stuck:
                t.join(timeout=2.0)
        return self.outcomes

    @property
    def stuck_threads(self) -> List[str]:
        return [t.name for t in self._threads if t.is_alive()]

    @property
    def errors(self) -> List[BaseException]:
        return [o.error for o in self.outcomes if o.error is not None]


def assert_no_leaked_threads(test_case, before_count: int, grace_s: float = 2.0):
    """لا خيوط اختبار باقية — وإلا تعلق العملية عند الخروج."""
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        leaked = [t for t in threading.enumerate()
                  if t.name.startswith('test-worker') and t.is_alive()]
        if not leaked:
            break
        time.sleep(0.05)
    leaked = [t.name for t in threading.enumerate()
              if t.name.startswith('test-worker') and t.is_alive()]
    test_case.assertEqual(leaked, [], f'خيوط عالقة: {leaked}')
    non_daemon = [t.name for t in threading.enumerate()
                  if t is not threading.main_thread() and not t.daemon
                  and t.is_alive()]
    test_case.assertEqual(non_daemon, [],
                          f'خيوط غير daemon تمنع الخروج: {non_daemon}')
