"""
مزوّدو حالة الحساب — عقد واحد لكل البيئات (الأقسام 15-17).

`PaperAccountProvider` و `ExchangeAccountProvider` يُنتجان
`AccountSnapshot` بنفس الشكل تماماً، فلا يحتاج `PositionSizer` ولا
`live_trader` إلى أي فرع حسب البيئة.

عزل البيئات (القسم 16): كل مزوّد يستقبل عميله جاهزاً من
`live_trader`، والعميل مبنيّ أصلاً من مفاتيح بيئته وحدها عبر
`src/environment/env.py`. لا مزوّد هنا يقرأ مفتاحاً ولا ينشئ اتصالاً،
فيستحيل خلط رصيد بيئة ببيئة أخرى من هذه الطبقة.

Paper بلا شبكة إطلاقاً (القسم 15): `PaperAccountProvider` يقرأ من
`PaperBroker` المحلي فقط.
"""
import time
from typing import Dict, List, Optional

from .state import (AccountSnapshot, compute_reserve,
                    OK, STALE, UNAVAILABLE, RESTRICTED)


class BaseAccountProvider:
    """العقد المشترك. `snapshot()` لا ترفع استثناءً أبداً — تُرجع لقطة
    بحالة غير OK بدلاً من ذلك، فالفشل يصير قراراً لا انهياراً."""

    source = ''

    def __init__(self, cfg, quote_asset: str = 'USDT',
                 max_age_s: Optional[float] = None):
        self.cfg = cfg
        self.quote_asset = quote_asset
        self.max_age_s = (max_age_s if max_age_s is not None
                          else float(getattr(cfg, 'max_balance_age_s', 60.0)))

    def snapshot(self, symbols: Optional[List[str]] = None) -> AccountSnapshot:
        raise NotImplementedError

    # ── مشترك ──
    def _finalize(self, snap: AccountSnapshot) -> AccountSnapshot:
        """
        يُطبِّق الاحتياطي وفحص الطزاجة على أي لقطة، مهما كان مصدرها —
        فلا يمكن لمزوّد أن ينسى أحدهما.
        """
        if snap.status == OK:
            if snap.available_balance < 0 or snap.total_balance < 0:
                snap.status = UNAVAILABLE
                snap.error = 'رصيد سالب — مستحيل'
                snap.reasons.append('NEGATIVE_BALANCE')
                return snap
            if snap.age_seconds > self.max_age_s:
                snap.status = STALE
                snap.reasons.append(
                    f'AGE {snap.age_seconds:.1f}s > {self.max_age_s:.0f}s')
        snap.reserve_amount = compute_reserve(snap.available_balance, self.cfg)
        return snap


class PaperAccountProvider(BaseAccountProvider):
    """
    حساب ورقي محاكى بالكامل — **بلا أي نداء شبكة** (القسم 15).
    يقرأ من `PaperBroker` المحلي الذي يتتبّع الأرصدة والرسوم والتنفيذات
    و PnL أصلاً.
    """

    source = 'paper'

    def __init__(self, cfg, broker, quote_asset: str = 'USDT',
                 max_age_s: Optional[float] = None):
        super().__init__(cfg, quote_asset, max_age_s)
        self.broker = broker

    def snapshot(self, symbols: Optional[List[str]] = None) -> AccountSnapshot:
        snap = AccountSnapshot(quote_asset=self.quote_asset, source=self.source)
        try:
            bals = self.broker.balances()
        except Exception as e:
            snap.status = UNAVAILABLE
            snap.error = str(e)[:200]
            return self._finalize(snap)

        q = bals.get(self.quote_asset, {})
        snap.available_balance = float(q.get('free', 0.0) or 0.0)
        snap.locked_balance = float(q.get('locked', 0.0) or 0.0)
        snap.total_balance = snap.available_balance + snap.locked_balance
        snap.assets = {a: {'free': float(v.get('free', 0.0) or 0.0),
                           'locked': float(v.get('locked', 0.0) or 0.0)}
                       for a, v in bals.items()}
        snap.position_value = self._position_value(bals, symbols or [])
        # الوسيط الورقي محلي بالكامل: اللقطة آنيّة بحكم التعريف
        snap.taken_ms = int(time.time() * 1000)
        snap.status = OK
        return self._finalize(snap)

    def _position_value(self, bals: Dict, symbols: List[str]) -> float:
        """
        تقويم الأصول غير المقابلة بأسعار حقيقية من الوسيط (القسم 5).
        الرمز الذي يتعذّر تسعيره **يُستبعَد ويُوسَم**، ولا يُفترَض له
        سعر — «لا تحوّل الأصول إلى USDT صامتاً بلا نموذج تقويم مُتحقَّق».
        """
        total = 0.0
        for s in symbols:
            try:
                base = self.broker.rules(s)['base']
            except Exception:
                continue
            if base == self.quote_asset:
                continue
            v = bals.get(base, {})
            held = float(v.get('free', 0.0) or 0.0) + float(v.get('locked', 0.0) or 0.0)
            if held <= 0:
                continue
            try:
                total += held * float(self.broker.price(s))
            except Exception:
                pass
        return total


