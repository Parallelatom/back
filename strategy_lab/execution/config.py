"""Explicit, non-secret settings. Live mode deliberately cannot be enabled here."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json

from ..replay import ALL_STRATEGIES


def micro(value) -> int:
    try:
        amount = Decimal(str(value)) * 1_000_000
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise ValueError("amounts must be positive USD values with at most six decimals")
        if amount > 10**12:
            raise ValueError("amount exceeds rehearsal limit")
        return int(amount)
    except InvalidOperation as exc:
        raise ValueError("invalid USD amount") from exc


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    mode: str = "paper"
    strategy: str = "Delta Edge"
    wallet_label: str = "btc-paper"
    symbols: tuple = ("BTC",)
    stake: int = 1_000_000
    bankroll: int = 10_000_000
    max_open_positions: int = 1
    max_exposure: int = 2_000_000
    daily_spend: int = 5_000_000
    daily_loss: int = 2_000_000
    slippage_bps: int = 100
    max_age_seconds: int = 15
    max_signal_age_seconds: int = 5

    def __post_init__(self):
        if self.mode != "paper":
            raise ValueError("live execution requires the separate run_live entry point")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a JSON boolean")
        if self.strategy not in {s.name for s in ALL_STRATEGIES}:
            raise ValueError("unknown strategy")
        if len(self.symbols) != 1 or not set(self.symbols) <= {"BTC", "XYZCL"}:
            raise ValueError("each wallet must have exactly one Symbol: BTC or XYZCL")
        if not isinstance(self.wallet_label, str) or not self.wallet_label.strip():
            raise ValueError("wallet_label must identify this rehearsal wallet")
        for name in ("stake", "bankroll", "max_open_positions", "max_exposure", "daily_spend",
                     "daily_loss", "max_age_seconds", "max_signal_age_seconds"):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= 10**12:
                raise ValueError(f"{name} must be a positive bounded integer")
        if type(self.slippage_bps) is not int or not 0 <= self.slippage_bps <= 1000:
            raise ValueError("slippage_bps must be between 0 and 1000")
        if self.stake > min(self.bankroll, self.max_exposure, self.daily_spend):
            raise ValueError("stake exceeds a funding or spend limit")

    @classmethod
    def read(cls, path):
        with open(path) as file:
            values = json.load(file)
        if not isinstance(values, dict):
            raise ValueError("configuration must be a JSON object")
        for name in ("stake", "bankroll", "max_exposure", "daily_spend", "daily_loss"):
            key = name + "_usd"
            if key in values:
                values[name] = micro(values.pop(key))
        if "symbols" in values:
            if not isinstance(values["symbols"], list):
                raise ValueError("symbols must be a JSON array")
            values["symbols"] = tuple(values["symbols"])
        return cls(**values)
