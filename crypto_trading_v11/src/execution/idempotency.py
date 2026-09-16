"""
الحتمية ومنع تكرار الأوامر
===========================
المبدأ: لكل **نيّة تداول** معرّف واحد ثابت، يُشتق من هوية النية نفسها
لا من العشوائية. إعادة المحاولة تحمل المعرّف ذاته، فترفضه بينانس
كمكرر بدل أن تنفّذه مرتين.

آلة الحالة:

    RESERVED ──send()──> IN_FLIGHT ──ok──────> CONFIRMED
        │                    │
        │                    ├──rejected────>  REJECTED
        │                    └──timeout─────>  UNKNOWN ──recover()──> CONFIRMED/FAILED
        └──abort()──────────────────────────>  ABORTED

القاعدة الحاكمة: النيّة تُحفَظ في القاعدة **قبل** أي نداء شبكة.
لو انهار البرنامج بين الحفظ والرد، النيّة موجودة بحالة IN_FLIGHT،
ويسترجعها `recover_in_flight()` عند الإقلاع بسؤال بينانس عن المعرّف.
"""
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Callable

# قيد بينانس على newClientOrderId
CID_PATTERN = re.compile(r'^[A-Za-z0-9_:.\-]{1,36}$')
CID_MAX = 36

from .order_state import (
    RESERVED, IN_FLIGHT, UNKNOWN, CONFIRMED, OPEN, PARTIALLY_FILLED, FILLED,
    CANCEL_REQUESTED, CANCELED, REJECTED, EXPIRED, ABORTED, MANUAL,
    TERMINAL, BLOCKING, LIVE_ON_EXCHANGE, from_exchange_status,
    can_transition, is_terminal, is_live)
from .errors import classify, ErrorClass, redact

FAILED = ABORTED   # توافق خلفي: الفشل النهائي بلا أمر = ABORTED

MAX_ATTEMPTS = 3

# اختصارات نوع الأمر — تبقى ثابتة عبر النسخ
TYPE_CODES = {
    'ENTRY': 'en', 'STOP': 'sl', 'TARGET': 'tp', 'EXIT': 'ex',
    'EMERGENCY_EXIT': 'em', 'EMERGENCY': 'em',
    'STOP_REPLACEMENT': 'sr', 'CANCEL': 'cx',
}


class DuplicateOrderError(Exception):
    """نيّة بنفس المعرّف نُفِّذت أو ما زالت جارية."""
    def __init__(self, msg, intent: Optional[Dict] = None):
        super().__init__(msg)
        self.intent = intent


def build_client_order_id(order_type: str, symbol: str,
                          recommendation_id: Optional[int] = None,
                          position_id: Optional[int] = None,
                          version: int = 1) -> str:
    """
    ينتج معرّفاً ثابتاً لنفس نية الأمر.

    نفس المدخلات ⇒ نفس المخرج دائماً، عبر إعادة التشغيل والعمليات.
    مدخلات مختلفة ⇒ معرّف مختلف (تصادم عملياً مستحيل).

    `version` يُزاد عمداً حين تكون إعادة المحاولة **مقصودة** — مثل
    إعادة وضع وقف بعد التأكد من إلغاء السابق. تركه كما هو يعني
    "نفس النية"، فيُرفض التكرار.

    Args:
        order_type: ENTRY | STOP | EXIT | EMERGENCY | CANCEL
        symbol: زوج التداول
        recommendation_id: معرّف التوصية (لأوامر الدخول)
        position_id: معرّف المركز (لأوامر الخروج والوقف)
        version: رقم المحاولة المقصودة

    Returns:
        معرّف ≤36 محرفاً مطابق لقيود بينانس
    """
    t = order_type.upper().strip()
    if t not in TYPE_CODES:
        raise ValueError(f"نوع أمر غير معروف: {order_type}")
    if recommendation_id is None and position_id is None:
        raise ValueError("يلزم recommendation_id أو position_id لتحديد النية")
    if version < 1:
        raise ValueError("version يبدأ من 1")

    code = TYPE_CODES[t]
    sym = re.sub(r'[^A-Za-z0-9]', '', symbol.upper())[:8]
    ref = f"r{recommendation_id}" if recommendation_id is not None else f"p{position_id}"

    cid = f"{code}-{sym}-{ref}-v{version}"

    # التقصير عند الحاجة يحافظ على الحتمية عبر بصمة المدخلات الكاملة
    if len(cid) > CID_MAX:
        seed = f"{t}|{symbol}|{recommendation_id}|{position_id}|{version}"
        h = hashlib.sha256(seed.encode()).hexdigest()[:12]
        cid = f"{code}-{h}-v{version}"[:CID_MAX]

    if not CID_PATTERN.match(cid):
        raise ValueError(f"معرّف غير صالح لبينانس: {cid}")
    return cid


