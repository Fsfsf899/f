"""
مصدر الحقيقة الوحيد لكل العتبات.
كل رقم يؤثر على قرار تداول موجود هنا، لا مبعثراً في الكود.

تنبيه: هذه العتبات ليست حقائق علمية — هي نقاط بداية قابلة للتعديل،
ويجب اختبار حساسيتها قبل الاعتماد عليها.
"""
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional
import json, hashlib

STRATEGY_VERSION = "v9.2.0-entry-research"


@dataclass
class CostConfig:
    maker_fee: float = 0.0010
    taker_fee: float = 0.0010
    bnb_discount: bool = False
    slippage_bps_entry: float = 3.0
    slippage_bps_exit: float = 3.0
    slippage_bps_stop: float = 12.0    # الوقف أسوأ: السوق يتحرك ضدك
    spread_bps: float = 2.0

    def eff_maker(self): return self.maker_fee * (0.75 if self.bnb_discount else 1.0)
    def eff_taker(self): return self.taker_fee * (0.75 if self.bnb_discount else 1.0)


@dataclass
class RegimeConfig:
    lookback: int = 50
    efficiency_trend_min: float = 0.30
    slope_trend_min_pct: float = 1.5
    vol_high_pct: float = 3.0
    vol_low_pct: float = 0.6
    adx_trend_min: float = 20.0
    # ADX عند هذا المستوى يُعد دليلاً كافياً على الاتجاه بمفرده،
    # فلا تنقضه كفاءةُ حركةٍ منخفضة قليلاً. المقياسان يقيسان نظافة
    # الاتجاه بطريقتين مختلفتين؛ اشتراط كليهما بكامل القوة ازدواج حساب.
    adx_strong_min: float = 35.0
    transition_efficiency: float = 0.20


@dataclass
class StructureConfig:
    swing_lookback: int = 5
    confirmation_bars: int = 5
    sr_tolerance_pct: float = 0.45
    sr_min_touches: int = 2
    sr_window: int = 200
    breakout_buffer_pct: float = 0.15
    retest_tolerance_pct: float = 0.35


@dataclass
class SignalConfig:
    min_score: float = 4.0
    min_confidence: float = 0.45
    min_probability: float = 0.40
    min_rr: float = 1.5
    atr_stop_mult: float = 1.8
    atr_target_mult: float = 3.6
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    warmup_bars: int = 200

    # ── البند 10: R/R بعد التكاليف — v9.1.0-research ──
    # النسخة الاسمية (target-entry)/(entry-stop) تتجاهل الرسوم
    # والانزلاق فتُبالغ في جاذبية كل صفقة. مفعَّل افتراضياً لأنه
    # إصلاح دقة محاسبية للمخاطرة، لا "تحسين استراتيجية" يحتاج إثبات
    # ربحية — لكنه يُغيّر أي الصفقات تعبر فلتر min_rr، فيُبدِّل
    # الـ fingerprint كما يجب.
    use_net_risk_reward: bool = True

    # ── البند 8C: خروج زمني — اختياري، معطَّل افتراضياً ──
    # None = معطَّل تماماً (السلوك الأصلي دون تغيير). أي قيمة تُفعِّله
    # تتطلب معايرة على بيانات حقيقية — لا رقم افتراضي "صحيح" بلا بيانات.
    max_holding_bars: Optional[int] = None

    # ── البند 8B: Trailing Stop — اختياري، معطَّل افتراضياً ──
    trailing_stop_enabled: bool = False
    trailing_atr_mult: float = 2.5          # يُستخدم فقط إن فُعِّل أعلاه
    trailing_activate_at_r: float = 1.0     # لا تحريك قبل تحقق هذا الـ R

    # ── البند 6: طريقة الوقف — الافتراضي 'atr' يطابق السلوك الأصلي ──
    stop_method: str = 'atr'                # atr | structure | hybrid
    stop_buffer_atr: float = 0.15           # هامش إضافي لـ structure/hybrid


@dataclass
class NoTradeConfig:
    min_data_quality: float = 0.80
    max_spread_bps: float = 15.0
    min_atr_pct: float = 0.20
    max_atr_pct: float = 6.0
    min_resistance_distance_pct: float = 0.80
    max_stop_distance_pct: float = 8.0
    min_stop_distance_pct: float = 0.30
    max_signal_age_bars: int = 1
    require_news_clearance: bool = False
    require_btc_ok: bool = True


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0
    max_risk_per_trade_pct: float = 2.0
    min_risk_per_trade_pct: float = 0.25
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 4
    max_open_positions: int = 1
    max_daily_trades: int = 5
    max_position_notional_pct: float = 25.0