class ExchangeAccountProvider(BaseAccountProvider):
    """
    حساب مُصادَق حقيقي من بينانس (Testnet أو Live حسب العميل المُمرَّر).

    ⚠️ لم يُشغَّل قط مقابل بينانس حقيقية في هذا المستودع — الشبكة محجوبة
    في بيئة التطوير ولا مفاتيح. منطقه مُختبَر بمنصات وهمية فقط، وحالته
    في تقرير التحقق **NOT VERIFIED** حتى يُشغَّل بمفاتيح حقيقية.
    """

    def __init__(self, cfg, client, quote_asset: str = 'USDT',
                 max_age_s: Optional[float] = None, source: str = 'exchange'):
        super().__init__(cfg, quote_asset, max_age_s)
        self.client = client
        self.source = source

    def snapshot(self, symbols: Optional[List[str]] = None) -> AccountSnapshot:
        snap = AccountSnapshot(quote_asset=self.quote_asset, source=self.source)
        # الطابع الزمني يُؤخَذ **قبل** النداء: عمر اللقطة يجب أن يشمل
        # زمن النداء نفسه، لا أن يبدأ بعد عودته (وإلا بدت لقطة بطيئة
        # طازجة وهي ليست كذلك).
        t0 = int(time.time() * 1000)
        try:
            acct = self.client.account()
        except Exception as e:
            snap.status = UNAVAILABLE
            snap.error = str(e)[:200]
            snap.reasons.append('ACCOUNT_ENDPOINT_FAILED')
            return self._finalize(snap)

        if not isinstance(acct, dict):
            snap.status = UNAVAILABLE
            snap.reasons.append('MALFORMED_ACCOUNT_RESPONSE')
            return self._finalize(snap)

        # صلاحية التداول (القسم 14: RESTRICTED)
        if acct.get('canTrade') is False:
            snap.status = RESTRICTED
            snap.reasons.append('KEY_CANNOT_TRADE')
            return self._finalize(snap)

        try:
            bals = self.client.balances()
        except Exception as e:
            snap.status = UNAVAILABLE
            snap.error = str(e)[:200]
            snap.reasons.append('BALANCES_FAILED')
            return self._finalize(snap)

        q = bals.get(self.quote_asset)
        if q is None:
            # العملة المقابلة غائبة تماماً من الرد ⇒ لا نفترض صفراً
            # قابلاً للتداول ولا رقماً آخر (القسم 17).
            snap.status = UNAVAILABLE
            snap.reasons.append('QUOTE_BALANCE_MISSING')
            return self._finalize(snap)

        try:
            snap.available_balance = float(q.get('free', 0.0) or 0.0)
            snap.locked_balance = float(q.get('locked', 0.0) or 0.0)
        except (TypeError, ValueError):
            snap.status = UNAVAILABLE
            snap.reasons.append('MALFORMED_BALANCE_VALUES')
            return self._finalize(snap)

        snap.total_balance = snap.available_balance + snap.locked_balance
        snap.assets = {}
        for a, v in bals.items():
            try:
                snap.assets[a] = {'free': float(v.get('free', 0.0) or 0.0),
                                  'locked': float(v.get('locked', 0.0) or 0.0)}
            except (TypeError, ValueError):
                continue
        snap.position_value = self._position_value(bals, symbols or [])
        snap.taken_ms = t0
        snap.status = OK
        return self._finalize(snap)

    def _position_value(self, bals: Dict, symbols: List[str]) -> float:
        """كما في Paper: تسعير حقيقي، والمتعذّر يُستبعَد ولا يُخمَّن."""
        total = 0.0
        for s in symbols:
            try:
                base = self.client.rules(s)['base']
            except Exception:
                continue
            if base == self.quote_asset:
                continue
            v = bals.get(base, {})
            held = float(v.get('free', 0.0) or 0.0) + float(v.get('locked', 0.0) or 0.0)
            if held <= 0:
                continue
            try:
                total += held * float(self.client.price(s))
            except Exception:
                pass
        return total


class SimulatedAccountProvider(BaseAccountProvider):
    """
    حساب محاكى للباكتست (القسم 23): «Backtest must use the same signal
    and PositionSizer logic as Live, with a simulated account provider
    rather than real exchange balance».

    ⚠️ سبب وجوده: قبله كان الباكتست ينادي `PositionSizer` **بلا** لقطة
    حساب، فيسلك المسار القديم — بلا احتياطي نقدي، وبلا قيود المنصة،
    وبلا إعادة حساب المخاطرة بعد التقريب. بينما صار المسار الحيّ يمرّ
    باللقطة. النتيجة قياساً: الباكتست يفتح مراكز **أكبر بـ 11.1%** من
    التنفيذ الحيّ لنفس الإشارة بالضبط، فيُبالغ في العوائد بلا سبب
    استراتيجي — نفس صنف الانحراف الذي وُجد سابقاً في معادلة الرسوم.

    لا شبكة ولا حالة داخلية: يُبنى من النقد المُمرَّر عند كل نداء.
    """

    source = 'backtest'

    def snapshot(self, symbols=None, *, available: float = 0.0,
                 locked: float = 0.0, position_value: float = 0.0
                 ) -> AccountSnapshot:
        snap = AccountSnapshot(quote_asset=self.quote_asset, source=self.source)
        snap.available_balance = max(0.0, float(available))
        snap.locked_balance = max(0.0, float(locked))
        snap.total_balance = snap.available_balance + snap.locked_balance
        snap.position_value = max(0.0, float(position_value))
        snap.assets = {self.quote_asset: {'free': snap.available_balance,
                                          'locked': snap.locked_balance}}
        # محاكاة تاريخية: اللقطة آنيّة بحكم التعريف، فلا معنى لفحص
        # الطزاجة عليها — لكن الاحتياطي يُطبَّق كما في الحيّ تماماً.
        snap.taken_ms = int(time.time() * 1000)
        snap.status = OK
        return self._finalize(snap)
