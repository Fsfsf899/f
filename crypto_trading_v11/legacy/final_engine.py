"""
المحرك النهائي - Final Integrated Engine
=========================================
يجمع كل شيء + Walk-Forward Validation

Walk-Forward = الاختبار الوحيد الصادق:
  تحسّن على فترة A، تختبر على فترة B (لم يرها النموذج أبداً)،
  ثم تتقدم للأمام وتكرر. هذا يكشف الـ Overfitting فوراً.
"""

import numpy as np
from typing import Dict, List, Optional
from market_structure import (SupportResistance, PriceAction,
                              VolumeProfile, MarketRegime, HigherTimeframe)
from execution_engine import CostModel, PositionSizer, ExitManager, RiskGuard


# ═══════════════════════════════════════════════════════════
# المؤشرات الأساسية
# ═══════════════════════════════════════════════════════════
def ema(prices, period):
    out = np.zeros_like(prices, dtype=float)
    out[0] = prices[0]
    k = 2 / (period + 1)
    for i in range(1, len(prices)):
        out[i] = prices[i] * k + out[i-1] * (1 - k)
    return out


def rsi(prices, period=14):
    d = np.diff(prices, prepend=prices[0])
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = np.zeros_like(prices, dtype=float)
    al = np.zeros_like(prices, dtype=float)
    if len(prices) > period:
        ag[period] = g[1:period+1].mean()
        al[period] = l[1:period+1].mean()
        for i in range(period+1, len(prices)):
            ag[i] = (ag[i-1]*(period-1) + g[i]) / period
            al[i] = (al[i-1]*(period-1) + l[i]) / period
    rs = np.divide(ag, al + 1e-9)
    out = 100 - 100/(1+rs)
    out[:period] = 50
    return out


def atr(high, low, close, period=14):
    pc = np.roll(close, 1); pc[0] = close[0]
    tr = np.maximum(high-low, np.maximum(np.abs(high-pc), np.abs(low-pc)))
    out = np.zeros_like(tr, dtype=float)
    out[:period] = tr[:period].mean() if len(tr) >= period else tr.mean()
    for i in range(period, len(tr)):
        out[i] = (out[i-1]*(period-1) + tr[i]) / period
    return out


def adx(high, low, close, period=14):
    n = len(close)
    pdm = np.zeros(n); ndm = np.zeros(n)
    for i in range(1, n):
        up = high[i]-high[i-1]; dn = low[i-1]-low[i]
        if up > dn and up > 0: pdm[i] = up
        if dn > up and dn > 0: ndm[i] = dn
    a = atr(high, low, close, period)
    out = np.zeros(n)
    for i in range(period*2, n):
        pdi = pdm[i-period+1:i+1].sum() / (a[i]+1e-9) * 100
        ndi = ndm[i-period+1:i+1].sum() / (a[i]+1e-9) * 100
        out[i] = abs(pdi-ndi) / (pdi+ndi+1e-9) * 100
    return out


