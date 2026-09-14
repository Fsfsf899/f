"""
محرك المنع — البند 16.
======================
NO_TRADE ميزة أساسية، لا حالة فشل. النظام غير مُجبر على إعطاء BUY.

كل فحص يُرجع سبباً مُرمّزاً يُسجَّل في سجل التدقيق.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import numpy as np
from ..core.config import NoTradeConfig, RiskConfig

# رموز الأسباب — ثابتة، تُستخدم في التحليل اللاحق
R = {
    'DATA_QUALITY':        'INSUFFICIENT_DATA_QUALITY',
    'WARMUP':              'INSUFFICIENT_HISTORY',
    'SPREAD':              'SPREAD_TOO_WIDE',
    'LIQUIDITY':           'INSUFFICIENT_LIQUIDITY',
    'VOL_LOW':             'VOLATILITY_TOO_LOW',
    'VOL_HIGH':            'VOLATILITY_TOO_HIGH',
    'REGIME':              'UNSUITABLE_MARKET_REGIME',
    'HTF_CONFLICT':        'HIGHER_TIMEFRAME_CONFLICT',
    'BTC_RISK':            'BTC_CONTEXT_RISK',
    'RESISTANCE':          'RESISTANCE_TOO_CLOSE',
    'STOP_FAR':            'STOP_DISTANCE_TOO_LARGE',
    'STOP_NEAR':           'STOP_DISTANCE_TOO_SMALL',
    'RR':                  'RISK_REWARD_INSUFFICIENT',
    'SCORE':               'SIGNAL_SCORE_TOO_LOW',
    'CONFIDENCE':          'CONFIDENCE_TOO_LOW',
    'PROBABILITY':         'PROBABILITY_INSUFFICIENT',
    'DAILY_LOSS':          'DAILY_LOSS_LIMIT_REACHED',
    'CONSEC_LOSS':         'CONSECUTIVE_LOSS_LIMIT',
    'MAX_POS':             'MAX_OPEN_POSITIONS',
    'MAX_TRADES':          'MAX_DAILY_TRADES',
    'EXPOSURE':            'EXPOSURE_LIMIT',
    'NEWS':                'NEWS_CLEARANCE_REQUIRED',
    'STALE':               'SIGNAL_STALE',
    'EXEC_QUALITY':        'EXECUTION_QUALITY_POOR',
    'NO_SETUP':            'NO_VALID_SETUP',
}


@dataclass
class Verdict:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    details: Dict[str, str] = field(default_factory=dict)

    def block(self, code: str, detail: str = ''):
        self.allowed = False
        if code not in self.reasons:
            self.reasons.append(code)
        if detail:
            self.details[code] = detail
        return self

    @property
    def primary(self) -> Optional[str]:
        return self.reasons[0] if self.reasons else None

    def to_dict(self) -> Dict:
        return {'allowed': self.allowed, 'reasons': self.reasons,
                'details': self.details}


class NoTradeEngine:
    def __init__(self, cfg: Optional[NoTradeConfig] = None,
                 risk_cfg: Optional[RiskConfig] = None):
        self.cfg = cfg or NoTradeConfig()
        self.risk = risk_cfg or RiskConfig()

    def check(self, *, data_quality: float, bars_available: int, warmup: int,
              atr_pct: Optional[float] = None,
              regime_long_friendly: Optional[bool] = None,
              regime_name: str = 'UNKNOWN',
              htf_aligned: Optional[bool] = None,
              btc_ok: Optional[bool] = None, btc_available: bool = False,
              resistance_distance_pct: Optional[float] = None,
              stop_distance_pct: Optional[float] = None,
              risk_reward: Optional[float] = None,
              min_rr: float = 1.5,
              score: Optional[float] = None, min_score: float = 4.0,
              confidence: Optional[float] = None,
              probability: Optional[float] = None,
              spread_bps: Optional[float] = None,
              liquidity_ok: Optional[bool] = None,
              signal_age_bars: int = 0,
              news_cleared: Optional[bool] = None,
              open_positions: int = 0, daily_trades: int = 0,
              daily_loss_hit: bool = False, consecutive_losses: int = 0,
              exposure_ok: bool = True) -> Verdict:
        v = Verdict(True)
        c = self.cfg

        # ── بيانات
        if data_quality < c.min_data_quality:
            v.block(R['DATA_QUALITY'], f"{data_quality:.2f} < {c.min_data_quality}")
        if bars_available < warmup:
            v.block(R['WARMUP'], f"{bars_available} < {warmup}")

        # ── حواجز الحساب (تُفحص دائماً، قبل أي تحليل)
        if daily_loss_hit:
            v.block(R['DAILY_LOSS'])
        if consecutive_losses >= self.risk.max_consecutive_losses:
            v.block(R['CONSEC_LOSS'], f"{consecutive_losses}")
        if open_positions >= self.risk.max_open_positions:
            v.block(R['MAX_POS'], f"{open_positions}")
        if daily_trades >= self.risk.max_daily_trades:
            v.block(R['MAX_TRADES'], f"{daily_trades}")
        if not exposure_ok:
            v.block(R['EXPOSURE'])

        # ── ظروف السوق
        if atr_pct is not None and np.isfinite(atr_pct):
            if atr_pct < c.min_atr_pct:
                v.block(R['VOL_LOW'], f"ATR {atr_pct:.2f}% < {c.min_atr_pct}%")
            elif atr_pct > c.max_atr_pct:
                v.block(R['VOL_HIGH'], f"ATR {atr_pct:.2f}% > {c.max_atr_pct}%")
        if regime_long_friendly is False:
            v.block(R['REGIME'], regime_name)
        if htf_aligned is False:
            v.block(R['HTF_CONFLICT'])
        if c.require_btc_ok and btc_available and btc_ok is False:
            v.block(R['BTC_RISK'])

        # ── جودة نقطة الدخول
        if (resistance_distance_pct is not None
                and resistance_distance_pct < c.min_resistance_distance_pct):
            v.block(R['RESISTANCE'], f"{resistance_distance_pct:.2f}%")
        if stop_distance_pct is not None:
            if stop_distance_pct > c.max_stop_distance_pct:
                v.block(R['STOP_FAR'], f"{stop_distance_pct:.2f}%")
            elif stop_distance_pct < c.min_stop_distance_pct:
                v.block(R['STOP_NEAR'], f"{stop_distance_pct:.2f}%")
        if risk_reward is not None and risk_reward < min_rr:
            v.block(R['RR'], f"{risk_reward:.2f} < {min_rr}")

        # ── جودة الإشارة
        if score is not None and score < min_score:
            v.block(R['SCORE'], f"{score:.2f} < {min_score}")
        if confidence is not None and confidence < c.min_data_quality * 0.6:
            v.block(R['CONFIDENCE'], f"{confidence:.2f}")
        if probability is not None and probability < 0.40:
            v.block(R['PROBABILITY'], f"{probability:.2f}")

        # ── التنفيذ
        if spread_bps is not None and spread_bps > c.max_spread_bps:
            v.block(R['SPREAD'], f"{spread_bps:.1f}bps")
        if liquidity_ok is False:
            v.block(R['LIQUIDITY'])
        if signal_age_bars > c.max_signal_age_bars:
            v.block(R['STALE'], f"{signal_age_bars} شمعة")

        # ── أخبار
        if c.require_news_clearance and news_cleared is not True:
            v.block(R['NEWS'], 'حالة الأخبار UNKNOWN')

        return v
