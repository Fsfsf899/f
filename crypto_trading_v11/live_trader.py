#!/usr/bin/env python3
"""
البوت الحي — نقطة الدخول
=========================
  python3 live_trader.py check       فحص الاتصال والمفاتيح
  python3 live_trader.py monitor     مراقبة وتسجيل التوصيات بلا أوامر
  python3 live_trader.py testnet     أوامر حقيقية، أموال وهمية
  python3 live_trader.py live        🔴 أموال حقيقية (خلف بوابة التحقق)
  python3 live_trader.py report      تقرير الدقة من قاعدة البيانات
  python3 live_trader.py kill        تفعيل مفتاح الإيقاف
  python3 live_trader.py release     تحرير مفتاح الإيقاف

كل دورة:
  مطابقة → حماية الأوقاف → جلب بيانات → فحص جودة → إشارة
  → تسجيل → فحوص ما قبل الأمر → تنفيذ
"""
import os, sys, time, json, argparse
from typing import Dict, List, Optional
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone

from src.core.config import Config
from src.data.binance import BinancePublic, DataUnavailable
from src.data.cache import Cache
from src.data.validation import validate
from src.data.types import INTERVAL_MS
from src.signals.engine import SignalEngine, BUY
from src.market.btc_context import evaluate as evaluate_btc_context
from src.research.router_adapter import RouterAsSignalEngine
from src.risk.position_sizing import PositionSizer
from src.account.state import AccountSnapshot
from src.account.providers import (PaperAccountProvider,
                                   ExchangeAccountProvider)
from src.risk.risk_guard import RiskGuard
from src.backtest.costs import CostModel
from src.storage.database import Database
from src.monitoring.health import HealthMonitor
from src.monitoring.notify import Notifier
from src.monitoring.accuracy import AccuracyTracker
from src.execution.binance_client import BinanceClient, BinanceError
from src.execution.order_manager import OrderManager
from src.execution.reconciliation import Reconciler
from src.execution.idempotency import IdempotentOrderGate
from src.execution.paper_broker import PaperBroker, PaperMarketProvider
from src.execution.binance_client import MainnetBlocked
from src.execution.order_state import BLOCKING, MANUAL
from src.execution.paper_broker import PaperCosts
from src.environment.env import (Env, build as build_env, preflight,
                                 print_banner, EnvironmentError_)
from src.monitoring.shadow import ShadowRecorder
from src.monitoring.paper_report import PaperReport
from src.monitoring import gates as G
from src.selection.opportunity import SUPPORTED_ASSETS
from src.risk.portfolio import PortfolioRisk

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    p = os.path.join(HERE, '.env')
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


def day_key() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


