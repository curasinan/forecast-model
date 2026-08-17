"""RETIRED (2026-08-16) — live Polymarket CLOB order placement/cancellation.

Extracted from src/trading/order_manager.py (_place_live_order and the
live branch of cancel_order) at retirement. This path never produced a
live fill: token ids were always empty upstream, so every live order was
rejected before signing. Kept for research reference only.

Both functions take an authenticated ``py_clob_client.client.ClobClient``
(i.e. one that has had ``set_api_creds`` called — see
legacy/polymarket_onchain/wallet.py).
"""

import logging

logger = logging.getLogger(__name__)


def place_live_order(client, token_id: str, price: float, size: float) -> dict:
    """Sign and post a GTC BUY limit order on Polymarket's CLOB.

    Returns the raw API response dict: ``{"success": bool, "orderID": str,
    "errorMsg": str, ...}``. Raises on network/signing failure.
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    if not token_id:
        raise ValueError("token_id is required to place a live order")

    args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=BUY,
    )

    signed_order = client.create_order(args)
    resp = client.post_order(signed_order, OrderType.GTC)

    if resp and resp.get("success"):
        logger.info(f"[LIVE] Order placed: CLOB ID {resp.get('orderID', '')}")
    else:
        error = resp.get("errorMsg", "Unknown error") if resp else "No response"
        logger.error(f"[LIVE] Order rejected: {error}")
    return resp or {}


def cancel_live_order(client, clob_order_id: str) -> bool:
    """Cancel a resting CLOB order by its Polymarket order id."""
    if not clob_order_id:
        return False
    try:
        client.cancel(clob_order_id)
        return True
    except Exception as e:
        logger.error(f"Cancel failed: {e}")
        return False
