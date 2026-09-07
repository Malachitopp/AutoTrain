"""The per-caller rate limiter's own bounds.

No database and no HTTP: the limiting decision is a pure function of the
structure it keeps, and that structure is the thing worth pinning. A rate
limiter that grows without bound is a cheaper attack than the one it exists
to stop, and the first version of this class had exactly that defect in two
places at once — so these are regression tests before they are anything else.
"""

from __future__ import annotations

import sys

from autotrain.api.middleware import _MAX_TRACKED_CLIENTS, RateLimitMiddleware


def _limiter(limit: int = 5, window_seconds: float = 3600.0) -> RateLimitMiddleware:
    async def _app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        raise AssertionError("the limiter under test never forwards")

    return RateLimitMiddleware(
        _app, limit=limit, window_seconds=window_seconds, paths=frozenset({"/x"})
    )


def test_a_blocked_caller_stops_costing_memory() -> None:
    """The defect this replaced: hits were recorded BEFORE the cap was
    checked, so a caller already refused kept appending — and since
    everything they sent was inside the window, nothing ever expired out of
    it. One address knocking at a thousand requests a second for an hour
    held about 115 MB. Now the list stops at the cap, and knocking while
    blocked neither costs memory nor extends the block."""
    limiter = _limiter(limit=5)

    allowed = [not limiter._over_limit("10.0.0.1") for _ in range(5)]
    assert allowed == [True] * 5

    for _ in range(10_000):
        assert limiter._over_limit("10.0.0.1") is True

    # Five timestamps after ten thousand and five requests.
    assert len(limiter._hits["10.0.0.1"]) == 5


def test_cycling_addresses_cannot_grow_the_table_past_its_cap() -> None:
    """The other half of the same defect. The old pruning pass only dropped
    callers with nothing left inside the window — so against the attack it
    was written for, an attacker using a fresh address every request, it
    found nothing to drop on every pass and the table grew anyway, while
    costing a walk of every entry per request. Eviction is now by least
    recently seen, which always has something to give."""
    limiter = _limiter(limit=5)

    for i in range(_MAX_TRACKED_CLIENTS + 2_000):
        limiter._over_limit(f"10.0.{i // 256}.{i % 256}")

    assert len(limiter._hits) <= _MAX_TRACKED_CLIENTS
    # The most recent callers are the ones kept; the earliest are gone.
    assert "10.0.0.0" not in limiter._hits
    assert f"10.0.{(_MAX_TRACKED_CLIENTS + 1999) // 256}.{(_MAX_TRACKED_CLIENTS + 1999) % 256}" in (
        limiter._hits
    )


def test_the_whole_structure_stays_small_under_the_worst_case() -> None:
    """The ceiling stated in the class docstring, measured rather than
    asserted by comment. Every tracked caller at its cap — the largest the
    structure can legally get — must stay in single-digit megabytes."""
    limiter = _limiter(limit=20)
    for i in range(_MAX_TRACKED_CLIENTS):
        host = f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}"
        for _ in range(25):  # past the cap, so each list is full at 20
            limiter._over_limit(host)

    assert len(limiter._hits) == _MAX_TRACKED_CLIENTS
    assert all(len(hits) == 20 for hits in limiter._hits.values())

    footprint = sys.getsizeof(limiter._hits) + sum(
        sys.getsizeof(host) + sys.getsizeof(hits) + sum(sys.getsizeof(h) for h in hits)
        for host, hits in limiter._hits.items()
    )
    assert footprint < 16 * 1024 * 1024, f"{footprint / 1024 / 1024:.1f} MB"


def test_capacity_returns_as_the_window_slides() -> None:
    """A blocked caller is not blocked for ever. Nothing is recorded while
    they are over, so once their oldest hits fall out of the window they are
    served again — which is what makes the memory bound safe to have."""
    limiter = _limiter(limit=3, window_seconds=0.0)  # every hit is instantly stale

    assert [not limiter._over_limit("10.0.0.9") for _ in range(10)] == [True] * 10
    # Ten requests, one live hit: each call expires what came before it and
    # records only itself, so nothing accumulates for a caller who is inside
    # their share.
    assert len(limiter._hits["10.0.0.9"]) == 1
