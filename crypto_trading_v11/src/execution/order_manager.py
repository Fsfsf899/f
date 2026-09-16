"""
مدير الأوامر — البندان 28 و30.
===============================
لا يُرسَل أمر قبل اجتياز سلسلة فحوص كاملة. أي فشل ⇒ لا أمر.

حماية الوقف (البند 30):
STOP_LOSS_LIMIT ليس ضماناً. عند هبوط حاد قد يتجاوز السعر حد الـ limit
فلا يُنفَّذ، ويبقى المركز مكشوفاً. لذلك:
  1. نضع limit أدنى من stop بهامش قابل للضبط (يزيد فرصة التنفيذ)
  2. نراقب حالة الأمر كل دورة
  3. عند تجاوز السعر الوقف بهامش خطر ولم يُنفَّذ الأمر → خروج سوقي طارئ
     بشرط التحقق أولاً من أن المركز ما زال مفتوحاً فعلاً
"""
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from ..storage.database import Database
from ..core.config import Config
from .binance_client import BinanceClient, BinanceError
from .idempotency import (IdempotentOrderGate, DuplicateOrderError,
                          build_client_order_id)
from .order_state import (IN_FLIGHT, UNKNOWN, MANUAL, FILLED, OPEN,
                          PARTIALLY_FILLED, CANCELED, TERMINAL, LIVE_ON_EXCHANGE,
                          is_live, is_terminal)
from .errors import redact
from .oco import parse_oco_response, OCOResult


@dataclass
class PreTradeCheck:
    passed: bool
    checks: List[Dict] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = ''):
        self.checks.append({'check': name, 'passed': ok, 'detail': detail})
        if not ok:
            self.passed = False
        return self

    @property
    def failures(self) -> List[str]:
        return [c['check'] for c in self.checks if not c['passed']]


