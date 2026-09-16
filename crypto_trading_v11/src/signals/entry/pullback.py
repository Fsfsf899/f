"""
Pullback Entry — البند 6. معطَّل افتراضياً (PullbackConfig.enabled).
=====================================================================
لا كل هبوط Pullback. يجب إثبات اتجاه صاعد أولاً، ثم منطقة تراجع
محدَّدة سببياً، ثم شمعة تأكيد فعلية — لا افتراض نجاح Pullback قبل
حدوثه.
"""
from typing import Any, Dict, List, Optional

import numpy as np

from .base import EntrySignal, PULLBACK, SubScores
from ..no_trade import NoTradeEngine, R
from ...risk.position_sizing import net_risk_reward as _net_rr


def _uptrend_score(ind, idx: int, reg) -> Dict[str, Any]:
    """
    كل شرط يُسجَّل مساهمته منفصلة — البند 6.1: "كل شرط يجب تسجيل
    مساهمته".
    """
    contributions: Dict[str, bool] = {}
    ef, es = ind['ema_fast'][idx], ind['ema_slow'][idx]
    contributions['ema_aligned'] = bool(np.isfinite(ef) and np.isfinite(es) and ef > es)

    ef_prev = ind['ema_fast'][max(0, idx - 5)]
    contributions['ema_slope_positive'] = bool(
        np.isfinite(ef) and np.isfinite(ef_prev) and ef > ef_prev)

    adx_v = ind['adx'][idx]
    contributions['adx_trending'] = bool(np.isfinite(adx_v) and adx_v >= 20)

    macd_ok = (np.isfinite(ind['macd'][idx]) and np.isfinite(ind['macd_signal'][idx])
               and ind['macd'][idx] > ind['macd_signal'][idx])
    contributions['macd_bullish'] = bool(macd_ok)
    contributions['regime_long_friendly'] = bool(reg.long_friendly)

    n_true = sum(1 for v in contributions.values() if v)
    return {'contributions': contributions, 'confirmed': n_true >= 3,
           'n_confirmed': n_true, 'n_total': len(contributions)}


def _pullback_zone(cfg_pb, ind, st, idx: int) -> Dict[str, Any]:
    """
    منطقة التراجع — سببية بالكامل (لا قيمة من `idx` نفسها أو ما بعدها
    غير ما هو معروف حتى هذه اللحظة).
    """
    zt = cfg_pb.zone_type
    if zt == 'ema_slow':
        center = ind['ema_slow'][idx]
    elif zt == 'structure':
        center = st['support'][idx]
    else:  # ema_fast (افتراضي)
        center = ind['ema_fast'][idx]

    if not np.isfinite(center):
        return {'zone_type': zt, 'available': False}

    atr = ind['atr'][idx]
    half = cfg_pb.zone_atr * (atr if np.isfinite(atr) else 0.0)
    return {'zone_type': zt, 'zone_price_low': float(center - half),
           'zone_price_high': float(center + half),
           'zone_price_center': float(center), 'available': True}