# ═══════════════════════════════════════════════════════════
# محرك الإشارات النهائي
# ═══════════════════════════════════════════════════════════
class FinalSignalEngine:
    """
    الإشارة النهائية تمر بـ 3 مراحل:
      المرحلة 1: بوابات إلزامية (Gates) — أي فشل = رفض فوري
      المرحلة 2: نقاط تأكيد (Score) — كم مؤشر يوافق؟
      المرحلة 3: جودة نقطة الدخول (Location) — هل السعر في مكان جيد؟

    الفرق عن النسخ السابقة: البوابات. سابقاً كان كل شيء "نقاط"،
    فإشارة سيئة تجمع نقاط من مكان آخر وتمر. الآن لا.
    """

    def __init__(self, min_score: int = 4, use_regime_filter: bool = True,
                 use_htf_filter: bool = True):
        self.min_score = min_score
        self.use_regime_filter = use_regime_filter
        self.use_htf_filter = use_htf_filter

    def evaluate(self, idx: int, o, h, l, c, v, ind: Dict) -> Dict:
        rejected = lambda why: {'enter': False, 'reason': why,
                                'stars': 0, 'score': 0}

        if idx < 60:
            return rejected('بيانات غير كافية')

        # ═══ المرحلة 1: البوابات الإلزامية ═══

        # بوابة 1: حالة السوق
        if self.use_regime_filter:
            reg = MarketRegime.detect(c, h, l, idx)
            if not reg['tradeable']:
                return rejected(f"حالة السوق: {reg['regime']}")
            if reg['regime'] != MarketRegime.TRENDING_UP:
                return rejected('الاتجاه ليس صاعداً')
        else:
            reg = {'confidence': 1.0, 'regime': 'N/A'}

        # بوابة 2: الإطار الأكبر
        if self.use_htf_filter:
            htf = HigherTimeframe.get_bias(c, idx, factor=4)
            if not htf['aligned_long']:
                return rejected('يعاكس الإطار الأكبر')
        else:
            htf = {'bias': 'N/A'}

        # بوابة 3: قوة الاتجاه
        if ind['adx'][idx] < 20:
            return rejected(f"ADX ضعيف ({ind['adx'][idx]:.1f})")

        # بوابة 4: اتجاه EMA
        if ind['ema_f'][idx] <= ind['ema_s'][idx]:
            return rejected('EMA هابط')

        # بوابة 5: تقلب معقول (لا ميت ولا مجنون)
        atr_pct = ind['atr'][idx] / c[idx] * 100
        if atr_pct < 0.15:
            return rejected('تقلب منخفض جداً — التكاليف ستأكل الربح')
        if atr_pct > 4.0:
            return rejected('تقلب مرتفع جداً — خطر')

        # ═══ المرحلة 2: نقاط التأكيد ═══
        score = 0
        confirms = []

        if 40 <= ind['rsi'][idx] <= 65:
            score += 1; confirms.append('RSI صحي')
        elif ind['rsi'][idx] < 40:
            score += 1.5; confirms.append('RSI مرتد')
        elif ind['rsi'][idx] > 72:
            score -= 1; confirms.append('⚠️ RSI مشبع شراء')

        if ind['adx'][idx] > 28:
            score += 1.5; confirms.append('اتجاه قوي جداً')
        elif ind['adx'][idx] > 23:
            score += 1; confirms.append('اتجاه قوي')

        avg_v = v[max(0, idx-20):idx].mean()
        if v[idx] > avg_v * 1.3:
            score += 1; confirms.append('حجم مرتفع')
        elif v[idx] < avg_v * 0.6:
            score -= 1; confirms.append('⚠️ حجم ضعيف')

        pa = PriceAction.detect(idx, o, h, l, c)
        pa_bias = PriceAction.bias(pa)
        if pa_bias > 0:
            score += 1; confirms.append(f'نمط صعودي: {",".join(pa)}')
        elif pa_bias < 0:
            score -= 1.5; confirms.append('⚠️ نمط هبوطي')

        # ═══ المرحلة 3: جودة موقع الدخول ═══
        levels = SupportResistance.get_levels(h[max(0, idx-150):idx+1],
                                              l[max(0, idx-150):idx+1])
        res = SupportResistance.distance_to_nearest(c[idx], levels['resistances'])
        sup = SupportResistance.distance_to_nearest(c[idx], levels['supports'])

        # مقاومة قوية قريبة جداً فوقنا = مساحة ربح ضيقة
        if res and res['above'] and res['distance_pct'] < 0.8 and res['strength'] > 0.6:
            return rejected(f"مقاومة قوية على بعد {res['distance_pct']:.2f}%")

        if sup and not sup['above'] and sup['distance_pct'] < 1.5:
            score += 1; confirms.append('دعم قريب أسفل')

        vp = VolumeProfile.build(c[max(0, idx-150):idx+1], v[max(0, idx-150):idx+1])
        vpos = VolumeProfile.position(c[idx], vp)
        if vpos == 'ABOVE_VALUE':
            score -= 0.5; confirms.append('⚠️ ممتد فوق منطقة القيمة')
        elif vpos == 'IN_VALUE':
            score += 0.5; confirms.append('داخل منطقة القيمة')

        # ═══ القرار ═══
        if score < self.min_score:
            return rejected(f'نقاط غير كافية ({score:.1f}/{self.min_score})')

        stars = int(np.clip(round(score), 1, 5))
        a = ind['atr'][idx]

        return {
            'enter': True,
            'stars': stars,
            'score': round(float(score), 2),
            'entry': float(c[idx]),
            'stop': float(c[idx] - a * 1.8),
            'target': float(c[idx] + a * 3.6),   # R:R = 2:1
            'atr': float(a),
            'confirms': confirms,
            'regime': reg['regime'],
            'regime_conf': reg['confidence'],
            'htf': htf['bias'],
            'reason': 'مقبول'
        }


