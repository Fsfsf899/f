"""
ظرف الرد ووسوم مصدر البيانات — المرحلة السابعة.
================================================
كل قيمة تحمل مصدرها. الفرق بين رقم أنتجه المحرك ورقم حسبته اللوحة
ليس تفصيلاً — الأول مصدر حقيقة، والثاني اشتقاق قد يخالفه.

الوسوم:
  ENGINE_REPORTED     مقروء كما هو من قاعدة المحرك
  DASHBOARD_DERIVED   تجميع وصفي حسبته اللوحة
  INSUFFICIENT_SAMPLE عينة أصغر من الحد — لا يُستنتج منها
  STALE_DATA          البيانات أقدم من الحد المقبول
  UNAVAILABLE         لا بيانات — null وليس صفر
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ENGINE_REPORTED = 'ENGINE_REPORTED'
DASHBOARD_DERIVED = 'DASHBOARD_DERIVED'
INSUFFICIENT_SAMPLE = 'INSUFFICIENT_SAMPLE'
STALE_DATA = 'STALE_DATA'
UNAVAILABLE = 'UNAVAILABLE'
SCHEMA_MISMATCH = 'SCHEMA_MISMATCH'

MIN_SAMPLE = 30


def iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(ms) / 1000))


@dataclass
class Metric:
    """قيمة واحدة مع مصدرها وحالتها."""
    value: Any
    source: str = DASHBOARD_DERIVED
    sample_size: Optional[int] = None
    as_of_ms: Optional[int] = None
    note: Optional[str] = None

    def to_dict(self) -> Dict:
        status = self.source
        if self.value is None:
            status = UNAVAILABLE
        elif (self.sample_size is not None
              and self.sample_size < MIN_SAMPLE
              and self.source == DASHBOARD_DERIVED):
            status = INSUFFICIENT_SAMPLE
        return {'value': self.value, 'source': self.source, 'status': status,
                'sample_size': self.sample_size,
                'as_of': iso(self.as_of_ms), 'note': self.note}


def metric(value, source=DASHBOARD_DERIVED, sample_size=None,
           as_of_ms=None, note=None) -> Dict:
    return Metric(value, source, sample_size, as_of_ms, note).to_dict()


def engine(value, as_of_ms=None, note=None) -> Dict:
    """قيمة مقروءة من المحرك كما هي — لا إعادة حساب."""
    return Metric(value, ENGINE_REPORTED, None, as_of_ms, note).to_dict()


def derived(value, sample_size=None, as_of_ms=None, note=None) -> Dict:
    return Metric(value, DASHBOARD_DERIVED, sample_size, as_of_ms, note).to_dict()


def unavailable(note: str = 'لا بيانات') -> Dict:
    return Metric(None, UNAVAILABLE, None, None, note).to_dict()


@dataclass
class Envelope:
    """
    { ok, data, meta }
    meta تحمل: البيئة، وقت التوليد، حداثة البيانات، حجم العينة، الحالة.
    """
    data: Any
    environment: str
    api_version: str
    dashboard_version: str
    engine_version: Optional[str] = None
    schema_version: Optional[int] = None
    data_as_of_ms: Optional[int] = None
    stale: bool = False
    sample_size: Optional[int] = None
    warnings: List[str] = field(default_factory=list)
    ok: bool = True

    def to_dict(self) -> Dict:
        now = int(time.time() * 1000)
        return {
            'ok': self.ok,
            'data': self.data,
            'meta': {
                'environment': self.environment,
                'generated_at': iso(now),
                'data_as_of': iso(self.data_as_of_ms),
                'data_age_ms': (None if self.data_as_of_ms is None
                                else now - self.data_as_of_ms),
                'stale': self.stale,
                'sample_size': self.sample_size,
                'read_only': True,
                'can_trade': False,
                'can_modify_engine': False,
                'api_version': self.api_version,
                'dashboard_version': self.dashboard_version,
                'engine_version': self.engine_version,
                'schema_version': self.schema_version,
                'warnings': self.warnings,
                'server_time_ms': now,
            },
        }


def error_envelope(code: str, message: str, environment: str,
                   api_version: str, dashboard_version: str,
                   **extra) -> Dict:
    """
    خطأ بلا تسريب: لا مسارات ملفات ولا stack trace ولا أسرار.
    """
    return {
        'ok': False,
        'data': None,
        'error': {'code': code, 'message': message, **extra},
        'meta': {
            'environment': environment,
            'generated_at': iso(int(time.time() * 1000)),
            'read_only': True, 'can_trade': False,
            'can_modify_engine': False,
            'api_version': api_version,
            'dashboard_version': dashboard_version,
        },
    }
