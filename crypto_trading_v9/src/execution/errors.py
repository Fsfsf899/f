"""
تصنيف أخطاء المنصة — المرحلة الخامسة.
=====================================
القاعدة الحاكمة: **Timeout ليس دليلاً على أن الأمر لم يُنفَّذ.**

كل خطأ يُصنَّف إلى فئة تحدد ثلاثة أشياء:
  • هل يجوز إعادة المحاولة؟
  • هل يجب الاستعلام عن حالة الأمر أولاً؟
  • هل يوقف النظام؟

الخطأ غير المعروف يُعامَل كملتبس (يستوجب الاستعلام) لا كفشل.
هذا هو الافتراض الآمن الوحيد.
"""
from enum import Enum
from dataclasses import dataclass
from typing import Optional


class ErrorClass(Enum):
    TIMEOUT = 'TIMEOUT'                    # انقطاع/مهلة — قد يكون نُفّذ
    RATE_LIMIT = 'RATE_LIMIT'              # 429/418 — backoff بنفس المعرّف
    AUTH = 'AUTH'                          # مصادقة — يوقف النظام
    INVALID_PARAMS = 'INVALID_PARAMS'      # كمية/سعر — لا إعادة قبل التصحيح
    INSUFFICIENT_BALANCE = 'INSUFFICIENT_BALANCE'
    RISK_REJECTED = 'RISK_REJECTED'        # رفض منطق المخاطر
    NOT_FOUND = 'NOT_FOUND'                # -2013
    DUPLICATE = 'DUPLICATE'                # المعرّف مستخدم — دليل على وجود الأمر
    SERVER = 'SERVER'                      # 5xx — ملتبس
    UNKNOWN = 'UNKNOWN'                    # يُعامَل كملتبس


@dataclass(frozen=True)
class ErrorPolicy:
    cls: ErrorClass
    retryable: bool          # يجوز إرسال نفس المعرّف مجدداً
    must_query: bool         # يجب الاستعلام قبل أي قرار
    halt_system: bool        # يوقف النظام فوراً
    backoff_s: float = 0.0
    detail: str = ''

    @property
    def definitive_failure(self) -> bool:
        """الأمر لم يُنشأ قطعاً — يقين لا تخمين."""
        return not self.must_query and not self.retryable


POLICIES = {
    ErrorClass.TIMEOUT:              ErrorPolicy(ErrorClass.TIMEOUT, True, True, False, 1.0),
    ErrorClass.SERVER:               ErrorPolicy(ErrorClass.SERVER, True, True, False, 2.0),
    ErrorClass.UNKNOWN:              ErrorPolicy(ErrorClass.UNKNOWN, False, True, False, 1.0),
    ErrorClass.RATE_LIMIT:           ErrorPolicy(ErrorClass.RATE_LIMIT, True, True, False, 5.0),
    ErrorClass.AUTH:                 ErrorPolicy(ErrorClass.AUTH, False, False, True),
    ErrorClass.INVALID_PARAMS:       ErrorPolicy(ErrorClass.INVALID_PARAMS, False, False, False),
    ErrorClass.INSUFFICIENT_BALANCE: ErrorPolicy(ErrorClass.INSUFFICIENT_BALANCE, False, False, False),
    ErrorClass.RISK_REJECTED:        ErrorPolicy(ErrorClass.RISK_REJECTED, False, False, False),
    ErrorClass.NOT_FOUND:            ErrorPolicy(ErrorClass.NOT_FOUND, False, False, False),
    ErrorClass.DUPLICATE:            ErrorPolicy(ErrorClass.DUPLICATE, False, True, False),
}