def validate_numeric(name: str, v: Optional[float],
                     allow_none: bool = True) -> Optional[float]:
    """
    يرفض NaN و inf والقيم غير الموجبة.
    كمية صفرية أو سالبة تصل للمنصة تُنتج رفضاً غامضاً أو — أسوأ —
    سلوكاً غير متوقع. الرفض المبكر أوضح.
    """
    import math
    if v is None:
        if allow_none:
            return None
        raise ValueError(f"{name} مطلوب")
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        raise ValueError(f"{name} قيمة غير صالحة: {v}")
    if f <= 0:
        raise ValueError(f"{name} يجب أن يكون موجباً: {v}")
    return f


def intent_fingerprint(order_type: str, symbol: str, side: str,
                       qty: Optional[float] = None,
                       quote_amount: Optional[float] = None,
                       stop_price: Optional[float] = None,
                       limit_price: Optional[float] = None) -> str:
    """
    بصمة معاملات الأمر. تُقارَن عند إعادة المحاولة: لو تغيّرت المعاملات
    فهذه نيّة مختلفة، ويجب رفعها بـ version لا إعادة إرسالها بنفس المعرّف.
    """
    def _fmt(x):
        import math
        if x is None:
            return '-'
        f = float(x)
        if math.isnan(f):
            return 'nan'
        if math.isinf(f):
            return 'inf' if f > 0 else '-inf'
        return f"{f:.10f}"

    parts = [order_type.upper(), symbol.upper(), side.upper(),
             _fmt(qty),
             _fmt(quote_amount), _fmt(stop_price), _fmt(limit_price)]
    return hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]


@dataclass
class Intent:
    id: int
    client_order_id: str
    order_type: str
    symbol: str
    side: str
    state: str
    fingerprint: str
    recommendation_id: Optional[int] = None
    position_id: Optional[int] = None
    version: int = 1
    exchange_order_id: Optional[str] = None
    attempts: int = 0
    created_ts: int = 0
    updated_ts: int = 0
    detail: str = ''
    payload_hash: str = ''
    full_identity: str = ''
    requested_qty: Optional[float] = None
    filled_qty: float = 0.0
    remaining_qty: Optional[float] = None
    last_error: str = ''
    last_error_class: str = ''

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @staticmethod
    def from_row(r: Dict) -> 'Intent':
        return Intent(
            id=r['id'], client_order_id=r['client_order_id'],
            order_type=r['order_type'], symbol=r['symbol'], side=r['side'],
            state=r['state'], fingerprint=r['fingerprint'],
            recommendation_id=r.get('recommendation_id'),
            position_id=r.get('position_id'), version=r.get('version', 1),
            exchange_order_id=r.get('exchange_order_id'),
            attempts=r.get('attempts', 0),
            created_ts=r.get('created_ts', 0), updated_ts=r.get('updated_ts', 0),
            detail=r.get('detail') or '',
            payload_hash=r.get('payload_hash') or r.get('fingerprint') or '',
            full_identity=r.get('full_identity') or '',
            requested_qty=r.get('requested_qty'),
            filled_qty=r.get('filled_qty') or 0.0,
            remaining_qty=r.get('remaining_qty'),
            last_error=r.get('last_error') or '',
            last_error_class=r.get('last_error_class') or '')


