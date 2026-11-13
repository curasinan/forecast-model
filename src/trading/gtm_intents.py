"""TradeIntents from a GTM signals payload, priced at the limit the order will carry."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime

from src.domain.ids import MarketId
from src.models.prediction import EdgeSignal
from src.trading.executor import TradeIntent
from src.trading.pricing import DEFAULT_PRICE_POLICY, DEFAULT_TICK, buy_limit_price, finite_or_none
from src.venues.base import VenueAdapter

log = logging.getLogger(__name__)


def _leg_limit(row: Mapping, buy_yes: bool, *, price_policy: str, tick: float,
               ticks_below_ask: int) -> float | None:
    """The limit for the leg being bought, from that leg's own bid and ask."""
    if buy_yes:
        return buy_limit_price(row.get("bid"), row.get("ask"), policy=price_policy, tick=tick,
                               ticks_below_ask=ticks_below_ask)
    return buy_limit_price(row.get("no_bid"), row.get("no_ask"), policy=price_policy, tick=tick,
                           ticks_below_ask=ticks_below_ask)


def intents_from_payload(payload: Mapping, venue: VenueAdapter, top: int | None,
                         min_edge: float, *, price_policy: str = DEFAULT_PRICE_POLICY,
                         ticks_below_ask: int = 0,
                         now: datetime | None = None) -> list[TradeIntent]:
    """Map a gtm_signals payload's tradeable rows to TradeIntents.

    The leg follows the sign of the edge at the mid (YES when the model is
    above the market). The limit comes from that leg's quote under
    `price_policy` ('ask': the ask less `ticks_below_ask` ticks; 'mid': the
    mid), and the edge and EV are taken at that limit, the price actually
    paid. A row whose edge at the limit is below `min_edge` is skipped. The
    intents come back sorted by that edge, largest first.

    `now` is the decision time of a replay: each intent's days to resolution
    is then counted from it instead of from the wall clock.
    """
    rows = [r for r in payload.get("signals", []) if r.get("tradeable")]
    rows.sort(key=lambda r: -abs(float(r.get("edge") or 0.0)))
    if top:
        rows = rows[:top]
    fee_model = venue.fee_model()  # refuses stale fee facts before anything is priced
    intents: list[TradeIntent] = []
    for r in rows:
        p = float(r["p_model"])
        yes_mid = finite_or_none(r.get("yes_mid"))
        if yes_mid is None or r.get("suspect_quote"):
            continue
        buy_yes = p - yes_mid > 0
        question_id = f"gtm:{r['target_month']}:{r['strike']}"
        limits = dict(price_policy=price_policy, ticks_below_ask=ticks_below_ask)

        price = _leg_limit(r, buy_yes, tick=DEFAULT_TICK, **limits)
        if price is None:
            log.warning("skipping %s: no usable %s quote", question_id, "YES" if buy_yes else "NO")
            continue
        if (p - price if buy_yes else (1.0 - p) - price) < min_edge:
            continue

        market_id = MarketId.ibkr(int(r["con_id"]))
        try:
            market = venue.get_market(market_id)
        except Exception as exc:  # noqa: BLE001 -- skip, don't guess a contract
            log.warning("skipping conId %s: %s", r["con_id"], exc)
            continue
        tick = market.tick_size or DEFAULT_TICK
        if tick != DEFAULT_TICK:
            price = _leg_limit(r, buy_yes, tick=tick, **limits)
            if price is None:
                continue

        edge = p - price if buy_yes else (1.0 - p) - price
        if edge < min_edge:
            continue
        yes_price = price if buy_yes else fee_model.yes_no_price_sum() - price
        title = f"GTM {r['target_month']} > {r['strike']}"
        signal = EdgeSignal(
            question_id=question_id,
            model_prob=p, market_prob=yes_price, edge_pct=edge if buy_yes else -edge,
            expected_value=edge - fee_model.execution_cost(price, 1),
            kelly_fraction=float(r.get("half_kelly") or 0.0),
            confidence=1.0,
        )
        days = None
        if now is not None and market.scheduled_resolution_time is not None:
            days = max(0, (market.scheduled_resolution_time - now).days)
        leg = "" if buy_yes else "no_"
        intents.append(TradeIntent(
            edge=signal, market=market, title=title,
            days_to_resolution=days,
            no_price=None if buy_yes else price,
            quote={"policy": price_policy, "ticks_below_ask": ticks_below_ask,
                   "bid": finite_or_none(r.get(f"{leg}bid")),
                   "ask": finite_or_none(r.get(f"{leg}ask")),
                   "limit": price},
        ))
    intents.sort(key=lambda i: -abs(i.edge.edge_pct))  # the group cap goes to the best edge first
    return intents


__all__ = ["intents_from_payload"]
