"""طبقة إدارة الصفقة التكيّفية — Part B من مواصفة V11 FINAL."""
from .engine import AdaptiveTradeManager
from .hysteresis import ConfirmedRegime, confirm, confirm_series
from .types import (BREAK_EVEN, ExitDecision, HOLD, MarketView, REDUCE_TARGET,
                    STAGNATION_EXIT, TRAILING_STOP, TradeState)

__all__ = ['AdaptiveTradeManager', 'TradeState', 'MarketView', 'ExitDecision',
           'ConfirmedRegime', 'confirm', 'confirm_series',
           'HOLD', 'BREAK_EVEN', 'TRAILING_STOP', 'REDUCE_TARGET',
           'STAGNATION_EXIT']