class IdempotentOrderGate:
    """
    البوابة الوحيدة لكل أوامر التداول — v7.

    التسلسل الإلزامي (المرحلة الخامسة):
      1. معرّف حتمي
      2. payload_hash
      3. تسجيل RESERVED محلياً  ← قبل أي نداء شبكة
      4. IN_FLIGHT
      5. إرسال بنفس المعرّف
      6. نجاح ⇒ ربط exchange_order_id وتحديث الحالة من حالة المنصة
      7. مهلة ⇒ UNKNOWN
      8. استعلام بـ origClientOrderId
      9. وُجد ⇒ ربط، ولا نسخة ثانية
     10. لم يوجد ⇒ إعادة بنفس المعرّف فقط (ضمن MAX_ATTEMPTS)
     11. بقي ملتبساً ⇒ ERROR_REQUIRES_MANUAL_REVIEW وتجميد التداول
    """

    def __init__(self, db, client, health=None, max_attempts: int = MAX_ATTEMPTS,
                 sleep_fn=None):
        self.db = db
        self.client = client
        self.health = health
        self.max_attempts = max_attempts
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep

    # ── قراءة ──
    def get(self, cid: str) -> Optional[Intent]:
        r = self.db.get_intent_by_client_order_id(cid)
        return Intent.from_row(r) if r else None

    def _transition(self, cid: str, to: str, reason: str = '', **fields) -> bool:
        res = self.db.transition_order_state(
            cid, to, reason=reason[:300], actor='gate', **fields)
        if not res['ok'] and res.get('error'):
            self.db.risk_event('ILLEGAL_TRANSITION', 'HIGH',
                               f"{cid}: {res['error']}")
        return res['ok']

    # ── حجز النية ──
    def reserve(self, *, order_type: str, symbol: str, side: str,
                recommendation_id: Optional[int] = None,
                position_id: Optional[int] = None,
                version: int = 1, qty: Optional[float] = None,
                quote_amount: Optional[float] = None,
                stop_price: Optional[float] = None,
                limit_price: Optional[float] = None) -> Intent:
        # فحص القيم قبل أي حجز — لا نية بمعاملات غير صالحة
        qty = validate_numeric('qty', qty)
        quote_amount = validate_numeric('quote_amount', quote_amount)
        stop_price = validate_numeric('stop_price', stop_price)
        limit_price = validate_numeric('limit_price', limit_price)
        if qty is None and quote_amount is None:
            raise ValueError('يلزم qty أو quote_amount')

        cid = build_client_order_id(order_type, symbol, recommendation_id,
                                    position_id, version)
        ph = intent_fingerprint(order_type, symbol, side, qty, quote_amount,
                                stop_price, limit_price)
        identity = (f"{order_type.upper()}:"
                    f"{recommendation_id if recommendation_id is not None else position_id}"
                    f":{version}")

        existing = self.get(cid)
        if existing:
            if is_terminal(existing.state) or existing.state in (CONFIRMED, OPEN,
                                                                 PARTIALLY_FILLED):
                raise DuplicateOrderError(
                    f"النية {cid} بحالة {existing.state} — لا إعادة إرسال",
                    existing.__dict__)
            if existing.state == MANUAL:
                raise DuplicateOrderError(
                    f"النية {cid} تحتاج مراجعة يدوية", existing.__dict__)
            if existing.payload_hash and existing.payload_hash != ph:
                raise DuplicateOrderError(
                    f"النية {cid} موجودة بحمولة مختلفة — ارفع version",
                    existing.__dict__)
            return existing

        new_id = self.db.reserve_intent(
            client_order_id=cid, order_type=order_type.upper(), symbol=symbol,
            side=side.upper(), state=RESERVED, fingerprint=ph, payload_hash=ph,
            full_identity=identity, recommendation_id=recommendation_id,
            position_id=position_id, version=version,
            requested_qty=qty, remaining_qty=qty)
        if new_id == 0:
            other = self.get(cid)
            raise DuplicateOrderError(
                f"سباق: النية {cid} محجوزة",
                other.__dict__ if other else None)
        got = self.get(cid)
        assert got is not None
        return got

    # ── التنفيذ ──
    def execute(self, *, order_type: str, symbol: str, side: str,
                send: Callable[[str], Dict],
                recommendation_id: Optional[int] = None,
                position_id: Optional[int] = None,
                version: int = 1, qty: Optional[float] = None,
                quote_amount: Optional[float] = None,
                stop_price: Optional[float] = None,
                limit_price: Optional[float] = None) -> Dict[str, Any]:
        intent = self.reserve(order_type=order_type, symbol=symbol, side=side,
                              recommendation_id=recommendation_id,
                              position_id=position_id, version=version, qty=qty,
                              quote_amount=quote_amount, stop_price=stop_price,
                              limit_price=limit_price)
        cid = intent.client_order_id

        # نية معلّقة من محاولة سابقة: تُحسم بالاستعلام لا بالإرسال
        if intent.state in (IN_FLIGHT, UNKNOWN):
            rec = self.recover_one(intent)
            res = self._result(cid, rec['state'], rec.get('response'),
                               recovered=True, error=rec.get('error'))
            res['not_owner'] = True     # نية بدأها استدعاء آخر
            return res

        return self._send_loop(cid, symbol, send)

    def _send_loop(self, cid: str, symbol: str,
                   send: Callable[[str], Dict]) -> Dict[str, Any]:
        for attempt in range(1, self.max_attempts + 1):
            it = self.get(cid)
            if it is None:
                return self._result(cid, ABORTED, None, error='نية مفقودة')
            if not self._transition(cid, IN_FLIGHT,
                                    reason=f'attempt {attempt}',
                                    attempts=it.attempts + 1):
                # خسرنا السباق: عامل آخر يملك هذه النية الآن.
                # الحالة في القاعدة تخصّه لا تخصّنا — إرجاع ok=True هنا
                # يجعل المستدعي يسجّل مركزاً لأمر لم يرسله إطلاقاً.
                cur = self.get(cid)
                return {'ok': False, 'response': None, 'client_order_id': cid,
                        'state': cur.state if cur else UNKNOWN,
                        'recovered': False, 'needs_manual': False,
                        'error': 'النية يملكها عامل آخر — لم يُرسل من هنا',
                        'not_owner': True}
            try:
                resp = send(cid)
            except Exception as e:
                msg = redact(str(e))
                pol = classify(e)
                self.db.execute(
                    'UPDATE order_intents SET last_error=?, last_error_class=? '
                    'WHERE client_order_id=?', (msg[:400], pol.cls.value, cid))

                if pol.halt_system:
                    self._transition(cid, MANUAL, reason=f'{pol.cls.value}')
                    if self.health:
                        self.health.engage_kill_switch(
                            f'خطأ مصادقة: {pol.cls.value}')
                    return self._result(cid, MANUAL, None, error=msg)

                if pol.definitive_failure:
                    # يقين بعدم الإنشاء
                    target = (REJECTED if pol.cls in (
                        ErrorClass.RISK_REJECTED, ErrorClass.INVALID_PARAMS,
                        ErrorClass.INSUFFICIENT_BALANCE) else ABORTED)
                    self._transition(cid, target, reason=pol.cls.value)
                    if self.health:
                        self.health.record_order_failure(msg)
                    return self._result(cid, target, None, error=msg)

                # ملتبس: UNKNOWN ثم استعلام
                self._transition(cid, UNKNOWN, reason=pol.cls.value)
                self.db.risk_event('ORDER_UNKNOWN_STATE', 'CRITICAL',
                                   f"{cid} [{pol.cls.value}]: {msg[:200]}", symbol)
                rec = self.recover_one(self.get(cid))

                if rec['state'] in LIVE_ON_EXCHANGE or rec['state'] == FILLED:
                    # المنصة رفضت بـ Duplicate ⇒ الأمر موجود لكن أرسله عامل
                    # آخر. الأمر مؤكَّد (ok=True) لكننا لسنا مالكيه، فلا يجوز
                    # للمستدعي أن يسجّل مركزاً بناءً على هذه النتيجة.
                    res = self._result(cid, rec['state'], rec.get('response'),
                                       recovered=True)
                    if pol.cls is ErrorClass.DUPLICATE:
                        res['not_owner'] = True
                    return res
                if rec['state'] in (CANCELED, REJECTED, EXPIRED):
                    return self._result(cid, rec['state'], None, recovered=True,
                                        error=msg)
                if rec['state'] == ABORTED and pol.retryable and attempt < self.max_attempts:
                    # ثبت أن الأمر لم يصل: إعادة بنفس المعرّف
                    if pol.backoff_s:
                        self._sleep(pol.backoff_s * attempt)
                    self.db.system_event('ORDER_RETRY',
                                         f'{cid} attempt {attempt+1} same id')
                    continue
                if rec['state'] == ABORTED:
                    return self._result(cid, ABORTED, None, recovered=True, error=msg)

                # بقي ملتبساً
                self._transition(cid, MANUAL, reason='تعذّر حسم الحالة')
                if self.health:
                    self.health.engage_kill_switch(f'نية غير محسومة: {cid}')
                return self._result(cid, MANUAL, None, recovered=True, error=msg)

            # نجاح
            state = from_exchange_status(resp.get('status', '')) or CONFIRMED
            self._apply_response(cid, state, resp)
            if self.health:
                self.health.record_order_success()
            return self._result(cid, state, resp)

        self._transition(cid, MANUAL, reason='تجاوز عدد المحاولات')
        if self.health:
            self.health.engage_kill_switch(f'تجاوز المحاولات: {cid}')
        return self._result(cid, MANUAL, None, error='تجاوز عدد المحاولات')

    def _apply_response(self, cid: str, state: str, resp: Dict):
        filled = float(resp.get('executedQty', 0) or 0)
        req = float(resp.get('origQty', 0) or 0)
        self._transition(cid, state, reason='exchange response',
                         exchange_order_id=str(resp.get('orderId', '')),
                         filled_qty=filled,
                         remaining_qty=max(req - filled, 0.0) if req else None,
                         detail=str(resp.get('status', ''))[:100])

    @staticmethod
    def _result(cid, state, response, recovered=False, error=None) -> Dict[str, Any]:
        ok = state in (CONFIRMED, OPEN, PARTIALLY_FILLED, FILLED)
        return {'ok': ok, 'response': response, 'client_order_id': cid,
                'state': state, 'recovered': recovered, 'error': error,
                'needs_manual': state == MANUAL}

    # ── التعافي ──
    def recover_one(self, intent: Optional[Intent]) -> Dict[str, Any]:
        """يسأل المنصة بـ origClientOrderId ويحسم الحالة."""
        if intent is None:
            return {'state': ABORTED, 'error': 'نية غير موجودة'}
        cid = intent.client_order_id
        try:
            resp = self.client.order_by_client_id(intent.symbol, cid)
        except Exception as e:
            pol = classify(e)
            if pol.cls is ErrorClass.NOT_FOUND:
                # القسم D من متطلبات V11: "query OCO list after uncertain
                # acceptance". معرّف نية OCO يُرسَل كـ listClientOrderId
                # لا كـ clientOrderId لأي أمر مفرد — /api/v3/order يرفض
                # بـ NOT_FOUND دائماً لـ OCO ناجحة فعلياً، حتى لو كانت
                # تحمي المركز فعلياً على المنصة هذه اللحظة. بلا هذا
                # الفحص، OCO حقيقية كانت ستُصنَّف ABORTED خطأً، فيُعاد
                # إرسالها من send_loop عند إعادة المحاولة — OCO مكرَّرة
                # فعلياً على حساب حقيقي.
                oco_state = self._try_recover_oco(intent, cid)
                if oco_state is not None:
                    return oco_state
                # يقين: لم يصل الأمر للمنصة (لا كأمر مفرد، ولا كقائمة OCO)
                self._transition(cid, ABORTED, reason='غير موجود على المنصة')
                return {'state': ABORTED, 'error': 'الأمر غير موجود'}
            if pol.halt_system:
                self._transition(cid, MANUAL, reason='مصادقة أثناء التعافي')
                if self.health:
                    self.health.engage_kill_switch('مصادقة أثناء التعافي')
                return {'state': MANUAL, 'error': redact(str(e))}
            self.db.risk_event('RECOVERY_FAILED', 'CRITICAL',
                               f"{cid}: {redact(str(e))[:200]}", intent.symbol)
            return {'state': intent.state, 'error': redact(str(e))}

        state = from_exchange_status(resp.get('status', ''))
        if state is None:
            return {'state': intent.state, 'response': resp,
                    'error': f"حالة غير معروفة: {resp.get('status')}"}
        self._apply_response(cid, state, resp)
        self.db.system_event('ORDER_RECOVERED', f"{cid} -> {state}")
        return {'state': state, 'response': resp}

    def _try_recover_oco(self, intent: Intent, cid: str) -> Optional[Dict[str, Any]]:
        """
        يُستدعى فقط عندما ترفض /api/v3/order بـ NOT_FOUND — يُجرِّب
        /api/v3/orderList عبر listClientOrderId قبل الحكم بعدم الوصول.
        يُعيد None إن لم توجد قائمة فعلاً (فيتابع recover_one حكمه
        الأصلي بـ ABORTED بأمان).
        """
        if not hasattr(self.client, 'query_oco'):
            return None
        try:
            resp = self.client.query_oco(list_client_order_id=cid)
        except Exception:
            return None
        if not resp or resp.get('listClientOrderId') != cid:
            return None

        list_status = resp.get('listOrderStatus', '')

        # ⚠️ إصلاح (تدقيق ما بعد V11): كان `ALL_DONE` يُترجَم FILLED
        # مباشرة بافتراض "أحد الطرفين نُفِّذ والآخر أُلغي تلقائياً".
        # هذا خطأ: `ALL_DONE` في بينانس تعني فقط أن القائمة لم تعد
        # نشطة — وهي تشمل حالة **إلغاء الطرفين معاً** (الحماية أُزيلت
        # يدوياً أو انتهت صلاحيتها) والمركز لا يزال مفتوحاً تماماً.
        #
        # الأثر قبل الإصلاح: تُسجَّل النية FILLED — أي "تنفيذ وقع"
        # ولم يقع. و`_send_loop` يعامل FILLED كنجاح (`ok=True`)، فيتابع
        # `_place_oco` ويُخزِّن معرّفَي وقف وهدف **مُلغيين** على المركز،
        # فيبدو محمياً في القاعدة وهو مكشوف تماماً على المنصة. سجل
        # التدقيق يقول إن المركز خرج وهو مفتوح.
        #
        # (تصحيح لادّعاء سابق: هذا **لا** يمنع إعادة الحماية للأبد —
        # `active_intents()` تستبعد الحالات النهائية، فتُصدِر
        # `_next_stop_version()` نسخة جديدة، و`guard_stops()` تُعيد وضع
        # وقف في دورة لاحقة. الضرر في زيف السجل وفي نافذة الانكشاف،
        # لا في استحالة التعافي.)
        #
        # الحسم الصحيح من حالات الأطراف نفسها (`orderReports`)، لا من
        # حالة القائمة المجمَّعة. لا نستنتج تنفيذاً لم يقع.
        reports = resp.get('orderReports') or resp.get('orders') or []
        leg_states = [str(o.get('status', '')).upper() for o in reports
                      if isinstance(o, dict) and o.get('status')]
        any_filled = any(st in ('FILLED', 'PARTIALLY_FILLED') for st in leg_states)
        all_gone = bool(leg_states) and all(
            st in ('CANCELED', 'EXPIRED', 'REJECTED') for st in leg_states)

        if any_filled:
            state = FILLED               # طرف نُفِّذ فعلاً — المركز خرج
        elif all_gone:
            # الحماية زالت ولم يُنفَّذ شيء — المركز (إن كان مفتوحاً) بلا
            # حماية الآن. حالة غير نهائية عمداً كي تستطيع
            # `_next_stop_version()` إصدار نسخة جديدة وإعادة الحماية.
            state = CANCELED
            self.db.risk_event(
                'OCO_CANCELED_NOT_FILLED', 'CRITICAL',
                f"{cid}: طرفا OCO أُلغيا بلا تنفيذ — الحماية زالت "
                f"({leg_states})", intent.symbol)
        elif not leg_states:
            # لا تفاصيل أطراف: لا نُخمِّن تنفيذاً. نعتمد حالة القائمة
            # للنشاط فقط، وأي شيء آخر يبقى غير محسوم.
            if list_status in ('EXECUTING', ''):
                state = OPEN
            else:
                return None      # يتابع recover_one حكمه الأصلي بأمان
        elif list_status in ('EXECUTING', ''):
            state = OPEN                 # لا تزال نشطة وتحمي المركز
        else:
            state = CONFIRMED

        self._apply_response(cid, state, resp)
        self.db.system_event('OCO_RECOVERED_VIA_LIST',
                             f"{cid} -> {state} (listOrderStatus={list_status})")
        return {'state': state, 'response': resp}

    def recover_in_flight(self, stale_reserved_ms: int = 0) -> List[Dict]:
        """
        يُستدعى عند الإقلاع وبعد أي انقطاع.

        يشمل نوايا RESERVED القديمة: لم تُرسل قط، لكن معرّفها الحتمي
        يبقى محجوزاً ويمنع أي محاولة لاحقة. إلغاؤها آمن تماماً لأن
        لا أمر يقابلها على المنصة.
        """
        out = []
        pending = list(self.db.unresolved_intents())
        now = int(time.time() * 1000)
        for r in self.db.query(
                "SELECT * FROM order_intents WHERE state=? ORDER BY id",
                (RESERVED,)):
            if now - int(r.get('created_ts') or 0) >= stale_reserved_ms:
                pending.append(r)

        for r in pending:
            it = Intent.from_row(r)
            if it.state == RESERVED:
                self._transition(it.client_order_id, ABORTED,
                                 reason='محجوزة ولم تُرسل')
                out.append({'cid': it.client_order_id, 'state': ABORTED})
                continue
            if it.state == MANUAL:
                out.append({'cid': it.client_order_id, 'state': MANUAL,
                            'error': 'تحتاج تدخلاً يدوياً'})
                continue
            res = self.recover_one(it)
            if res['state'] in BLOCKING and res['state'] != MANUAL:
                self._transition(it.client_order_id, MANUAL,
                                 reason='تعذّر الحسم عند الإقلاع')
                res['state'] = MANUAL
            out.append({'cid': it.client_order_id, 'state': res['state'],
                        'error': res.get('error')})
        return out

    def has_unresolved(self) -> int:
        return len(self.db.unresolved_intents())

    def unresolved_detail(self) -> List[Dict]:
        return [{'cid': r['client_order_id'], 'state': r['state'],
                 'type': r['order_type'], 'symbol': r['symbol']}
                for r in self.db.unresolved_intents()]
