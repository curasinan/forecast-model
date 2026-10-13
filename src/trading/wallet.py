"""Wallet state — dry-run simulation only."""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_RETIRED_MSG = (
    "live Polymarket execution retired to legacy/polymarket_onchain (2026-08-16); "
    "live trading is migrating to IBKR -- see src/venues/"
)


@dataclass
class WalletBalance:
    """Current wallet balances."""

    usdce: float = 0.0
    matic: float = 0.0
    positions: dict[str, float] = field(default_factory=dict)  # token_id -> quantity


class PolymarketWallet:
    """Simulated wallet state for dry-run trading."""

    def __init__(
        self,
        private_key: str = "",
        funder_address: str = "",
        dry_run: bool = True,
    ):
        self._private_key = private_key
        self._funder_address = funder_address
        self._dry_run = dry_run
        self._connected = False
        self._simulated_balance = WalletBalance(usdce=1000.0, matic=1.0)

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run

    @property
    def is_connected(self) -> bool:
        return self._connected or self._dry_run

    def connect(self) -> bool:
        """Mark the simulated wallet connected (dry-run only)."""
        if self._dry_run:
            logger.info("Wallet running in DRY-RUN mode (no real trades)")
            self._connected = True
            return True

        raise RuntimeError(_RETIRED_MSG)

    def get_balance(self) -> WalletBalance:
        """Get current wallet balances."""
        if self._dry_run:
            return self._simulated_balance

        return WalletBalance()

    def get_client(self):
        """Retired: there is no live CLOB client anymore."""
        if self._dry_run:
            return None
        raise RuntimeError(_RETIRED_MSG)

    def simulate_trade(self, side: str, price: float, size: float) -> bool:
        """Update simulated balance for dry-run mode."""
        cost = price * size
        if side == "BUY":
            if self._simulated_balance.usdce >= cost:
                self._simulated_balance.usdce -= cost
                return True
            return False
        else:  # SELL
            self._simulated_balance.usdce += cost
            return True
