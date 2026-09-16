"""
Breakout Entry — البندان 4 و5. معطَّل افتراضياً (BreakoutConfig.enabled).
==========================================================================
كل شرط قابل للتفعيل/التعطيل منفرداً. لا افتراض أن كل الشروط مفيدة —
القياس الفعلي لكل شرط مسؤولية STRATEGY_EXPERIMENTS، لا هذا الملف.
"""
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from .base import EntrySignal, BREAKOUT, BREAKOUT_RETEST, NO_TRADE, SubScores
from ..no_trade import NoTradeEngine, R
from ...risk.position_sizing import net_risk_reward as _net_rr

# ── حالات آلة Retest ──
NONE = 'NONE'
BREAKOUT_DETECTED = 'BREAKOUT_DETECTED'
RETEST_WAITING = 'RETEST_WAITING'
RETEST_CONFIRMED = 'RETEST_CONFIRMED'
ENTERED = 'ENTERED'
BREAKOUT_FAILED = 'BREAKOUT_FAILED'
EXPIRED = 'EXPIRED'


def get_breakout_level(data, idx: int, lookback: int) -> Dict[str, Any]:
    """
    مستوى اختراق سببي بحت — `high[idx-lookback : idx]`، **يستبعد
    الشمعة idx نفسها عمداً**. تضمين idx يجعل "الاختراق" مستحيلاً
    منطقياً (الشمعة لا يمكن أن تخترق أعلى نقطة فيها هي)، وهذا بالضبط
    فخ look-ahead دقيق يسهل الوقوع فيه بالخطأ.
    """
    start = max(0, idx - lookback)
    if start >= idx:
        return {'level': None, 'source': 'rolling_high', 'lookback': lookback,
               'calculated_until': idx - 1, 'available': False}
    window = data.high[start:idx]
    if len(window) == 0 or not np.any(np.isfinite(window)):
        return {'level': None, 'source': 'rolling_high', 'lookback': lookback,
               'calculated_until': idx - 1, 'available': False}
    return {'level': float(np.nanmax(window)), 'source': 'rolling_high',
           'lookback': lookback, 'calculated_until': idx - 1, 'available': True}


def _body_and_wicks(o: float, h: float, l: float, c: float) -> Dict[str, float]:
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    rng = max(h - l, 1e-12)
    return {'body': body, 'upper_wick': max(upper_wick, 0.0),
           'lower_wick': max(lower_wick, 0.0), 'range': rng,
           'upper_wick_ratio': max(upper_wick, 0.0) / max(body, 1e-9)}


