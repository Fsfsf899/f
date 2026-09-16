"""
محرك التنفيذ الواقعي - Realistic Execution Engine
==================================================
هذا الملف يعالج أهم فجوة بين الباكتست والتداول الحقيقي:
التكاليف. معظم الاستراتيجيات "الرابحة" تنهار بمجرد إضافة
الرسوم والانزلاق.

الوحدات:
1. CostModel           — رسوم + انزلاق واقعي
2. PositionSizer       — حجم المركز الديناميكي
3. ExitManager         — خروج جزئي + Trailing + Breakeven
4. RiskGuard           — حواجز الأمان (حد يومي، صفقات متزامنة)
"""

import numpy as np
from typing import Dict, List, Optional
from datetime import datetime


# ═══════════════════════════════════════════════════════════
# 1. نموذج التكاليف
# ═══════════════════════════════════════════════════════════
class CostModel:
    """
    ⚠️ أهم كلاس في المشروع كله.

    بدونه، الباكتست يكذب عليك.
    مثال: استراتيجية بـ 126 صفقة شهرياً ورسوم 0.1% ذهاب وإياب
    = 25% من رأس المال تذهب رسوماً فقط في السنة.
    """

    def __init__(self,
                 maker_fee: float = 0.001,      # 0.10% (Binance spot عادي)
                 taker_fee: float = 0.001,      # 0.10%
                 slippage_bps: float = 5.0,     # 5 نقاط أساس = 0.05%
                 use_bnb_discount: bool = False):
        self.maker_fee = maker_fee * (0.75 if use_bnb_discount else 1.0)
        self.taker_fee = taker_fee * (0.75 if use_bnb_discount else 1.0)
        self.slippage_bps = slippage_bps

    def entry_price(self, ideal: float, side: str,
                    volatility_mult: float = 1.0) -> float:
        """
        السعر الفعلي للدخول بعد الانزلاق.
        الانزلاق يزيد مع التقلب — لهذا volatility_mult.
        """
        slip = self.slippage_bps / 10000 * volatility_mult
        return ideal * (1 + slip) if side == 'long' else ideal * (1 - slip)

    def exit_price(self, ideal: float, side: str,
                   volatility_mult: float = 1.0,
                   is_stop: bool = False) -> float:
        """
        سعر الخروج. ملاحظة مهمة:
        الانزلاق عند وقف الخسارة أسوأ بكثير (السوق يتحرك ضدك بسرعة).
        """
        mult = volatility_mult * (2.5 if is_stop else 1.0)
        slip = self.slippage_bps / 10000 * mult
        return ideal * (1 - slip) if side == 'long' else ideal * (1 + slip)

    def round_trip_cost(self, notional: float) -> float:
        """تكلفة الرسوم ذهاباً وإياباً"""
        return notional * self.taker_fee * 2

    def breakeven_move_pct(self) -> float:
        """
        كم يجب أن يتحرك السعر لتغطية التكاليف فقط؟
        هذا الرقم يصدمك عادة.
        """
        return (self.taker_fee * 2 + self.slippage_bps / 10000 * 2) * 100


