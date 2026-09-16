"""
محرك الإشارات الوحيد — البندان 15 و40.
=======================================
نفس هذا الملف يُستخدم في الباكتست والتحليل الحي. لا نسخة ثانية.

القرار: BUY | WAIT | NO_TRADE   (Spot فقط — لا SHORT)

  NO_TRADE : فحص منع أوقف كل شيء
  WAIT     : لا مانع، لكن لا يوجد إعداد صالح الآن
  BUY      : إعداد صالح واجتاز كل الفحوص
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from ..core.config import Config, DEFAULT
from ..data.types import OHLCV
from ..indicators.engine import IndicatorEngine
from ..market.structure import StructureEngine, levels_at
from ..market.regime import detect_at
from ..market.mtf import MultiTimeframe
from .scoring import Evidence, aggregate, normalized_score, ScoreResult
from .no_trade import NoTradeEngine, Verdict, R
from ..risk.position_sizing import net_risk_reward as _net_rr

BUY, WAIT, NO_TRADE = 'BUY', 'WAIT', 'NO_TRADE'


@dataclass
class Signal:
    index: int
    timestamp: int
    symbol: str
    interval: str
    decision: str
    strategy_version: str
    score: float = 0.0
    stars: int = 1
    confidence: float = 0.0
    raw_probability: Optional[float] = None
    calibrated_probability: Optional[float] = None
    probability_source: str = 'UNCALIBRATED'
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward: Optional[float] = None
    net_risk_reward: Optional[float] = None
    stop_method_used: str = 'atr'
    atr: Optional[float] = None
    regime: str = 'UNKNOWN'
    data_quality: float = 0.0
    btc_context: str = 'UNKNOWN'
    evidence: List[str] = field(default_factory=list)
    evidence_against: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        for k in ('score', 'confidence', 'entry', 'stop_loss', 'take_profit',
                  'risk_reward', 'atr', 'raw_probability', 'calibrated_probability'):
            if isinstance(d.get(k), float):
                d[k] = round(d[k], 6)
        return d


class SignalEngine:
    def __init__(self, cfg: Optional[Config] = None,
                 calibrator=None, mtf: Optional[MultiTimeframe] = None):
        self.cfg = cfg or DEFAULT
        self.indicators = IndicatorEngine(self.cfg.signal)
        self.structure = StructureEngine(self.cfg.structure)
        self.no_trade = NoTradeEngine(self.cfg.no_trade, self.cfg.risk)
        self.calibrator = calibrator      # اختياري؛ None ⇒ لا احتمال معروض
        self.mtf = mtf
        self._prep: Dict[str, Dict] = {}

    # ── تحضير لمرة واحدة لكل سلسلة ──
    def prepare(self, data: OHLCV, key: Optional[str] = None) -> Dict:
        k = key or f"{data.symbol}_{data.interval}_{len(data)}_{int(data.open_time[-1])}"
        if k in self._prep:
            return self._prep[k]
        ind = self.indicators.compute(data, key=k)
        st = self.structure.compute(data.high, data.low, data.close, data.open_time)
        self._prep[k] = {'ind': ind, 'st': st}
        return self._prep[k]

    # ── التقييم عند شمعة واحدة ──
    def evaluate(self, data: OHLCV, idx: int, *,
                 data_quality: float = 1.0,
                 account_state: Optional[Dict] = None,
                 btc_ctx=None, spread_bps: Optional[float] = None,
                 prep: Optional[Dict] = None) -> Signal:
        cfg = self.cfg
        prep = prep or self.prepare(data)
        ind, st = prep['ind'], prep['st']
        acc = account_state or {}
        ts = int(data.open_time[idx])

        sig = Signal(index=idx, timestamp=ts, symbol=data.symbol,
                     interval=data.interval, decision=NO_TRADE,
                     strategy_version=cfg.version, data_quality=data_quality)

        warmup = max(cfg.signal.warmup_bars, self.indicators.warmup_bars())
        if idx < warmup:
            sig.reasons = [R['WARMUP']]
            return sig

        c = float(data.close[idx])
        atr = float(ind['atr'][idx]) if np.isfinite(ind['atr'][idx]) else np.nan
        atr_pct = float(ind['atr_pct'][idx]) if np.isfinite(ind['atr_pct'][idx]) else np.nan
        sig.atr = None if np.isnan(atr) else atr

        adx_series = ind['adx']
        reg = detect_at(data.close, idx, cfg.regime, adx_series, data.interval)
        sig.regime = reg.regime

        # ── الإطار الأعلى
        htf_aligned = None
        if self.mtf is not None:
            biases = [self.mtf.bias_at(tf, idx) for tf in self.mtf.frames]
            avail = [b for b in biases if b.get('available')]
            if avail:
                htf_aligned = all(b['aligned_long'] for b in avail)

        # ── المستويات (سببية)
        res_v = st['resistance'][idx]
        sup_v = st['support'][idx]
        res_dist = (float((res_v - c) / c * 100)
                    if np.isfinite(res_v) and res_v > c else None)

        # ── الأدلة
        ev: List[Evidence] = []
        ef, es = ind['ema_fast'][idx], ind['ema_slow'][idx]
        ema_cross = (np.isfinite(ind['ema_fast'][idx-1]) and
                     ind['ema_fast'][idx-1] <= ind['ema_slow'][idx-1] and ef > es)
        ev.append(Evidence('EMA_TREND', 2.0, bool(np.isfinite(ef) and ef > es),
                           'صاعد' if np.isfinite(ef) and ef > es else 'هابط'))
        ev.append(Evidence('EMA_CROSS', 1.0, bool(ema_cross), 'تقاطع جديد'))

        r = ind['rsi'][idx]
        ev.append(Evidence('RSI_HEALTHY', 1.0,
                           bool(np.isfinite(r) and 40 <= r <= 65), f"{r:.1f}" if np.isfinite(r) else ''))
        ev.append(Evidence('RSI_OVERBOUGHT', -1.5, bool(np.isfinite(r) and r > 75),
                           f"{r:.1f}" if np.isfinite(r) else ''))

        adx_v = adx_series[idx]
        ev.append(Evidence('ADX_STRONG', 1.5,
                           bool(np.isfinite(adx_v) and adx_v >= 25),
                           f"{adx_v:.1f}" if np.isfinite(adx_v) else ''))
        ev.append(Evidence('DI_BULLISH', 0.5,
                           bool(np.isfinite(ind['di_plus'][idx]) and
                                ind['di_plus'][idx] > ind['di_minus'][idx])))

        macd_ok = (np.isfinite(ind['macd'][idx]) and np.isfinite(ind['macd_signal'][idx])
                   and ind['macd'][idx] > ind['macd_signal'][idx])
        ev.append(Evidence('MACD_BULLISH', 1.0, bool(macd_ok)))

        rv = ind['rel_volume'][idx]
        ev.append(Evidence('VOLUME_HIGH', 1.0, bool(np.isfinite(rv) and rv >= 1.3),
                           f"{rv:.2f}x" if np.isfinite(rv) else ''))
        ev.append(Evidence('VOLUME_WEAK', -1.0, bool(np.isfinite(rv) and rv < 0.6)))

        recent = [e for e in st['swing_events'] if e.confirmation_index <= idx][-4:]
        bull_struct = sum(1 for e in recent if e.type in ('HH', 'HL'))
        ev.append(Evidence('STRUCTURE_BULLISH', 1.5, bull_struct >= 3,
                           f"{bull_struct}/4 HH-HL"))

        breaks = [e for e in st['break_events'] if e.index <= idx][-3:]
        ev.append(Evidence('RECENT_BREAKOUT', 1.0,
                           any(e.type in ('BREAKOUT', 'RETEST') for e in breaks)))
        ev.append(Evidence('FAILED_BREAKOUT', -1.5,
                           any(e.type == 'FAILED_BREAKOUT' for e in breaks)))

        ev.append(Evidence('SUPPORT_NEAR', 1.0,
                           bool(np.isfinite(sup_v) and (c - sup_v) / c * 100 < 2.0)))
        ev.append(Evidence('RESISTANCE_NEAR', -1.5,
                           res_dist is not None and res_dist < 1.0,
                           f"{res_dist:.2f}%" if res_dist is not None else ''))
        ev.append(Evidence('REGIME_BULLISH', 1.5, bool(reg.long_friendly), reg.regime))
        ev.append(Evidence('HTF_ALIGNED', 1.0, htf_aligned is True))

        evaluable = [e.name for e in ev
                     if not (e.name in ('HTF_ALIGNED',) and htf_aligned is None)]
        sr: ScoreResult = aggregate(ev, data_quality, evaluable)

        sig.score, sig.stars = sr.score, sr.stars
        sig.confidence = sr.confidence
        sig.evidence = sr.evidence_present
        sig.evidence_against = sr.evidence_against

        # ── الاحتمال: من المعايرة فقط
        raw = normalized_score(sr)
        sig.raw_probability = raw
        if self.calibrator is not None and getattr(self.calibrator, 'is_fitted', False):
            sig.calibrated_probability = float(self.calibrator.predict_one(raw))
            sig.probability_source = self.calibrator.method
        else:
            sig.calibrated_probability = None
            sig.probability_source = 'UNCALIBRATED'

        # ── مستويات الصفقة
        if np.isfinite(atr) and atr > 0:
            entry = c
            atr_stop = entry - cfg.signal.atr_stop_mult * atr
            stop = atr_stop
            method = 'atr'

            if cfg.signal.stop_method in ('structure', 'hybrid'):
                # آخر قعر مؤكَّد سببياً (نفس نمط STRUCTURE_BULLISH أعلاه —
                # لا نستخدم أي swing لم يُؤكَّد بعد الشمعة idx)
                lows = [e for e in st['swing_events']
                       if e.confirmation_index <= idx and e.type in ('HL', 'LL')
                       and e.price < entry]
                if lows:
                    swing_low = max(lows, key=lambda e: e.confirmation_index).price
                    structure_stop = swing_low - cfg.signal.stop_buffer_atr * atr
                    if cfg.signal.stop_method == 'structure':
                        stop, method = structure_stop, 'structure'
                    else:  # hybrid — الأبعد بين الاثنين (الأكثر تحفّظاً)
                        stop = min(atr_stop, structure_stop)
                        method = 'hybrid'

            target = entry + cfg.signal.atr_target_mult * atr
            if res_dist is not None and np.isfinite(res_v) and res_v > entry:
                target = min(target, float(res_v) * 0.999)
            rr = (target - entry) / (entry - stop) if entry > stop else None
            net_rr = (_net_rr(entry, stop, target, cfg.costs)
                     if entry > stop and target > entry else None)
            stop_pct = (entry - stop) / entry * 100 if entry > 0 else None
            sig.entry, sig.stop_loss, sig.take_profit = entry, stop, target
            sig.risk_reward, sig.net_risk_reward = rr, net_rr
            sig.stop_method_used = method
            # الفلتر يستخدم R/R الصافي إن فُعِّل — نسخة اسمية أكثر تفاؤلاً
            # مما ستحصل عليه فعلياً بعد الرسوم والانزلاق (البند 10)
            rr_for_filter = (net_rr if cfg.signal.use_net_risk_reward
                            and net_rr is not None else rr)
        else:
            rr, net_rr, stop_pct, rr_for_filter = None, None, None, None

        # ── بوابة المنع
        v: Verdict = self.no_trade.check(
            data_quality=data_quality, bars_available=idx + 1, warmup=warmup,
            atr_pct=atr_pct, regime_long_friendly=reg.long_friendly,
            regime_name=reg.regime, htf_aligned=htf_aligned,
            btc_ok=(None if btc_ctx is None else btc_ctx.trend_ok),
            btc_available=bool(btc_ctx is not None and btc_ctx.available),
            resistance_distance_pct=res_dist, stop_distance_pct=stop_pct,
            risk_reward=rr_for_filter, min_rr=cfg.signal.min_rr,
            score=sr.score, min_score=cfg.signal.min_score,
            confidence=sr.confidence, min_confidence=cfg.signal.min_confidence,
            probability=sig.calibrated_probability,
            min_probability=cfg.signal.min_probability,
            spread_bps=spread_bps,
            open_positions=acc.get('open_positions', 0),
            daily_trades=acc.get('daily_trades', 0),
            daily_loss_hit=acc.get('daily_loss_hit', False),
            consecutive_losses=acc.get('consecutive_losses', 0),
            exposure_ok=acc.get('exposure_ok', True))

        sig.btc_context = (btc_ctx.risk_level if btc_ctx is not None else 'UNKNOWN')
        sig.reasons = v.reasons

        if not v.allowed:
            hard = {R['DATA_QUALITY'], R['WARMUP'], R['DAILY_LOSS'], R['CONSEC_LOSS'],
                    R['MAX_POS'], R['MAX_TRADES'], R['EXPOSURE'], R['SPREAD'],
                    R['LIQUIDITY'], R['NEWS'], R['VOL_HIGH'], R['VOL_LOW']}
            sig.decision = NO_TRADE if set(v.reasons) & hard else WAIT
        else:
            sig.decision = BUY

        return sig
