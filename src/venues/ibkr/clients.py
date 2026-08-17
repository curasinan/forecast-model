"""Gateway connection management under the single-writer client-id rule.

Each process role owns exactly one client id (migration spec Stage 2.3/2.4):

    live_trading   -> 1   (never connected by this codebase)
    data_collector -> 2   (the recorder, the only sanctioned writer of ticks)
    research       -> 3

Ids are NEVER auto-incremented on collision: a second holder of an id means
two processes think they own a role, which is exactly the bug the map exists
to make loud. The collision raises ClientIdInUseError instead.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import warnings

from src.venues.base import VenueError
from src.venues.ibkr.settings import IBKRSettings

logger = logging.getLogger(__name__)

CLIENT_IDS: dict[str, int] = {"live_trading": 1, "data_collector": 2, "research": 3}

_IN_USE_MARKERS = ("326", "already in use", "client id")


class ClientIdInUseError(VenueError):
    """The role's client id is already held by another process."""


def _configure_win32_event_loop() -> None:
    """ib_async needs the selector loop on Windows (raw sockets).

    The policy API is deprecated (removal in 3.16) but is still the only way
    to steer ib_async's internally created loop; suppress the warning, same
    as scripts/verify_ibkr_entitlements.py.
    """
    if sys.platform == "win32":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def connect_ib(role: str, settings: IBKRSettings, timeout: float = 10.0, readonly: bool = True):
    """Connect to the Gateway as ``role`` and return a connected ib_async IB.

    Raises ValueError for an unknown role and ClientIdInUseError when the
    role's id is already connected (never auto-increments -- see module
    docstring). ``readonly`` defaults to True: no code path in this build
    transmits orders.
    """
    if role not in CLIENT_IDS:
        raise ValueError(f"unknown role {role!r}; valid roles: {sorted(CLIENT_IDS)}")
    _configure_win32_event_loop()
    from ib_async import IB  # lazy: the loop policy must be set first

    # Without this, any request dropped during a Gateway<->IBKR connectivity
    # blip (error 1100) blocks its sync wrapper forever (verified 2026-08-16).
    IB.RequestTimeout = 60

    client_id = CLIENT_IDS[role]
    ib = IB()
    try:
        ib.connect(
            settings.host,
            settings.port,
            clientId=client_id,
            timeout=timeout,
            readonly=readonly,
        )
    except Exception as exc:
        ib.disconnect()
        msg = str(exc).lower()
        if any(marker in msg for marker in _IN_USE_MARKERS):
            raise ClientIdInUseError(
                f"client id {client_id} for role {role!r} is already in use on "
                f"{settings.host}:{settings.port}. The id map is {CLIENT_IDS} -- one "
                "process per role, ids are never auto-incremented (single-writer "
                "rule, spec Stage 2.3/2.4). Stop the other holder of this role."
            ) from exc
        raise
    logger.info("connected to %s:%s as role=%s clientId=%s", settings.host, settings.port,
                role, client_id)
    return ib