class OrderManager:
    def __init__(self, db: Database, client: BinanceClient, cfg: Config,
                 health=None, stop_limit_offset_pct: float = 0.30,
                 emergency_breach_pct: float = 0.50, risk_guard=None):
        self.db = db
        self.client = client
        self.cfg = cfg
        self.health = health
        self.risk_guard = risk_guard   # اختياري — None = سلوك قديم بلا تغيير
        self.stop_limit_offset_pct = stop_limit_offset_pct
        self.emergency_breach_pct = emergency_breach_pct
        # البوابة الوحيدة لكل أوامر التداول
        self.gate = IdempotentOrderGate(db, client, health)

    # ═══ توحيد قراءة الردود ═══
    @staticmethod
    def normalize_response(resp: Dict, fallback_price: float = 0.0, *,
                           base_asset: Optional[str] = None,
                           quote_asset: Optional[str] = None) -> Dict:
        """
        يوحّد رد المنصة إلى
        {qty, avg_price, fee, fee_by_asset, fee_uncertain, fills}.

        الردود المسترجَعة عبر origClientOrderId **لا تحوي fills** — كانت
        تُقرأ بسعر 0.0 فتُنتج PnL كارثياً وهمياً في التقارير. هنا يُشتق
        المتوسط من cummulativeQuoteQty ÷ executedQty.

        ⚠️ `fee` **مُقوَّم بالعملة المقابلة دائماً** (USDT عادةً) — إصلاح
        تدقيق ما بعد V11. كان الحساب يجمع `commission` من كل fill بلا أي
        نظر إلى `commissionAsset`، وبينانس تخصم عمولة **الشراء من الأصل
        الأساس** افتراضياً (BTC في BTCUSDT) لا من العملة المقابلة. فكان
        رقم بالبيتكوين يُجمَع ويُطرَح لاحقاً كأنه دولارات. الوسيط الورقي
        يخصم دائماً من العملة المقابلة، فالانحراف يظهر على المنصة
        الحقيقية وحدها ولا يكشفه أي اختبار.

        عمولة بأصل ثالث (BNB مثلاً عند تفعيل الخصم) **لا تُحوَّل تخميناً**
        ولا تُطرَح كأنها دولارات: تُستبعَد من `fee` ويُرفع
        `fee_uncertain=True` ليُسجِّلها المستدعي صراحةً. القيمة الخام لكل
        أصل محفوظة في `fee_by_asset` فلا يضيع شيء.
        """
        fills = resp.get('fills') or []
        if fills:
            qty = sum(float(f['qty']) for f in fills)
            avg = (sum(float(f['price']) * float(f['qty']) for f in fills) / qty
                   if qty > 0 else fallback_price)
            fee = 0.0
            fee_by_asset: Dict[str, float] = {}
            uncertain = False
            for f in fills:
                c = float(f.get('commission', 0) or 0)
                if c <= 0:
                    continue
                asset = (f.get('commissionAsset') or '').upper()
                fee_by_asset[asset] = fee_by_asset.get(asset, 0.0) + c
                if quote_asset and asset == quote_asset.upper():
                    fee += c                      # بالعملة المقابلة أصلاً
                elif base_asset and asset == base_asset.upper():
                    px = float(f.get('price', 0) or 0) or avg or fallback_price
                    if px > 0:
                        fee += c * px             # تحويل بسعر التنفيذ نفسه
                    else:
                        uncertain = True
                elif not base_asset and not quote_asset:
                    # لا معلومات رموز — السلوك القديم (يُفترَض المقابلة)
                    fee += c
                else:
                    uncertain = True              # أصل ثالث: لا تخمين
            return {'qty': qty, 'avg_price': avg, 'fee': fee,
                    'fee_by_asset': fee_by_asset, 'fee_uncertain': uncertain,
                    'fills': fills}

        qty = float(resp.get('executedQty', 0) or 0)
        quote = float(resp.get('cummulativeQuoteQty', 0) or 0)
        if qty > 0 and quote > 0:
            avg = quote / qty
        elif qty > 0:
            avg = float(resp.get('price', 0) or 0) or fallback_price
        else:
            avg = 0.0
        return {'qty': qty, 'avg_price': avg, 'fee': 0.0,
                'fee_by_asset': {}, 'fee_uncertain': False, 'fills': []}

    def _symbol_assets(self, symbol: str):
        """(base, quote) من قواعد الرمز المُخزَّنة مؤقتاً — بلا نداء شبكة
        إضافي (`rules()` تُخزِّن داخلياً). عند التعذّر: (None, None)،
        و`normalize_response` تعود عندها للسلوك القديم بلا انهيار."""
        try:
            r = self.client.rules(symbol)
            return r.get('base'), r.get('quote')
        except Exception:
            return None, None

    # ═══ سلسلة الفحوص قبل الأمر ═══
    def pre_trade(self, *, symbol: str, notional: float, entry: float,
                  stop: float, signal_bar_time: int, interval_ms: int,
                  data_quality: float, equity: float,
                  day_key: str) -> PreTradeCheck:
        c = PreTradeCheck(True)

        # 1) حالة الحساب
        try:
            acct = self.client.account()
            c.add('1_account_reachable', True)
            c.add('2_can_trade', bool(acct.get('canTrade')),
                  'صلاحية التداول' if acct.get('canTrade') else 'المفتاح لا يسمح بالتداول')
        except BinanceError as e:
            return c.add('1_account_reachable', False, str(e))

        # 3) الرمز وقواعده
        try:
            rules = self.client.rules(symbol)
            c.add('3_symbol_valid', rules['status'] == 'TRADING' and rules['spot'],
                  f"status={rules['status']} spot={rules['spot']}")
        except BinanceError as e:
            return c.add('3_symbol_valid', False, str(e))

        # 4) قواعد المنصة
        qty = self.client.round_qty(symbol, notional / entry)
        c.add('4_lot_size', qty >= rules['min_qty'],
              f"qty={qty} min={rules['min_qty']}")
        c.add('4b_min_notional', qty * entry >= rules['min_notional'],
              f"${qty*entry:.2f} min=${rules['min_notional']}")

        # 5) الرصيد
        try:
            bals = self.client.balances()
            free = bals.get(rules['quote'], {}).get('free', 0.0)
            c.add('5_balance', free >= notional * 1.01,
                  f"متاح ${free:.2f} مطلوب ${notional*1.01:.2f}")
        except BinanceError as e:
            c.add('5_balance', False, str(e))

        # 6) حدود المخاطرة
        max_notional = equity * self.cfg.risk.max_position_notional_pct / 100
        c.add('6_risk_limit', notional <= max_notional * 1.001,
              f"${notional:.2f} ≤ ${max_notional:.2f}")

        # 7) الحد اليومي
        day = self.db.get_day(day_key)
        if day and day.get('starting_equity'):
            loss = (equity - day['starting_equity']) / day['starting_equity'] * 100
            c.add('7_daily_loss', loss > -self.cfg.risk.max_daily_loss_pct,
                  f"{loss:.2f}%")
        else:
            c.add('7_daily_loss', True, 'يوم جديد')

        # 8) الانكشاف
        open_pos = self.db.open_positions()
        c.add('8_exposure', len(open_pos) < self.cfg.risk.max_open_positions,
              f"{len(open_pos)}/{self.cfg.risk.max_open_positions}")

        # 9) طزاجة الإشارة
        age = self.client.now_ms() - signal_bar_time
        c.add('9_signal_fresh', age <= interval_ms * 2,
              f"عمر {age/60000:.1f}د")

        # 10) جودة البيانات ومسافة الوقف
        c.add('10_data_quality', data_quality >= self.cfg.no_trade.min_data_quality,
              f"{data_quality:.3f}")
        sd = (entry - stop) / entry * 100 if entry > 0 else 0
        c.add('10b_stop_distance',
              self.cfg.no_trade.min_stop_distance_pct <= sd <= self.cfg.no_trade.max_stop_distance_pct,
              f"{sd:.2f}%")

        # 11) سلامة التشغيل
        if self.health is not None:
            h = self.health.check(equity=equity, interval_ms=interval_ms,
                                  data_quality=data_quality, day_key=day_key)
            c.add('11_health', h.can_open_new, ','.join(h.reasons) or 'سليم')

        return c

    # ═══ فتح مركز ═══
    def open_long(self, *, symbol: str, notional: float, entry_ref: float,
                  stop: float, target: float, recommendation_id: int,
                  check: PreTradeCheck) -> Optional[Dict]:
        if not check.passed:
            self.db.risk_event('PRE_TRADE_BLOCKED', 'INFO',
                               f"{symbol}: {check.failures}", symbol)
            return None

        # حارس: مركز مفتوح لهذه التوصية = تم الدخول سابقاً
        if any(p.get('recommendation_id') == recommendation_id
               for p in self.db.open_positions()):
            self.db.risk_event('DUPLICATE_ENTRY_BLOCKED', 'HIGH',
                               f"التوصية {recommendation_id} لها مركز مفتوح", symbol)
            return None

        try:
            res = self.gate.execute(
                order_type='ENTRY', symbol=symbol, side='BUY',
                recommendation_id=recommendation_id, quote_amount=notional,
                send=lambda cid: self.client.market_buy_quote(symbol, notional, cid))
        except DuplicateOrderError as e:
            self.db.risk_event('DUPLICATE_ENTRY_BLOCKED', 'HIGH',
                               f"{symbol} r{recommendation_id}: {e}", symbol)
            return None

        cid = res['client_order_id']
        if res.get('not_owner'):
            # الأمر موجود على المنصة لكن أرسله عامل/تشغيل آخر — تسجيل مركز
            # هنا ينتج سجلاً مكرراً لنفس الصفقة.
            self.db.risk_event('DUPLICATE_ENTRY_BLOCKED', 'HIGH',
                               f"{symbol} r{recommendation_id}: نية يملكها "
                               f"عامل آخر ({cid})", symbol)
            return None
        if not res['ok']:
            sev = 'CRITICAL' if res['state'] in (IN_FLIGHT, UNKNOWN) else 'HIGH'
            self.db.risk_event('ORDER_FAILED', sev,
                               f"{symbol} شراء [{res['state']}]: {res['error']}", symbol)
            if res['state'] == UNKNOWN and self.health:
                self.health.engage_kill_switch(
                    f"أمر دخول بحالة غير محسومة: {cid}")
            return None

        r = res['response'] or {}
        if res.get('recovered'):
            self.db.system_event('ENTRY_RECOVERED', cid)
            # الرد المسترجَع لا يحمل fills، فنقرأ الكميات من حقول الأمر
            if not r.get('fills') and float(r.get('executedQty', 0)) > 0:
                q = float(r['executedQty'])
                quote = float(r.get('cummulativeQuoteQty', 0))
                r = dict(r, fills=[{'qty': q,
                                    'price': (quote / q) if q > 0 else entry_ref,
                                    'commission': 0}])

        _b, _q = self._symbol_assets(symbol)
        norm = self.normalize_response(r, fallback_price=entry_ref,
                                       base_asset=_b, quote_asset=_q)
        qty, avg, fee = norm['qty'], norm['avg_price'], norm['fee']
        fills = norm['fills']
        if norm.get('fee_uncertain'):
            self.db.risk_event(
                'FEE_ASSET_UNCONVERTIBLE', 'WARNING',
                f"{symbol} دخول: عمولة بأصل ثالث {norm.get('fee_by_asset')} — "
                f"مستبعَدة من التكلفة، لا تُخمَّن", symbol)
        if qty <= 0:
            self.db.risk_event('ORDER_NOT_FILLED', 'HIGH', f"{symbol}", symbol)
            return None
        if avg <= 0:
            avg = entry_ref
        requested = notional / max(entry_ref, 1e-12)
        if qty < requested * 0.95:
            self.db.risk_event('PARTIAL_ENTRY_FILL', 'WARNING',
                               f"{symbol} نُفّذ {qty:.8f} من {requested:.8f}", symbol)

        pos_id = self.db.open_position(
            recommendation_id=recommendation_id, symbol=symbol, qty=qty,
            entry_price=avg, stop_loss=stop, take_profit=target,
            entry_order_id=str(r.get('orderId')), fees=fee)
        if not pos_id:
            # UNIQUE(recommendation_id) رفض: مركز لهذه التوصية مسجَّل بالفعل.
            # المتابعة بـ pos_id=0 تكتب أوامر ووقفاً بمرجع مركز غير موجود.
            self.db.risk_event('DUPLICATE_POSITION_BLOCKED', 'CRITICAL',
                               f"{symbol} r{recommendation_id}: مركز مسجَّل مسبقاً",
                               symbol)
            return None
        if self.risk_guard is not None:
            self.risk_guard.record_open()
        ins = self.db.insert_order_if_absent(
            position_id=pos_id, exchange_order_id=str(r.get('orderId')),
            client_order_id=cid, symbol=symbol, side='BUY', type='MARKET',
            qty=qty, price=avg, filled_qty=qty,
            status=r.get('status', 'FILLED'), raw_response=r)
        oid = (ins['order'] or {}).get('id')
        for f in fills:
            self.db.record_fill_if_absent(
                order_id=oid, exchange_trade_id=(str(f['tradeId'])
                                                 if f.get('tradeId') else None),
                symbol=symbol, qty=float(f['qty']), price=float(f['price']),
                commission=float(f.get('commission', 0) or 0),
                commission_asset=f.get('commissionAsset'))

        # ── الحماية على المنصة فوراً.
        # OCO يحمي بالوقف والهدف معاً؛ على Spot لا يمكن حجز نفس الكمية
        # لأمرين منفصلين، فالوقف وحده يمنع وضع الهدف.
        stop_id = target_id = None
        if target and target > avg:
            stop_id, target_id = self._place_oco(symbol, qty, stop, target, pos_id)
        if stop_id is None:
            target_id = None
            stop_id = self._place_stop(symbol, qty, stop, pos_id)
            if stop_id is not None and target and target > avg:
                self.db.risk_event(
                    'TARGET_SKIPPED_NO_OCO', 'WARNING',
                    f"{symbol} pos {pos_id}: الوقف يحجز الرصيد — الخروج "
                    f"بالوقف فقط أو بإشارة", symbol)
        if stop_id is None:
            self.db.risk_event('STOP_PLACEMENT_FAILED', 'CRITICAL',
                               f"{symbol} مركز {pos_id} بلا وقف على المنصة", symbol)

        return {'position_id': pos_id, 'qty': qty, 'entry': avg,
                'order_id': r.get('orderId'), 'stop_order_id': stop_id,
                'target_order_id': target_id}

    def _active_stop_intent(self, pos_id: int) -> Optional[Dict]:
        """أحدث نية وقف غير نهائية لهذا المركز."""
        rows = self.db.active_intents('STOP', pos_id)
        return rows[0] if rows else None

    def _max_intent_version(self, order_type: str, pos_id: int) -> int:
        """أعلى نسخة مسجَّلة لهذا النوع والمركز، شاملة الحالات النهائية."""
        r = self.db.query(
            "SELECT MAX(version) v FROM order_intents "
            "WHERE order_type=? AND position_id=?", (order_type, pos_id))
        return int(r[0]['v'] or 0) if r else 0

    def _next_stop_version(self, pos_id: int, intentional: bool) -> Optional[int]:
        """
        النسخة تُرفع **فقط** عند استبدال مقصود ومُثبَت أمانه.

        الخلل في v6: النسخة كانت تُرفع تلقائياً بعدد النوايا السابقة، فأي
        نية عالقة بحالة UNKNOWN تدفع الاستدعاء التالي لوضع وقف ثانٍ —
        وهو التفاف كامل على البوابة.

        يُرجع None إذا وُجدت نية حيّة أو غير محسومة (لا وقف جديد).
        """
        highest = self._max_intent_version('STOP', pos_id)
        cur = self._active_stop_intent(pos_id)
        if cur is None:
            # لا نية حيّة. إن سبق وُجدت نوايا (أُلغيت مثلاً) فالنسخة التالية
            # هي أعلى نسخة + 1 — العودة إلى 1 تصطدم بمعرّف مستهلَك.
            return highest + 1 if highest else 1
        state = cur['state']
        if state in (IN_FLIGHT, UNKNOWN, MANUAL):
            self.db.risk_event('STOP_UNRESOLVED', 'CRITICAL',
                               f"pos {pos_id}: نية وقف بحالة {state}")
            return None
        if is_live(state) and not intentional:
            return None          # وقف حيّ موجود — لا تضاعفه
        if state == FILLED:
            return None          # الوقف نُفّذ — المركز خرج
        return max(int(cur['version']), highest) + 1

    def _place_stop(self, symbol: str, qty: float, stop: float,
                    pos_id: int, version: Optional[int] = None,
                    intentional: bool = False) -> Optional[str]:
        limit = stop * (1 - self.stop_limit_offset_pct / 100)
        v = version if version is not None else self._next_stop_version(
            pos_id, intentional)
        if v is None:
            return None
        try:
            res = self.gate.execute(
                order_type='STOP', symbol=symbol, side='SELL',
                position_id=pos_id, version=v, qty=qty, stop_price=stop,
                limit_price=limit,
                send=lambda cid: self.client.stop_loss_limit(
                    symbol, qty, stop, limit, cid))
        except DuplicateOrderError:
            return None
        if not res['ok']:
            if self.health: self.health.record_order_failure(str(res['error']))
            return None
        s = res['response'] or {}
        try:
            sid = str(s.get('orderId'))
            self.db.update_position(pos_id, stop_order_id=sid)
            self.db.save_order(position_id=pos_id, exchange_order_id=sid,
                               client_order_id=res['client_order_id'],
                               symbol=symbol, side='SELL', type='STOP_LOSS_LIMIT',
                               qty=qty, stop_price=stop, price=limit,
                               status=s.get('status', 'NEW'), raw_response=s)
            return sid
        except BinanceError as e:
            if self.health: self.health.record_order_failure(str(e))
            return None

    # ═══ حماية الوقف (البند 30) ═══
    def guard_stops(self) -> List[Dict]:
        """
        يُستدعى كل دورة. يتحقق أن كل مركز مفتوح محميّ فعلاً.
        الخروج الطارئ لا يُنفَّذ إلا بعد التأكد من أن المركز ما زال قائماً
        وأن أمر الوقف لم يُنفَّذ.
        """
        actions = []
        for p in self.db.open_positions():
            sym, pos_id = p['symbol'], p['id']
            try:
                px = self.client.price(sym)
            except BinanceError as e:
                if self.health: self.health.record_api_failure(str(e))
                continue

            stop = p['stop_loss']
            if not stop:
                continue

            # 1) هل أمر الوقف ما زال قائماً؟
            stop_alive = False
            stop_filled = False
            stop_status = None
            if p.get('stop_order_id'):
                try:
                    st = self.client.order_status(sym, p['stop_order_id'])
                    stop_status = st.get('status')
                    stop_alive = stop_status in ('NEW', 'PARTIALLY_FILLED')
                    stop_filled = stop_status == 'FILLED'
                except BinanceError as e:
                    # فشل الاستعلام لا يعني زوال الوقف. الافتراض السابق
                    # (stop_alive=False) كان يضع وقفاً ثانياً عند كل انقطاع.
                    if self.health: self.health.record_api_failure(str(e))
                    self.db.risk_event('STOP_STATUS_UNKNOWN', 'WARNING',
                                       f"{sym} pos {pos_id}: {e}", sym)
                    actions.append({'position': pos_id, 'action': 'STOP_CHECK_FAILED'})
                    continue

            if stop_filled:
                cid = str(p.get('stop_client_order_id') or p.get('stop_order_id'))
                list_cid = p.get('list_client_order_id')
                self._record_exit(p, sym, st, 'STOP_LOSS', client_order_id=cid)
                if list_cid:
                    # نتحقق من النتيجة الفعلية بعد _record_exit() لا قبلها
                    # — "أمر الوقف FILLED" لا يعني بالضرورة "المركز أُغلق
                    # بالكامل" إن كانت كمية الأمر نفسها أصغر من كمية
                    # المركز المتبقية (حافة نادرة لكن حقيقية؛ اكتُشفت أثناء
                    # مراجعة الإصلاح السابق نفسه — كان يُصنِّف النية FILLED
                    # قبل معرفة النتيجة، فيتناقض مع مركز لا يزال OPEN فعلياً
                    # عند خروج جزئي).
                    pos_after = self.db.query(
                        'SELECT status FROM positions WHERE id=?', (pos_id,))
                    final_state = ('FILLED' if pos_after and
                                   pos_after[0]['status'] == 'CLOSED'
                                   else 'PARTIALLY_FILLED')
                    self.db.transition_order_state(
                        list_cid, final_state, reason='guard_stops: stop filled',
                        actor='guard_stops')
                actions.append({'position': pos_id, 'action': 'STOP_FILLED_SETTLED'})
                continue

            # 1ب) — القسم المُكتشَف حديثاً: هل الهدف نُفِّذ؟ لم يكن هناك
            # أي فحص لجانب الهدف إطلاقاً — على منصة حقيقية (Live/Testnet،
            # لا Paper، حيث evaluate_pending() تغطي الحالتين بمسار
            # منفصل تماماً)، تفعيل الهدف يُلغي الوقف تلقائياً على
            # المنصة (سلوك OCO الحقيقي)، فيبدو للفحص أعلاه أن "الوقف
            # مفقود" لا "مُنفَّذ" — وكان القسم التالي (2) سيحاول **وضع
            # وقف جديد لمركز أُغلق بالفعل بالكامل**، بدل التعرّف على أن
            # الهدف هو ما أغلقه فعلياً.
            if p.get('target_order_id'):
                try:
                    tst = self.client.order_status(sym, p['target_order_id'])
                    if tst.get('status') == 'FILLED':
                        cid = str(p.get('target_client_order_id')
                                 or p.get('target_order_id'))
                        list_cid = p.get('list_client_order_id')
                        self._record_exit(p, sym, tst, 'TAKE_PROFIT',
                                         client_order_id=cid)
                        if list_cid:
                            pos_after = self.db.query(
                                'SELECT status FROM positions WHERE id=?', (pos_id,))
                            final_state = ('FILLED' if pos_after and
                                          pos_after[0]['status'] == 'CLOSED'
                                          else 'PARTIALLY_FILLED')
                            self.db.transition_order_state(
                                list_cid, final_state,
                                reason='guard_stops: target filled',
                                actor='guard_stops')
                        actions.append({'position': pos_id,
                                        'action': 'TARGET_FILLED_SETTLED'})
                        continue
                except BinanceError as e:
                    if self.health: self.health.record_api_failure(str(e))
                    # لا نُوقف الدورة — فحص الوقف أولوية أعلى، تابعناه أعلاه بالفعل

            # 2) وقف مفقود ⇒ أعِد وضعه
            if not stop_alive and px > stop:
                sid = self._place_stop(sym, p['qty'], stop, pos_id)
                actions.append({'position': pos_id,
                                'action': 'STOP_REPLACED' if sid else 'STOP_REPLACE_FAILED'})
                if sid is None:
                    # ⚠️ إصلاح (تدقيق ما بعد V11): كان يُسجَّل حدث CRITICAL
                    # ثم **يتابع النظام التداول طبيعياً** — لا مفتاح إيقاف
                    # ولا حظر. و`HealthMonitor` لا يمسح `risk_events` حسب
                    # الخطورة إطلاقاً، فالحدث يبقى سطراً في القاعدة لا أثر
                    # له. النتيجة: مركز أُكِّد زوال وقفه من المنصة يبقى
                    # مكشوفاً، والنظام يفتح مراكز جديدة فوقه.
                    #
                    # هذا يخالف القاعدة المطلقة التي يقتبسها المشروع نفسه
                    # في `_record_exit` وينفّذها هناك: "If protection cannot
                    # be proven, the position is NOT protected and new
                    # trading must be blocked". نُوحِّد السلوك هنا بنفس
                    # الطريقة — الحالة مؤكَّدة لا مشكوكة: الاستعلام أثبت أن
                    # الوقف غير قائم، وإعادة وضعه فشلت.
                    self.db.risk_event('UNPROTECTED_POSITION', 'CRITICAL',
                                       f"{sym} مركز {pos_id}", sym)
                    if self.health:
                        self.health.engage_kill_switch(
                            f"مركز {pos_id} ({sym}) بلا وقف على المنصة وتعذّرت "
                            f"إعادة وضعه — يلزم تدخل يدوي")
                continue

            breach = (stop - px) / stop * 100 if stop > 0 else 0

            # 2ب) ⚠️ إصلاح (تدقيق ما بعد V11) — النافذة العمياء:
            # القسم (2) يشترط `px > stop` لأن بينانس ترفض وقفاً يُفعَّل
            # فوراً، والقسم (3) يشترط اختراقاً ≥ emergency_breach_pct.
            # فإذا اختفى الوقف والسعر بينهما (0 ≤ اختراق < الحد)، لم يكن
            # يحدث **أي شيء**: لا وقف يُعاد، لا خروج، ولا حتى تسجيل —
            # مركز بلا أي حماية على المنصة والنظام صامت تماماً عنه.
            # السعر تحت الوقف أصلاً يعني أن شرط الخروج تحقَّق فعلياً،
            # فالخروج السوقي هو التصرّف الصحيح لا الانتظار حتى يتسع
            # الاختراق. لا يُنفَّذ إلا بعد التأكد من أن المركز قائم فعلاً،
            # كما في القسم (3) تماماً.
            if not stop_alive and not stop_filled and px <= stop:
                self.db.risk_event(
                    'STOP_MISSING_BELOW_TRIGGER', 'CRITICAL',
                    f"{sym} مركز {pos_id}: الوقف غير قائم والسعر {px} عند/تحت "
                    f"الوقف {stop} (اختراق {breach:.2f}%) — لا يمكن إعادة وضع "
                    f"وقف يُفعَّل فوراً، فالخروج السوقي هو الحماية الوحيدة", sym)
                held = self._verify_still_held(sym, p['qty'])
                if held <= 0:
                    self.db.close_position(pos_id, realized_pnl=0.0)
                    actions.append({'position': pos_id, 'action': 'ALREADY_CLOSED'})
                    continue
                ok = self._exit_via_gate(p, sym, min(held, p['qty']),
                                         'EMERGENCY', 'STOP_MISSING_EXIT')
                actions.append({'position': pos_id,
                                'action': 'STOP_MISSING_EXIT' if ok
                                          else 'STOP_MISSING_EXIT_FAILED'})
                continue

            # 3) السعر اخترق الوقف بهامش خطر والأمر لم يُنفَّذ ⇒ خروج طارئ
            if breach >= self.emergency_breach_pct and not stop_filled:
                held = self._verify_still_held(sym, p['qty'])
                if held <= 0:
                    self.db.close_position(pos_id, realized_pnl=0.0)
                    actions.append({'position': pos_id, 'action': 'ALREADY_CLOSED'})
                    continue
                self.db.risk_event('EMERGENCY_EXIT', 'CRITICAL',
                                   f"{sym} السعر {px} تحت الوقف {stop} بـ {breach:.2f}%", sym)
                ok = self._emergency_exit(p, sym, min(held, p['qty']))
                actions.append({'position': pos_id,
                                'action': 'EMERGENCY_EXIT' if ok else 'EMERGENCY_EXIT_FAILED'})
        return actions

    def _verify_still_held(self, symbol: str, qty: float) -> float:
        try:
            base = self.client.rules(symbol)['base']
            b = self.client.balances().get(base, {})
            return b.get('free', 0.0) + b.get('locked', 0.0)
        except BinanceError:
            return -1.0     # غير معروف ⇒ لا خروج طارئ

    def _exit_via_gate(self, pos: Dict, symbol: str, qty: float,
                       order_type: str, reason: str) -> bool:
        """
        كل خروج يمر من هنا. البوابة تضمن أمر بيع واحداً لكل مركز
        مهما تكرر الاستدعاء — والبيع المكرر على Spot أخطر من الشراء
        لأنه يبيع عملة لا تملكها.
        """
        if pos.get('status') == 'CLOSED':
            return True

        # (1) الوقف يجب أن يزول قبل البيع. فشل الإلغاء المبتلَع في v6
        # كان يسمح ببيعين متزامنين: الوقف والأمر السوقي.
        if pos.get('stop_order_id'):
            cancel_ok, note, st = self._ensure_stop_gone(symbol, pos)
            if not cancel_ok:
                self.db.risk_event('EXIT_BLOCKED_STOP_ALIVE', 'CRITICAL',
                                   f"{symbol} pos {pos['id']}: {note}", symbol)
                if self.health:
                    self.health.engage_kill_switch(
                        f"تعذّر إلغاء الوقف قبل الخروج (pos {pos['id']})")
                return False
            if note == 'STOP_FILLED':
                # الوقف نفّذ الخروج فعلاً — لا بيع ثانٍ. موحَّد الآن عبر
                # _record_exit() نفسها (تدقيق V11) بدل _settle_after_stop()
                # المنفصلة التي كانت لا تزال تُستدعى من هنا رغم تعليق
                # سابق يقول إنها لم تعد نشطة — تصحيح لذلك الخطأ.
                cid = str(pos.get('stop_client_order_id')
                         or pos.get('stop_order_id'))
                list_cid = pos.get('list_client_order_id')
                self._record_exit(pos, symbol, st, 'STOP_LOSS',
                                  client_order_id=cid)
                if list_cid:
                    # نتحقق من النتيجة الفعلية بعد _record_exit() — راجع
                    # نفس التعليق في guard_stops() لسبب الترتيب.
                    pos_after = self.db.query(
                        'SELECT status FROM positions WHERE id=?', (pos['id'],))
                    final_state = ('FILLED' if pos_after and
                                   pos_after[0]['status'] == 'CLOSED'
                                   else 'PARTIALLY_FILLED')
                    self.db.transition_order_state(
                        list_cid, final_state, reason='exit_via_gate: stop filled',
                        actor='exit_via_gate')
                return True

        # (2) لا تبع أكثر من الرصيد الفعلي. العمولة قد تُخصم من الأصل
        # الأساس، فتصبح الكمية المسجلة أكبر من المتاح.
        try:
            free = self.client.base_free(symbol)
            sellable = self.client.round_qty(symbol, min(qty, free))
        except BinanceError as e:
            self.db.risk_event('EXIT_BALANCE_UNKNOWN', 'CRITICAL',
                               f"{symbol}: {redact(str(e))[:200]}", symbol)
            return False
        if sellable <= 0:
            self.db.risk_event('EXIT_NOTHING_TO_SELL', 'HIGH',
                               f"{symbol} pos {pos['id']} رصيد {free}", symbol)
            self.db.close_position(pos['id'], realized_pnl=0.0)
            return True

        try:
            res = self.gate.execute(
                order_type=order_type, symbol=symbol, side='SELL',
                position_id=pos['id'], qty=sellable,
                send=lambda cid: self.client.market_sell(symbol, sellable, cid))
        except DuplicateOrderError as e:
            self.db.risk_event('DUPLICATE_EXIT_BLOCKED', 'HIGH',
                               f"{symbol} pos {pos['id']}: {e}", symbol)
            return False
        if not res['ok']:
            if self.health: self.health.record_order_failure(str(res['error']))
            self.db.risk_event(f'{order_type}_EXIT_FAILED',
                               'CRITICAL' if res['state'] == UNKNOWN else 'HIGH',
                               f"{symbol} [{res['state']}]: {res['error']}", symbol)
            return False
        self._record_exit(pos, symbol, res['response'] or {}, reason,
                          client_order_id=res['client_order_id'])
        return True

    def _ensure_stop_gone(self, symbol: str, pos: Dict):
        """
        يُرجع (ok, note, st). ok=False يعني: لا يجوز البيع الآن.
        note='STOP_FILLED' يعني: الوقف خرج بالمركز فعلاً — `st` عندها
        استجابة حالة الأمر الكاملة (تُستخدَم مباشرة في `_record_exit()`
        بدل استعلام إضافي مكرَّر داخل `_settle_after_stop()` القديمة).
        """
        sid = pos.get('stop_order_id')
        try:
            st = self.client.order_status(symbol, sid)
        except BinanceError as e:
            from .errors import classify, ErrorClass
            if classify(e).cls is ErrorClass.NOT_FOUND:
                return True, 'STOP_ABSENT', None
            return False, f'تعذّر قراءة حالة الوقف: {redact(str(e))[:120]}', None

        status = (st.get('status') or '').upper()
        if status == 'FILLED':
            return True, 'STOP_FILLED', st
        if status in ('CANCELED', 'EXPIRED', 'REJECTED'):
            return True, 'STOP_GONE', None
        try:
            self.client.cancel(symbol, sid)
        except BinanceError as e:
            from .errors import classify, ErrorClass
            if classify(e).cls is ErrorClass.NOT_FOUND:
                return True, 'STOP_ABSENT', None
            return False, f'فشل الإلغاء: {redact(str(e))[:120]}', None
        # تأكيد الزوال بعد الإلغاء
        try:
            st2 = self.client.order_status(symbol, sid)
            s2 = (st2.get('status') or '').upper()
            if s2 == 'FILLED':
                return True, 'STOP_FILLED', st2
            if s2 in ('CANCELED', 'EXPIRED', 'REJECTED'):
                return True, 'STOP_GONE', None
            return False, f'الوقف ما زال {s2}', None
        except BinanceError:
            return True, 'STOP_GONE', None

    def _place_oco(self, symbol: str, qty: float, stop: float, target: float,
                   pos_id: int, version: Optional[int] = None):
        """
        يضع OCO (هدف + وقف) عبر `POST /api/v3/orderList/oco` — الآلية
        الحالية المدعومة (V10). يُرجع (stop_order_id, target_order_id)
        — معرّفا الأمرين الفرعيين **الحقيقيين والمنفصلين**.

        **لا اعتماد على تخمين معرّفات الأبناء**: نُرسل
        `above_client_id`/`below_client_id` صراحةً للمنصة، ونتحقق أن
        ما أعادته يطابقهما فعلياً عبر `parse_oco_response()` الصارم —
        لا نفترض نجاحاً لمجرد أن الاستجابة غير فارغة.
        """
        if not hasattr(self.client, 'oco_sell'):
            return None, None
        v = version if version is not None else self._next_stop_version(pos_id, False)
        if v is None:
            return None, None
        limit = stop * (1 - self.stop_limit_offset_pct / 100)
        above_cid_holder: Dict[str, str] = {}

        def _send(cid: str):
            above_cid_holder['below'] = f'{cid}-STOP'
            above_cid_holder['above'] = f'{cid}-TARGET'
            return self.client.oco_sell(
                symbol, qty, target, stop, limit, cid,
                below_client_id=above_cid_holder['below'],
                above_client_id=above_cid_holder['above'])

        try:
            res = self.gate.execute(
                order_type='STOP', symbol=symbol, side='SELL',
                position_id=pos_id, version=v, qty=qty, stop_price=stop,
                limit_price=target, send=_send)
        except DuplicateOrderError:
            return None, None
        if not res['ok']:
            self.db.risk_event('OCO_PLACEMENT_FAILED', 'HIGH',
                               f"{symbol} pos {pos_id}: {res['error']}", symbol)
            return None, None

        oco = parse_oco_response(res['response'] or {}, expected_symbol=symbol)
        if not oco.valid or not oco.fully_protected:
            # استجابة مشوَّهة أو حماية غير مؤكَّدة — فشل صريح، لا تخمين
            # ولا نجاح مصطنع (البندان 16/19 المطلقان في متطلبات V10).
            self.db.risk_event(
                'OCO_RESPONSE_MALFORMED', 'CRITICAL',
                f"{symbol} pos {pos_id}: {oco.error or 'حماية غير مكتملة'}", symbol)
            if self.health:
                self.health.engage_kill_switch(
                    f'استجابة OCO غير صالحة لمركز {pos_id} — الحماية غير مؤكَّدة: '
                    f'{oco.error}')
            return None, None

        self.db.update_position(
            pos_id, order_list_id=oco.order_list_id,
            list_client_order_id=oco.list_client_order_id,
            stop_order_id=oco.stop_order_id,
            stop_client_order_id=oco.stop_client_order_id,
            target_order_id=oco.target_order_id,
            target_client_order_id=oco.target_client_order_id)
        self.db.insert_order_if_absent(
            position_id=pos_id, exchange_order_id=oco.stop_order_id,
            client_order_id=oco.stop_client_order_id, symbol=symbol, side='SELL',
            type=oco.stop_type or 'STOP_LOSS_LIMIT', qty=qty, price=None,
            stop_price=stop, status=oco.stop_status or 'NEW',
            raw_response=oco.raw_response)
        self.db.insert_order_if_absent(
            position_id=pos_id, exchange_order_id=oco.target_order_id,
            client_order_id=oco.target_client_order_id, symbol=symbol, side='SELL',
            type=oco.target_type or 'LIMIT_MAKER', qty=qty, price=target,
            status=oco.target_status or 'NEW', raw_response=oco.raw_response)
        return oco.stop_order_id, oco.target_order_id

    # ═══ أمر الهدف (C1) ═══
    def place_target(self, symbol: str, qty: float, target: float,
                     pos_id: int, version: int = 1) -> Optional[str]:
        """
        أمر بيع محدَّد عند الهدف.

        في v6 كان take_profit يُخزَّن ولا يُرسل أمر إطلاقاً — فالمركز
        يخرج بالوقف فقط، بينما الباكتست يفترض خروجاً عند الهدف. هذه
        فجوة بين المحاكاة والتنفيذ، وهذه الدالة تسدّها.

        ملاحظة: بينانس Spot يدعم OCO، لكن أمر الهدف المنفصل يبقيه
        مستقلاً عن الوقف ويسهّل تتبّع كل نية على حدة.
        """
        rows = self.db.active_intents('TARGET', pos_id)
        if rows and not is_terminal(rows[0]['state']):
            return None       # هدف قائم بالفعل
        try:
            res = self.gate.execute(
                order_type='TARGET', symbol=symbol, side='SELL',
                position_id=pos_id, version=version, qty=qty,
                limit_price=target,
                send=lambda cid: self.client.limit_sell(symbol, qty, target, cid))
        except DuplicateOrderError:
            return None
        if not res['ok']:
            self.db.risk_event('TARGET_PLACEMENT_FAILED', 'HIGH',
                               f"{symbol} pos {pos_id}: {res['error']}", symbol)
            return None
        r = res['response'] or {}
        tid = str(r.get('orderId'))
        self.db.update_position(pos_id, target_order_id=tid)
        self.db.insert_order_if_absent(
            position_id=pos_id, exchange_order_id=tid,
            client_order_id=res['client_order_id'], symbol=symbol, side='SELL',
            type='LIMIT', qty=qty, price=target,
            status=r.get('status', 'NEW'), raw_response=r)
        return tid

    def _emergency_exit(self, pos: Dict, symbol: str, qty: float) -> bool:
        return self._exit_via_gate(pos, symbol, qty, 'EMERGENCY', 'EMERGENCY_EXIT')

    def close_long(self, pos: Dict, reason: str = 'SIGNAL_EXIT') -> bool:
        return self._exit_via_gate(pos, pos['symbol'], pos['qty'], 'EXIT', reason)

    def _record_exit(self, pos: Dict, symbol: str, r: Dict, reason: str,
                     client_order_id: Optional[str] = None):
        """
        يُسجِّل خروجاً — كاملاً أو جزئياً. كانت هذه الدالة تُغلق المركز
        بالكامل دائماً بصرف النظر عن الكمية المُنفَّذة الفعلية —
        القسمان D/G من متطلبات V11: تنفيذ جزئي على طرف OCO (STOP_LOSS_LIMIT
        يستقر كأمر Limit حي، قابل للتنفيذ الجزئي فعلياً على أي منصة
        حقيقية، بصرف النظر عن أي إعداد محاكاة محلي) كان سيُغلق المركز
        بكمية أصغر من الحقيقية، تاركاً الباقي **بلا حماية وبلا تتبّع
        إطلاقاً** (الأخ أُلغي فور أي تنفيذ، جزئياً كان أم كاملاً).
        """
        _b, _q = self._symbol_assets(symbol)
        norm = self.normalize_response(r, fallback_price=pos.get('stop_loss') or 0.0,
                                       base_asset=_b, quote_asset=_q)
        qty, avg, fee = norm['qty'], norm['avg_price'], norm['fee']
        fills = norm['fills']
        if norm.get('fee_uncertain'):
            self.db.risk_event(
                'FEE_ASSET_UNCONVERTIBLE', 'WARNING',
                f"{symbol} خروج: عمولة بأصل ثالث {norm.get('fee_by_asset')} — "
                f"مستبعَدة من التكلفة، لا تُخمَّن", symbol)
        if qty <= 0 or avg <= 0:
            # لا نسجّل خروجاً بأرقام غير موثوقة — تُنتج PnL وهمياً
            self.db.risk_event('EXIT_UNRELIABLE_FILL', 'CRITICAL',
                               f"{symbol} pos {pos['id']}: qty={qty} avg={avg}", symbol)
            if self.health:
                self.health.engage_kill_switch(
                    f"بيانات خروج غير موثوقة (pos {pos['id']})")
            return

        pos_qty = float(pos['qty'])
        already_sold = float(pos.get('sold_qty') or 0.0)
        remaining_before = pos_qty - already_sold
        if qty > remaining_before + 1e-9:
            # كمية مُنفَّذة أكبر من المتبقي فعلياً من المركز — بيانات
            # غير متّسقة، لا نتجاهلها بصمت ولا نفترض تصحيحاً تلقائياً
            self.db.risk_event(
                'EXIT_QTY_EXCEEDS_POSITION', 'CRITICAL',
                f"{symbol} pos {pos['id']}: نُفِّذ {qty} لكن المتبقي محلياً "
                f"{remaining_before} فقط", symbol)
            if self.health:
                self.health.engage_kill_switch(
                    f"كمية خروج تتجاوز المركز {pos['id']} — تحقَّق يدوياً")
            qty = remaining_before   # الحد الآمن الأقصى، لا الرقم الخام غير المتّسق

        # ⚠️ إصلاح حرِج (تدقيق ما بعد V11): كانت تطرح رسم الخروج وحده،
        # ورسم الدخول (المُخزَّن في `positions.fees` منذ `open_long`) لا
        # يدخل الحساب إطلاقاً — فكل PnL محقَّق مُبالَغ فيه بمقدار عمولة
        # الدخول. الأخطر أنه انحراف عن الباكتست نفسه
        # (`backtest/engine.py`: `pnl = gross - pos.entry_fee - fill.fee`)،
        # أي أن التشغيل الورقي — الموجود أصلاً ليتحقّق من الباكتست —
        # كان سيُظهر نتائج أفضل منه لنفس الصفقات بالضبط، فيُقرأ الفارق
        # كإشارة سوق وهو خطأ حسابي. برسوم 0.1% لكل جهة، نصف تكلفة
        # التداول كانت مخفيّة عن كل تقرير.
        #
        # رسم الدخول يُطرَح مرة واحدة فقط — في أول شريحة خروج — وإلا
        # تكرَّر طرحه في كل شريحة من خروج جزئي متعدد المراحل.
        entry_fee_unbilled = (float(pos.get('fees') or 0.0)
                              if float(pos.get('sold_qty') or 0.0) <= 0 else 0.0)
        pnl_chunk = (avg - pos['entry_price']) * qty - fee - entry_fee_unbilled
        ins = self.db.insert_order_if_absent(
            position_id=pos['id'], exchange_order_id=str(r.get('orderId')),
            client_order_id=client_order_id, symbol=symbol, side='SELL',
            type='MARKET', qty=qty, price=avg, filled_qty=qty,
            status=r.get('status', 'FILLED'), raw_response=r)
        oid = (ins['order'] or {}).get('id')
        for f in fills:
            self.db.record_fill_if_absent(
                order_id=oid, exchange_trade_id=(str(f['tradeId'])
                                                 if f.get('tradeId') else None),
                symbol=symbol, qty=float(f['qty']), price=float(f['price']),
                commission=float(f.get('commission', 0) or 0),
                commission_asset=f.get('commissionAsset'))

        new_sold = already_sold + qty
        prior_pnl = float(pos.get('realized_pnl') or 0.0)
        prior_fees = float(pos.get('fees') or 0.0)
        total_pnl = prior_pnl + pnl_chunk
        total_fees = prior_fees + fee

        if new_sold >= pos_qty - 1e-9:
            # خروج كامل (أو آخر شريحة من خروج جزئي متعدد المراحل)
            self.db.close_position(pos['id'], realized_pnl=total_pnl,
                                   fees=total_fees, exit_order_id=str(r.get('orderId')))
            # close_position() لا تُحدِّث sold_qty (عمود منفصل عمداً
            # للمسار الجزئي) — في خروج متعدد المراحل، يجب أن يعكس
            # الإغلاق النهائي الكمية الكاملة المُباعة فعلياً، لا القيمة
            # الجزئية من مرحلة سابقة فقط.
            self.db.update_position(pos['id'], sold_qty=new_sold)
            if pos.get('recommendation_id'):
                self.db.close_recommendation(
                    pos['recommendation_id'],
                    outcome='WIN' if total_pnl > 0 else 'LOSS',
                    exit_price=avg, exit_reason=reason, pnl=total_pnl,
                    pnl_pct=total_pnl / max(pos['entry_price'] * pos_qty, 1e-9) * 100)
        else:
            # خروج جزئي — القسم D/G: لا يُغلَق المركز، والأخ أُلغي فوراً
            # (سلوك OCO الحقيقي عند أي تنفيذ لأي طرف) فالمتبقي أصبح
            # بلا حماية الآن — لا إعادة حماية تلقائية مبنية بعد، فنمنع
            # تداولاً جديداً ونُعلمها صراحةً بدل الاستمرار كأن شيئاً لم
            # يحدث (البند 7 المطلق: "If protection cannot be proven,
            # the position is NOT protected and new trading must be
            # blocked").
            self.db.update_position(
                pos['id'], sold_qty=new_sold, realized_pnl=total_pnl,
                fees=total_fees, stop_order_id=None, target_order_id=None,
                order_list_id=None, list_client_order_id=None,
                stop_client_order_id=None, target_client_order_id=None)
            self.db.risk_event(
                'PARTIAL_EXIT_UNPROTECTED_REMAINDER', 'CRITICAL',
                f"{symbol} pos {pos['id']}: بيع {qty} من {pos_qty} — "
                f"المتبقي {pos_qty - new_sold} بلا حماية بعد إلغاء الأخ", symbol)
            if self.health:
                self.health.engage_kill_switch(
                    f"خروج جزئي لمركز {pos['id']} — الباقي غير محمي، يلزم تدخل يدوي")
            return   # لا تحديث RiskGuard/التوصية — المركز لا يزال مفتوحاً فعلياً

        if self.risk_guard is not None:
            # trade_id ثابت لهذا الخروج بالذات — يمنع تحديث العداد
            # مرتين لو أُعيد استدعاء نفس الحدث (إعادة تشغيل، تكرار fill)
            tid = client_order_id or f"exit-{pos['id']}-{r.get('orderId')}"
            self.risk_guard.record_trade(total_pnl, trade_id=tid, symbol=symbol)

    def _settle_after_stop(self, pos: Dict, symbol: str):
        """
        ⚠️ لم تعد تُستدعى من أي مسار نشط (تدقيق V11) — كانت لا تزال
        تُستدعى من `_exit_via_gate()` رغم تعليق سابق أدّعى خلاف ذلك
        (خطأ صححته بنفسي بعد فحص أدق)؛ استُبدلت هناك أيضاً الآن بمسار
        موحَّد عبر `_record_exit()` مباشرة، يرث حماية التنفيذ الجزئي
        وفحص تجاوز الكمية. أُبقيت بلا حذف تحسّباً لاعتماد خارجي محتمل،
        لكنها غير نشطة في أي مسار حالي — تحقَّقت بالبحث الشامل.
        أمر الوقف نُفِّذ على المنصة — نسجّل النتيجة من بيانات المنصة.
        """
        try:
            st = self.client.order_status(symbol, pos['stop_order_id'])
            qty = float(st.get('executedQty', pos['qty']))
            quote = float(st.get('cummulativeQuoteQty', 0))
            avg = quote / qty if qty > 0 else float(st.get('price', 0))
        except BinanceError:
            qty, avg = pos['qty'], pos['stop_loss']
        pnl = (avg - pos['entry_price']) * qty - (pos.get('fees') or 0)
        self.db.close_position(pos['id'], realized_pnl=pnl,
                               fees=pos.get('fees') or 0,
                               exit_order_id=str(pos.get('stop_order_id')))
        if pos.get('recommendation_id'):
            self.db.close_recommendation(
                pos['recommendation_id'], outcome='WIN' if pnl > 0 else 'LOSS',
                exit_price=avg, exit_reason='STOP_LOSS', pnl=pnl,
                pnl_pct=pnl / max(pos['entry_price'] * qty, 1e-9) * 100)
        if self.risk_guard is not None:
            tid = f"exit-{pos['id']}-{pos.get('stop_order_id')}"
            self.risk_guard.record_trade(pnl, trade_id=tid, symbol=symbol)
