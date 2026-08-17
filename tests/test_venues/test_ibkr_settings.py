"""IBKRSettings loud-failure behavior and settings-driven adapter rules.

Adapter tests live here too (only fakes -- no gateway, no ib_async): the
behaviors under test (mode mismatch, fee/carry gating, read-only writes)
are all driven by settings values.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.domain.carry import ForecastExCouponCarry, NullCarry
from src.domain.fees import FlatCommissionFeeModel
from src.domain.ids import MarketId
from src.domain.models import OrderRequest
from src.venues.base import VenueError, VenueReadOnlyError
from src.venues.ibkr.adapter import IBKRAdapter, ModeMismatchError
from src.venues.ibkr.settings import IBKRSettings

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_ibkr_env(monkeypatch):
    """Tests must see only explicit kwargs, never ambient IBKR_* env vars."""
    for key in list(os.environ):
        if key.startswith("IBKR_"):
            monkeypatch.delenv(key)


def make_settings(**kwargs) -> IBKRSettings:
    kwargs.setdefault("port", 4002)
    kwargs.setdefault("trading_mode", "paper")
    return IBKRSettings(_env_file=None, **kwargs)


def make_request() -> OrderRequest:
    return OrderRequest(market_id=MarketId.ibkr(712856754), side="BUY", leg="YES", quantity=1)


class FakeIB:
    """Just enough of ib_async.IB for connect()/is_healthy paths."""

    def __init__(self, accounts: list[str]):
        self._accounts = accounts
        self.disconnected = False

    def isConnected(self) -> bool:
        return not self.disconnected

    def managedAccounts(self) -> list[str]:
        return self._accounts

    def reqCurrentTime(self) -> int:
        return 0

    def disconnect(self) -> None:
        self.disconnected = True


class TestSettings:
    def test_missing_port_raises(self):
        with pytest.raises(ValidationError):
            IBKRSettings(_env_file=None, trading_mode="paper")

    def test_missing_trading_mode_raises(self):
        with pytest.raises(ValidationError):
            IBKRSettings(_env_file=None, port=4002)

    def test_invalid_trading_mode_raises(self):
        with pytest.raises(ValidationError):
            make_settings(trading_mode="demo")

    def test_defaults(self):
        s = make_settings()
        assert s.host == "127.0.0.1"
        assert s.market_data_type == 4
        assert s.max_data_lines == 100
        assert s.record_interval_minutes == 10
        assert s.commission_per_contract is None
        assert s.fees_verified_on is None
        assert s.coupon_apy is None
        assert s.coupon_verified_on is None

    def test_data_root_default_is_outside_repo(self):
        root = make_settings().resolved_data_root
        assert not root.is_relative_to(REPO_ROOT)
        assert "onedrive" not in {part.lower() for part in root.parts}
        assert root.name == "ibkr_ticks"

    def test_data_root_override(self, tmp_path):
        s = make_settings(data_root=str(tmp_path / "ticks"))
        assert s.resolved_data_root == (tmp_path / "ticks").resolve()


class TestAdapterReadOnly:
    def test_place_order_raises_on_read_only(self, tmp_path):
        adapter = IBKRAdapter(make_settings(data_root=str(tmp_path)))
        with pytest.raises(VenueReadOnlyError):
            adapter.place_order(make_request())

    @pytest.mark.parametrize("role", ["research", "data_collector"])
    def test_writable_requires_live_trading_role(self, role, tmp_path):
        with pytest.raises(ValueError):
            IBKRAdapter(make_settings(data_root=str(tmp_path)), role=role, read_only=False)

    def test_even_live_trading_cannot_transmit(self, tmp_path):
        adapter = IBKRAdapter(
            make_settings(trading_mode="live", data_root=str(tmp_path)),
            role="live_trading",
            read_only=False,
        )
        with pytest.raises(NotImplementedError):
            adapter.place_order(make_request())

    def test_cancel_order_raises_on_read_only(self, tmp_path):
        adapter = IBKRAdapter(make_settings(data_root=str(tmp_path)))
        with pytest.raises(VenueReadOnlyError):
            adapter.cancel_order("1")


class TestModeMismatch:
    @pytest.mark.parametrize(
        ("accounts", "trading_mode", "ok"),
        [
            (["DU1234567"], "paper", True),
            (["DF1234567"], "paper", True),
            (["DU1234567"], "live", False),
            (["U0000000"], "live", True),
            (["U0000000"], "paper", False),
        ],
    )
    def test_account_prefix_vs_trading_mode(self, accounts, trading_mode, ok, tmp_path):
        adapter = IBKRAdapter(make_settings(trading_mode=trading_mode, data_root=str(tmp_path)))
        adapter.ib = FakeIB(accounts)  # already-connected session: connect_ib is skipped
        if ok:
            adapter.connect()
        else:
            with pytest.raises(ModeMismatchError):
                adapter.connect()

    def test_no_accounts_raises(self, tmp_path):
        adapter = IBKRAdapter(make_settings(data_root=str(tmp_path)))
        adapter.ib = FakeIB([])
        with pytest.raises(VenueError):
            adapter.connect()

    def test_is_healthy(self, tmp_path):
        adapter = IBKRAdapter(make_settings(data_root=str(tmp_path)))
        assert not adapter.is_healthy()
        adapter.ib = FakeIB(["DU1234567"])
        assert adapter.is_healthy()
        adapter.disconnect()
        assert not adapter.is_healthy()


class TestEconomics:
    def test_fee_model_raises_without_verified_facts(self, tmp_path):
        adapter = IBKRAdapter(make_settings(data_root=str(tmp_path)))
        with pytest.raises(VenueError, match="fee facts unverified"):
            adapter.fee_model()

    def test_fee_model_raises_with_only_commission(self, tmp_path):
        adapter = IBKRAdapter(
            make_settings(commission_per_contract=0.01, data_root=str(tmp_path))
        )
        with pytest.raises(VenueError, match="fee facts unverified"):
            adapter.fee_model()

    def test_fee_model_with_verified_facts(self, tmp_path):
        adapter = IBKRAdapter(make_settings(
            commission_per_contract=0.01,
            fees_verified_on=date.today(),
            data_root=str(tmp_path),
        ))
        model = adapter.fee_model()
        assert isinstance(model, FlatCommissionFeeModel)
        assert model.execution_cost(0.50, 3) == pytest.approx(0.03)
        assert model.yes_no_price_sum() == 1.0

    def test_carry_model_defaults_to_null(self, tmp_path):
        adapter = IBKRAdapter(make_settings(data_root=str(tmp_path)))
        assert isinstance(adapter.carry_model(), NullCarry)

    def test_carry_model_with_verified_coupon(self, tmp_path):
        adapter = IBKRAdapter(make_settings(
            coupon_apy=0.0314,
            coupon_verified_on=date.today(),
            data_root=str(tmp_path),
        ))
        model = adapter.carry_model()
        assert isinstance(model, ForecastExCouponCarry)
        assert model.daily_accrual_rate() == pytest.approx(0.0314 / 365.0)
