"""أنواع البيانات المشتركة — مصدر واحد للشكل."""
from dataclasses import dataclass, field
from typing import Optional, List, Dict
import numpy as np

INTERVAL_MS = {
    '1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000,
    '30m': 1_800_000, '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000,
    '6h': 21_600_000, '12h': 43_200_000, '1d': 86_400_000,
}


@dataclass
class OHLCV:
    """
    سلسلة شموع. الثابت المحوري:
    كل شمعة هنا **مغلقة**. الشمعة الجارية لا تدخل أبداً.
    open_time[i] هو وقت فتح الشمعة i؛ close_time[i] = open_time[i] + interval_ms.
    """
    symbol: str
    interval: str
    open_time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    source: str = "unknown"
    fetched_at: Optional[int] = None

    def __post_init__(self):
        """تحقق بنيوي: كل المصفوفات بنفس الطول والإطار مدعوم."""
        if self.interval not in INTERVAL_MS:
            raise ValueError(f"إطار غير مدعوم: {self.interval}")
        n = len(self.close)
        for name in ('open_time', 'open', 'high', 'low', 'volume'):
            m = len(getattr(self, name))
            if m != n:
                raise ValueError(
                    f"طول غير متطابق: {name}={m} بينما close={n}")

    def __len__(self): return len(self.close)

    @property
    def interval_ms(self) -> int: return INTERVAL_MS[self.interval]

    @property
    def close_time(self) -> np.ndarray:
        return self.open_time + self.interval_ms - 1

    def slice(self, start: int, end: Optional[int] = None) -> "OHLCV":
        e = len(self) if end is None else end
        return OHLCV(self.symbol, self.interval,
                     self.open_time[start:e], self.open[start:e],
                     self.high[start:e], self.low[start:e],
                     self.close[start:e], self.volume[start:e],
                     self.source, self.fetched_at)

    def to_dict(self) -> Dict:
        return {'symbol': self.symbol, 'interval': self.interval,
                'open_time': self.open_time.tolist(), 'open': self.open.tolist(),
                'high': self.high.tolist(), 'low': self.low.tolist(),
                'close': self.close.tolist(), 'volume': self.volume.tolist(),
                'source': self.source, 'fetched_at': self.fetched_at}

    @staticmethod
    def from_dict(d: Dict) -> "OHLCV":
        return OHLCV(d['symbol'], d['interval'],
                     np.asarray(d['open_time'], dtype=np.int64),
                     np.asarray(d['open'], float), np.asarray(d['high'], float),
                     np.asarray(d['low'], float), np.asarray(d['close'], float),
                     np.asarray(d['volume'], float),
                     d.get('source', 'cache'), d.get('fetched_at'))


@dataclass
class DataQuality:
    """
    نتيجة فحص جودة البيانات.
    score في [0,1]. أقل من العتبة ⇒ ممنوع إصدار توصية.
    """
    score: float
    completeness: float
    freshness: float
    integrity: float
    validity: float
    n_bars: int
    issues: List[str] = field(default_factory=list)
    missing_bars: int = 0
    duplicate_bars: int = 0
    invalid_ohlc: int = 0
    out_of_order: int = 0
    future_bars: int = 0
    age_ms: Optional[int] = None

    @property
    def ok(self) -> bool: return self.score >= 0.80

    def to_dict(self) -> Dict:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}
