"""
محرك المؤشرات الوحيد — البند 12 و40.
=====================================
قاعدة صارمة: كل قيمة عند الفهرس i تُحسب من بيانات [0..i] فقط.
لا نافذة مركزية، لا تمرير عكسي، لا تعبئة خلفية.

نفس هذا الملف يُستخدم في الباكتست والتحليل الحي — لا يمكن أن تختلف
النتيجة بينهما لأن الكود واحد.

كل الدوال تُرجع مصفوفة بطول len(prices)، والقيم قبل اكتمال الإحماء
تكون NaN صراحةً (وليس صفراً) حتى لا تُستخدم بالخطأ.
"""
import numpy as np
from typing import Dict, Tuple, Optional
from ..data.types import OHLCV


def _nan(n: int) -> np.ndarray:
    return np.full(n, np.nan, dtype=float)


# ═══════════ المتوسطات ═══════════
def sma(x: np.ndarray, period: int) -> np.ndarray:
    n = len(x); out = _nan(n)
    if n < period: return out
    cs = np.cumsum(np.insert(x.astype(float), 0, 0.0))
    out[period-1:] = (cs[period:] - cs[:-period]) / period
    return out


def ema(x: np.ndarray, period: int) -> np.ndarray:
    """
    EMA بتهيئة SMA — نفس اصطلاح TradingView.
    القيم قبل period-1 تبقى NaN.
    """
    n = len(x); out = _nan(n)
    if n < period: return out
    k = 2.0 / (period + 1.0)
    out[period-1] = float(np.mean(x[:period]))
    for i in range(period, n):
        out[i] = x[i] * k + out[i-1] * (1 - k)
    return out


def wilder_rma(x: np.ndarray, period: int) -> np.ndarray:
    """متوسط Wilder — يُستخدم في RSI و ATR و ADX."""
    n = len(x); out = _nan(n)
    if n < period: return out
    out[period-1] = float(np.mean(x[:period]))
    for i in range(period, n):
        out[i] = (out[i-1] * (period - 1) + x[i]) / period
    return out


# ═══════════ الزخم ═══════════
def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close); out = _nan(n)
    if n < period + 1: return out
    d = np.diff(close)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = wilder_rma(gain, period)
    al = wilder_rma(loss, period)
    for i in range(period, n):
        g, l = ag[i-1], al[i-1]
        if np.isnan(g) or np.isnan(l): continue
        out[i] = 100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)
    return out


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    ef, es = ema(close, fast), ema(close, slow)
    line = ef - es
    valid = ~np.isnan(line)
    sig = _nan(len(close))
    if valid.sum() >= signal:
        first = int(np.argmax(valid))
        s = ema(line[first:], signal)
        sig[first:] = s
    return line, sig, line - sig


def roc(close: np.ndarray, period: int = 12) -> np.ndarray:
    n = len(close); out = _nan(n)
    if n <= period: return out
    prev = close[:-period]
    out[period:] = np.where(prev != 0, (close[period:] - prev) / prev * 100, np.nan)
    return out


def _causal_sma(x: np.ndarray, period: int) -> np.ndarray:
    """
    SMA على مصفوفة قد تحتوي NaN في البداية فقط.
    تُحسب من القيم الصالحة السابقة حصراً — لا تستعين بأي إحصاء عام
    على المصفوفة (وهذا كان مصدر تسريب مستقبلي في نسخة سابقة).
    """
    n = len(x); out = _nan(n)
    for i in range(n):
        if i + 1 < period:
            continue
        w = x[i - period + 1: i + 1]
        if np.isnan(w).any():
            continue
        out[i] = float(np.mean(w))
    return out


def stoch_rsi(close: np.ndarray, rsi_period: int = 14, stoch_period: int = 14,
              k_smooth: int = 3, d_smooth: int = 3) -> Tuple[np.ndarray, np.ndarray]:
    r = rsi(close, rsi_period)
    n = len(close); raw = _nan(n)
    for i in range(n):
        w = r[max(0, i - stoch_period + 1): i + 1]
        if len(w) < stoch_period or np.isnan(w).any():
            continue
        lo, hi = float(w.min()), float(w.max())
        raw[i] = 50.0 if hi == lo else (r[i] - lo) / (hi - lo) * 100
    k = _causal_sma(raw, k_smooth)
    d = _causal_sma(k, d_smooth)
    return k, d


# ═══════════ التقلب ═══════════
def true_range(high, low, close) -> np.ndarray:
    n = len(close); tr = _nan(n)
    tr[0] = high[0] - low[0]
    pc = close[:-1]
    tr[1:] = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - pc), np.abs(low[1:] - pc)))
    return tr


def atr(high, low, close, period: int = 14) -> np.ndarray:
    return wilder_rma(true_range(high, low, close), period)


