"""
حواجز المخاطر — البند 18، وإصلاح الاستمرارية (RISK_STATE_DESIGN.md).
=====================================================================
الحد اليومي يُقاس مقابل حقوق **بداية اليوم** المحفوظة، لا مقابل
الرصيد الحالي المتغير. القياس مقابل رصيد متحرك يجعل الحد يتمدد مع كل
خسارة — وهو خطأ خطر كان موجوداً في V3.

`db` اختياري تماماً: `None` = سلوك الباكتست القديم بلا تغيير (في
الذاكرة فقط، محاكاة تاريخية لا تحتاج نجاة من Restart). عند تمريره
(من `live_trader.py`) تصبح الحالة محفوظة ذرّياً ومُستعادة تلقائياً —
هذا ما يجعل Paper/Testnet والباكتست يستخدمان **نفس الكائن بالضبط**،
لا تطبيقين متوازيين كما كان الحال (`_consec_losses()` في live_trader.py
كانت تُعيد حساب رقم مختلف المعنى من قاعدة البيانات في كل استدعاء).
"""
import json
import time
from typing import Optional, Dict, List
from ..core.config import RiskConfig

KV_KEY = 'risk_state'
ACCOUNT_WIDE = 'ACCOUNT_WIDE'
PER_SYMBOL = 'PER_SYMBOL'


