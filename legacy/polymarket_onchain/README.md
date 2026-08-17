# Retired: Polymarket on-chain / CLOB write path

Retired 2026-08-16 (migration spec Stage 4.4: move, do not delete).

## Why this exists

`src/trading/` originally targeted live execution on Polymarket's CLOB via
`py-clob-client` (Polygon wallet, API credential derivation, signed GTC
orders). Live trading is migrating to IBKR ForecastEx behind the
venue-neutral `VenueAdapter` port (`src/venues/base.py`), so the
Polymarket-specific write path was moved here rather than deleted, for
research reference and in case a Polymarket adapter is ever revisited.

## What worked

- Dry-run simulation: simulated balance, `simulate_trade`, and the
  `OrderManager` simulated-fill path. That code still lives in
  `src/trading/` and remains supported.
- ClobClient construction and API credential derivation (`connect()` in
  `wallet.py` here) executed without error when a private key was supplied.

## What never worked

- Live fills and position creation. The live order path was dead code in
  practice: market token ids were always empty upstream (the Gamma API
  search never populated them), so `_place_live_order` rejected every
  order before signing. No live order was ever placed or filled, and no
  on-chain position was ever created by this code.

## Contents

- `wallet.py` — verbatim copy of the pre-retirement `src/trading/wallet.py`
  (authenticated `ClobClient` init, credential derivation, dry-run sim).
- `clob_orders.py` — the live order placement / cancellation logic from
  `src/trading/order_manager.py`, as standalone functions taking an
  authenticated `ClobClient`.

## See also

- `docs/venue_semantics.md` — verified venue semantics (fees, YES/NO
  price conventions, identifiers) for IBKR ForecastEx vs Polymarket.
- `src/venues/` — the IBKR migration: `VenueAdapter` port and adapters.
