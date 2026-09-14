"""
بوابات الترقية — المراحل 6 و14.
================================
لا انتقال إلى مرحلة إلا بعد اجتياز بوابة السابقة.

معيار النجاح ليس الربحية فقط: الاستقرار وسلامة التنفيذ شرطان
مستقلان، وفشل أيٍّ منهما يمنع الترقية.

    shadow → paper → testnet → مراجعة يدوية → live canary
"""
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

NOT_READY = 'NOT_READY'
PAPER_ONLY = 'PAPER_ONLY'
TESTNET_ONLY = 'TESTNET_ONLY'
PRODUCTION_READY = 'PRODUCTION_READY'


@dataclass
class GateCriteria:
    """كل عتبة قابلة للضبط. القيم افتراضات لا توصيات."""
    min_runtime_days: float = 14.0
    min_signals: int = 50
    min_trades: int = 30
    max_drawdown_pct: float = 25.0
    max_unknown_orders: int = 0
    max_reconciliation_mismatches: int = 0
    max_duplicate_orders: int = 0
    max_duplicate_fills: int = 0
    require_all_tests_pass: bool = True
    require_stop_tested: bool = True
    require_timeout_tested: bool = True
    require_restart_tested: bool = True
    require_partial_fill_tested: bool = True
    require_kill_switch_tested: bool = True


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ''
    blocking: bool = True


@dataclass
class GateResult:
    gate: str
    passed: bool
    checks: List[Check] = field(default_factory=list)
    note: str = ''

    @property
    def failures(self) -> List[str]:
        return [c.name for c in self.checks if not c.passed and c.blocking]

    def to_dict(self) -> Dict:
        return {'gate': self.gate, 'passed': self.passed,
                'failures': self.failures,
                'checks': [asdict(c) for c in self.checks], 'note': self.note}

    def render(self) -> str:
        L = ['', '=' * 62, f"  بوابة {self.gate}", '=' * 62]
        for c in self.checks:
            mark = 'PASS' if c.passed else ('FAIL' if c.blocking else 'WARN')
            L.append(f"  {mark:4s}  {c.name:38s} {c.detail}")
        L += ['', f"  النتيجة: {'✅ اجتازت' if self.passed else '❌ لم تجتز'}"]
        if self.note:
            L.append(f"  {self.note}")
        L.append('=' * 62)
        return '\n'.join(L)


def _dup_orders(db) -> int:
    r = db.query("""SELECT COUNT(*) c FROM (
        SELECT client_order_id FROM orders WHERE client_order_id IS NOT NULL
        GROUP BY client_order_id HAVING COUNT(*) > 1)""")
    return int(r[0]['c']) if r else 0


def _dup_fills(db) -> int:
    r = db.query("""SELECT COUNT(*) c FROM (
        SELECT exchange_trade_id FROM fills WHERE exchange_trade_id IS NOT NULL
        GROUP BY exchange_trade_id HAVING COUNT(*) > 1)""")
    return int(r[0]['c']) if r else 0


def _unknown(db) -> int:
    return len(db.unresolved_intents())


def _mismatches(db) -> int:
    return len(db.query(
        "SELECT 1 FROM risk_events WHERE kind='RECONCILIATION_MISMATCH'"))


def _runtime_days(db) -> float:
    st = db.get_kv('run_started_ts') or db.get_kv('paper_started_ts')
    if not st:
        return 0.0
    return (int(time.time() * 1000) - int(st)) / 86_400_000


def _exit_reasons(db) -> set:
    return {r['exit_reason'] for r in db.query(
        "SELECT DISTINCT exit_reason FROM recommendations "
        "WHERE exit_reason IS NOT NULL")}