def bollinger(close: np.ndarray, period: int = 20, k: float = 2.0
              ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(close, period)
    n = len(close); sd = _nan(n)
    for i in range(period - 1, n):
        sd[i] = float(np.std(close[i-period+1:i+1]))
    return mid + k * sd, mid, mid - k * sd


def hist_volatility(close: np.ndarray, period: int = 20,
                    bars_per_year: int = 2190) -> np.ndarray:
    """تقلب تاريخي سنوي %. bars_per_year افتراضي = 4h (6×365)."""
    n = len(close); out = _nan(n)
    lr = _nan(n)
    lr[1:] = np.log(np.maximum(close[1:], 1e-12) / np.maximum(close[:-1], 1e-12))
    for i in range(period, n):
        w = lr[i-period+1:i+1]
        out[i] = float(np.std(w)) * np.sqrt(bars_per_year) * 100
    return out


# ═══════════ الاتجاه ═══════════
def dmi_adx(high, low, close, period: int = 14
            ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """يُرجع (ADX, DI+, DI-) بحساب Wilder القياسي."""
    n = len(close)
    up = _nan(n); dn = _nan(n)
    up[1:] = high[1:] - high[:-1]
    dn[1:] = low[:-1] - low[1:]
    pdm = np.where((up > dn) & (up > 0), np.nan_to_num(up), 0.0)
    ndm = np.where((dn > up) & (dn > 0), np.nan_to_num(dn), 0.0)
    pdm[0] = ndm[0] = 0.0

    atr_ = wilder_rma(true_range(high, low, close), period)
    spdm = wilder_rma(pdm, period)
    sndm = wilder_rma(ndm, period)

    with np.errstate(divide='ignore', invalid='ignore'):
        pdi = np.where(atr_ > 0, spdm / atr_ * 100, np.nan)
        ndi = np.where(atr_ > 0, sndm / atr_ * 100, np.nan)
        dx = np.where((pdi + ndi) > 0, np.abs(pdi - ndi) / (pdi + ndi) * 100, np.nan)

    adx = _nan(n)
    valid = np.where(~np.isnan(dx))[0]
    if len(valid) >= period:
        s = valid[0]
        adx[s + period - 1] = float(np.mean(dx[s:s + period]))
        for i in range(s + period, n):
            if np.isnan(dx[i]) or np.isnan(adx[i-1]): continue
            adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
    return adx, pdi, ndi


# ═══════════ الحجم ═══════════
def obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    n = len(close); out = np.zeros(n)
    out[0] = volume[0]
    sign = np.sign(np.diff(close))
    out[1:] = np.cumsum(sign * volume[1:]) + volume[0]
    return out


def volume_ma(volume: np.ndarray, period: int = 20) -> np.ndarray:
    return sma(volume, period)


def relative_volume(volume: np.ndarray, period: int = 20) -> np.ndarray:
    """الحجم الحالي ÷ متوسط الفترة السابقة (لا يشمل الشمعة الحالية)."""
    n = len(volume); out = _nan(n)
    for i in range(period, n):
        m = float(np.mean(volume[i-period:i]))
        out[i] = volume[i] / m if m > 0 else np.nan
    return out


# ═══════════ المحرك ═══════════
class IndicatorEngine:
    """
    يحسب كل المؤشرات مرة واحدة ويخزّنها.
    البند 12: منع تكرار الحسابات، وضمان تطابق Backtest/Live.
    """
    def __init__(self, cfg=None):
        from ..core.config import SignalConfig
        self.cfg = cfg or SignalConfig()
        self._cache: Dict[str, Dict[str, np.ndarray]] = {}

    def compute(self, data: OHLCV, key: Optional[str] = None) -> Dict[str, np.ndarray]:
        k = key or f"{data.symbol}_{data.interval}_{len(data)}_{int(data.open_time[-1])}"
        if k in self._cache:
            return self._cache[k]

        c, h, l, v = data.close, data.high, data.low, data.volume
        cfg = self.cfg
        macd_line, macd_sig, macd_hist = macd(c)
        bb_u, bb_m, bb_l = bollinger(c)
        adx_, pdi, ndi = dmi_adx(h, l, c, cfg.adx_period)
        k_, d_ = stoch_rsi(c)

        out = {
            'ema_fast': ema(c, cfg.ema_fast), 'ema_slow': ema(c, cfg.ema_slow),
            'ema_200': ema(c, 200), 'sma_50': sma(c, 50),
            'rsi': rsi(c, cfg.rsi_period),
            'macd': macd_line, 'macd_signal': macd_sig, 'macd_hist': macd_hist,
            'atr': atr(h, l, c, cfg.atr_period),
            'adx': adx_, 'di_plus': pdi, 'di_minus': ndi,
            'stoch_k': k_, 'stoch_d': d_,
            'roc': roc(c), 'bb_upper': bb_u, 'bb_mid': bb_m, 'bb_lower': bb_l,
            'hist_vol': hist_volatility(c),
            'obv': obv(c, v), 'volume_ma': volume_ma(v), 'rel_volume': relative_volume(v),
        }
        with np.errstate(divide='ignore', invalid='ignore'):
            out['atr_pct'] = np.where(c > 0, out['atr'] / c * 100, np.nan)

        self._cache[k] = out
        return out

    def warmup_bars(self) -> int:
        return max(self.cfg.warmup_bars, 200 + self.cfg.adx_period * 2)
