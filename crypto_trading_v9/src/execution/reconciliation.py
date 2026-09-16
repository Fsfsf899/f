"""
المصالحة مع بينانس — المرحلة السابعة.
=====================================
المصدر النهائي للحقيقة هو **المنصة**، لا قاعدتنا المحلية.

يُنفَّذ عند: بدء التشغيل، قبل فتح مركز، بعد أي مهلة، بعد إعادة اتصال،
وبعد كل أمر خروج.

أي اختلاف ⇒ إيقاف فتح الصفقات + تسجيل كامل + إصلاح آمن إن أمكن.
الإصلاح الآمن **سجلّي فقط**: لا يبيع ولا يشتري شيئاً.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from .binance_client import BinanceClient, BinanceError
from .errors import classify, ErrorClass, redact
from .order_state import (BLOCKING, TERMINAL, LIVE_ON_EXCHANGE, MANUAL,
                          FILLED, CANCELED, is_live, is_terminal,
                          from_exchange_status)

# أنواع الاختلاف
D_POSITION_MISSING = 'POSITION_MISSING_ON_EXCHANGE'
D_UNTRACKED_BALANCE = 'UNTRACKED_BALANCE'
D_QTY_MISMATCH = 'QUANTITY_MISMATCH'
D_STOP_MISSING = 'STOP_ORDER_MISSING'
D_NO_STOP = 'POSITION_WITHOUT_STOP'
D_UNRESOLVED_INTENT = 'UNRESOLVED_INTENT'
D_UNKNOWN_EXCHANGE_ORDER = 'UNKNOWN_EXCHANGE_ORDER'
D_INTENT_STATE_DRIFT = 'INTENT_STATE_DRIFT'
D_MISSING_FILLS = 'MISSING_FILLS'
D_FILLED_QTY_DRIFT = 'FILLED_QTY_DRIFT'
D_MANUAL_REVIEW = 'REQUIRES_MANUAL_REVIEW'

CRITICAL_KINDS = {D_UNRESOLVED_INTENT, D_MANUAL_REVIEW, D_NO_STOP,
                  D_QTY_MISMATCH, D_POSITION_MISSING}


@dataclass
class ReconResult:
    ok: bool
    discrepancies: List[Dict] = field(default_factory=list)
    local_open: int = 0
    exchange_qty: Dict[str, float] = field(default_factory=dict)
    open_orders: int = 0
    unresolved_intents: int = 0
    repaired: List[str] = field(default_factory=list)
    note: str = ''

    @property
    def blocks_trading(self) -> bool:
        return not self.ok

    @property
    def needs_manual(self) -> bool:
        return any(d['kind'] in (D_MANUAL_REVIEW, D_UNRESOLVED_INTENT)
                   for d in self.discrepancies)

    def to_dict(self):
        return {'ok': self.ok, 'discrepancies': self.discrepancies,
                'local_open': self.local_open, 'exchange_qty': self.exchange_qty,
                'open_orders': self.open_orders,
                'unresolved_intents': self.unresolved_intents,
                'repaired': self.repaired, 'note': self.note,
                'needs_manual': self.needs_manual}


class Reconciler:
    def __init__(self, db, client: BinanceClient, tolerance: float = 0.02,
                 gate=None):
        self.db = db
        self.client = client
        self.tolerance = tolerance
        self.gate = gate

    def run(self, symbols: Optional[List[str]] = None,
            auto_repair: bool = True) -> ReconResult:
        disc: List[Dict] = []
        repaired: List[str] = []

        # ── 1) النوايا غير المحسومة أولاً: أخطر من أي اختلاف آخر
        unresolved = self.db.unresolved_intents()
        if unresolved and self.gate is not None:
            for r in self.gate.recover_in_flight():
                if r['state'] in BLOCKING:
                    disc.append({'kind': D_UNRESOLVED_INTENT, 'cid': r['cid'],
                                 'state': r['state'],
                                 'action': 'يلزم تدخل يدوي — التداول موقوف'})
                else:
                    repaired.append(f"نية {r['cid']} حُسمت إلى {r['state']}")
            unresolved = self.db.unresolved_intents()
        for r in unresolved:
            kind = D_MANUAL_REVIEW if r['state'] == MANUAL else D_UNRESOLVED_INTENT
            disc.append({'kind': kind, 'cid': r['client_order_id'],
                         'state': r['state'], 'order_type': r['order_type'],
                         'action': 'حالة غير محسومة تمنع فتح الصفقات'})

        # ── 2) حالة الحساب
        try:
            balances = self.client.balances()
        except BinanceError as e:
            return ReconResult(False, disc, note=f'تعذّر جلب الحساب: {redact(str(e))}')

        local = self.db.open_positions()
        syms = symbols or sorted({p['symbol'] for p in local}) or []
        ex_qty: Dict[str, float] = {}
        n_orders = 0

        for sym in syms:
            try:
                rules = self.client.rules(sym)
                oo = self.client.open_orders(sym)
                price = self.client.price(sym)
            except BinanceError as e:
                return ReconResult(False, disc,
                                   note=f'تعذّر جلب {sym}: {redact(str(e))}')
            n_orders += len(oo)
            base = rules['base']
            held = (balances.get(base, {}).get('free', 0.0)
                    + balances.get(base, {}).get('locked', 0.0))
            ex_qty[sym] = held
            sym_pos = [p for p in local if p['symbol'] == sym]
            local_qty = sum(p['qty'] for p in sym_pos)

            # 3) المراكز مقابل الأرصدة
            if local_qty > 0 and held * price < rules['min_notional']:
                disc.append({'kind': D_POSITION_MISSING, 'symbol': sym,
                             'local_qty': local_qty, 'exchange_qty': held,
                             'action': 'المركز أُغلق خارج النظام'})
            elif local_qty <= 0 and held * price > rules['min_notional']:
                disc.append({'kind': D_UNTRACKED_BALANCE, 'symbol': sym,
                             'exchange_qty': held,
                             'action': 'رصيد غير مسجَّل — شراء يدوي؟'})
            elif local_qty > 0 and held > 0:
                rel = abs(local_qty - held) / max(local_qty, 1e-12)
                if rel > self.tolerance:
                    disc.append({'kind': D_QTY_MISMATCH, 'symbol': sym,
                                 'local_qty': local_qty, 'exchange_qty': held,
                                 'relative_diff': round(rel, 4),
                                 'action': 'تنفيذ جزئي أو بيع خارجي'})

            # 4) أوامر المنصة مقابل المحلية عبر clientOrderId
            local_cids = {r['client_order_id'] for r in self.db.query(
                'SELECT client_order_id FROM order_intents WHERE symbol=?', (sym,))
                if r['client_order_id']}
            for o in oo:
                cid = o.get('clientOrderId', '')
                # طرفا OCO الفرعيان يحملان {cid}-STOP / {cid}-TARGET، لا
                # cid الأصلي المسجَّل في order_intents (وهو معرّف نية
                # القائمة، لا الأبناء). بلا هذا الفحص، كل أمر OCO حقيقي
                # كان سيُصنَّف "غير معروف" خطأً — بالضبط ما يحذّر منه
                # التوثيق: طرف OCO شرعي يتبع نية محلية معروفة لا يُعامَل
                # كأمر مجهول.
                base_cid = cid
                for suf in ('-STOP', '-TARGET'):
                    if cid.endswith(suf):
                        base_cid = cid[:-len(suf)]
                        break
                known = cid in local_cids or base_cid in local_cids
                if cid and not known:
                    disc.append({'kind': D_UNKNOWN_EXCHANGE_ORDER, 'symbol': sym,
                                 'client_order_id': cid,
                                 'exchange_order_id': str(o.get('orderId')),
                                 'type': o.get('type'),
                                 'action': 'أمر على المنصة غير معروف محلياً'})
                elif cid:
                    drift = self._check_intent_drift(
                        cid if cid in local_cids else base_cid, o)
                    if drift:
                        disc.append(drift)

            # 5) كل مركز يجب أن يكون محمياً
            for p in sym_pos:
                live_stop = any(
                    str(o.get('orderId')) == str(p.get('stop_order_id'))
                    for o in oo)
                if p.get('stop_order_id') and not live_stop:
                    status = self._stop_final_status(sym, p['stop_order_id'])
                    if status == 'FILLED':
                        disc.append({'kind': D_POSITION_MISSING, 'symbol': sym,
                                     'position_id': p['id'],
                                     'action': 'الوقف نُفّذ — المركز خرج'})
                    else:
                        disc.append({'kind': D_STOP_MISSING, 'symbol': sym,
                                     'position_id': p['id'],
                                     'stop_order_id': p['stop_order_id'],
                                     'exchange_status': status,
                                     'action': '⚠️ مركز بلا حماية'})
                elif not p.get('stop_order_id'):
                    disc.append({'kind': D_NO_STOP, 'symbol': sym,
                                 'position_id': p['id'],
                                 'action': '⚠️ مركز بلا وقف مسجَّل'})

                # 6) التعبئات المفقودة
                miss = self._missing_fills(sym, p)
                if miss:
                    disc.append(miss)

        ok = not disc
        if not ok:
            self.db.risk_event('RECONCILIATION_MISMATCH',
                               'CRITICAL' if any(d['kind'] in CRITICAL_KINDS
                                                 for d in disc) else 'HIGH',
                               str([d['kind'] for d in disc])[:400])

        res = ReconResult(ok, disc, len(local), ex_qty, n_orders,
                          len(self.db.unresolved_intents()), repaired,
                          'متطابق' if ok else 'اختلاف — فتح الصفقات موقوف')
        if auto_repair and not ok:
            res.repaired += self.auto_resolve(res)
        return res

    def _check_intent_drift(self, cid: str, ex_order: Dict) -> Optional[Dict]:
        row = self.db.get_intent_by_client_order_id(cid)
        if not row:
            return None
        ex_state = from_exchange_status(ex_order.get('status', ''))
        if ex_state and ex_state != row['state']:
            self.db.transition_order_state(cid, ex_state, reason='reconcile',
                                           actor='reconciler')
            return None      # صُحّح محلياً — ليس اختلافاً باقياً
        ex_filled = float(ex_order.get('executedQty', 0) or 0)
        local_filled = float(row.get('filled_qty') or 0)
        if abs(ex_filled - local_filled) > 1e-9:
            self.db.execute(
                'UPDATE order_intents SET filled_qty=?, remaining_qty=? '
                'WHERE client_order_id=?',
                (ex_filled,
                 max(float(ex_order.get('origQty', 0) or 0) - ex_filled, 0.0), cid))
            return {'kind': D_FILLED_QTY_DRIFT, 'client_order_id': cid,
                    'local_filled': local_filled, 'exchange_filled': ex_filled,
                    'action': 'صُحّحت الكمية المنفَّذة محلياً'}
        return None

    def _stop_final_status(self, symbol: str, order_id) -> str:
        try:
            return (self.client.order_status(symbol, order_id).get('status')
                    or 'UNKNOWN').upper()
        except BinanceError as e:
            return ('NOT_FOUND' if classify(e).cls is ErrorClass.NOT_FOUND
                    else 'UNREADABLE')

    def _missing_fills(self, symbol: str, pos: Dict) -> Optional[Dict]:
        """تعبئات على المنصة لم تُسجَّل محلياً."""
        try:
            trades = self.client.my_trades(symbol, limit=50)
        except BinanceError:
            return None
        local_ids = {r['exchange_trade_id'] for r in self.db.query(
            'SELECT exchange_trade_id FROM fills WHERE symbol=?', (symbol,))
            if r['exchange_trade_id']}
        opened = pos.get('opened_ts') or 0
        missing = [t for t in trades
                   if str(t.get('id')) not in local_ids
                   and int(t.get('time', 0)) >= opened]
        if not missing:
            return None
        return {'kind': D_MISSING_FILLS, 'symbol': symbol,
                'position_id': pos['id'], 'count': len(missing),
                'trade_ids': [str(t.get('id')) for t in missing[:5]],
                'action': 'تعبئات على المنصة غير مسجَّلة محلياً'}

    def auto_resolve(self, res: ReconResult) -> List[str]:
        """
        إصلاح سجلّي آمن فقط. لا يرسل أي أمر تداول إطلاقاً.
        ما لا يمكن إصلاحه سجلّياً يبقى ويمنع التداول.
        """
        actions: List[str] = []
        for d in res.discrepancies:
            if d['kind'] == D_POSITION_MISSING:
                for p in self.db.open_positions():
                    if p['symbol'] == d.get('symbol'):
                        self.db.close_position(p['id'], realized_pnl=0.0)
                        self.db.system_event(
                            'RECON_AUTOCLOSE',
                            f"pos {p['id']} {d['symbol']} أُغلق سجلياً")
                        actions.append(f"أُغلق سجل المركز {p['id']}")
            elif d['kind'] == D_STOP_MISSING and d.get('exchange_status') in (
                    'CANCELED', 'EXPIRED', 'NOT_FOUND'):
                self.db.update_position(d['position_id'], stop_order_id=None)
                actions.append(f"مُسح مرجع وقف زائل للمركز {d['position_id']}")
        return actions
