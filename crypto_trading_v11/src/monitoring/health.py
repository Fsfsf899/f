"""
سلامة التشغيل ومفتاح الإيقاف — البند 43.
=========================================
أي خطأ حرج ⇒ NO NEW TRADES.

المفتاح مخزّن في قاعدة البيانات لا في الذاكرة — يبقى مفعّلاً بعد
إعادة التشغيل، ويمكن تفعيله من عملية أخرى (أو يدوياً بـ SQL).
"""
import time
from dataclasses import dataclass
from typing import Optional, List, Dict
from ..storage.database import Database

KILL = 'kill_switch'


@dataclass
class HealthState:
    healthy: bool
    can_open_new: bool
    reasons: List[str]
    detail: Dict


class HealthMonitor:
    def __init__(self, db: Database, cfg=None, notifier=None):
        from ..core.config import RiskConfig
        from .notify import Notifier
        self.db = db
        self.cfg = cfg or RiskConfig()
        self.notifier = notifier or Notifier()   # بلا backends = no-op آمن
        self.api_failures = 0
        self.order_failures = 0
        self.last_data_ts: Optional[int] = None
        self.reconciliation_ok = True
        self.max_api_failures = 5
        self.max_order_failures = 3

    # ── مفتاح الإيقاف ──
    def kill_switch_on(self) -> bool:
        return bool(self.db.get_kv(KILL, False))

    def engage_kill_switch(self, reason: str):
        self.db.set_kv(KILL, True)
        self.db.set_kv('kill_switch_reason', reason)
        self.db.risk_event('KILL_SWITCH', 'CRITICAL', reason)
        # فشل التنبيه (لا مزوّد مضبوط، أو انقطاع شبكة) لا يجوز أن يمنع
        # تفعيل المفتاح نفسه — أُوقِف التداول أولاً، ثم أُحاول التبليغ
        self.notifier.notify('🛑 Kill Switch Engaged', reason,
                             severity='CRITICAL', key='kill_switch')

    def blocking_conditions(self, *, equity=None, day_key=None,
                            interval_ms=None) -> List[str]:
        """الأسباب القائمة الآن — لا تشمل المفتاح نفسه."""
        out: List[str] = []
        if not self.reconciliation_ok:
            out.append('RECONCILIATION_FAILED')
        unresolved = self.db.unresolved_intents()
        if unresolved:
            out.append(f'UNRESOLVED_INTENTS={len(unresolved)}')
        if self.api_failures >= self.max_api_failures:
            out.append(f'API_FAILURES={self.api_failures}')
        if self.order_failures >= self.max_order_failures:
            out.append(f'ORDER_FAILURES={self.order_failures}')
        if self.last_data_ts and interval_ms:
            age = int(time.time() * 1000) - self.last_data_ts
            if age > interval_ms * 3:
                out.append(f'STALE_DATA({age/60000:.1f}m)')
        if day_key and equity is not None:
            d = self.db.get_day(day_key)
            if d and d['starting_equity']:
                loss = (equity - d['starting_equity']) / d['starting_equity'] * 100
                if loss <= -self.cfg.max_daily_loss_pct:
                    out.append(f'DAILY_LOSS_LIMIT({loss:.2f}%)')
        # مركز بلا وقف
        for p in self.db.open_positions():
            if not p.get('stop_order_id'):
                out.append(f"POSITION_WITHOUT_STOP(id={p['id']})")
        return out

    def release_kill_switch(self, who: str = 'manual', *, force: bool = False,
                            **ctx) -> Dict:
        """
        لا يُلغى الإيقاف إذا بقي سببه قائماً — تمنعه المواصفات صراحةً.
        force=True يتطلب قراراً واعياً ويُسجَّل كتجاوز.
        """
        remaining = self.blocking_conditions(**ctx)
        if remaining and not force:
            return {'released': False, 'blocking': remaining,
                    'reason': 'أسباب الإيقاف ما زالت قائمة'}
        self.db.set_kv(KILL, False)
        self.db.set_kv('kill_switch_reason', '')
        self.db.system_event(
            'KILL_SWITCH_RELEASED',
            f"{who}{' FORCED:' + str(remaining) if remaining else ''}")
        if remaining:
            self.db.risk_event('KILL_SWITCH_FORCE_RELEASED', 'CRITICAL',
                               str(remaining)[:300])
            self.notifier.notify('⚠️ Kill Switch Force-Released', str(remaining),
                                 severity='CRITICAL', key='kill_switch_forced',
                                 force=True)
        else:
            self.notifier.notify('✅ Kill Switch Released', f'by={who}',
                                 severity='INFO', key='kill_switch_released')
        return {'released': True, 'blocking': remaining, 'forced': bool(remaining)}

    # ── تسجيل الأعطال ──
    def record_api_failure(self, detail: str = ''):
        self.api_failures += 1
        self.db.risk_event('API_FAILURE', 'WARNING', detail)
        if self.api_failures >= self.max_api_failures:
            self.engage_kill_switch(f'أعطال API متتالية: {self.api_failures}')

    def record_api_success(self):
        self.api_failures = 0

    def record_order_failure(self, detail: str = ''):
        self.order_failures += 1
        self.db.risk_event('ORDER_FAILURE', 'WARNING', detail)
        if self.order_failures >= self.max_order_failures:
            self.engage_kill_switch(f'أعطال أوامر متتالية: {self.order_failures}')

    def record_order_success(self):
        self.order_failures = 0

    def set_data_freshness(self, last_close_ms: int):
        self.last_data_ts = last_close_ms

    def set_reconciliation(self, ok: bool, detail: str = '', *,
                           unavailable: bool = False):
        """
        تمييز مقصود: `unavailable=True` يعني أن المصالحة **لم تُنفَّذ أصلاً**
        (تعذّر جلب السعر/الحساب — انقطاع شبكة عابر مثلاً)، لا أنها كُشفت
        ووجدت اختلافاً حقيقياً. الخلط بين الحالتين كان يُسجِّل كل انقطاع
        شبكة كـ RECONCILIATION_MISMATCH حرِج، وهذا الحدث تحديداً يُحتسَب
        في بوابة الترقية (`no_reconciliation_mismatch`) — فانقطاع شبكة
        عابر أثناء تشغيل حقيقي طويل كان سيُسقِط البوابة ظلماً.
        """
        self.reconciliation_ok = ok
        if not ok:
            if unavailable:
                self.db.risk_event('RECONCILIATION_UNAVAILABLE', 'WARNING', detail)
            else:
                self.db.risk_event('RECONCILIATION_MISMATCH', 'CRITICAL', detail)

    def report(self, *, env=None, equity=None, day_key=None,
               interval_ms=None, data_quality=None) -> Dict:
        """تقرير الصحة — المرحلة 12."""
        now = int(time.time() * 1000)
        unresolved = self.db.unresolved_intents()
        open_pos = self.db.open_positions()
        last_cycle = self.db.get_kv('last_successful_cycle_ts')
        last_recon = self.db.get_kv('last_successful_recon_ts')

        db_ok, db_err = True, ''
        try:
            self.db.query('SELECT 1 FROM signals LIMIT 1')
        except Exception as e:
            db_ok, db_err = False, str(e)[:120]

        import threading
        blocking = self.blocking_conditions(
            equity=equity, day_key=day_key, interval_ms=interval_ms)
        if data_quality is not None and data_quality < 0.80:
            blocking.append(f'LOW_DATA_QUALITY={data_quality:.2f}')

        rep = {
            'healthy': not blocking and db_ok and not self.kill_switch_on(),
            'kill_switch': self.kill_switch_on(),
            'kill_switch_reason': self.db.get_kv('kill_switch_reason', ''),
            'blocking_conditions': blocking,
            'last_successful_cycle': last_cycle,
            'last_cycle_age_s': (round((now - int(last_cycle)) / 1000)
                                 if last_cycle else None),
            'last_data_fetch': self.last_data_ts,
            'data_age_s': (round((now - self.last_data_ts) / 1000)
                           if self.last_data_ts else None),
            'last_successful_reconciliation': last_recon,
            'unknown_orders': len(unresolved),
            'unknown_detail': [{'cid': r['client_order_id'], 'state': r['state']}
                               for r in unresolved[:5]],
            'open_positions': len(open_pos),
            'open_positions_without_stop': len(
                [p for p in open_pos if not p.get('stop_order_id')]),
            'api_failure_count': self.api_failures,
            'order_failure_count': self.order_failures,
            'reconciliation_ok': self.reconciliation_ok,
            'database_ok': db_ok,
            'database_error': db_err,
            'active_threads': threading.active_count(),
        }
        if env is not None:
            rep.update(env.safe_dict())
        return rep

    @staticmethod
    def render(rep: Dict) -> str:
        L = ['', '=' * 62, '  تقرير الصحة', '=' * 62]
        L.append(f"  الحالة: {'✅ سليم' if rep['healthy'] else '❌ غير سليم'}")
        for k in ('environment', 'endpoint', 'api_key_fingerprint', 'database',
                  'symbol', 'interval', 'strategy_version', 'config_fingerprint'):
            if k in rep:
                L.append(f"  {k:24s} {rep[k]}")
        L += ['',
              f"  آخر دورة ناجحة        {rep['last_cycle_age_s']}s مضت"
              if rep['last_cycle_age_s'] is not None else "  آخر دورة ناجحة        —",
              f"  عمر البيانات          {rep['data_age_s']}s"
              if rep['data_age_s'] is not None else "  عمر البيانات          —",
              f"  أوامر UNKNOWN         {rep['unknown_orders']}",
              f"  مراكز مفتوحة          {rep['open_positions']} "
              f"(بلا وقف: {rep['open_positions_without_stop']})",
              f"  أعطال API / أوامر     {rep['api_failure_count']} / "
              f"{rep['order_failure_count']}",
              f"  المصالحة              {'✅' if rep['reconciliation_ok'] else '❌'}",
              f"  قاعدة البيانات        {'✅' if rep['database_ok'] else '❌ ' + rep['database_error']}",
              f"  Threads               {rep['active_threads']}",
              f"  Kill Switch           {'🛑 مفعّل: ' + str(rep['kill_switch_reason']) if rep['kill_switch'] else '✅ مطفأ'}"]
        if rep['blocking_conditions']:
            L += ['', '  ⛔ أسباب مانعة:']
            for b in rep['blocking_conditions']:
                L.append(f"     {b}")
        L.append('=' * 62)
        return '\n'.join(L)

    # ── الفحص الشامل ──
    def check(self, *, equity: Optional[float] = None,
              interval_ms: Optional[int] = None,
              data_quality: Optional[float] = None,
              day_key: Optional[str] = None) -> HealthState:
        reasons: List[str] = []

        if self.kill_switch_on():
            reasons.append(f"KILL_SWITCH: {self.db.get_kv('kill_switch_reason','')}")

        if not self.reconciliation_ok:
            reasons.append('RECONCILIATION_FAILED')

        if self.api_failures:
            reasons.append(f'API_FAILURES={self.api_failures}')

        if self.last_data_ts and interval_ms:
            age = int(time.time() * 1000) - self.last_data_ts
            if age > interval_ms * 3:
                reasons.append(f'STALE_DATA ({age/60000:.1f}د)')

        if data_quality is not None and data_quality < 0.80:
            reasons.append(f'LOW_DATA_QUALITY={data_quality:.2f}')

        if day_key and equity is not None:
            d = self.db.get_day(day_key)
            if d and d['starting_equity']:
                loss_pct = (equity - d['starting_equity']) / d['starting_equity'] * 100
                if loss_pct <= -self.cfg.max_daily_loss_pct:
                    reasons.append(f'DAILY_LOSS_LIMIT ({loss_pct:.2f}%)')

        critical = any(r.startswith(('KILL_SWITCH', 'RECONCILIATION')) for r in reasons)
        return HealthState(healthy=not reasons, can_open_new=not reasons,
                           reasons=reasons,
                           detail={'api_failures': self.api_failures,
                                   'order_failures': self.order_failures,
                                   'critical': critical})