@dataclass
class ExecutionConfig:
    same_candle_policy: str = "conservative"   # conservative|optimistic|intrabar
    fill_on: str = "next_open"                 # next_open|close
    allow_gap_fills: bool = True


@dataclass
class BreakoutConfig:
    """
    البند 4 — معطَّل افتراضياً. القيم قيم ابتدائية للتجربة، ليست
    نتائج محسَّنة على بيانات حقيقية (لا بيانات متاحة لتحسينها أصلاً).
    """
    enabled: bool = False
    lookback: int = 20
    min_distance_atr: float = 0.10
    max_distance_atr: float = 1.00
    max_extension_atr: float = 1.50
    volume_filter_enabled: bool = False
    volume_multiplier: float = 1.20
    wick_filter_enabled: bool = True
    max_upper_wick_ratio: float = 0.40      # نسبة الفتيل العلوي لجسم الشمعة
    max_candle_range_atr: float = 3.0       # شمعة أكبر بكثير من ATR = حركة استثنائية لا اختراق نظيف
    # مفاتيح استبعاد للتحليل (Ablation) — القسم الخامس. الافتراضي True
    # يطابق السلوك الحالي؛ False يُعطِّل الشرط **لهذا التحليل فقط**.
    body_filter_enabled: bool = True
    extension_filter_enabled: bool = True
    candle_size_filter_enabled: bool = True
    resistance_distance_filter_enabled: bool = True
    htf_confirmation_enabled: bool = False
    higher_resistance_min_distance_pct: float = 1.0
    stop_mode: str = 'atr'                  # atr | level | hybrid
    stop_buffer_atr: float = 0.15
    # Retest — البند 5
    retest_enabled: bool = False
    retest_window_bars: int = 3
    retest_tolerance_atr: float = 0.25


@dataclass
class PullbackConfig:
    """البند 6 — معطَّل افتراضياً."""
    enabled: bool = False
    confirmation_required: bool = True
    zone_atr: float = 0.50
    max_depth_atr: float = 1.50
    confirmation_window_bars: int = 3
    max_age_bars: int = 8
    support_tolerance_atr: float = 0.25
    stop_mode: str = 'atr'                  # atr | structure | hybrid
    stop_buffer_atr: float = 0.15
    zone_type: str = 'ema_fast'              # ema_fast | ema_slow | structure
    # مفاتيح استبعاد للتحليل (Ablation) — القسم الخامس
    support_filter_enabled: bool = True
    regime_filter_enabled: bool = True
    net_rr_filter_enabled: bool = True


@dataclass
class MTFEntryConfig:
    """البند 7 — معطَّل افتراضياً."""
    confirmation_enabled: bool = False


@dataclass
class ValidationConfig:
    min_trades: int = 100
    min_profit_factor: float = 1.30
    max_drawdown_pct: float = 25.0
    min_wf_consistency: float = 0.70
    max_brier_score: float = 0.25
    min_data_quality: float = 0.85
    wf_train_bars: int = 1500
    wf_test_bars: int = 500
    wf_step_bars: int = 500


@dataclass
class DataConfig:
    max_staleness_multiple: float = 2.5
    max_gap_ratio: float = 0.02
    min_bars_required: int = 250
    cache_ttl_seconds: int = 3600


@dataclass
class Config:
    costs: CostConfig = field(default_factory=CostConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    structure: StructureConfig = field(default_factory=StructureConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    no_trade: NoTradeConfig = field(default_factory=NoTradeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)
    pullback: PullbackConfig = field(default_factory=PullbackConfig)
    mtf_entry: MTFEntryConfig = field(default_factory=MTFEntryConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    data: DataConfig = field(default_factory=DataConfig)
    version: str = STRATEGY_VERSION
    experiment_id: str = ''    # البند 13 — يُملأ صراحةً عند تشغيل تجربة بحث

    def to_dict(self) -> Dict: return asdict(self)

    def to_json(self, path: str):
        with open(path, 'w') as f: json.dump(self.to_dict(), f, indent=2)

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()[:12]


DEFAULT = Config()
