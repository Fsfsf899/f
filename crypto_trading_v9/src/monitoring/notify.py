"""
تنبيهات — Telegram أو Webhook عام. اختياري بالكامل.
====================================================
لا يفعل شيئاً إن لم يُضبط مزوّد — `Notifier()` بلا backends هو no-op آمن.

قواعد صارمة:
  • لا يرفع استثناءً إلى المستدعي أبداً — فشل إرسال تنبيه لا يجوز أن
    يوقف التداول أو يكسر أي مسار حرج (كل خطأ يُلتقَط ويُعاد كنتيجة)
  • لا يُرسل أي سر — النص يمر عبر redact() قبل أي إرسال
  • تبريد (cooldown) لكل مفتاح حدث — يمنع عاصفة تنبيهات من حالة متكررة
    (مثال: نية UNKNOWN تُعاد محاولتها كل دورة فتُرسل 300 رسالة في ساعة)
"""
import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

try:
    from ..execution.errors import redact
except Exception:  # pragma: no cover - يعمل مستقلاً أيضاً
    def redact(text: str) -> str:
        return text

Transport = Callable[[str, str, Dict[str, str], bytes], int]


class Backend:
    def build_request(self, title: str, body: str, severity: str
                      ) -> Optional[Tuple[str, str, Dict[str, str], bytes]]:
        raise NotImplementedError


class TelegramBackend(Backend):
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()

    def build_request(self, title, body, severity):
        text = f"[{severity}] {title}\n{body}".strip()[:4000]
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = json.dumps({'chat_id': self.chat_id, 'text': text}).encode()
        return url, 'POST', {'Content-Type': 'application/json'}, data


class WebhookBackend(Backend):
    """Webhook عام — JSON بسيط، متوافق مع أغلب أدوات الاستقبال."""

    def __init__(self, url: str):
        self.url = url.strip()

    def build_request(self, title, body, severity):
        data = json.dumps({'title': title, 'text': body[:4000],
                           'severity': severity,
                           'source': 'crypto-trading-engine',
                           'ts': int(time.time() * 1000)}).encode()
        return self.url, 'POST', {'Content-Type': 'application/json'}, data


def _default_transport(url: str, method: str, headers: Dict[str, str],
                       data: bytes) -> int:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


@dataclass
class NotifyResult:
    sent: bool
    reason: str = ''
    details: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {'sent': self.sent, 'reason': self.reason, 'details': self.details}


class Notifier:
    """
    بلا backends = no-op آمن دائماً (السلوك الافتراضي عند عدم الضبط).
    """

    def __init__(self, backends: Optional[List[Backend]] = None,
                 cooldown_s: float = 300.0, transport: Optional[Transport] = None):
        self.backends = backends or []
        self.cooldown_s = cooldown_s
        self._transport = transport or _default_transport
        self._last_sent: Dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.backends)

    def notify(self, title: str, body: str = '', severity: str = 'INFO',
              key: Optional[str] = None, force: bool = False) -> NotifyResult:
        if not self.backends:
            return NotifyResult(False, 'NO_BACKEND_CONFIGURED')

        k = key or title
        now = time.time()
        last = self._last_sent.get(k, 0.0)
        if not force and (now - last) < self.cooldown_s:
            return NotifyResult(False, 'COOLDOWN')

        safe_body = redact(body or '')
        details: List[Dict] = []
        any_ok = False
        for b in self.backends:
            try:
                built = b.build_request(title, safe_body, severity)
                if built is None:
                    continue
                url, method, headers, data = built
                status = self._transport(url, method, headers, data)
                details.append({'backend': type(b).__name__, 'ok': True,
                                'status': status})
                any_ok = True
            except Exception as e:
                # لا يُعاد رفع الاستثناء — فشل تنبيه واحد لا يُسقط الباقي
                # ولا يُسمح له بالوصول إلى مسار التداول
                details.append({'backend': type(b).__name__, 'ok': False,
                                'error': str(e)[:200]})

        self._last_sent[k] = now
        return NotifyResult(any_ok, 'SENT' if any_ok else 'ALL_BACKENDS_FAILED',
                            details)

    @classmethod
    def from_env(cls, cooldown_s: Optional[float] = None) -> 'Notifier':
        backends: List[Backend] = []
        tok = os.getenv('NOTIFY_TELEGRAM_BOT_TOKEN', '').strip()
        chat = os.getenv('NOTIFY_TELEGRAM_CHAT_ID', '').strip()
        if tok and chat:
            backends.append(TelegramBackend(tok, chat))
        hook = os.getenv('NOTIFY_WEBHOOK_URL', '').strip()
        if hook:
            backends.append(WebhookBackend(hook))
        cd = cooldown_s
        if cd is None:
            try:
                cd = float(os.getenv('NOTIFY_COOLDOWN_SECONDS', '300') or 300)
            except ValueError:
                cd = 300.0
        return cls(backends, cooldown_s=cd)
