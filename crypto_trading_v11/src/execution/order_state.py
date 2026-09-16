"""
آلة حالات الأوامر — المرحلة الرابعة.
====================================
الانتقالات المسموحة معرَّفة صراحةً. أي انتقال خارجها يرمي
IllegalTransition — لا يُبتلَع ولا يُتجاهَل.

الحالات:
  RESERVED       حُجزت النية محلياً، لم تُرسل بعد
  IN_FLIGHT      أُرسلت، لا رد بعد
  UNKNOWN        انقطع الرد — الأمر قد يكون نُفّذ. ⛔ يمنع فتح صفقات
  CONFIRMED      المنصة أقرّت الأمر (ربط تم)
  OPEN           قائم على المنصة (NEW)
  PARTIALLY_FILLED
  FILLED
  CANCEL_REQUESTED
  CANCELED / REJECTED / EXPIRED / ABORTED
  ERROR_REQUIRES_MANUAL_REVIEW   ⛔ تجميد التداول

قواعد غير قابلة للخرق:
  • لا انتقال من FILLED إلى RESERVED
  • UNKNOWN لا يخرج إلا بالمصالحة، لا بإرسال جديد
  • الحالات النهائية لا تُغادَر
"""
from typing import Dict, Set, Optional, List

RESERVED = 'RESERVED'
IN_FLIGHT = 'IN_FLIGHT'
UNKNOWN = 'UNKNOWN'
CONFIRMED = 'CONFIRMED'
OPEN = 'OPEN'
PARTIALLY_FILLED = 'PARTIALLY_FILLED'
FILLED = 'FILLED'
CANCEL_REQUESTED = 'CANCEL_REQUESTED'
CANCELED = 'CANCELED'
REJECTED = 'REJECTED'
EXPIRED = 'EXPIRED'
ABORTED = 'ABORTED'
MANUAL = 'ERROR_REQUIRES_MANUAL_REVIEW'

ALL_STATES: Set[str] = {
    RESERVED, IN_FLIGHT, UNKNOWN, CONFIRMED, OPEN, PARTIALLY_FILLED, FILLED,
    CANCEL_REQUESTED, CANCELED, REJECTED, EXPIRED, ABORTED, MANUAL,
}

# حالات نهائية: لا انتقال منها إطلاقاً
TERMINAL: Set[str] = {FILLED, CANCELED, REJECTED, EXPIRED, ABORTED}

# حالات تمنع فتح صفقة جديدة
BLOCKING: Set[str] = {IN_FLIGHT, UNKNOWN, CANCEL_REQUESTED, MANUAL}

# حالات تعني وجود أمر حيّ على المنصة
LIVE_ON_EXCHANGE: Set[str] = {CONFIRMED, OPEN, PARTIALLY_FILLED, CANCEL_REQUESTED}

ALLOWED: Dict[str, Set[str]] = {
    RESERVED:         {IN_FLIGHT, ABORTED, MANUAL},
    IN_FLIGHT:        {CONFIRMED, OPEN, PARTIALLY_FILLED, FILLED,
                       REJECTED, EXPIRED, UNKNOWN, MANUAL},
    # UNKNOWN يُحسم بالمصالحة فقط — لا عودة إلى IN_FLIGHT بمعرّف جديد
    UNKNOWN:          {CONFIRMED, OPEN, PARTIALLY_FILLED, FILLED,
                       CANCELED, REJECTED, EXPIRED, ABORTED, MANUAL, IN_FLIGHT},
    CONFIRMED:        {OPEN, PARTIALLY_FILLED, FILLED, CANCEL_REQUESTED,
                       CANCELED, EXPIRED, UNKNOWN, MANUAL},
    OPEN:             {PARTIALLY_FILLED, FILLED, CANCEL_REQUESTED,
                       CANCELED, EXPIRED, UNKNOWN, MANUAL},
    PARTIALLY_FILLED: {PARTIALLY_FILLED, FILLED, CANCEL_REQUESTED,
                       CANCELED, EXPIRED, UNKNOWN, MANUAL},
    CANCEL_REQUESTED: {CANCELED, FILLED, PARTIALLY_FILLED, UNKNOWN, MANUAL},
    FILLED:           set(),
    CANCELED:         set(),
    REJECTED:         set(),
    EXPIRED:          set(),
    ABORTED:          set(),
    MANUAL:           {ABORTED, CANCELED, FILLED},   # حسم يدوي فقط
}

# حالة بينانس ← حالتنا
EXCHANGE_STATUS = {
    'NEW': OPEN,
    'PARTIALLY_FILLED': PARTIALLY_FILLED,
    'FILLED': FILLED,
    'CANCELED': CANCELED,
    'PENDING_CANCEL': CANCEL_REQUESTED,
    'REJECTED': REJECTED,
    'EXPIRED': EXPIRED,
    'EXPIRED_IN_MATCH': EXPIRED,
}


class IllegalTransition(Exception):
    def __init__(self, frm: str, to: str, reason: str = ''):
        super().__init__(f"انتقال غير مسموح: {frm} → {to}"
                         + (f" ({reason})" if reason else ""))
        self.frm, self.to = frm, to


def can_transition(frm: str, to: str) -> bool:
    if frm not in ALL_STATES or to not in ALL_STATES:
        return False
    if frm == to:
        return to in ALLOWED.get(frm, set())
    return to in ALLOWED.get(frm, set())


def assert_transition(frm: str, to: str, reason: str = ''):
    if not can_transition(frm, to):
        raise IllegalTransition(frm, to, reason)


def from_exchange_status(status: str) -> Optional[str]:
    return EXCHANGE_STATUS.get((status or '').upper())


def is_blocking(state: str) -> bool:
    return state in BLOCKING


def is_terminal(state: str) -> bool:
    return state in TERMINAL


def is_live(state: str) -> bool:
    return state in LIVE_ON_EXCHANGE


def unresolved_states() -> List[str]:
    """الحالات التي تمنع فتح صفقات وتستوجب حسماً."""
    return sorted(BLOCKING)