class PullbackEntryModel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.no_trade = NoTradeEngine(cfg.no_trade, cfg.risk)

    def evaluate(self, data, idx: int, context: Dict[str, Any]) -> EntrySignal:
        pc = self.cfg.pullback
        diag: Dict[str, Any] = {}
        if not pc.enabled:
            return EntrySignal.rejected(PULLBACK, ['PULLBACK_DISABLED'])

        prep = context['prep']
        ind, st = prep['ind'], prep['st']
        atr = float(ind['atr'][idx]) if np.isfinite(ind['atr'][idx]) else None
        if atr is None or atr <= 0:
            return EntrySignal.rejected(PULLBACK, [R['WARMUP']], diag)

        from ...market.regime import detect_at
        reg = detect_at(data.close, idx, self.cfg.regime, ind['adx'], data.interval)

        # ── 1) إثبات اتجاه صاعد أولاً — البند 6.1 ──
        trend = _uptrend_score(ind, idx, reg)
        diag['uptrend'] = trend
        rejections: List[str] = []
        if not trend['confirmed']:
            rejections.append('PULLBACK_NO_UPTREND')

        # ── 2) منطقة التراجع — البند 6.2 ──
        zone = _pullback_zone(pc, ind, st, idx)
        diag['zone'] = zone
        c = float(data.close[idx]); l = float(data.low[idx]); o = float(data.open[idx])

        in_zone = (zone.get('available') and
                  zone['zone_price_low'] <= l <= zone['zone_price_high'] * 1.02)
        diag['in_zone'] = bool(in_zone)

        depth_atr = None
        if zone.get('available'):
            # عمق التراجع من آخر قمة معروفة سببياً (أعلى إغلاق خلال نافذة قصيرة ماضية)
            lookback_start = max(0, idx - 20)
            recent_high = float(np.nanmax(data.high[lookback_start:idx])) \
                if idx > lookback_start else c
            depth_atr = (recent_high - l) / atr if atr > 0 else None
            diag['depth_atr'] = round(depth_atr, 4) if depth_atr is not None else None
            if depth_atr is not None and depth_atr > pc.max_depth_atr:
                rejections.append('PULLBACK_TOO_DEEP')

        # ── عمر التراجع — كان max_age_bars معرَّفاً بلا استخدام فعلي.
        # لا حالة محفوظة هنا (النموذج بلا حالة بين الشموع بالتصميم)،
        # فنستخدم بديلاً سببياً: عدد الشموع منذ آخر إغلاق كان أعلى من
        # سقف المنطقة بوضوح — تقريب معقول لـ"متى بدأ التراجع فعلياً"
        # بلا الحاجة لآلة حالة كاملة كما في Breakout Retest.
        age_bars = None
        if zone.get('available'):
            scan_start = max(0, idx - (pc.max_age_bars + 10))
            above = None
            for j in range(idx - 1, scan_start - 1, -1):
                if float(data.close[j]) > zone['zone_price_high']:
                    above = j
                    break
            age_bars = (idx - above) if above is not None else None
            diag['age_bars'] = age_bars
            if age_bars is None or age_bars > pc.max_age_bars:
                rejections.append('PULLBACK_EXPIRED')

        if not in_zone:
            rejections.append('PULLBACK_NOT_IN_ZONE')

        # ── 3) عدم كسر الدعم / Swing Low ──
        sup_v = st['support'][idx]
        support_broken = bool(np.isfinite(sup_v) and
                              c < sup_v - pc.support_tolerance_atr * atr)
        if pc.support_filter_enabled and support_broken:
            rejections.append('PULLBACK_SUPPORT_BROKEN')

        recent_swings = [e for e in st['swing_events']
                         if e.confirmation_index <= idx and e.type in ('HL', 'LL')][-1:]
        if recent_swings:
            last_low = recent_swings[0].price
            if pc.support_filter_enabled and c < last_low - pc.support_tolerance_atr * atr:
                rejections.append('PULLBACK_SWING_LOW_BROKEN')

        # ── 4) شمعة تأكيد — البند 6.4 ──
        confirmed_candle = c > o and c >= float(data.high[max(0, idx - 1)]) * 0.999
        # بديل أبسط أيضاً مقبول: إغلاق صاعد بعد لمس المنطقة
        bullish_close = c > o
        confirmation = confirmed_candle or (in_zone and bullish_close)
        diag['confirmation_candle'] = bool(confirmation)
        if pc.confirmation_required and not confirmation:
            rejections.append('PULLBACK_NO_CONFIRMATION')

        # ── مقاومة قريبة ──
        res_v = st['resistance'][idx]
        if np.isfinite(res_v) and res_v > c:
            dist_pct = (res_v - c) / c * 100
            if dist_pct < self.cfg.no_trade.min_resistance_distance_pct:
                rejections.append('PULLBACK_RESISTANCE_TOO_CLOSE')

        if not reg.long_friendly and pc.regime_filter_enabled:
            rejections.append('PULLBACK_REGIME_INVALID')

        dq = context.get('data_quality', 1.0)
        if dq < self.cfg.no_trade.min_data_quality:
            rejections.append('PULLBACK_DATA_QUALITY_LOW')

        spread_bps = context.get('spread_bps')
        if spread_bps is not None and spread_bps > self.cfg.no_trade.max_spread_bps:
            rejections.append('PULLBACK_SPREAD_TOO_HIGH')

        # ── الوقف والهدف ──
        entry = c
        if pc.stop_mode == 'structure' and recent_swings:
            stop = recent_swings[0].price - pc.stop_buffer_atr * atr
        elif pc.stop_mode == 'hybrid' and recent_swings:
            stop = min(entry - self.cfg.signal.atr_stop_mult * atr,
                      recent_swings[0].price - pc.stop_buffer_atr * atr)
        else:
            stop = entry - self.cfg.signal.atr_stop_mult * atr
        target = entry + self.cfg.signal.atr_target_mult * atr
        if np.isfinite(res_v) and res_v > entry:
            target = min(target, res_v * 0.999)

        net_rr = None
        if entry > stop and target > entry:
            net_rr = _net_rr(entry, stop, target, self.cfg.costs)
        if pc.net_rr_filter_enabled and (net_rr is None or net_rr < self.cfg.signal.min_rr):
            rejections.append('PULLBACK_RR_INSUFFICIENT')
        stop_pct = (entry - stop) / entry * 100 if entry > stop else None

        stop_valid = (entry > stop and stop_pct is not None
                     and self.cfg.no_trade.min_stop_distance_pct
                        <= stop_pct <= self.cfg.no_trade.max_stop_distance_pct)
        if not stop_valid:
            rejections.append('PULLBACK_STOP_INVALID')

        # ── البوابة المشتركة ──
        acc = context.get('account_state', {})
        v = self.no_trade.check(
            data_quality=dq, bars_available=idx + 1,
            warmup=self.cfg.signal.warmup_bars,
            atr_pct=float(ind['atr_pct'][idx]) if np.isfinite(ind['atr_pct'][idx]) else None,
            regime_long_friendly=reg.long_friendly, regime_name=reg.regime,
            htf_aligned=None,
            btc_ok=(None if context.get('btc_ctx') is None
                   else context['btc_ctx'].trend_ok),
            btc_available=bool(context.get('btc_ctx') is not None
                              and context['btc_ctx'].available),
            resistance_distance_pct=None, stop_distance_pct=stop_pct,
            risk_reward=net_rr, min_rr=self.cfg.signal.min_rr,
            score=6.0, min_score=0.0, confidence=0.5, probability=None,
            spread_bps=context.get('spread_bps'),
            open_positions=acc.get('open_positions', 0),
            daily_trades=acc.get('daily_trades', 0),
            daily_loss_hit=acc.get('daily_loss_hit', False),
            consecutive_losses=acc.get('consecutive_losses', 0),
            exposure_ok=acc.get('exposure_ok', True))
        rejections.extend(v.reasons)

        sub = SubScores(
            trend_score=float(trend['n_confirmed']),
            momentum_score=1.0 if trend['contributions'].get('macd_bullish') else 0.0,
            structure_score=2.0 if in_zone and not support_broken else -1.0,
            risk_score=1.0 if (net_rr and net_rr >= self.cfg.signal.min_rr) else -1.0,
            execution_score=1.0 if confirmation else -1.0)

        eligible = len(rejections) == 0
        return EntrySignal(
            setup_type=PULLBACK, eligible=eligible,
            score=sub.final_score(), confidence=None,
            entry_price=entry if eligible else None,
            stop_loss=stop if eligible else None,
            take_profit=target if eligible else None,
            net_risk_reward=net_rr,
            reasons=(['PULLBACK_CONFIRMED'] if eligible else []),
            rejection_reasons=rejections, diagnostics=diag, sub_scores=sub)