class BreakoutEntryModel:
    """
    `evaluate()` يُنتج EntrySignal للحظة idx فقط — لا حالة داخلية بين
    الاستدعاءات هنا؛ إدارة Retest (إن فُعِّلت) في `entry_setups` عبر
    `db` المُمرَّر في `context`، لا في هذا الكائن.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.no_trade = NoTradeEngine(cfg.no_trade, cfg.risk)

    def evaluate(self, data, idx: int, context: Dict[str, Any]) -> EntrySignal:
        bc = self.cfg.breakout
        diag: Dict[str, Any] = {}
        reasons: List[str] = []

        if not bc.enabled:
            return EntrySignal.rejected(BREAKOUT, ['BREAKOUT_DISABLED'])

        prep = context['prep']
        ind, st = prep['ind'], prep['st']
        atr = float(ind['atr'][idx]) if np.isfinite(ind['atr'][idx]) else None
        if atr is None or atr <= 0:
            return EntrySignal.rejected(BREAKOUT, [R['WARMUP']], diag)

        lvl = get_breakout_level(data, idx, bc.lookback)
        diag['breakout_level'] = lvl
        if not lvl['available']:
            return EntrySignal.rejected(
                BREAKOUT, ['BREAKOUT_RESISTANCE_UNAVAILABLE'], diag)
        resistance = lvl['level']

        o, h, l, c = (float(data.open[idx]), float(data.high[idx]),
                      float(data.low[idx]), float(data.close[idx]))
        candle = _body_and_wicks(o, h, l, c)
        diag['candle'] = candle

        rejections: List[str] = []

        # ── مجرد اللمس ليس اختراقاً: الإغلاق يجب أن يتجاوز المستوى ──
        if c <= resistance:
            rejections.append('BREAKOUT_NOT_YET_ABOVE_LEVEL')

        distance_atr = (c - resistance) / atr
        diag['breakout_distance_atr'] = round(distance_atr, 4)

        if c > resistance:
            if distance_atr < bc.min_distance_atr:
                rejections.append('BREAKOUT_DISTANCE_TOO_SMALL')
            if bc.extension_filter_enabled and distance_atr > bc.max_distance_atr:
                # إغلاق بعيد جداً فوق المستوى دفعة واحدة — قد يكون
                # حركة استثنائية لا اختراقاً نظيفاً، أو دخولاً متأخراً
                rejections.append('BREAKOUT_EXTENDED_NO_CHASE')

        # ── جسم الشمعة وفتيلها ──
        if bc.body_filter_enabled and candle['body'] < 0.15 * candle['range']:
            rejections.append('BREAKOUT_NO_REAL_BODY')

        if (bc.candle_size_filter_enabled
                and candle['range'] > bc.max_candle_range_atr * atr):
            # شمعة أكبر بكثير من ATR — حركة استثنائية/فجوة، لا اختراق
            # نظيف قابل للاعتماد عليه لتحديد وقف منطقي
            rejections.append('BREAKOUT_CANDLE_TOO_LARGE')

        if bc.wick_filter_enabled and candle['upper_wick_ratio'] > bc.max_upper_wick_ratio:
            rejections.append('BREAKOUT_UPPER_WICK_TOO_LARGE')

        # ── Spread — نفس الحد المشترك المُستخدَم في كل مكان آخر ──
        spread_bps = context.get('spread_bps')
        if spread_bps is not None and spread_bps > self.cfg.no_trade.max_spread_bps:
            rejections.append('BREAKOUT_SPREAD_TOO_HIGH')

        # ── الحجم ──
        rv = ind.get('rel_volume')
        if bc.volume_filter_enabled:
            v = float(rv[idx]) if rv is not None and np.isfinite(rv[idx]) else None
            diag['rel_volume'] = v
            if v is None or v < bc.volume_multiplier:
                rejections.append('BREAKOUT_VOLUME_TOO_LOW')

        # ── مقاومة أعلى قريبة (بعد الاختراق) ──
        res_v = st['resistance'][idx]
        if bc.resistance_distance_filter_enabled and np.isfinite(res_v) and res_v > c:
            higher_dist_pct = (res_v - c) / c * 100
            diag['higher_resistance_distance_pct'] = round(higher_dist_pct, 4)
            if higher_dist_pct < bc.higher_resistance_min_distance_pct:
                rejections.append('BREAKOUT_HIGHER_RESISTANCE_TOO_CLOSE')

        # ── الاتجاه/الزخم/النظام — من الأدلة المحسوبة أصلاً (سببياً) ──
        ef, es = ind['ema_fast'][idx], ind['ema_slow'][idx]
        trend_up = bool(np.isfinite(ef) and np.isfinite(es) and ef > es)
        if not trend_up:
            rejections.append('BREAKOUT_TREND_NOT_SUPPORTIVE')

        from ...market.regime import detect_at
        reg = detect_at(data.close, idx, self.cfg.regime, ind['adx'], data.interval)
        diag['regime'] = reg.regime
        if not reg.long_friendly:
            rejections.append('BREAKOUT_REGIME_INVALID')

        macd_ok = (np.isfinite(ind['macd'][idx]) and np.isfinite(ind['macd_signal'][idx])
                   and ind['macd'][idx] > ind['macd_signal'][idx])

        htf_aligned = None
        mtf = context.get('mtf')
        if bc.htf_confirmation_enabled and mtf is not None:
            biases = [mtf.bias_at(tf, idx) for tf in mtf.frames]
            avail = [b for b in biases if b.get('available')]
            htf_aligned = all(b['aligned_long'] for b in avail) if avail else None
            if htf_aligned is False:
                rejections.append('BREAKOUT_HTF_NOT_ALIGNED')

        # ── الوقف والهدف ──
        entry = c
        if bc.stop_mode == 'level':
            stop = resistance - bc.stop_buffer_atr * atr
        elif bc.stop_mode == 'hybrid':
            stop = min(entry - bc.stop_buffer_atr * atr * 2,
                      resistance - bc.stop_buffer_atr * atr)
        else:  # atr
            stop = entry - self.cfg.signal.atr_stop_mult * atr
        target = entry + self.cfg.signal.atr_target_mult * atr
        if np.isfinite(res_v) and res_v > entry:
            target = min(target, res_v * 0.999)

        # ── صلاحية الوقف الصريحة — قبل أي حساب R:R ──
        stop_pct = (entry - stop) / entry * 100 if entry > 0 else None
        stop_valid = (entry > stop and stop_pct is not None
                     and self.cfg.no_trade.min_stop_distance_pct
                        <= stop_pct <= self.cfg.no_trade.max_stop_distance_pct)
        if not stop_valid:
            rejections.append('BREAKOUT_STOP_INVALID')

        net_rr = None
        if entry > stop and target > entry:
            net_rr = _net_rr(entry, stop, target, self.cfg.costs)
        diag['net_risk_reward'] = net_rr

        if net_rr is None or net_rr < self.cfg.signal.min_rr:
            rejections.append('BREAKOUT_RR_INSUFFICIENT')

        # ── البوابة المشتركة (نفس المستخدَمة في Baseline) — لا تجاوز ──
        acc = context.get('account_state', {})
        v = self.no_trade.check(
            data_quality=context.get('data_quality', 1.0),
            bars_available=idx + 1, warmup=self.cfg.signal.warmup_bars,
            atr_pct=float(ind['atr_pct'][idx]) if np.isfinite(ind['atr_pct'][idx]) else None,
            regime_long_friendly=reg.long_friendly, regime_name=reg.regime,
            htf_aligned=htf_aligned,
            btc_ok=(None if context.get('btc_ctx') is None
                   else context['btc_ctx'].trend_ok),
            btc_available=bool(context.get('btc_ctx') is not None
                              and context['btc_ctx'].available),
            resistance_distance_pct=None, stop_distance_pct=stop_pct,
            risk_reward=net_rr, min_rr=self.cfg.signal.min_rr,
            score=6.0, min_score=0.0,   # الفلترة النوعية تمت أعلاه بأسباب مخصَّصة
            confidence=0.5, probability=None,
            spread_bps=context.get('spread_bps'),
            open_positions=acc.get('open_positions', 0),
            daily_trades=acc.get('daily_trades', 0),
            daily_loss_hit=acc.get('daily_loss_hit', False),
            consecutive_losses=acc.get('consecutive_losses', 0),
            exposure_ok=acc.get('exposure_ok', True))
        rejections.extend(v.reasons)

        sub = SubScores(
            trend_score=2.0 if trend_up else -1.0,
            momentum_score=1.0 if macd_ok else 0.0,
            structure_score=max(0.0, 2.0 - abs(distance_atr - 0.5)),
            risk_score=1.0 if (net_rr and net_rr >= self.cfg.signal.min_rr) else -1.0,
            execution_score=1.0 if not rejections else -1.0)

        eligible = len(rejections) == 0
        return EntrySignal(
            setup_type=BREAKOUT, eligible=eligible,
            score=sub.final_score(), confidence=None,
            entry_price=entry if eligible else None,
            stop_loss=stop if eligible else None,
            take_profit=target if eligible else None,
            net_risk_reward=net_rr,
            reasons=(['BREAKOUT_CONFIRMED'] if eligible else []),
            rejection_reasons=rejections, diagnostics=diag, sub_scores=sub)


# ══ آلة حالة Retest — البند 5 ══

def _setup_id(symbol: str, interval: str, bar_time: int, level: float) -> str:
    import hashlib
    seed = f'{symbol}|{interval}|{bar_time}|{round(level, 8)}'
    return 'bo_' + hashlib.sha256(seed.encode()).hexdigest()[:20]


def process_breakout_retest(data, idx: int, context: Dict[str, Any],
                            base_signal: EntrySignal) -> EntrySignal:
    """
    يُستدعى بعد `BreakoutEntryModel.evaluate()`. إن كان Retest معطَّلاً،
    يُعيد `base_signal` كما هو (دخول فوري عند الاختراق، كالسابق).
    إن فُعِّل: **لا دخول فوري** — يُسجَّل Setup وينتظر إعادة اختبار
    المستوى، والقرار يُتَّخذ عند اكتمال الشرط فعلياً في الشمعة التي
    يتحقق فيها، لا بأثر رجعي.
    """
    bc = context['cfg'].breakout
    if not bc.retest_enabled:
        return base_signal

    db = context.get('db')
    symbol, interval = context['symbol'], context['interval']
    now_ms = context.get('now_ms') or int(data.open_time[idx])
    prep = context['prep']
    atr = float(prep['ind']['atr'][idx])
    interval_ms = context.get('interval_ms', 0)

    # ── 1) إشارة اختراق جديدة صالحة الآن ⇒ سجّل Setup وانتظر ──
    if base_signal.eligible and db is not None:
        lvl = base_signal.diagnostics['breakout_level']['level']
        sid = _setup_id(symbol, interval, int(data.open_time[idx]), lvl)
        expiry = now_ms + bc.retest_window_bars * max(interval_ms, 1)
        created = db.reserve_setup(
            setup_id=sid, setup_type=BREAKOUT, state=BREAKOUT_DETECTED,
            symbol=symbol, interval=interval,
            strategy_version=context['cfg'].version,
            config_fingerprint=context['cfg'].fingerprint(),
            breakout_level=lvl, expiry_at=expiry,
            detail='breakout_close={:.8f}'.format(data.close[idx]))
        if created:
            return EntrySignal.rejected(
                BREAKOUT_RETEST, ['BREAKOUT_RETEST_WAITING'],
                {**base_signal.diagnostics, 'setup_id': sid, 'state': RETEST_WAITING})

    # ── 2) تقدّم أي Setups نشطة لهذا الرمز/الفريم ──
    if db is None:
        return EntrySignal.rejected(BREAKOUT_RETEST, ['NO_DB_CONTEXT'])

    active = db.active_setups(symbol, interval, setup_type=BREAKOUT)
    c = float(data.close[idx]); l = float(data.low[idx])
    for s in active:
        sid = s['setup_id']
        if s['state'] == BREAKOUT_DETECTED:
            db.transition_setup_state(sid, RETEST_WAITING, reason='دورة تالية')
            s = db.get_setup(sid)

        if s['expiry_at'] and now_ms > s['expiry_at']:
            db.transition_setup_state(sid, EXPIRED, reason='BREAKOUT_RETEST_EXPIRED')
            continue

        level = s['breakout_level']
        # كسر واضح تحت المستوى ⇒ فشل، لا انتظار أبعد
        if c < level - bc.retest_tolerance_atr * atr:
            db.transition_setup_state(sid, BREAKOUT_FAILED,
                                      reason='BREAKOUT_RETEST_SUPPORT_BROKEN')
            continue

        touched = l <= level + bc.retest_tolerance_atr * atr
        bullish_confirm = c > float(data.open[idx]) and c >= level

        if touched and s['state'] == RETEST_WAITING:
            if bullish_confirm:
                db.transition_setup_state(sid, RETEST_CONFIRMED,
                                          reason='شمعة تأكيد صاعدة')
                entry = c
                stop = level - bc.stop_buffer_atr * atr
                target = entry + context['cfg'].signal.atr_target_mult * atr
                net_rr = (_net_rr(entry, stop, target, context['cfg'].costs)
                         if entry > stop else None)
                if net_rr is None or net_rr < context['cfg'].signal.min_rr:
                    db.transition_setup_state(
                        sid, BREAKOUT_FAILED, reason='BREAKOUT_RETEST_RR_INSUFFICIENT')
                    continue
                # حجز ذرّي لمنع دخول مزدوج — إن كان مُستهلَكاً بالفعل من
                # استدعاء متزامن/سابق، لا نُصدر إشارة أهلية ثانية أبداً.
                if not db.mark_setup_entered(sid):
                    continue
                db.transition_setup_state(sid, ENTERED, reason='دخول مؤكَّد')
                return EntrySignal(
                    setup_type=BREAKOUT_RETEST, eligible=True,
                    score=base_signal.score, entry_price=entry,
                    stop_loss=stop, take_profit=target, net_risk_reward=net_rr,
                    reasons=['BREAKOUT_RETEST_CONFIRMED'],
                    diagnostics={'setup_id': sid, 'breakout_level': level},
                    setup_id=sid)

    return EntrySignal.rejected(BREAKOUT_RETEST, ['BREAKOUT_RETEST_WAITING'])