# ═══════════════════════════════════════════════════════════
# 2. حجم المركز
# ═══════════════════════════════════════════════════════════
class PositionSizer:
    """
    حجم المركز = أهم قرار في التداول، أهم من نقطة الدخول نفسها.
    """

    def __init__(self, base_risk_pct: float = 1.0,
                 max_risk_pct: float = 2.0,
                 min_risk_pct: float = 0.3):
        self.base_risk_pct = base_risk_pct
        self.max_risk_pct = max_risk_pct
        self.min_risk_pct = min_risk_pct

    def calculate(self, balance: float, entry: float, stop: float,
                  signal_stars: int = 3,
                  recent_win_rate: Optional[float] = None,
                  consecutive_losses: int = 0,
                  regime_confidence: float = 1.0) -> Dict:
        """
        حجم المركز حسب:
        - المسافة للوقف (الأساس)
        - قوة الإشارة
        - سلسلة الخسائر (تقليل تلقائي)
        - ثقة حالة السوق
        """
        stop_dist = abs(entry - stop)
        if stop_dist <= 0:
            return {'size': 0, 'risk_pct': 0, 'reason': 'وقف غير صالح'}

        risk_pct = self.base_risk_pct

        # قوة الإشارة: 3 نجوم = محايد
        risk_pct *= (0.7 + (signal_stars / 5) * 0.6)

        # سلسلة خسائر → تقليل حاد (حماية نفسية ومالية)
        if consecutive_losses >= 3:
            risk_pct *= 0.5
        elif consecutive_losses == 2:
            risk_pct *= 0.75

        # أداء أخير ضعيف
        if recent_win_rate is not None and recent_win_rate < 0.35:
            risk_pct *= 0.7

        # حالة السوق غير واضحة
        risk_pct *= max(regime_confidence, 0.5)

        risk_pct = float(np.clip(risk_pct, self.min_risk_pct, self.max_risk_pct))

        risk_amount = balance * risk_pct / 100
        size = risk_amount / stop_dist

        return {
            'size': round(size, 8),
            'risk_pct': round(risk_pct, 3),
            'risk_amount': round(risk_amount, 2),
            'notional': round(size * entry, 2),
            'stop_distance_pct': round(stop_dist / entry * 100, 2)
        }


# ═══════════════════════════════════════════════════════════
# 3. إدارة الخروج
# ═══════════════════════════════════════════════════════════
class ExitManager:
    """
    الخروج أصعب من الدخول.
    الاستراتيجية هنا: خروج جزئي + نقل الوقف لنقطة التعادل + تريلينق.

    لماذا الخروج الجزئي؟
    يحوّل صفقة "50/50" إلى صفقة بمخاطرة صفر بسرعة.
    """

    def __init__(self,
                 partial_1_r: float = 1.0,      # اخرج جزء عند 1R
                 partial_1_pct: float = 0.4,     # 40% من المركز
                 partial_2_r: float = 2.0,
                 partial_2_pct: float = 0.3,
                 breakeven_at_r: float = 1.0,    # انقل الوقف بعد 1R
                 trail_atr_mult: float = 2.0):
        self.partial_1_r = partial_1_r
        self.partial_1_pct = partial_1_pct
        self.partial_2_r = partial_2_r
        self.partial_2_pct = partial_2_pct
        self.breakeven_at_r = breakeven_at_r
        self.trail_atr_mult = trail_atr_mult

    def init_position(self, entry: float, stop: float, size: float,
                      side: str = 'long') -> Dict:
        risk_per_unit = abs(entry - stop)
        return {
            'entry': entry,
            'initial_stop': stop,
            'current_stop': stop,
            'size': size,
            'remaining': size,
            'side': side,
            'risk_per_unit': risk_per_unit,
            'partial_1_done': False,
            'partial_2_done': False,
            'breakeven_done': False,
            'realized_pnl': 0.0,
            'peak_r': 0.0
        }

    def update(self, pos: Dict, price: float, atr: float) -> List[Dict]:
        """
        تحديث المركز. يرجع قائمة بالأحداث (خروج جزئي / نقل وقف / إغلاق).
        """
        events = []
        side = pos['side']
        rpu = pos['risk_per_unit']
        if rpu <= 0:
            return events

        # كم R حققنا؟
        if side == 'long':
            current_r = (price - pos['entry']) / rpu
        else:
            current_r = (pos['entry'] - price) / rpu

        pos['peak_r'] = max(pos['peak_r'], current_r)

        # ── الخروج الجزئي الأول
        if not pos['partial_1_done'] and current_r >= self.partial_1_r:
            qty = pos['size'] * self.partial_1_pct
            pnl = qty * rpu * self.partial_1_r * (1 if side == 'long' else 1)
            pos['remaining'] -= qty
            pos['realized_pnl'] += pnl
            pos['partial_1_done'] = True
            events.append({'type': 'PARTIAL_1', 'qty': qty,
                           'price': price, 'r': current_r})

        # ── نقل الوقف لنقطة التعادل
        if not pos['breakeven_done'] and current_r >= self.breakeven_at_r:
            pos['current_stop'] = pos['entry']
            pos['breakeven_done'] = True
            events.append({'type': 'BREAKEVEN', 'new_stop': pos['entry']})

        # ── الخروج الجزئي الثاني
        if not pos['partial_2_done'] and current_r >= self.partial_2_r:
            qty = pos['size'] * self.partial_2_pct
            pnl = qty * rpu * self.partial_2_r
            pos['remaining'] -= qty
            pos['realized_pnl'] += pnl
            pos['partial_2_done'] = True
            events.append({'type': 'PARTIAL_2', 'qty': qty,
                           'price': price, 'r': current_r})

        # ── التريلينق (يبدأ بعد نقطة التعادل فقط)
        if pos['breakeven_done'] and atr > 0:
            if side == 'long':
                new_stop = price - atr * self.trail_atr_mult
                if new_stop > pos['current_stop']:
                    pos['current_stop'] = new_stop
                    events.append({'type': 'TRAIL', 'new_stop': new_stop})
            else:
                new_stop = price + atr * self.trail_atr_mult
                if new_stop < pos['current_stop']:
                    pos['current_stop'] = new_stop
                    events.append({'type': 'TRAIL', 'new_stop': new_stop})

        # ── ضرب الوقف
        hit = (price <= pos['current_stop']) if side == 'long' \
            else (price >= pos['current_stop'])
        if hit and pos['remaining'] > 0:
            events.append({'type': 'STOP_HIT', 'qty': pos['remaining'],
                           'price': pos['current_stop'], 'r': current_r})

        return events


