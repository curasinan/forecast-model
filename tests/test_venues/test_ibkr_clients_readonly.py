"""connect_ib resolves readonly by role and never auto-increments ids."""
import pytest
import src.venues.ibkr.clients as clients
from src.venues.ibkr.settings import IBKRSettings

class _FakeIB:
    last_kwargs: dict = {}
    def connect(self, host, port, clientId, timeout, readonly):
        _FakeIB.last_kwargs = dict(host=host, port=port, clientId=clientId,
                                   timeout=timeout, readonly=readonly)
    def disconnect(self): pass

@pytest.fixture
def fake_ib(monkeypatch):
    monkeypatch.setattr("ib_async.IB", _FakeIB)
    return _FakeIB

def _settings():
    return IBKRSettings(port=4002, trading_mode="paper")

@pytest.mark.parametrize("role,expected_readonly,expected_id", [
    ("live_trading", False, 1),
    ("data_collector", True, 2),
    ("research", True, 3),
    ("paper_sync", True, 4),
])
def test_readonly_resolves_by_role(fake_ib, role, expected_readonly, expected_id):
    clients.connect_ib(role, _settings())
    assert fake_ib.last_kwargs["readonly"] is expected_readonly
    assert fake_ib.last_kwargs["clientId"] == expected_id

def test_explicit_readonly_still_wins(fake_ib):
    clients.connect_ib("live_trading", _settings(), readonly=True)
    assert fake_ib.last_kwargs["readonly"] is True