def evaluate(db, gate_name: str, criteria: Optional[GateCriteria] = None,
             tests_passed: Optional[bool] = None,
             environment: str = 'paper') -> GateResult:
    """
    يقيّم بوابة على قاعدة بيئة واحدة.
    tests_passed=None يعني "لم يُشغَّل" ⇒ لا يُعد نجاحاً.
    """
    c = criteria or GateCriteria()
    ck: List[Check] = []

    days = _runtime_days(db)
    ck.append(Check('runtime_days', days >= c.min_runtime_days,
                    f"{days:.2f} / {c.min_runtime_days}"))

    n_sig = len(db.query('SELECT 1 FROM signals'))
    ck.append(Check('signals', n_sig >= c.min_signals,
                    f"{n_sig} / {c.min_signals}"))

    trades = db.query("SELECT * FROM recommendations "
                      "WHERE outcome IS NOT NULL AND acted=1")
    ck.append(Check('trades', len(trades) >= c.min_trades,
                    f"{len(trades)} / {c.min_trades}"))

    unk = _unknown(db)
    ck.append(Check('no_unknown_orders', unk <= c.max_unknown_orders, f"{unk}"))

    mm = _mismatches(db)
    ck.append(Check('no_reconciliation_mismatch',
                    mm <= c.max_reconciliation_mismatches, f"{mm}"))

    do, df = _dup_orders(db), _dup_fills(db)
    ck.append(Check('no_duplicate_orders', do <= c.max_duplicate_orders, f"{do}"))
    ck.append(Check('no_duplicate_fills', df <= c.max_duplicate_fills, f"{df}"))

    if trades:
        import numpy as np
        pnl = np.array([t['pnl'] for t in trades if t.get('pnl') is not None])
        if len(pnl):
            base = float(np.abs(pnl).sum()) * 2 + 1.0
            eq = base + np.cumsum(pnl)
            peak = np.maximum.accumulate(eq)
            dd = float((np.maximum(peak - eq, 0) / np.maximum(peak, 1e-9) * 100).max())
        else:
            dd = 0.0
    else:
        dd = 0.0
    ck.append(Check('max_drawdown', dd <= c.max_drawdown_pct,
                    f"{dd:.2f}% / {c.max_drawdown_pct}%"))

    n_orders = len(db.query('SELECT 1 FROM orders'))
    n_fills = len(db.query('SELECT 1 FROM fills'))
    ck.append(Check('orders_and_fills_recorded',
                    (n_orders > 0 and n_fills > 0) if trades else True,
                    f"أوامر {n_orders}, تعبئات {n_fills}"))

    if c.require_all_tests_pass:
        ck.append(Check('automated_tests', tests_passed is True,
                        'لم تُشغَّل' if tests_passed is None
                        else ('نجحت' if tests_passed else 'فشلت')))

    if gate_name == 'testnet':
        reasons = _exit_reasons(db)
        if c.require_stop_tested:
            ck.append(Check('stop_loss_exercised', 'STOP_LOSS' in reasons,
                            str(sorted(reasons)[:4])))
        partial = len(db.query(
            "SELECT 1 FROM order_intents WHERE state='PARTIALLY_FILLED' "
            "OR (filled_qty > 0 AND remaining_qty > 0)"))
        if c.require_partial_fill_tested:
            ck.append(Check('partial_fill_exercised', partial > 0, f"{partial}"))
        recovered = len(db.query(
            "SELECT 1 FROM system_events WHERE kind IN "
            "('ORDER_RECOVERED','STARTUP_RECOVERY')"))
        if c.require_timeout_tested:
            ck.append(Check('timeout_recovery_exercised', recovered > 0,
                            f"{recovered}"))
        if c.require_restart_tested:
            restarts = len(db.query(
                "SELECT 1 FROM system_events WHERE kind='STARTUP'"))
            ck.append(Check('restart_exercised', restarts >= 2, f"{restarts}"))
        if c.require_kill_switch_tested:
            kills = len(db.query(
                "SELECT 1 FROM risk_events WHERE kind='KILL_SWITCH'"))
            ck.append(Check('kill_switch_exercised', kills > 0, f"{kills}",
                            blocking=False))
        wrong_env = len(db.query(
            "SELECT 1 FROM system_events WHERE kind='ENV_MISMATCH'"))
        ck.append(Check('no_wrong_endpoint', wrong_env == 0, f"{wrong_env}"))

    passed = all(x.passed for x in ck if x.blocking)
    note = ('' if passed else
            'الترقية ممنوعة. سلامة التنفيذ شرط مستقل عن الربحية.')
    return GateResult(gate_name, passed, ck, note)


def readiness(paper_db=None, testnet_db=None,
              criteria: Optional[GateCriteria] = None,
              tests_passed: Optional[bool] = None) -> Dict:
    """
    تصنيف الجاهزية التشغيلية — لا يحكم على الربحية إطلاقاً.
    PRODUCTION_READY يتطلب اجتياز paper و testnet معاً.
    """
    res: Dict = {'generated_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    paper_ok = testnet_ok = False

    if paper_db is not None:
        g = evaluate(paper_db, 'paper', criteria, tests_passed, 'paper')
        res['paper'] = g.to_dict(); paper_ok = g.passed
    else:
        res['paper'] = {'gate': 'paper', 'passed': False,
                        'note': 'لا بيانات تشغيل ورقي'}

    if testnet_db is not None:
        g = evaluate(testnet_db, 'testnet', criteria, tests_passed, 'testnet')
        res['testnet'] = g.to_dict(); testnet_ok = g.passed
    else:
        res['testnet'] = {'gate': 'testnet', 'passed': False,
                          'note': 'لا بيانات تشغيل testnet'}

    if paper_ok and testnet_ok and tests_passed is True:
        cls = PRODUCTION_READY
        note = ('جاهزية **تشغيلية** فقط. لا تعني ربحية. الانتقال إلى live '
                'يتطلب موافقة يدوية صريحة ومراجعة مستقلة.')
    elif paper_ok:
        cls = TESTNET_ONLY
        note = 'اجتاز الورقي. المرحلة التالية testnet.'
    elif tests_passed is not False:
        cls = PAPER_ONLY
        note = 'الكود جاهز للورقي. الجاهزية تُثبَت بسجلات تشغيل لا باختبارات.'
    else:
        cls = NOT_READY
        note = 'اختبارات فاشلة.'

    res['classification'] = cls
    res['note'] = note
    res['live_activation'] = 'يدوي فقط — لا تفعيل تلقائي بأي حال'
    return res
