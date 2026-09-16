"""نموذج التكاليف — البند 23. كل القيم من Config."""
from dataclasses import dataclass
from typing import Optional
from ..core.config import CostConfig


@dataclass
class Fill:
    price: float
    fee: float
    slippage_cost: float
    notional: float


class CostModel:
    def __init__(self, cfg: Optional[CostConfig] = None):
        self.cfg = cfg or CostConfig()

    def _slip(self, bps: float, vol_mult: float) -> float:
        return bps / 10000.0 * max(vol_mult, 0.1)

    def buy(self, ideal: float, qty: float, vol_mult: float = 1.0,
            maker: bool = False) -> Fill:
        half_spread = self.cfg.spread_bps / 20000.0
        px = ideal * (1 + self._slip(self.cfg.slippage_bps_entry, vol_mult) + half_spread)
        notional = px * qty
        fee = notional * (self.cfg.eff_maker() if maker else self.cfg.eff_taker())
        return Fill(px, fee, (px - ideal) * qty, notional)

    def sell(self, ideal: float, qty: float, vol_mult: float = 1.0,
             is_stop: bool = False, maker: bool = False) -> Fill:
        bps = self.cfg.slippage_bps_stop if is_stop else self.cfg.slippage_bps_exit
        half_spread = self.cfg.spread_bps / 20000.0
        px = ideal * (1 - self._slip(bps, vol_mult) - half_spread)
        notional = px * qty
        fee = notional * (self.cfg.eff_maker() if maker else self.cfg.eff_taker())
        return Fill(px, fee, (ideal - px) * qty, notional)

    def breakeven_move_pct(self) -> float:
        """كم يجب أن يتحرك السعر لتغطية التكاليف فقط."""
        c = self.cfg
        return (c.eff_taker() * 2
                + (c.slippage_bps_entry + c.slippage_bps_exit) / 10000.0
                + c.spread_bps / 10000.0) * 100