# ═══════════════════════════════════════════════════════════
# الباكتست النهائي (بتكاليف واقعية)
# ═══════════════════════════════════════════════════════════
class FinalBacktest:
    def __init__(self, capital=10000, cost_model=None, engine=None):
        self.initial = capital
        self.cost = cost_model or CostModel()
        self.engine = engine or FinalSignalEngine()
        self.sizer = PositionSizer()
        self.exit_mgr = ExitManager()
        self.guard = RiskGuard()

    def run(self, o, h, l, c, v, verbose=False) -> Dict:
        ind = {
            'ema_f': ema(c, 20), 'ema_s': ema(c, 50),
            'rsi': rsi(c, 14), 'atr': atr(h, l, c, 14),
            'adx': adx(h, l, c, 14)
        }

        balance = self.initial
        equity_curve = [balance]
        trades = []
        pos = None
        rejections = {}
        bars_per_day = 96   # 15m

        for i in range(len(c)):
            if i % bars_per_day == 0:
                self.guard.new_day()

            # ── إدارة المركز المفتوح
            if pos:
                events = self.exit_mgr.update(pos, c[i], ind['atr'][i])
                for e in events:
                    if e['type'] in ('PARTIAL_1', 'PARTIAL_2'):
                        px = self.cost.exit_price(e['price'], pos['side'])
                        gross = (px - pos['entry']) * e['qty']
                        fee = self.cost.round_trip_cost(px * e['qty']) / 2
                        balance += gross - fee
                    elif e['type'] == 'STOP_HIT':
                        px = self.cost.exit_price(e['price'], pos['side'], is_stop=True)
                        gross = (px - pos['entry']) * e['qty']
                        fee = self.cost.round_trip_cost(px * e['qty']) / 2
                        net = gross - fee
                        balance += net
                        total_pnl = net + pos['realized_pnl']
                        trades.append({
                            'entry': pos['entry'], 'exit': px,
                            'pnl': total_pnl, 'stars': pos.get('stars', 0),
                            'r': e.get('r', 0), 'bars': i - pos['bar']
                        })
                        self.guard.record_trade(total_pnl)
                        pos = None

            # ── بحث عن دخول
            if pos is None:
                ok = self.guard.can_trade(balance, 0)
                if ok['allowed']:
                    sig = self.engine.evaluate(i, o, h, l, c, v, ind)
                    if sig['enter']:
                        entry_px = self.cost.entry_price(sig['entry'], 'long')
                        sz = self.sizer.calculate(
                            balance, entry_px, sig['stop'],
                            signal_stars=sig['stars'],
                            consecutive_losses=self.guard.consecutive_losses,
                            regime_confidence=sig['regime_conf'])
                        if sz['size'] > 0 and sz['notional'] <= balance * 3:
                            entry_fee = self.cost.round_trip_cost(sz['notional']) / 2
                            balance -= entry_fee
                            pos = self.exit_mgr.init_position(
                                entry_px, sig['stop'], sz['size'], 'long')
                            pos['stars'] = sig['stars']
                            pos['bar'] = i
                    else:
                        r = sig['reason'].split('(')[0].strip()
                        rejections[r] = rejections.get(r, 0) + 1

            equity_curve.append(balance)

        return self._stats(trades, equity_curve, rejections)

    def _stats(self, trades, eq, rejections) -> Dict:
        eq = np.array(eq)
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / np.maximum(peak, 1e-9) * 100
        max_dd = float(dd.max())

        if not trades:
            return {'total_trades': 0, 'final': float(eq[-1]),
                    'max_dd': max_dd, 'rejections': rejections,
                    'note': 'لا صفقات — الفلاتر رفضت كل شيء'}

        pnls = np.array([t['pnl'] for t in trades])
        wins = pnls[pnls > 0]; losses = pnls[pnls < 0]
        gp = float(wins.sum()); gl = float(abs(losses.sum()))

        return {
            'total_trades': len(trades),
            'wins': int(len(wins)), 'losses': int(len(losses)),
            'win_rate': round(len(wins)/len(trades)*100, 2),
            'net_profit': round(float(pnls.sum()), 2),
            'return_pct': round((eq[-1]-self.initial)/self.initial*100, 2),
            'profit_factor': round(gp/gl, 2) if gl > 0 else None,
            'avg_win': round(float(wins.mean()), 2) if len(wins) else 0,
            'avg_loss': round(float(losses.mean()), 2) if len(losses) else 0,
            'expectancy': round(float(pnls.mean()), 2),
            'max_dd': round(max_dd, 2),
            'final': round(float(eq[-1]), 2),
            'rejections': dict(sorted(rejections.items(),
                                      key=lambda x: -x[1])[:6])
        }


