"""ContractResolver cache behavior against a fabricated universe snapshot.

No gateway, no real data/ file: the snapshot JSON is built in tmp_path and
the API is a fake object with a reqContractDetails method.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.venues.base import VenueError
from src.venues.ibkr.contracts import ContractResolver
from src.venues.ibkr.settings import IBKRSettings

YES_ID, NO_ID, IND_ID, EXPIRED_ID = 1001, 1002, 900, 1003
FUTURE_EXPIRY = "20991231"


def _opt_row(con_id: int, right: str, expiry: str = FUTURE_EXPIRY, strike: float = 2.5) -> dict:
    suffix = "YES" if right == "C" else "NO"
    return {
        "conId": con_id,
        "symbol": "GDP",
        "localSymbol": f"GDP_1226_{strike}_{suffix}",
        "secType": "OPT",
        "exchange": "FORECASTX",
        "currency": "USD",
        "tradingClass": "GDP",
        "multiplier": "1",
        "lastTradeDateOrContractMonth": expiry,
        "right": right,
        "strike": strike,
        "minTick": 0.01,
        "longName": "US Gross Domestic Product",
    }


@pytest.fixture
def snapshot_path(tmp_path):
    snapshot = {
        "mode": "live",
        "timestamp": "2026-08-16T00:00:00+00:00",
        "con_ids": [YES_ID, NO_ID, IND_ID, EXPIRED_ID],
        "contracts": {
            str(YES_ID): _opt_row(YES_ID, "C"),
            str(NO_ID): _opt_row(NO_ID, "P"),
            str(IND_ID): {
                "conId": IND_ID,
                "symbol": "GDP",
                "localSymbol": "GDP.X",
                "secType": "IND",
                "exchange": "FORECASTX",
                "currency": "USD",
                "minTick": 0.001,
                "longName": "US Gross Domestic Product",
            },
            str(EXPIRED_ID): _opt_row(EXPIRED_ID, "C", expiry="20200101", strike=3.0),
            "broken": {"symbol": "GDP", "secType": "OPT"},  # no conId -> skipped
        },
    }
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


@pytest.fixture
def resolver(tmp_path, snapshot_path):
    settings = IBKRSettings(
        _env_file=None, port=4002, trading_mode="paper", data_root=str(tmp_path / "cache")
    )
    resolver = ContractResolver(settings)
    resolver.seed_from_snapshot(snapshot_path)
    yield resolver
    resolver.close()


class FakeIB:
    """Answers reqContractDetails from a canned conId -> details mapping."""

    def __init__(self, details_by_con_id: dict):
        self._details = details_by_con_id
        self.requests = []

    def reqContractDetails(self, contract):
        self.requests.append(contract)
        return self._details.get(contract.conId, [])


def _fake_details(con_id: int, right: str = "C") -> SimpleNamespace:
    contract = SimpleNamespace(
        conId=con_id,
        symbol="UNR",
        secType="OPT",
        lastTradeDateOrContractMonth=FUTURE_EXPIRY,
        strike=4.5,
        right=right,
        localSymbol=f"UNR_1226_4.5_{'YES' if right == 'C' else 'NO'}",
        tradingClass="UNR",
        multiplier="1",
        currency="USD",
        exchange="FORECASTX",
    )
    return SimpleNamespace(contract=contract, minTick=0.01, longName="US Unemployment Rate")


class TestSeeding:
    def test_seed_counts_rows_with_con_id(self, tmp_path, snapshot_path):
        settings = IBKRSettings(
            _env_file=None, port=4002, trading_mode="paper", data_root=str(tmp_path / "c2")
        )
        resolver = ContractResolver(settings)
        assert resolver.seed_from_snapshot(snapshot_path) == 4
        resolver.close()

    def test_get_maps_snapshot_fields(self, resolver):
        row = resolver.get(YES_ID)
        assert row["sec_type"] == "OPT"
        assert row["expiry"] == FUTURE_EXPIRY
        assert row["right"] == "C"
        assert row["local_symbol"] == "GDP_1226_2.5_YES"
        assert row["min_tick"] == 0.01
        assert row["long_name"] == "US Gross Domestic Product"

    def test_get_miss_returns_none(self, resolver):
        assert resolver.get(424242) is None


class TestTradable:
    def test_excludes_ind_and_expired(self, resolver):
        assert resolver.tradable_con_ids() == [YES_ID, NO_ID]

    def test_stale_rows_excluded(self, resolver):
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with sqlite3.connect(resolver.db_path) as conn:
            conn.execute(
                "UPDATE contracts SET fetched_at = ? WHERE con_id = ?", (stale, YES_ID)
            )
            conn.commit()
        assert resolver.tradable_con_ids() == [NO_ID]

    def test_invalidate_evicts(self, resolver):
        resolver.invalidate(YES_ID)
        assert resolver.get(YES_ID) is None
        assert resolver.tradable_con_ids() == [NO_ID]

    def test_flush_all(self, resolver):
        resolver.flush_all()
        assert resolver.tradable_con_ids() == []
        assert resolver.get(NO_ID) is None


class TestYesNoPair:
    def test_pair_from_cache(self, resolver):
        assert resolver.yes_no_pair("GDP", FUTURE_EXPIRY, 2.5) == (YES_ID, NO_ID)

    def test_missing_leg_raises(self, resolver):
        with pytest.raises(VenueError, match="YES/NO pair"):
            resolver.yes_no_pair("GDP", "20200101", 3.0)  # expired row has no NO leg

    def test_unknown_market_raises(self, resolver):
        with pytest.raises(VenueError):
            resolver.yes_no_pair("FF", FUTURE_EXPIRY, 4.375)


class TestResolve:
    def test_fresh_cache_hit_makes_no_api_call(self, resolver):
        row = resolver.resolve(ib=None, con_id=YES_ID)  # ib=None proves no call
        assert row["con_id"] == YES_ID

    def test_miss_fetches_by_con_id_and_caches(self, resolver):
        fake = FakeIB({2001: [_fake_details(2001)]})
        row = resolver.resolve(fake, 2001)
        assert row["symbol"] == "UNR"
        assert row["sec_type"] == "OPT"
        assert len(fake.requests) == 1
        assert fake.requests[0].conId == 2001
        assert fake.requests[0].exchange == "FORECASTX"
        # Second call is served from cache: no new request.
        resolver.resolve(fake, 2001)
        assert len(fake.requests) == 1
        assert 2001 in resolver.tradable_con_ids()

    def test_unresolvable_con_id_raises(self, resolver):
        fake = FakeIB({})
        with pytest.raises(VenueError, match="did not resolve"):
            resolver.resolve(fake, 999999)