class RiskGuard:
    def __init__(self, cfg: Optional[RiskConfig] = None, db=None,
                 scope: str = ACCOUNT_WIDE):
        """
        scope: قرار صريح موثَّق في RISK_STATE_DESIGN.md — ACCOUNT_WIDE
        هو الافتراضي لأن النظام يُشغِّل رمزاً واحداً لكل عملية تشغيل
        (مؤكَّد في ENTRY_LOGIC_BASELINE.md). PER_SYMBOL غير مُنفَّذ بعد
        — استخدامه يرفع NotImplementedError صريحاً، لا سلوكاً صامتاً
        خاطئاً.
        """
        if scope not in (ACCOUNT_WIDE, PER_SYMBOL):
            raise ValueError(f'scope غير معروف: {scope}')
        if scope == PER_SYMBOL:
            raise NotImplementedError(
                'PER_SYMBOL غير مُنفَّذ بعد — استخدم ACCOUNT_WIDE صريحاً')
        self.cfg = cfg or RiskConfig()
        self.db = db
        self.scope = scope
        self.daily_starting_equity: Optional[float] = None
        self.daily_trades = 0
        self.consecutive_losses = 0
        self.open_positions = 0
        self.halted = False
        self.halt_reason = ''
        self.day_key: Optional[str] = None
        self.last_trade_id: Optional[str] = None
        self.last_trade_symbol: Optional[str] = None
        self.last_trade_pnl: Optional[float] = None
        self._recent_ids: List[str] = []

        if self.db is not None:
            self._restore()

    # ── الاستمرارية ──
    def _restore(self):
        raw = self.db.get_kv(KV_KEY)
        if not raw:
            self.db.system_event(
                'RISK_STATE_RESTORED', f'لا حالة سابقة — بداية جديدة ({self.scope})')
            return
        try:
            d = raw if isinstance(raw, dict) else json.loads(raw)
            if d.get('scope') and d['scope'] != self.scope:
                self.db.risk_event(
                    'RISK_STATE_SCOPE_MISMATCH', 'HIGH',
                    f"محفوظ={d.get('scope')} مطلوب={self.scope} — تُجوهِل الحالة القديمة")
                return
            self.consecutive_losses = int(d.get('consecutive_losses', 0))
            self.halted = bool(d.get('halted', False))
            self.halt_reason = str(d.get('halt_reason', '') or '')
            self.daily_trades = int(d.get('daily_trades', 0))
            self.day_key = d.get('day_key')
            eq = d.get('daily_starting_equity')
            self.daily_starting_equity = float(eq) if eq is not None else None
            self.last_trade_id = d.get('last_trade_id')
            self.last_trade_symbol = d.get('last_trade_symbol')
            pnl = d.get('last_trade_pnl')
            self.last_trade_pnl = float(pnl) if pnl is not None else None
            self._recent_ids = list(d.get('recent_ids', []) or [])
            inconsistent = (not self.halted
                           and self.consecutive_losses >= self.cfg.max_consecutive_losses)
            self.db.system_event(
                'RISK_STATE_RESTORED',
                f'consecutive_losses={self.consecutive_losses} halted={self.halted} '
                f'day_key={self.day_key} scope={self.scope}'
                + (' [INCONSISTENT_STATE_DETECTED]' if inconsistent else ''))
        except Exception as e:
            self.db.risk_event('RISK_STATE_RESTORE_FAILED', 'HIGH', str(e)[:200])

    def _save(self):
        if self.db is None:
            return
        self.db.set_kv(KV_KEY, {
            'scope': self.scope,
            'consecutive_losses': self.consecutive_losses,
            'halted': self.halted, 'halt_reason': self.halt_reason,
            'daily_trades': self.daily_trades, 'day_key': self.day_key,
            'daily_starting_equity': self.daily_starting_equity,
            'last_trade_id': self.last_trade_id,
            'last_trade_symbol': self.last_trade_symbol,
            'last_trade_pnl': self.last_trade_pnl,
            'recent_ids': self._recent_ids[-50:],
            'saved_at': int(time.time() * 1000)})

    # ── الدورة اليومية ──
    def new_day(self, equity: float, day_key: Optional[str] = None):
        self.daily_starting_equity = float(equity)
        self.daily_trades = 0
        self.halted = False
        self.halt_reason = ''
        self.day_key = day_key
        self.consecutive_losses = 0
        self._save()
        if self.db is not None:
            self.db.system_event('RISK_NEW_DAY', f'equity={equity} day={day_key}')

    def daily_pnl(self, equity: float) -> float:
        if self.daily_starting_equity is None:
            return 0.0
        return equity - self.daily_starting_equity

    def daily_loss_hit(self, equity: float) -> bool:
        if self.daily_starting_equity is None:
            return False
        limit = self.daily_starting_equity * self.cfg.max_daily_loss_pct / 100
        return self.daily_pnl(equity) <= -limit

    def can_trade(self, equity: float) -> Dict:
        if self.halted:
            return {'allowed': False, 'reason': self.halt_reason}
        if self.daily_loss_hit(equity):
            self.halted = True
            self.halt_reason = 'DAILY_LOSS_LIMIT_REACHED'
            self._save()
            return {'allowed': False, 'reason': self.halt_reason}
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.halted = True
            self.halt_reason = 'CONSECUTIVE_LOSS_LIMIT'
            self._save()
            return {'allowed': False, 'reason': self.halt_reason}
        if self.open_positions >= self.cfg.max_open_positions:
            return {'allowed': False, 'reason': 'MAX_OPEN_POSITIONS'}
        if self.daily_trades >= self.cfg.max_daily_trades:
            return {'allowed': False, 'reason': 'MAX_DAILY_TRADES'}
        return {'allowed': True, 'reason': ''}

    def record_open(self):
        self.open_positions += 1
        self.daily_trades += 1
        self._save()

    def record_trade(self, pnl: float, *, trade_id: Optional[str] = None,
                     symbol: Optional[str] = None) -> bool:
        """
        `trade_id` اختياري لكن **إلزامي فعلياً في مسار Live/Paper**
        لمنع تكرار التحديث عند إعادة إرسال Fill. يُرجع False ولا
        يُحدِّث أي شيء إن كان `trade_id` معالَجاً سابقاً.
        """
        if trade_id is not None:
            if trade_id == self.last_trade_id or trade_id in self._recent_ids:
                if self.db is not None:
                    self.db.system_event(
                        'RISK_DUPLICATE_TRADE_IGNORED', f'trade_id={trade_id}')
                return False
            self._recent_ids.append(trade_id)
            self.last_trade_id = trade_id

        self.open_positions = max(0, self.open_positions - 1)
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        self.last_trade_symbol = symbol
        self.last_trade_pnl = float(pnl)
        self._save()
        if self.db is not None:
            self.db.system_event(
                'RISK_TRADE_RECORDED',
                f'pnl={pnl} consecutive_losses={self.consecutive_losses} '
                f'trade_id={trade_id} symbol={symbol}')
        return True
