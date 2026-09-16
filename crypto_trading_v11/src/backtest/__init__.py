"""
محرك الباكتست الرسمي الوحيد.

    from src.backtest.engine import BacktestEngine

أي محرك آخر في legacy/ تاريخي ولا يُستخدم. أرقامه لا تُعتمد.
"""
from .engine import BacktestEngine, BacktestResult

OFFICIAL_ENGINE = BacktestEngine
__all__ = ['BacktestEngine', 'BacktestResult', 'OFFICIAL_ENGINE']