# ═══════════════════════════════════════════════════════════
# Walk-Forward Validation
# ═══════════════════════════════════════════════════════════
class WalkForward:
    """
    ⭐ الاختبار الوحيد الذي يستحق الثقة.

    يقسم البيانات لنوافذ متتالية. كل نافذة:
      - جزء "تدريب" (لا نستخدمه للتحسين هنا، فقط للإحماء)
      - جزء "اختبار" لم يُرَ من قبل
    إذا كانت النتائج متذبذبة بشدة بين النوافذ = النموذج غير مستقر.
    """

    @staticmethod
    def run(o, h, l, c, v, n_windows=4, capital=10000) -> Dict:
        n = len(c)
        win_size = n // n_windows
        results = []

        for w in range(n_windows):
            s = w * win_size
            e = min(s + win_size, n)
            if e - s < 300:
                continue
            bt = FinalBacktest(capital=capital)
            r = bt.run(o[s:e], h[s:e], l[s:e], c[s:e], v[s:e])
            r['window'] = w + 1
            results.append(r)

        valid = [r for r in results if r.get('total_trades', 0) > 0]
        if not valid:
            return {'windows': results, 'verdict': 'لا صفقات في أي نافذة'}

        returns = [r['return_pct'] for r in valid]
        wrs = [r['win_rate'] for r in valid]
        profitable = sum(1 for x in returns if x > 0)

        consistency = profitable / len(valid)
        if consistency >= 0.75 and np.mean(returns) > 0:
            verdict = '✅ مستقر نسبياً'
        elif consistency >= 0.5:
            verdict = '⚠️ متذبذب — يحتاج حذر'
        else:
            verdict = '❌ غير مستقر — لا تستخدمه بأموال حقيقية'

        return {
            'windows': results,
            'avg_return': round(float(np.mean(returns)), 2),
            'std_return': round(float(np.std(returns)), 2),
            'avg_win_rate': round(float(np.mean(wrs)), 2),
            'profitable_windows': f'{profitable}/{len(valid)}',
            'consistency': round(consistency, 2),
            'verdict': verdict
        }