class LiveTrader:
    def __init__(self, envcfg, cfg: Config, max_notional: float,
                scan_symbols: Optional[List[str]] = None):
        self.envcfg = envcfg
        self.env = envcfg.env
        self.mode = envcfg.env.value
        self.symbol = envcfg.symbol
        self.interval = envcfg.interval
        self.cfg = cfg
        self.max_notional = max_notional
        # محرك اختيار أفضل فرصة (الأقسام 78-98) — اختياري تماماً،
        # افتراضي None يحافظ على السلوك أحادي الرمز الأصلي حرفياً
        # بلا أي تغيير. عند تفعيله بقائمة رموز، كل دورة بلا مركز مفتوح
        # تمسح كل الرموز وتختار الأفضل بدل التقيّد بـ self.symbol
        # الثابت (انظر _scan_and_select_symbol أدناه).
        self.scan_symbols = list(scan_symbols) if scan_symbols else None
        db_path = envcfg.db_path

        self.db = Database(db_path)
        if self.db.get_kv('run_started_ts') is None:
            self.db.set_kv('run_started_ts', int(time.time() * 1000))
        # علامة البيئة تُخزَّن وتُقارن — لا خلط بين قواعد البيئات
        prev = self.db.get_kv('environment')
        if prev and prev != self.mode:
            raise EnvironmentError_(
                f"⛔ قاعدة البيئة {prev} تُستخدم لتشغيل {self.mode}")
        self.db.set_kv('environment', self.mode)
        self.db.set_kv('endpoint', envcfg.endpoint or 'local')
        self.db.set_kv('api_key_fingerprint', envcfg.key_fingerprint)
        self.notifier = Notifier.from_env()
        self.health = HealthMonitor(self.db, cfg.risk, notifier=self.notifier)
        self.public = BinancePublic()
        self.cache = Cache()
        # RouterAsSignalEngine يحقّق نفس واجهة SignalEngine.evaluate()
        # تماماً، لكنه يمر عبر EntryRouter — فيُفعِّل Breakout/Pullback
        # فعلياً عند تفعيلهما في cfg، بدل تجاهلهما بصمت كما كان (كانا
        # مبنيَّين ومُختبَرين بالكامل في البحث، لكن غير موصولين
        # بمسار Paper/Testnet/Live الحقيقي إطلاقاً). عند تعطيلهما
        # (الافتراضي)، النتيجة مطابقة حرفياً لـ SignalEngine المباشر
        # — مُختبَر صراحة (test_adapter_identical_to_direct_signal_engine_when_disabled).
        self.engine = RouterAsSignalEngine(cfg, db=self.db)
        self.sizer = PositionSizer(cfg.risk, CostModel(cfg.costs))

        self.client = None
        self.orders = None
        self.recon = None
        self.gate = None
        self.paper = None
        self.paper_market = None   # PaperMarketProvider — Paper فقط
        self.account_provider = None   # مزوّد حالة الحساب (الأقسام 3-17)
        self.shadow = None
        # RiskGuard مشترك — نفس الكائن الذي يستخدمه الباكتست، محفوظ في
        # القاعدة فعلياً هنا (خلافاً للباكتست: db=None هناك، بلا حاجة
        # لاستمرارية عبر محاكاة تاريخية منتهية). يستبدل _consec_losses()
        # القديمة التي كانت تُعيد حساب رقم بمعنى مختلف من القاعدة —
        # تطبيق مستقل ثانٍ لم يكن متسقاً مع منطق الباكتست.
        self.risk_guard = RiskGuard(cfg.risk, db=self.db)

        if self.env is Env.SHADOW:
            self.shadow = ShadowRecorder(self.db, self.mode)
        elif self.env is Env.PAPER:
            costs = PaperCosts(
                maker_fee=cfg.costs.maker_fee, taker_fee=cfg.costs.taker_fee,
                spread_bps=cfg.costs.spread_bps,
                normal_slippage_bps=cfg.costs.slippage_bps_entry,
                stop_slippage_bps=cfg.costs.slippage_bps_stop,
                gap_slippage_bps=float(os.getenv('GAP_SLIPPAGE_BPS', '60')),
                latency_ms=int(os.getenv('PAPER_LATENCY_MS', '250')),
                partial_fill_ratio=float(os.getenv('PAPER_PARTIAL_RATIO', '0')))
            # القسم A من متطلبات V11: Paper كان يستقبل BinancePublic()
            # الحقيقية مباشرة — أي دورة Paper كانت تستطيع فعلياً لمس
            # الشبكة عبر _book()/rules(). PaperMarketProvider محلي
            # بالكامل، بلا اتصال شبكة إطلاقاً؛ يُغذَّى من tick() بعد كل
            # جلب بيانات ناجح (انظر أسفل).
            self.paper_market = PaperMarketProvider()
            self.paper = PaperBroker(self.db, self.paper_market, costs=costs,
                                     starting_quote=float(os.getenv('CAPITAL', '1000')),
                                     env_tag=envcfg.tag)
            self.client = self.paper
            # القسم 15: حساب ورقي محاكى بالكامل، بلا أي نداء شبكة
            self.account_provider = PaperAccountProvider(
                cfg.risk, self.paper, quote_asset='USDT')
            self.orders = OrderManager(self.db, self.client, cfg, self.health,
                                       risk_guard=self.risk_guard)
            self.gate = self.orders.gate
            self.recon = Reconciler(self.db, self.client, gate=self.gate,
                                    order_manager=self.orders)
        elif self.env.touches_exchange:
            self.client = BinanceClient(envcfg.api_key, envcfg.api_secret,
                                        testnet=(self.env is Env.TESTNET))
            # تحقق أن العميل يشير للـ endpoint المتوقَّع فعلاً
            if self.client.base != envcfg.endpoint:
                self.db.system_event('ENV_MISMATCH',
                                     f'{self.client.base} != {envcfg.endpoint}')
                raise EnvironmentError_(
                    f"⛔ endpoint العميل {self.client.base} "
                    f"لا يطابق البيئة {envcfg.endpoint}")
            self.client.sync_time()
            # القسم 16: العميل مبنيّ من مفاتيح هذه البيئة وحدها، فلا
            # يمكن لهذا المزوّد أن يقرأ رصيد بيئة أخرى إطلاقاً.
            self.account_provider = ExchangeAccountProvider(
                cfg.risk, self.client, quote_asset='USDT',
                source=self.mode)
            self.orders = OrderManager(self.db, self.client, cfg, self.health,
                                       risk_guard=self.risk_guard)
            self.gate = self.orders.gate
            self.recon = Reconciler(self.db, self.client, gate=self.gate,
                                    order_manager=self.orders)

        import socket
        self.pid = os.getpid()
        self.host = socket.gethostname()
        self.lock_name = envcfg.lock_name
        self.holds_lock = False

        # القفل يشمل كل الأوضاع: مراقبان متوازيان يفسدان إحصاءات الدقة
        if True:
            if not self.db.acquire_lock(self.lock_name, self.pid, self.host):
                h = self.db.lock_holder(self.lock_name) or {}
                raise RuntimeError(
                    f"عملية أخرى تملك القفل (pid={h.get('pid')} "
                    f"host={h.get('host')}). لا يجوز تشغيل عاملين على نفس الحساب.")
            self.holds_lock = True

        # حسم كل نية عالقة قبل أي تداول جديد (المرحلة 8)
        if self.gate is not None:
            for r in self.gate.recover_in_flight():
                print(f"  ♻️ نية مستعادة {r['cid']} → {r['state']}")
                self.db.system_event('STARTUP_RECOVERY', f"{r['cid']}={r['state']}")
            if self.gate.has_unresolved():
                self.health.engage_kill_switch(
                    'نوايا أوامر غير محسومة عند الإقلاع')
                print('  ⛔ نوايا غير محسومة — مفتاح الإيقاف مفعّل')

        self.db.system_event(
            'STARTUP', f'{self.mode} {self.symbol} {self.interval} {cfg.version}')

    def equity(self) -> float:
        if self.paper is not None:
            return self.paper.equity([self.symbol])
        if self.client is None:
            return float(os.getenv('CAPITAL', '1000'))
        try:
            b = self.client.balances()
            quote = self.client.rules(self.symbol)['quote']
            eq = b.get(quote, {}).get('free', 0.0) + b.get(quote, {}).get('locked', 0.0)
            for p in self.db.open_positions():
                eq += p['qty'] * self.client.price(p['symbol'])
            return eq
        except BinanceError as e:
            self.health.record_api_failure(str(e))
            return 0.0

    def _symbol_rules(self, symbol: str) -> Dict:
        """قواعد المنصة للرمز — مُخزَّنة مؤقتاً في العميل، بلا نداء
        إضافي. التعذّر يُعيد قاموساً فارغاً فيعود المحجِّم لقيمه
        الافتراضية بدل الانهيار."""
        try:
            return dict(self.client.rules(symbol)) if self.client else {}
        except Exception:
            return {}

    def account_snapshot(self, symbols: Optional[List[str]] = None) -> AccountSnapshot:
        """
        لقطة حالة الحساب — المصدر الوحيد للتحجيم (الأقسام 3-8).

        ⚠️ `equity()` أعلاه تُرجع الحقوق **الكلية** (متاح + محجوز + قيمة
        المراكز) وتبقى للتقارير ومنحنى الحقوق وحدها. تمريرها إلى
        `PositionSizer` كان يعني معاملة الرصيد المحجوز في أوامر قائمة
        كأنه نقد قابل للإنفاق — ممنوع صراحةً في القسمين 3 و32.
        التحجيم يستخدم `usable_equity` من هذه اللقطة: المتاح فقط، ناقص
        الاحتياطي.

        لا مزوّد ⇒ لقطة UNAVAILABLE، فيفشل التحجيم مغلقاً (القسم 17).
        """
        if self.account_provider is None:
            snap = AccountSnapshot(quote_asset='USDT', source=self.mode)
            snap.reasons.append('NO_ACCOUNT_PROVIDER')
            return snap
        return self.account_provider.snapshot(
            symbols or list(self.scan_symbols or [self.symbol]))

    def _sync_symbol_to_open_position(self, now: str, verbose: bool) -> Optional[dict]:
        """
        ⚠️ إصلاح حرِج (تدقيق ما بعد V11): في وضع المسح متعدد الرموز،
        `self.symbol` يُضبَط من `envcfg.symbol` في الـ constructor
        ويُعدَّل داخل `tick()` فقط عند فوز رمز بالمسح — أي أن قيمته
        **لا تنجو من إعادة التشغيل**. عند الإقلاع بمركز مفتوح على رمز
        غير الافتراضي (فائز مسح سابق)، `open_positions()` غير مُصفّاة
        بالرمز فتُلغي المسح بحق (البند 84)، لكن بقية `tick()` كلها —
        `evaluate_pending()`، `guard_stops()`، `recon.run()`،
        `_settle_paper_trigger()` (تستعلم `WHERE symbol=?`)، جلب
        البيانات — تعمل على الرمز الافتراضي بينما المركز الحقيقي على
        رمز آخر. النتيجة: **وقف وهدف المركز المفتوح لا يُسوَّيان أبداً
        بعد إعادة التشغيل.** مُثبَت بإعادة إنتاج فعلية، لا بالقراءة.

        الإصلاح: تبنّي رمز المركز المفتوح صراحةً قبل أي خطوة تلمسه.
        يُعيد `None` عند النجاح، أو قاموس حالة يُوقف الدورة عند خرق
        سياسة المركز الواحد (رموز مفتوحة متعدِّدة) — إذ لا يمكن لدورة
        واحدة حماية أكثر من رمز، فالمرور بصمت هنا يترك مركزاً بلا
        حراسة وهو أخطر من التوقف الصريح.
        """
        symbols = {p['symbol'] for p in self.db.open_positions() if p.get('symbol')}
        if not symbols:
            return None
        if len(symbols) > 1:
            detail = ','.join(sorted(symbols))
            self.db.system_event('MULTI_SYMBOL_POSITIONS',
                                 f'مراكز مفتوحة على رموز متعدِّدة: {detail}')
            if verbose:
                print(f'[{now}] ⛔ مراكز مفتوحة على رموز متعدِّدة: {detail} '
                      f'— دورة واحدة لا تحمي أكثر من رمز')
            return {'status': 'MULTI_SYMBOL_POSITIONS',
                   'symbols': sorted(symbols)}
        held = symbols.pop()
        if held != self.symbol:
            self.db.system_event(
                'SYMBOL_ADOPTED',
                f'{self.symbol} → {held} (رمز المركز المفتوح بعد إعادة تشغيل)')
            if verbose:
                print(f'[{now}] 🔁 تبنّي رمز المركز المفتوح: '
                      f'{self.symbol} → {held}')
            self.symbol = held
        return None

    def tick(self, verbose: bool = True) -> dict:
        now = datetime.now().strftime('%H:%M:%S')
        dk = day_key()
        # ⚠️ قبل أي خطوة تلمس self.symbol (0ب، 1، 2، 3): في وضع المسح
        # يجب أن يُدار رمز المركز المفتوح فعلياً، لا الرمز الافتراضي.
        if self.scan_symbols:
            blocked = self._sync_symbol_to_open_position(now, verbose)
            if blocked is not None:
                return blocked
        if self.risk_guard.day_key != dk:
            # يوم جديد — يُصفِّر consecutive_losses وfailed daily_trades
            # بنفس سياسة new_day() المستخدَمة في الباكتست بالضبط.
            self.risk_guard.new_day(self.equity(), day_key=dk)

        # 0) أي نية غير محسومة تمنع كل شيء (المرحلة 12)
        if self.gate is not None:
            pend = self.gate.unresolved_detail()
            if pend:
                if verbose:
                    print(f'[{now}] ⛔ نوايا غير محسومة: '
                          f'{[(p["cid"], p["state"]) for p in pend]}')
                for p in pend:
                    if p['state'] == MANUAL:
                        self.health.engage_kill_switch(
                            f"نية تحتاج مراجعة يدوية: {p['cid']}")
                return {'status': 'UNRESOLVED_INTENTS', 'pending': pend}

        # 0ب) الوسيط الورقي يقيّم أوامر الوقف والهدف عند كل نبضة
        if self.paper is not None:
            for t in self.paper.evaluate_pending(self.symbol):
                print(f'[{now}] 📄 {t["kind"]} نُفّذ @ ${t["price"]:,.2f}')
                self._settle_paper_trigger(t)

        # 1) المطابقة أولاً — لا شيء قبلها
        if self.recon is not None:
            r = self.recon.run([self.symbol])
            # تمييز: تعذّر الفحص (لا بيانات/شبكة) ≠ اختلاف فعلي مكتشَف.
            # الأول لا يجوز أن يُسجَّل كحدث حرِج يُسقِط بوابة الترقية.
            unavailable = (not r.ok) and not r.discrepancies
            detail = str(r.discrepancies)[:300] if r.discrepancies else r.note
            self.health.set_reconciliation(r.ok, detail, unavailable=unavailable)
            if r.ok:
                self.db.set_kv('last_successful_recon_ts', int(time.time() * 1000))
            for rep in r.repaired:
                print(f'[{now}] 🔧 {rep}')
            if not r.ok:
                if verbose:
                    if unavailable:
                        print(f'[{now}] ⚠️ تعذّرت المصالحة: {r.note}')
                    else:
                        print(f'[{now}] ⛔ عدم تطابق — التداول موقوف')
                        for d in r.discrepancies[:5]:
                            print(f'      {d["kind"]}: {d["action"]}')
                if r.needs_manual:
                    self.health.engage_kill_switch('مصالحة تتطلب تدخلاً يدوياً')
                return {'status': 'RECON_FAILED', 'unavailable': unavailable,
                       'discrepancies': r.discrepancies}

        # 2) حماية الأوقاف
        if self.orders is not None:
            for a in self.orders.guard_stops():
                print(f'[{now}] 🛡️ {a}')

        # 2ب) مسح أفضل فرصة — اختياري تماماً (الأقسام 78-84، 97).
        # لا يُصرِّح بصفقة بنفسه — يُغيِّر self.symbol فقط للرمز الفائز
        # قبل خطوة البيانات التالية؛ بقية tick() تتابع كمسارها الأصلي
        # حرفياً من هنا (نفس الحماية/RiskGuard/PositionSizer/OrderManager).
        # القسم 84 صريح: مركز مفتوح ⇒ لا مسح جديد إطلاقاً.
        if self.scan_symbols and not self.db.open_positions():
            winner = self._scan_and_select_symbol(now, verbose)
            if winner is None:
                return {'status': 'NO_ELIGIBLE_OPPORTUNITY',
                       'scanned': self.scan_symbols}
            self.symbol = winner

        # 3) البيانات
        try:
            data = self.cache.get(self.public, self.symbol, self.interval,
                                  days=120, verbose=False)
            self.health.record_api_success()
            if self.paper_market is not None:
                # تغذية المزوّد المحلي بآخر إغلاق معروف — تُستخدَم في
                # evaluate_pending()/equity() لبقية هذه الدورة وبداية
                # التالية، بلا أي استعلام شبكة إضافي (القسم A، V11).
                self.paper_market.set_price(
                    self.symbol, float(data.close[-1]),
                    spread_bps=self.cfg.costs.spread_bps)
        except DataUnavailable as e:
            self.health.record_api_failure(str(e))
            if verbose: print(f'[{now}] ❌ DATA_UNAVAILABLE: {e}')
            return {'status': 'NO_DATA', 'sentinel': 'DATA_UNAVAILABLE',
                   'error': str(e)}

        q = validate(data, now_ms=self.public.now_ms())
        self.db.save_data_quality(self.symbol, self.interval, q.to_dict())
        self.health.set_data_freshness(int(data.open_time[-1]) + data.interval_ms - 1)

        # ⚠️ إصلاح حرِج (تدقيق V11 Final Patch، القسم 12): سياق BTC لم
        # يكن يُجلَب أو يُمرَّر إطلاقاً هنا رغم أن `require_btc_ok=True`
        # افتراضي في NoTradeConfig — بعد إصلاح فشل-الإغلاق عند غياب BTC
        # في no_trade.py (`btc_available=False` يمنع التداول الآن
        # صراحةً بدل المرور بصمت)، غياب هذا الجلب هنا كان سيعني حظر
        # كل قرارات BUY نهائياً وبلا استثناء — لا بق نظري، بل انحدار
        # وظيفي حقيقي في المسار الوحيد الحي فعلياً. نجلب بيانات BTC
        # (نعيد استخدام بيانات الرمز نفسها إن كان الرمز BTCUSDT أصلاً
        # — لا جلب مزدوج بلا داعٍ) ونمرِّر السياق الناتج صراحةً.
        btc_data = data if self.symbol == 'BTCUSDT' else None
        if btc_data is None:
            try:
                btc_data = self.cache.get(self.public, 'BTCUSDT', self.interval,
                                          days=120, verbose=False)
            except DataUnavailable:
                btc_data = None   # evaluate() أدناه تتعامل معها بأمان: available=False
        btc_ctx = evaluate_btc_context(
            btc_data, int(data.open_time[-1]) + data.interval_ms - 1,
            cfg=self.cfg.regime)

        eq = self.equity()
        self.db.start_day(dk, eq)

        self.db.set_kv('last_successful_cycle_ts', int(time.time() * 1000))

        # 4) سلامة التشغيل
        h = self.health.check(equity=eq, interval_ms=data.interval_ms,
                              data_quality=q.score, day_key=dk)
        if not h.can_open_new and verbose:
            print(f'[{now}] ⚠️ {h.reasons}')

        # 5) الإشارة — على آخر شمعة مغلقة
        i = len(data) - 1
        open_pos = self.db.open_positions()
        acc = {'open_positions': len(open_pos),
               'daily_trades': (self.db.get_day(dk) or {}).get('trades', 0) or 0,
               'daily_loss_hit': self.risk_guard.daily_loss_hit(self.equity()),
               'consecutive_losses': self.risk_guard.consecutive_losses,
               'exposure_ok': True}
        sig = self.engine.evaluate(data, i, data_quality=q.score, account_state=acc,
                                   btc_ctx=btc_ctx)

        sid = self.db.save_signal(sig.to_dict(), self.cfg.fingerprint())
        if sid == 0:
            # نفس الشمعة سُجّلت سابقاً (إعادة تشغيل داخل الشمعة نفسها).
            # نربط بالسجل القائم بدل إنشاء توصية بمعرّف معلّق.
            prev = self.db.query(
                """SELECT id FROM signals WHERE symbol=? AND interval=?
                     AND bar_time=? AND strategy_version=?""",
                (self.symbol, self.interval, sig.timestamp, sig.strategy_version))
            sid = prev[0]['id'] if prev else 0
            if self.db.query('SELECT 1 FROM recommendations WHERE signal_id=?', (sid,)):
                if verbose:
                    print(f'[{now}] ⏭️ الشمعة {sig.timestamp} معالَجة سابقاً — تخطٍ')
                return {'status': 'ALREADY_PROCESSED', 'signal_id': sid}
        if verbose:
            px = float(data.close[i])
            if sig.decision == BUY:
                print(f'[{now}] 🟢 BUY ${px:,.2f} | {sig.stars}★ score={sig.score} '
                      f'RR={sig.risk_reward:.2f} | {sig.regime}')
            else:
                print(f'[{now}] {sig.decision} ${px:,.2f} — '
                      f'{sig.reasons[0] if sig.reasons else "لا إعداد"}')

        if sig.decision != BUY or not h.can_open_new:
            self.db.save_recommendation(sid, sig.to_dict(), acted=False,
                                        reason=';'.join(sig.reasons + h.reasons))
            if self.shadow is not None:
                self.shadow.record(sig, None, sid)
            return {'status': sig.decision, 'signal_id': sid,
                    'health': h.reasons, 'data_quality': round(q.score, 3)}

        # 6) الحجم
        # ── الحجم من حالة الحساب الحقيقية (الأقسام 3-10) ──
        # `eq` أعلاه حقوق كلية للتقارير؛ التحجيم يستخدم اللقطة وحدها.
        snap = self.account_snapshot([self.symbol])
        rules = self._symbol_rules(self.symbol)
        sz = self.sizer.calculate(
            equity=eq, entry=sig.entry, stop=sig.stop_loss, stars=sig.stars,
            consecutive_losses=acc['consecutive_losses'],
            account=snap, step_size=rules.get('step_size'),
            min_qty=rules.get('min_qty', 0.0),
            min_notional=rules.get('min_notional', 10.0),
            fee_rate=self.cfg.costs.taker_fee)
        if sz.get('blocked') or sz['notional'] <= 0:
            reason = sz.get('reason', 'SIZING_BLOCKED')
            self.db.save_recommendation(sid, sig.to_dict(), acted=False,
                                        reason=reason)
            self.db.system_event('SIZING_BLOCKED',
                                 f"{self.symbol}: {reason} | "
                                 f"usable={snap.usable_equity:.2f} "
                                 f"status={snap.status}")
            if verbose:
                print(f'[{now}] ⛔ لا حجم صالح: {reason}')
            return {'status': 'NO_TRADE', 'sentinel': reason,
                    'signal_id': sid, 'account': snap.to_dict()}
        notional = min(sz['notional'], self.max_notional)
        rid = self.db.save_recommendation(sid, sig.to_dict(), acted=False,
                                          reason='pending')

        if self.env is Env.SHADOW:
            self.shadow.record(sig, sz, sid)
            print(f'      👁️ ظل — سُجّل بلا تنفيذ (كان ${notional:.2f})')
            return {'status': 'SHADOW', 'signal_id': sid}

        if self.env is Env.MONITOR:
            print(f'      📝 مراقبة فقط — لم يُرسل أمر (كان ${notional:.2f})')
            return {'status': 'MONITORED', 'signal_id': sid, 'recommendation_id': rid}

        # 7) فحوص ما قبل الأمر
        chk = self.orders.pre_trade(
            symbol=self.symbol, notional=notional, entry=sig.entry, stop=sig.stop_loss,
            signal_bar_time=int(data.open_time[i]), interval_ms=data.interval_ms,
            data_quality=q.score, equity=eq, day_key=dk)
        if not chk.passed:
            print(f'      ⛔ فحوص ما قبل الأمر: {chk.failures}')
            for c in chk.checks:
                if not c['passed']:
                    print(f'         {c["check"]}: {c["detail"]}')
            self.db.execute('UPDATE recommendations SET reason=? WHERE id=?',
                            (f"PRE_TRADE:{','.join(chk.failures)}", rid))
            return {'status': 'PRE_TRADE_BLOCKED', 'failures': chk.failures}

        # 8) التنفيذ
        res = self.orders.open_long(symbol=self.symbol, notional=notional,
                                    entry_ref=sig.entry, stop=sig.stop_loss,
                                    target=sig.take_profit,
                                    recommendation_id=rid, check=chk)
        if res:
            self.db.execute('UPDATE recommendations SET acted=1, reason=? WHERE id=?',
                            ('executed', rid))
            d = self.db.get_day(dk) or {}
            self.db.update_day(dk, trades=(d.get('trades') or 0) + 1, ending_equity=eq)
            print(f'      ✅ نُفّذ: {res["qty"]} @ ${res["entry"]:,.2f} '
                  f'| وقف #{res["stop_order_id"]}')
            return {'status': 'EXECUTED', **res}
        return {'status': 'ORDER_FAILED'}

    def _scan_and_select_symbol(self, now: str, verbose: bool) -> Optional[str]:
        """
        الأقسام 78-84، 91، 97: يمسح `self.scan_symbols`، يختار الأفضل
        عبر `scan_and_rank` الكنسي (`src/selection/opportunity.py`) —
        لا منطق ترتيب موازٍ هنا. يُعيد الرمز الفائز، أو `None` صراحة
        إن لم يتأهل أحد (البند 82: لا اختيار قسري).

        ⚠️ نطاق هذا التكامل: يختار الرمز فقط قبل خطوة البيانات
        العادية — بقية `tick()` (الإشارة، PositionSizer، OrderManager)
        تعمل بمسارها الأصلي حرفياً بعد ذلك، بلا تكرار منطق.

        سجل التدقيق المُخزَّن في القاعدة (البند 93) **مُنفَّذ** عبر
        `save_opportunity_scan()` أدناه — كل رمز بدرجته وأسباب رفضه،
        لا الفائز وحده. (كان هذا النص يقول "غير مُنفَّذ" بينما الكود
        تحته ينادي الدالة فعلاً — تناقض توثيقي صُحِّح بعد تدقيق V11.)
        باكتست طبقة الاختيار نفسها (البند 94) **لا يزال غير مُنفَّذ**.
        """
        from src.selection.opportunity import scan_and_rank
        from src.data.validation import validate as _validate

        engines: Dict[str, RouterAsSignalEngine] = {}
        data_by_symbol: Dict = {}
        idx_by_symbol: Dict[str, int] = {}
        dq_by_symbol: Dict[str, float] = {}
        btc_data = None
        for sym in self.scan_symbols:
            try:
                d = self.cache.get(self.public, sym, self.interval,
                                   days=120, verbose=False)
            except DataUnavailable:
                continue   # يُترَك خارج القائمة — scan_and_rank يُصنِّفه INVALID_MARKET_DATA
            q = _validate(d, now_ms=self.public.now_ms())
            engines[sym] = RouterAsSignalEngine(self.cfg, db=self.db)
            data_by_symbol[sym] = d
            idx_by_symbol[sym] = len(d) - 1
            dq_by_symbol[sym] = q.score
            if sym == 'BTCUSDT':
                btc_data = d

        if btc_data is None:
            # سياق BTC عامل سوق مشترك لكل الرموز الممسوحة، لا رمزاً
            # فردياً — نجلبه مرة واحدة حتى لو BTCUSDT نفسه غير مدرَج
            # في قائمة المسح.
            try:
                btc_data = self.cache.get(self.public, 'BTCUSDT', self.interval,
                                          days=120, verbose=False)
            except DataUnavailable:
                btc_data = None

        ref = next(iter(data_by_symbol), None)
        at_ms = (int(data_by_symbol[ref].open_time[-1])
                + data_by_symbol[ref].interval_ms - 1) if ref else 0
        btc_ctx = evaluate_btc_context(btc_data, at_ms, cfg=self.cfg.regime)

        dk = day_key()
        open_pos = self.db.open_positions()
        eq = self.equity()
        # لقطة واحدة لكل المسح — كل المرشَّحين يُقيَّمون بنفس حالة
        # الحساب بالضبط، فلا يفوز رمز لأن لقطته أُخذت في لحظة أفضل.
        snap = self.account_snapshot(list(self.scan_symbols or []))
        acc = {'open_positions': len(open_pos),
              'daily_trades': (self.db.get_day(dk) or {}).get('trades', 0) or 0,
              'daily_loss_hit': self.risk_guard.daily_loss_hit(eq),
              'consecutive_losses': self.risk_guard.consecutive_losses,
              'exposure_ok': True}

        # ⚠️ إصلاح (تدقيق ما بعد V11): كان يُمرَّر `False` ثابتاً هنا،
        # فحارس البند 84 داخل `scan_and_rank` يصير بلا أثر ويعتمد
        # الأمان كلياً على شرط نقطة النداء في tick(). تمرير الحالة
        # الحقيقية يجعل الحارسين مستقلَّين فعلاً — وهذا الغرض منهما.
        has_open = bool(open_pos)

        # ⚠️ إصلاح (تدقيق ما بعد V11): البند 88 (فحص التعرّض/الارتباط
        # على المرشَّح الفائز) لم يكن يعمل في الإنتاج إطلاقاً — لا
        # `portfolio` ولا `notional_estimates` كانا يُمرَّران، فيُتخطّى
        # الفحص بصمت في المسار الحيّ الوحيد رغم أنه مُنفَّذ ومُختبَر.
        # الحجم يحتاج entry/stop غير المعروفَين قبل التقييم الداخلي،
        # لذا نُمرِّر دالة كسولة تُفوِّض إلى PositionSizer الكنسي نفسه
        # (`self.sizer`) وسقف `max_notional` نفسه المستخدَم في مسار
        # التنفيذ — لا مصدر حجم موازٍ.
        def _notional_for(sym: str, sig) -> float:
            # القسم 11: تقدير الحجم للمرشَّح يمرّ بنفس حالة الحساب
            # الحقيقية — أصل لا يمكن تداوله بالرصيد الحالي لا يجوز أن
            # يُختار كأفضل فرصة.
            rules = self._symbol_rules(sym)
            sz = self.sizer.calculate(
                equity=eq, entry=sig.entry, stop=sig.stop_loss,
                stars=sig.stars,
                consecutive_losses=acc['consecutive_losses'],
                account=snap, step_size=rules.get('step_size'),
                min_qty=rules.get('min_qty', 0.0),
                min_notional=rules.get('min_notional', 10.0),
                fee_rate=self.cfg.costs.taker_fee)
            if sz.get('blocked') or sz['notional'] <= 0:
                return 0.0
            return min(sz['notional'], self.max_notional)

        open_notional = {p['symbol']: p['qty'] * p['entry_price']
                        for p in open_pos
                        if p.get('symbol') and p.get('qty') and p.get('entry_price')}
        price_series = {sym: d.close for sym, d in data_by_symbol.items()}

        result = scan_and_rank(
            self.scan_symbols, engines=engines, data_by_symbol=data_by_symbol,
            idx_by_symbol=idx_by_symbol, data_quality_by_symbol=dq_by_symbol,
            btc_ctx=btc_ctx, account_state=acc,
            portfolio=PortfolioRisk(self.cfg.portfolio)
                      if getattr(self.cfg, 'portfolio', None) else PortfolioRisk(),
            equity=eq, open_notional=open_notional, price_series=price_series,
            notional_fn=_notional_for,
            already_has_open_position=has_open)

        # القسم 93: سجل تدقيق كامل — كل رمز بدرجته وأسباب رفضه، لا
        # الفائز فقط. يُمكِّن لاحقاً الإجابة على "لماذا اختار النظام
        # SOL بدل ETH؟" من سجل حقيقي مُهيكَل قابل للاستعلام، لا حدث
        # نصّي مختصر فقط كما كان.
        self.db.save_opportunity_scan(result, btc_context=btc_ctx.risk_level)
        self.db.system_event(
            'OPPORTUNITY_SCAN',
            f"{result.decision} — {result.selected_symbol or 'none'} "
            f"({result.reason})")
        if verbose:
            summary = ', '.join(
                (f"{o.symbol}:{o.score:.1f}" if o.eligible else f"{o.symbol}:REJECTED")
                for o in result.opportunities)
            print(f'[{now}] 🔍 مسح الفرص: {summary} → {result.decision} '
                 f'{result.selected_symbol or ""}')

        if result.decision != 'BUY' or not result.selected_symbol:
            return None
        return result.selected_symbol

    def _settle_paper_trigger(self, trig: Dict):
        """
        يسجّل خروج مركز ورقي بعد تنفيذ وقف أو هدف.

        ⚠️ إصلاح حرِج (تدقيق V11): كانت تبحث عن المركز عبر
        `get_intent_by_client_order_id(trig['cid'])` — لكن `trig['cid']`
        معرّف **الطرف الفرعي** (`{list_cid}-STOP`/`{list_cid}-TARGET`)
        كما يُرجعه `evaluate_pending()`، بينما `order_intents` يخزِّن
        نية واحدة فقط لكامل OCO بمعرّف **القائمة** الأصلي (`list_cid`).
        هذا الخلط كان يجعل البحث يُعيد `None` دائماً، فتخرج الدالة من
        أول سطر **بصمت تام** — لا إغلاق مركز، لا PnL، لا تحديث
        RiskGuard، عند أي تفعيل وقف أو هدف في Paper على الإطلاق.
        المركز يبقى "OPEN" في القاعدة إلى الأبد رغم إغلاقه فعلياً على
        المنصة المحاكاة.

        الإصلاح: البحث المباشر عن المركز عبر `stop_client_order_id`/
        `target_client_order_id` (مُخزَّنان في `positions` نفسها منذ
        إصلاح OCO)، لا عبر `order_intents`.
        """
        cid = trig['cid']
        pos_rows = self.db.query(
            "SELECT * FROM positions WHERE symbol=? AND status='OPEN' "
            "AND (stop_client_order_id=? OR target_client_order_id=?)",
            (self.symbol, cid, cid))
        if not pos_rows:
            return
        pos = pos_rows[0]
        resp = self.paper.order_by_client_id(self.symbol, cid)
        list_cid = pos.get('list_client_order_id')
        if list_cid:
            self.db.transition_order_state(list_cid, 'FILLED',
                                           reason='paper trigger', actor='paper')
        reason = 'STOP_LOSS' if trig['kind'] == 'STOP' else 'TAKE_PROFIT'
        self.orders._record_exit(pos, self.symbol, resp, reason,
                                 client_order_id=cid)
        # ملاحظة تدقيق V11: الحلقة التالية كانت تُلغي "الأمر المقابل"
        # عبر البحث عن نية بنوع TARGET منفصلة — تصميم قديم يسبق توحيد
        # OCO (حيث كان الوقف والهدف نيتين مستقلتين تماماً، تُوضَعان عبر
        # place_target()/_place_stop() منفصلتين). place_target() لم
        # تعد تُستدعى من مسار التداول الفعلي إطلاقاً (تحقَّقت: فقط
        # _place_oco() يُستدعى من open_long())، فـ active_intents('TARGET', ...)
        # تُعيد دائماً قائمة فارغة لمراكز OCO — هذه الحلقة **لا تُنفَّذ
        # جسمها أبداً** لأي مركز حالي. الإلغاء الفعلي لطرف OCO الآخر
        # يحدث داخلياً في PaperBroker._cancel_sibling() (استُدعيت
        # بالفعل من evaluate_pending() قبل هذه الدالة)، وأي انحراف
        # متبقٍ في order_intents يُصحَّحه Reconciler._check_intent_drift()
        # في الدورة التالية. أُبقيها بلا حذف (اختبار test_v7_reliability.py
        # القديم يعتمد place_target() بمعزل) لكن موسومة صراحةً كمسار
        # غير نشط، لا حذفاً صامتاً قد يكسر ذلك الاختبار بلا داعٍ.
        other = 'TARGET' if trig['kind'] == 'STOP' else 'STOP'
        for r in self.db.active_intents(other, pos['id']):
            try:
                self.paper.cancel(self.symbol,
                                  self.paper.order_by_client_id(
                                      self.symbol, r['client_order_id'])['orderId'])
                self.db.transition_order_state(r['client_order_id'], 'CANCELED',
                                               reason='OCO counterpart', actor='paper')
            except Exception:
                pass

    def run(self, poll: int = 300, iterations: Optional[int] = None) -> int:
        """
        iterations=None يعني حلقة مستمرة (السلوك الافتراضي القديم).
        iterations=N ينفّذ N دورة بالضبط ثم يعود — يُستخدم مع --once
        (N=1) أو --iterations N لاختبار Paper/Testnet دون حلقة لا نهائية.

        يُرجع رمز خروج: 0 عند اكتمال طبيعي (بما فيه Ctrl+C)، 1 عند توقف
        قسري (فقدان قفل العملية) — حتى لا يُظن أن التشغيل نجح بينما
        توقف فعلياً بسبب حالة حرجة.
        """
        icon = {'shadow': '👁️ ظل (تسجيل فقط)',
                'monitor': '📝 مراقبة (بلا أوامر)', 'paper': '📄 تداول ورقي',
                'testnet': '🧪 TESTNET', 'live': '🔴 مال حقيقي'}[self.mode]
        bound = f' | {iterations} دورة ثم توقف' if iterations else ''
        print(f"\n{'='*60}\n  {icon} | {self.symbol} {self.interval} | "
              f"حد المركز ${self.max_notional}{bound}\n  Ctrl+C للإيقاف\n{'='*60}\n")
        self.notifier.notify(
            '▶️ Trading Engine Started',
            f'mode={self.mode} symbol={self.symbol} interval={self.interval} '
            f'poll={poll}s iterations={iterations or "∞"}', severity='INFO',
            key=f'startup_{self.mode}')
        tick_errors = 0
        executed = 0
        aborted = False
        try:
            while iterations is None or executed < iterations:
                try:
                    if self.holds_lock and not self.db.heartbeat(self.lock_name, self.pid):
                        self.health.engage_kill_switch('فُقد قفل العملية')
                        print('⛔ فُقد القفل — إيقاف')
                        aborted = True
                        break
                    if self.client is not None:
                        try:
                            if self.client.maybe_resync():
                                self.db.system_event('TIME_RESYNC', 'دورية')
                        except Exception:
                            pass
                    self.tick()
                    tick_errors = 0
                except BinanceError as e:
                    self.health.record_api_failure(str(e))
                    print(f'⚠️ بينانس: {e}')
                except Exception as e:
                    tick_errors += 1
                    self.db.system_event('TICK_ERROR', f'{type(e).__name__}: {e}')
                    print(f'⚠️ {type(e).__name__}: {e}')
                    # عطل واحد لا يستحق تنبيهاً — تكراره يستحق. لا إغراق
                    # لأن notify() نفسه مُبرَّد لكل مفتاح على حدة.
                    if tick_errors >= 3:
                        self.notifier.notify(
                            '⚠️ Repeated Tick Errors', f'{tick_errors} أعطال '
                            f'متتالية | آخرها: {type(e).__name__}: {str(e)[:200]}',
                            severity='HIGH', key=f'tick_errors_{self.mode}')
                executed += 1
                if iterations is not None and executed >= iterations:
                    break               # لا نوم عبثي بعد آخر دورة في تشغيل محدود
                time.sleep(poll)
        except KeyboardInterrupt:
            print('\n⏹️ إيقاف')
            self.db.system_event('SHUTDOWN', self.mode)
            self.notifier.notify('⏹ Trading Engine Stopped',
                                 f'mode={self.mode} (Ctrl+C)', severity='INFO',
                                 key=f'shutdown_{self.mode}', force=True)
            if self.holds_lock:
                self.db.release_lock(self.lock_name, self.pid)
            op = self.db.open_positions()
            if op:
                print(f'⚠️ {len(op)} مركز مفتوح — الأوقاف على المنصة تحميه')
            return 0

        if iterations is not None:
            print(f'\n✅ اكتملت {executed}/{iterations} دورة')
        if self.holds_lock:
            self.db.release_lock(self.lock_name, self.pid)
        if aborted:
            self.db.system_event('ABORTED', f'{self.mode}: فقدان القفل')
            return 1
        self.db.system_event('RUN_COMPLETE', f'{self.mode}: {executed} دورة')
        return 0