# ═══════════════════════════════════════════════════════════
# 4. حواجز الأمان
# ═══════════════════════════════════════════════════════════
class RiskGuard:
    """
    الحواجز التي تمنعك من تدمير حسابك في يوم سيء.
    """

    def __init__(self, daily_loss_limit_pct: float = 3.0,
                 max_concurrent: int = 3,
                 max_consecutive_losses: int = 5,
                 max_daily_trades: int = 10):
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_concurrent = max_concurrent
        self.max_consecutive_losses = max_consecutive_losses
        self.max_daily_trades = max_daily_trades

        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.consecutive_losses = 0
        self.halted = False
        self.halt_reason = ''

    def new_day(self):
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.halted = False
        self.halt_reason = ''

    def can_trade(self, balance: float, open_positions: int) -> Dict:
        if self.halted:
            return {'allowed': False, 'reason': self.halt_reason}

        if self.daily_pnl < -(balance * self.daily_loss_limit_pct / 100):
            self.halted = True
            self.halt_reason = f'⛔ تجاوز حد الخسارة اليومي ({self.daily_loss_limit_pct}%)'
            return {'allowed': False, 'reason': self.halt_reason}

        if self.consecutive_losses >= self.max_consecutive_losses:
            self.halted = True
            self.halt_reason = f'⛔ {self.max_consecutive_losses} خسائر متتالية — توقف وراجع'
            return {'allowed': False, 'reason': self.halt_reason}

        if open_positions >= self.max_concurrent:
            return {'allowed': False, 'reason': 'الحد الأقصى للصفقات المتزامنة'}

        if self.daily_trades >= self.max_daily_trades:
            return {'allowed': False, 'reason': 'الحد الأقصى للصفقات اليومية'}

        return {'allowed': True, 'reason': ''}

    def record_trade(self, pnl: float):
        self.daily_pnl += pnl
        self.daily_trades += 1
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0


if __name__ == '__main__':
    cm = CostModel(slippage_bps=5)
    print("💰 نموذج التكاليف:")
    print(f"   السعر يجب أن يتحرك {cm.breakeven_move_pct():.3f}% فقط لتغطية التكاليف")
    print(f"   على 100 صفقة شهرياً = {cm.breakeven_move_pct()*100:.1f}% من رأس المال رسوماً!")
    print()
    ps = PositionSizer()
    r = ps.calculate(10000, 50000, 49000, signal_stars=5, consecutive_losses=0)
    print("📏 مثال حجم مركز (رصيد $10,000، دخول 50000، وقف 49000، 5 نجوم):")
    for k, v in r.items():
        print(f"   {k}: {v}")