# رموز بينانس ← الفئة
CODE_MAP = {
    -1000: ErrorClass.UNKNOWN,
    -1001: ErrorClass.SERVER,        # DISCONNECTED
    -1003: ErrorClass.RATE_LIMIT,    # TOO_MANY_REQUESTS
    -1006: ErrorClass.TIMEOUT,       # UNEXPECTED_RESP
    -1007: ErrorClass.TIMEOUT,       # TIMEOUT — الأمر قد يكون نُفّذ
    -1013: ErrorClass.INVALID_PARAMS,
    -1015: ErrorClass.RATE_LIMIT,
    -1021: ErrorClass.AUTH,          # timestamp خارج recvWindow
    -1022: ErrorClass.AUTH,          # توقيع غير صالح
    -1100: ErrorClass.INVALID_PARAMS,
    -1102: ErrorClass.INVALID_PARAMS,
    -1104: ErrorClass.INVALID_PARAMS,
    -1111: ErrorClass.INVALID_PARAMS,
    -1117: ErrorClass.INVALID_PARAMS,
    -1121: ErrorClass.INVALID_PARAMS,
    -2010: ErrorClass.RISK_REJECTED,  # NEW_ORDER_REJECTED — يُدقَّق بالنص
    -2011: ErrorClass.NOT_FOUND,      # CANCEL_REJECTED
    -2013: ErrorClass.NOT_FOUND,
    -2014: ErrorClass.AUTH,
    -2015: ErrorClass.AUTH,
}

_TIMEOUT_HINTS = ('timed out', 'timeout', 'connection reset', 'connection aborted',
                  'broken pipe', 'read operation', 'temporarily unavailable',
                  'connection refused', 'remote end closed', 'ssl')
_BALANCE_HINTS = ('insufficient balance', 'account has insufficient',
                  'رصيد غير كافٍ', 'insufficient funds')
_DUP_HINTS = ('duplicate order', 'order already exists')


def classify(exc: Exception) -> ErrorPolicy:
    """
    يصنّف استثناءً إلى سياسة تعامل.
    غير المعروف ⇒ must_query=True. لا نفترض الفشل أبداً.
    """
    code = getattr(exc, 'code', None)
    text = str(exc).lower()

    if code is not None and code in CODE_MAP:
        cls = CODE_MAP[code]
        # -2010 عام: نميّز الرصيد عن رفض المخاطر عن التكرار
        if code == -2010:
            if any(h in text for h in _DUP_HINTS):
                cls = ErrorClass.DUPLICATE
            elif any(h in text for h in _BALANCE_HINTS):
                cls = ErrorClass.INSUFFICIENT_BALANCE
        return POLICIES[cls]

    if isinstance(code, int) and 500 <= code < 600:
        return POLICIES[ErrorClass.SERVER]
    if code in (429, 418):
        return POLICIES[ErrorClass.RATE_LIMIT]
    if code in (401, 403):
        return POLICIES[ErrorClass.AUTH]

    if any(h in text for h in _TIMEOUT_HINTS):
        return POLICIES[ErrorClass.TIMEOUT]
    if any(h in text for h in _DUP_HINTS):
        return POLICIES[ErrorClass.DUPLICATE]
    if any(h in text for h in _BALANCE_HINTS):
        return POLICIES[ErrorClass.INSUFFICIENT_BALANCE]
    if 'http 5' in text or 'internal error' in text:
        return POLICIES[ErrorClass.SERVER]
    if 'rate limit' in text or 'too many requests' in text:
        return POLICIES[ErrorClass.RATE_LIMIT]

    return POLICIES[ErrorClass.UNKNOWN]


SECRET_HINTS = ('signature=', 'apikey', 'api_key', 'x-mbx-apikey', 'secret')


def redact(text: str) -> str:
    """
    يحجب الأسرار من أي نص قبل تسجيله — المرحلة 12.
    يُطبَّق على كل رسالة خطأ قبل حفظها أو طباعتها.
    """
    import re
    if not text:
        return text
    out = re.sub(r'(signature=)[A-Fa-f0-9]+', r'\1***REDACTED***', text)
    out = re.sub(r'(?i)(api[_-]?key["\s:=]+)[A-Za-z0-9]{8,}', r'\1***REDACTED***', out)
    out = re.sub(r'(?i)(secret["\s:=]+)[A-Za-z0-9]{8,}', r'\1***REDACTED***', out)
    out = re.sub(r'(?i)(X-MBX-APIKEY["\s:=]+)[A-Za-z0-9]{8,}', r'\1***REDACTED***', out)
    return out