def main():
    load_env()
    ap = argparse.ArgumentParser(
        description='نظام تداول — الوضع الافتراضي monitor (بلا أوامر)')
    ap.add_argument('mode', nargs='?', default='monitor',
                    choices=['check', 'shadow', 'monitor', 'paper', 'testnet',
                             'live', 'report', 'recon', 'health', 'gate',
                             'readiness', 'kill', 'release', 'migrate', 'intents',
                             'live-status'])
    ap.add_argument('--symbol', default=os.getenv('SYMBOL', 'BTCUSDT'))
    ap.add_argument('--interval', default=os.getenv('INTERVAL', '4h'))
    ap.add_argument('--env', default=None,
                    help='بيئة القاعدة للأوامر التقريرية (افتراضي: paper)')
    ap.add_argument('--db', default=None, help='تجاوز مسار القاعدة (نادراً)')
    ap.add_argument('--poll', type=int, default=300)
    ap.add_argument('--once', action='store_true',
                    help='دورة واحدة ثم توقف (مكافئ لـ --iterations 1)')
    ap.add_argument('--iterations', type=int, default=None,
                    help='عدد دورات محدَّد ثم توقف — بلا حلقة لا نهائية')
    ap.add_argument('--force', action='store_true',
                    help='تجاوز واعٍ لـ release — يُسجَّل كحدث حرج')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--max-notional', type=float,
                    default=float(os.getenv('MAX_POSITION_USD', '50')))
    # ⚠️ إصلاح (تدقيق ما بعد V11): محرك اختيار أفضل فرصة (الأقسام
    # 78-98) كان مبنياً ومُختبَراً بالكامل لكن **بلا أي مسار تشغيل** —
    # `scan_symbols` وسيط constructor فقط، و main() كانت تبني
    # `LiveTrader(c, cfg, a.max_notional)` بلا تمريره. أي أن الميزة
    # كانت حيّة في الاختبارات وميتة في الإنتاج تماماً. هذا العلَم هو
    # مسار التشغيل الوحيد لها؛ الغياب (الافتراضي) يُبقي السلوك أحادي
    # الرمز الأصلي حرفياً بلا أي تغيير.
    ap.add_argument('--scan-symbols', default=os.getenv('SCAN_SYMBOLS'),
                    help='رموز مفصولة بفواصل لتفعيل مسح أفضل فرصة — '
                         'مثال: BTCUSDT,ETHUSDT,SOLUSDT. '
                         'الغياب = السلوك أحادي الرمز (--symbol)')
    a = ap.parse_args()
    cfg = Config()
    # ربط أعلام البحث بمتغيرات بيئة CLI صريحة — كانت مقروءة من Config
    # مباشرة فقط في سكربتات البحث، بلا مسار من --once/live_trader.py
    # الفعلي. القيمة الافتراضية False تُبقي كل شيء معطَّلاً كما كان.
    cfg.breakout.enabled = os.getenv('BREAKOUT_ENABLED', '0') == '1'
    cfg.pullback.enabled = os.getenv('PULLBACK_ENABLED', '0') == '1'
    cfg.breakout.retest_enabled = os.getenv('BREAKOUT_RETEST_ENABLED', '0') == '1'
    cfg.mtf_entry.confirmation_enabled = (
        os.getenv('MTF_ENTRY_CONFIRMATION_ENABLED', '0') == '1')

    def envcfg_for(name):
        return build_env(name, base_dir=HERE, symbol=a.symbol,
                         interval=a.interval,
                         strategy_version=cfg.version,
                         config_fingerprint=cfg.fingerprint(),
                         db_override=a.db)

    # ── أوامر لا تحتاج تشغيلاً ──
    if a.mode == 'live-status':
        # تشخيص كامل بلا تشغيل حلقة ولا اتصال حي — يقرأ ما هو موجود فقط
        from src.live.live_config import LiveConfig, check_activation, Stage
        from src.live.promotion import paper_gate_status, testnet_gate_status
        # ⚠️ لا تستورد Database محلياً هنا — الاسم مستورَد عالمياً في
        # رأس الملف. استيراد محلي بنفس الاسم يجعله محلياً لكامل نطاق
        # main() في بايثون (حتى قبل نقطة التنفيذ)، فتنكسر كل الفروع
        # الأخرى (report/health/gate/intents/readiness) بـ
        # UnboundLocalError — وهذا بالضبط ما حدث هنا واكتُشف بتشغيل
        # فعلي، لا بالقراءة.

        os.environ.setdefault('TRADING_ENVIRONMENT', 'live')
        lc = LiveConfig()
        print(f"\n  بيئة التنفيذ الحي المضبوطة: {lc.stage.value}")
        if lc.stage != Stage.LIVE and lc.stage != Stage.LIVE_CANARY:
            print(f"  ⚠️  TRADING_ENVIRONMENT={lc.stage.value} — هذا ليس تقييماً "
                  f"لبوابة Live. اضبط TRADING_ENVIRONMENT=live للتحقق الفعلي.")

        pdb_path = os.path.join(HERE, 'data', 'paper', 'paper.db')
        tdb_path = os.path.join(HERE, 'data', 'testnet', 'testnet.db')
        pdb = Database(pdb_path) if os.path.exists(pdb_path) else None
        tdb = Database(tdb_path) if os.path.exists(tdb_path) else None

        paper_ok = paper_gate_status(pdb) if pdb else False
        testnet_ok = testnet_gate_status(tdb) if tdb else False

        snap = dict(account_info=None, unresolved_intents=None,
                   reconciliation_ok=None, kill_switch_on=None,
                   db_healthy=None, clock_offset_ms=None)
        if pdb is not None:
            snap['unresolved_intents'] = len(pdb.unresolved_intents())
            snap['kill_switch_on'] = bool(pdb.get_kv('kill_switch', False))
            try:
                pdb.query('SELECT 1'); snap['db_healthy'] = True
            except Exception:
                snap['db_healthy'] = False

        act = check_activation(lc, paper_gate_passed=paper_ok,
                               testnet_gate_passed=testnet_ok, **snap)
        print(act.render())
        print(f"\n  ملاحظة: account_info وreconciliation وclock_offset تتطلب "
              f"اتصالاً حياً بالمنصة — غير مقروءة هنا عمداً (لا اتصال Mainnet "
              f"إلا داخل بوابة Live المفعَّلة فعلياً).")
        return 0 if act.allowed else 1

    if a.mode == 'migrate':
        from src.storage import migrations
        target = a.db or envcfg_for(a.env or 'paper').db_path
        print(json.dumps(migrations.run(target, verbose=True),
                         ensure_ascii=False, indent=2))
        return 0

    if a.mode == 'check':
        from src.execution.binance_client import mainnet_allowed
        print('  فحص البيئات:')
        for name in ('shadow', 'monitor', 'paper', 'testnet', 'live'):
            try:
                c = envcfg_for(name)
                w = preflight(c)
                status = '✅ جاهزة' + (f' ({len(w)} تحذير)' if w else '')
            except EnvironmentError_ as e:
                status = f'⛔ {e}'
            print(f'    {name:8s} {status}')
        print(f'\n  Mainnet مسموح؟ {mainnet_allowed()} '
              f'(ALLOW_MAINNET=1 للسماح)')
        # اتصال testnet فقط، وبمفاتيحه المستقلة — هذا الجزء الوحيد الذي
        # يقرر رمز الخروج: shadow/monitor/paper جاهزة دائماً بلا شبكة،
        # وlive ممنوع بالتصميم، فلا معنى لجعل فشلهما "فشل check".
        # الجاهزية الفعلية القابلة للفحص هنا هي: هل Testnet يعمل؟
        testnet_ok = False
        try:
            c = envcfg_for('testnet'); preflight(c)
            cl = BinanceClient(c.api_key, c.api_secret, testnet=True)
            cl.ping(); off = cl.sync_time()
            print(f'  ✅ TESTNET متصل (فرق {off}ms) | تداول={cl.can_trade()} '
                  f'| بصمة المفتاح {c.key_fingerprint}')
            acct = cl.account()
            if acct.get('canWithdraw'):
                print('  ⛔ المفتاح يسمح بالسحب — أعد إنشاءه بلا صلاحية سحب')
            else:
                testnet_ok = True
        except (BinanceError, EnvironmentError_, MainnetBlocked) as e:
            print(f'  ❌ TESTNET: {e}')
        return 0 if testnet_ok else 1

    if a.mode in ('report', 'gate', 'readiness', 'health', 'intents',
                  'kill', 'release', 'recon'):
        name = a.env or ('paper' if a.mode != 'readiness' else 'paper')
        c = envcfg_for(name)
        db = Database(a.db or c.db_path)

        if a.mode == 'report':
            pr = PaperReport(db, name)
            if a.json:
                print(json.dumps(pr.full(), ensure_ascii=False,
                                 indent=2, default=str))
            else:
                print(pr.render())
                daily = pr.daily()
                if daily:
                    print('\n  يومياً:')
                    for d in daily[-14:]:
                        print(f"    {d['day']}  صفقات {d.get('n',0):3d}  "
                              f"PnL {d.get('net_pnl',0):>10}  "
                              f"PF {d.get('profit_factor')}")
                sh = ShadowRecorder(db, name).report()
                if sh.get('n_signals'):
                    print(f"\n  الظل: {sh}")
            return 0

        if a.mode == 'health':
            h = HealthMonitor(db, cfg.risk)
            rep = h.report(env=c)
            print(json.dumps(rep, ensure_ascii=False, indent=2, default=str)
                  if a.json else HealthMonitor.render(rep))
            return 0 if rep['healthy'] else 1

        if a.mode == 'gate':
            res = G.evaluate(db, name, tests_passed=None)
            print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2)
                  if a.json else res.render())
            return 0 if res.passed else 1

        if a.mode == 'readiness':
            paper_db = Database(envcfg_for('paper').db_path)
            tn = envcfg_for('testnet')
            tdb = Database(tn.db_path) if os.path.exists(tn.db_path) else None
            r = G.readiness(paper_db, tdb, tests_passed=None)
            print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
            return 0 if r['classification'] == G.PRODUCTION_READY else 1

        if a.mode == 'intents':
            for r in db.query('SELECT * FROM order_intents ORDER BY id DESC LIMIT 30'):
                print(f"  {r['client_order_id']:<24} {r['state']:<30} "
                      f"{r['order_type']:<8} filled={r.get('filled_qty')}")
            un = db.unresolved_intents()
            print(f"\n  غير محسومة: {len(un)}")
            for r in un:
                print(f"    ⛔ {r['client_order_id']} = {r['state']}")
            return 1 if un else 0

        if a.mode == 'kill':
            HealthMonitor(db, cfg.risk, notifier=Notifier.from_env()).engage_kill_switch('يدوي')
            print(f'🛑 مفتاح الإيقاف مفعّل على بيئة {name}')
            return 0

        if a.mode == 'release':
            h = HealthMonitor(db, cfg.risk, notifier=Notifier.from_env())
            res = h.release_kill_switch('يدوي', force=a.force)
            if res['released']:
                print(f"✅ تحرَّر{' (تجاوز واعٍ)' if res.get('forced') else ''}")
                return 0
            print('⛔ لم يتحرّر — أسباب الإيقاف قائمة:')
            for b in res['blocking']:
                print(f'   {b}')
            print('   استخدم --force للتجاوز الواعي (يُسجَّل كحدث حرج)')
            return 1

        if a.mode == 'recon':
            preflight(c)
            if c.env is Env.PAPER:
                # Paper لا يتصل ببينانس إطلاقاً — المصالحة ضد PaperBroker
                # نفسه، لا BinanceClient. كان الكود القديم يحاول بناء
                # BinanceClient دائماً فيفشل بمفاتيح مفقودة حتى في Paper،
                # حيث لا معنى لوجود مفاتيح أصلاً.
                costs = PaperCosts(
                    maker_fee=cfg.costs.maker_fee, taker_fee=cfg.costs.taker_fee,
                    spread_bps=cfg.costs.spread_bps,
                    normal_slippage_bps=cfg.costs.slippage_bps_entry,
                    stop_slippage_bps=cfg.costs.slippage_bps_stop,
                    gap_slippage_bps=float(os.getenv('GAP_SLIPPAGE_BPS', '60')),
                    latency_ms=int(os.getenv('PAPER_LATENCY_MS', '250')),
                    partial_fill_ratio=float(os.getenv('PAPER_PARTIAL_RATIO', '0')))
                # نفس إصلاح القسم A: Paper بلا اتصال شبكة إطلاقاً — حتى
                # في مسار recon المستقل هذا. نُغذِّي بآخر سعر إشارة
                # معروف من القاعدة إن وُجد (لا يلزم سعر حي للمصالحة
                # نفسها، لكنه يمنع equity()/price() من الفشل بلا داعٍ).
                pm = PaperMarketProvider()
                last_sig = db.query(
                    'SELECT entry FROM signals WHERE symbol=? '
                    'ORDER BY bar_time DESC LIMIT 1', (c.symbol,))
                if last_sig and last_sig[0].get('entry'):
                    pm.set_price(c.symbol, float(last_sig[0]['entry']),
                                spread_bps=cfg.costs.spread_bps)
                cl = PaperBroker(db, pm, costs=costs,
                                 starting_quote=float(os.getenv('CAPITAL', '1000')),
                                 env_tag=c.tag)
            elif c.env in (Env.SHADOW, Env.MONITOR):
                print(json.dumps({'ok': True, 'note':
                    f'بيئة {c.env.value} لا تنفّذ أوامر — لا شيء للمصالحة'},
                    ensure_ascii=False, indent=2))
                return 0
            else:
                cl = BinanceClient(c.api_key, c.api_secret,
                                   testnet=(c.env is Env.TESTNET))
                cl.sync_time()
            gate = IdempotentOrderGate(db, cl)
            r = Reconciler(db, cl, gate=gate).run([a.symbol])
            print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
            return 0 if r.ok else 1

    # ── أوضاع التشغيل ──
    try:
        c = envcfg_for(a.mode)
        warns = preflight(c)
    except EnvironmentError_ as e:
        print(f'\n{e}\n')
        return 2

    if a.mode == 'live':
        print('\n🔴 وضع المال الحقيقي — يتطلب موافقة صريحة ومراجعة مستقلة.\n')
        r = G.readiness(Database(envcfg_for('paper').db_path), None,
                        tests_passed=None)
        print(f"  تصنيف الجاهزية: {r['classification']}")
        if r['classification'] != G.PRODUCTION_READY:
            print('\n⛔ الوضع الحقيقي مقفول. الشروط غير مكتملة:')
            for k in ('paper', 'testnet'):
                for f in r.get(k, {}).get('failures', []):
                    print(f'   {k}: {f}')
            print('\n  لا يوجد تفعيل تلقائي. راجع gate و readiness أولاً.')
            return 1
        if os.getenv('I_HAVE_REVIEWED_AND_ACCEPT_RISK') != 'yes':
            print('\n⛔ يلزم إقرار صريح في .env:')
            print('   I_HAVE_REVIEWED_AND_ACCEPT_RISK=yes')
            return 1
        print('\n⚠️ تشغيل محدود (canary). راقب باستمرار.')

    scan_symbols = None
    if a.scan_symbols:
        scan_symbols = [x.strip().upper() for x in a.scan_symbols.split(',')
                       if x.strip()]
        unsupported = [x for x in scan_symbols if x not in SUPPORTED_ASSETS]
        if unsupported:
            print(f'\n⛔ UNSUPPORTED_SCAN_SYMBOL: {unsupported} — '
                  f'المدعوم: {SUPPORTED_ASSETS}')
            return 1

    print(print_banner(c, warns))
    try:
        t = LiveTrader(c, cfg, a.max_notional, scan_symbols=scan_symbols)
    except Exception as e:
        sentinel = {'paper': 'PAPER_INITIALIZATION_FAILED',
                   'testnet': 'TESTNET_CONNECTION_FAILED',
                   'live': 'MAINNET_BLOCKED'}.get(a.mode, 'INITIALIZATION_FAILED')
        print(f'\n⛔ {sentinel}: {type(e).__name__}: {str(e)[:300]}')
        return 1
    iters = 1 if a.once else a.iterations
    return t.run(a.poll, iterations=iters)


if __name__ == '__main__':
    sys.exit(main())
