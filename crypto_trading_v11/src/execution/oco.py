"""
نموذج بيانات OCO موحَّد — القسم 5 من متطلبات V10.

يُحلِّل استجابة `/api/v3/orderList/oco` (الحقيقية أو محاكاتها) إلى
كائن واحد واضح — لا قاموساً خاماً يُعاد تفسيره بمنطق مختلف في كل
مكان يلمس OCO (وهذا بالضبط ما كان يسبب أخطاء الخلط السابقة).

مُحلِّل صارم: أي حقل إلزامي غائب أو غير متّسق ⇒ `valid=False` صراحة،
لا تخمين، لا نجاح مصطنع.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

STOP_TYPES = {'STOP_LOSS', 'STOP_LOSS_LIMIT'}
TARGET_TYPES = {'LIMIT_MAKER', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT', 'LIMIT'}


@dataclass
class OCOResult:
    order_list_id: Optional[str] = None
    list_client_order_id: Optional[str] = None
    stop_order_id: Optional[str] = None
    stop_client_order_id: Optional[str] = None
    stop_type: Optional[str] = None
    stop_status: Optional[str] = None
    target_order_id: Optional[str] = None
    target_client_order_id: Optional[str] = None
    target_type: Optional[str] = None
    target_status: Optional[str] = None
    symbol: Optional[str] = None
    quantity: Optional[float] = None
    list_status: Optional[str] = None        # listStatusType
    list_order_status: Optional[str] = None  # listOrderStatus
    raw_response: Dict = field(default_factory=dict)
    valid: bool = False
    error: str = ''

    @property
    def fully_protected(self) -> bool:
        """
        كل الشروط اللازمة لاعتبار الحماية مؤكَّدة فعلياً — القسم 16
        من القواعد المطلقة: "If protection cannot be proven, the
        position is NOT considered protected."
        """
        return (self.valid and bool(self.order_list_id)
               and bool(self.stop_order_id) and self.stop_order_id != 'None'
               and bool(self.target_order_id) and self.target_order_id != 'None'
               and self.stop_order_id != self.target_order_id)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d.pop('raw_response', None)   # لا تُخزَّن الاستجابة الخام في تقارير مختصرة
        return d


def parse_oco_response(r: Dict, *, expected_symbol: Optional[str] = None
                       ) -> OCOResult:
    """
    المُحلِّل الوحيد المعتمَد لاستجابة OCO في هذا المشروع — القسم 5.
    لا يُخمِّن معرّفاً غائباً، لا ينجح جزئياً.
    """
    res = OCOResult(raw_response=r or {})
    if not r:
        res.error = 'استجابة فارغة'
        return res

    olid = r.get('orderListId')
    res.order_list_id = str(olid) if olid is not None else None
    res.list_client_order_id = r.get('listClientOrderId')
    res.list_status = r.get('listStatusType')
    res.list_order_status = r.get('listOrderStatus')
    res.symbol = r.get('symbol')

    reports: List[Dict] = r.get('orderReports') or r.get('orders') or []

    stop_rep, target_rep = None, None
    for rep in reports:
        t = str(rep.get('type', '')).upper()
        cid = str(rep.get('clientOrderId', ''))
        if t in STOP_TYPES or (not t and cid.endswith('-STOP')):
            stop_rep = stop_rep or rep
        elif t in TARGET_TYPES or (not t and cid.endswith('-TARGET')):
            target_rep = target_rep or rep

    if res.order_list_id is None:
        res.error = 'orderListId مفقود'
        return res
    if stop_rep is None or target_rep is None:
        res.error = (f'تعذّر تحديد كلا طرفَي OCO '
                    f'(stop={bool(stop_rep)}, target={bool(target_rep)})')
        return res

    def _oid(rep) -> Optional[str]:
        v = rep.get('orderId')
        return str(v) if v is not None else None

    res.stop_order_id = _oid(stop_rep)
    res.stop_client_order_id = stop_rep.get('clientOrderId')
    res.stop_type = stop_rep.get('type')
    res.stop_status = stop_rep.get('status')

    res.target_order_id = _oid(target_rep)
    res.target_client_order_id = target_rep.get('clientOrderId')
    res.target_type = target_rep.get('type')
    res.target_status = target_rep.get('status')

    # لا نستنتج معرّفاً مفقوداً أبداً — القاعدة 17/18 المطلقتان
    if not res.stop_order_id or res.stop_order_id == 'None':
        res.error = 'stop orderId مفقود'
        return res
    if not res.target_order_id or res.target_order_id == 'None':
        res.error = 'target orderId مفقود'
        return res
    if res.stop_order_id == res.target_order_id:
        res.error = 'stop وtarget بنفس orderId — خلط معرّفات'
        return res
    if expected_symbol and res.symbol and res.symbol != expected_symbol:
        res.error = f'symbol غير متطابق: متوقَّع {expected_symbol}، وصل {res.symbol}'
        return res

    res.valid = True
    return res
