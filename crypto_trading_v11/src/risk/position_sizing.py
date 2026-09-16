"""
حجم المركز — البند 17.
======================
لا يكفي risk_amount / stop_distance.

المخاطرة الفعلية تشمل: رسوم الدخول + رسوم الخروج + انزلاق الدخول
+ انزلاق الوقف (وهو الأسوأ). تجاهلها يعني أن خسارتك الحقيقية عند
ضرب الوقف أكبر مما خطّطت له دائماً.
"""
import numpy as np
from typing import Dict, Optional
from ..core.config import RiskConfig


class PositionSizer:
    def __init__(self, cfg: Optional[RiskConfig] = None, cost_model=None):
        self.cfg = cfg or RiskConfig()
        self.costs = cost_model

    def effective_risk_per_unit(self, entry: float, stop: float) -> float:
        """الخسارة الحقيقية للوحدة عند ضرب الوقف، شاملة كل التكاليف."""
        base = entry - stop
        if base <= 0:
            return 0.0
        if self.costs is None:
            return base
        c = self.costs.cfg
        entry_slip = entry * (c.slippage_bps_entry / 10000 + c.spread_bps / 20000)
        stop_slip = stop * (c.slippage_bps_stop / 10000 + c.spread_bps / 20000)
        fees = (entry + stop) * c.eff_taker()
        return base + entry_slip + stop_slip + fees

    def calculate(self, *, equity: float, entry: float, stop: float,
                  stars: int = 3, consecutive_losses: int = 0,
                  recent_win_rate: Optional[float] = None,
                  regime_confidence: float = 1.0,
                  step_size: Optional[float] = None,
                  min_notional: float = 10.0) -> Dict:
        cfg = self.cfg
        if entry <= 0 or stop <= 0 or entry <= stop or equity <= 0:
            return {'qty': 0.0, 'notional': 0.0, 'risk_pct': 0.0,
                    'reason': 'مدخلات غير صالحة'}

        risk_pct = cfg.risk_per_trade_pct

        # النجوم: 3 = محايد. المدى ×0.75 .. ×1.25
        risk_pct *= 0.75 + (np.clip(stars, 1, 5) - 1) / 4 * 0.5

        # سلسلة خسائر — تقليل حاد
        if consecutive_losses >= 3:
            risk_pct *= 0.5
        elif consecutive_losses == 2:
            risk_pct *= 0.75

        if recent_win_rate is not None and recent_win_rate < 0.35:
            risk_pct *= 0.7

        risk_pct *= float(np.clip(regime_confidence, 0.5, 1.0))
        risk_pct = float(np.clip(risk_pct, cfg.min_risk_per_trade_pct,
                                 cfg.max_risk_per_trade_pct))

        risk_amount = equity * risk_pct / 100
        eff = self.effective_risk_per_unit(entry, stop)
        if eff <= 0:
            return {'qty': 0.0, 'notional': 0.0, 'risk_pct': 0.0,
                    'reason': 'مخاطرة فعلية غير صالحة'}

        qty = risk_amount / eff
        notional = qty * entry

        cap = equity * cfg.max_position_notional_pct / 100
        if notional > cap:
            qty = cap / entry
            notional = qty * entry

        if step_size and step_size > 0:
            qty = np.floor(qty / step_size) * step_size
            notional = qty * entry

        if notional < min_notional:
            return {'qty': 0.0, 'notional': round(notional, 4), 'risk_pct': risk_pct,
                    'reason': f'أقل من الحد الأدنى ${min_notional}'}

        return {
            'qty': float(qty), 'notional': float(notional),
            'risk_pct': round(risk_pct, 4),
            'risk_amount': round(risk_amount, 4),
            'nominal_risk_per_unit': round(entry - stop, 8),
            'effective_risk_per_unit': round(eff, 8),
            'cost_overhead_pct': round((eff / (entry - stop) - 1) * 100, 2),
            'stop_distance_pct': round((entry - stop) / entry * 100, 4),
            'reason': 'ok',
        }


def net_risk_reward(entry: float, stop: float, target: float,
                    costs) -> Optional[float]:
    """
    R/R بعد التكاليف — البند 10.

    النسخة الاسمية (target-entry)/(entry-stop) تفترض تنفيذاً مثالياً
    بلا رسوم ولا انزلاق. صفقة بنسبة اسمية 2.0 قد تصبح فعلياً 1.3 بعد
    الرسوم والانزلاق — والفرق أكبر كلما صغرت المسافات (صفقات ضيقة).

    التطابق مع `PositionSizer.effective_risk_per_unit`: نفس مكوّنات
    التكلفة (رسوم + انزلاق + سبريد) على جانبي المخاطرة والعائد.
    """
    if entry <= 0 or stop <= 0 or target <= 0 or entry <= stop or target <= entry:
        return None

    fee_rate = costs.eff_taker()
    entry_slip = entry * (costs.slippage_bps_entry / 10000 + costs.spread_bps / 20000)
    stop_slip = stop * (costs.slippage_bps_stop / 10000 + costs.spread_bps / 20000)
    exit_slip = target * (costs.slippage_bps_exit / 10000 + costs.spread_bps / 20000)

    net_risk = (entry - stop) + entry_slip + stop_slip + (entry + stop) * fee_rate
    net_reward = (target - entry) - entry_slip - exit_slip - (entry + target) * fee_rate

    if net_risk <= 0:
        return None
    return net_reward / net_risk
