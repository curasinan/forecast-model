"""Wallet management — Polymarket CLOB API client initialization.

Handles:
- ClobClient initialization with private key
- API credential derivation/creation
- Balance checking (USDCe + outcome tokens)
- Dry-run mode (no wallet needed)

Requirements for live trading:
- py-clob-client installed (`pip install py-clob-client`)
- Private key for a Polygon wallet
- USDCe balance on Polygon
- Small MATIC balance for gas
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Polymarket contract addresses on Polygon
USDCE_ADDRESS = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
POLYGON_CHAIN_ID = 137
CLOB_HOST = "https://clob.polymarket.com"


@dataclass
class WalletBalance:
    """Current wallet balances."""

    usdce: float = 0.0
    matic: float = 0.0
    positions: dict[str, float] = field(default_factory=dict)  # token_id -> quantity


class PolymarketWallet:
    """Manages Polymarket CLOB API connection and wallet state.

    Supports two modes:
    - dry_run=True: No wallet needed, simulates all operations
    - dry_run=False: Requires private key and funded wallet
    """

    def __init__(
        self,
        private_key: str = "",
        funder_address: str = "",
        dry_run: bool = True,
    ):
        self._private_key = private_key
        self._funder_address = funder_address
        self._dry_run = dry_run
        self._client = None
        self._connected = False
        self._simulated_balance = WalletBalance(usdce=1000.0, matic=1.0)

    @property
    def is_dry_run(self) -> bool:
        return self._dry_run

    @property
    def is_connected(self) -> bool:
        return self._connected or self._dry_run

    def connect(self) -> bool:
        """Initialize the CLOB client and derive API credentials.

        Returns True if connection successful, False otherwise.
        """
        if self._dry_run:
            logger.info("Wallet running in DRY-RUN mode (no real trades)")
            self._connected = True
            return True

        if not self._private_key:
            logger.error("Private key required for live trading")
            return False

        try:
            from py_clob_client.client import ClobClient

            self._client = ClobClient(
                CLOB_HOST,
                key=self._private_key,
                chain_id=POLYGON_CHAIN_ID,
                signature_type=1,
                funder=self._funder_address or None,
            )

            # Derive or create API credentials
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)

            self._connected = True
            logger.info("Wallet connected to Polymarket CLOB")
            return True

        except ImportError:
            logger.error(
                "py-clob-client not installed. "
                "Run: pip install py-clob-client"
            )
            return False
        except Exception as e:
            logger.error(f"Wallet connection failed: {e}")
            return False

    def get_balance(self) -> WalletBalance:
        """Get current wallet balances."""
        if self._dry_run:
            return self._simulated_balance

        # In live mode, query on-chain balances
        # For now return placeholder — full implementation needs web3
        return WalletBalance()

    def get_client(self):
        """Get the underlying ClobClient for direct API calls.

        Returns None in dry-run mode.
        """
        return self._client

    def simulate_trade(self, side: str, price: float, size: float) -> bool:
        """Update simulated balance for dry-run mode.

        Returns True if sufficient balance.
        """
        cost = price * size
        if side == "BUY":
            if self._simulated_balance.usdce >= cost:
                self._simulated_balance.usdce -= cost
                return True
            return False
        else:  # SELL
            self._simulated_balance.usdce += cost
            return True
