"""Order-book walking for realistic fill-price modeling.

Phase 3 Part 1.

Phase 2's executor simulated fills at top-of-book ask. That overstates
fill quality for any non-trivial order — Kalshi's books for the
Trump-meets-X markets are typically thin (a handful of contracts at
each level), so a $20 buy can easily eat through 3-5 levels and pay
3-7 c above the best ask. The Phase-2 backtest accordingly overstated
ROI per trade.

This module turns an order book + a dollar budget into a deterministic
walk: *exactly* which levels we'd consume, the integer-cents average
fill price, the resulting slippage from top-of-book, and the
fee-inclusive total cost. Same code runs in production (DryRunExecutor)
and in the backtester so historical replays use realistic prices.

Unit conventions (do not deviate):

- Every price is integer cents (1..99 for Kalshi YES contracts).
- Every quantity is integer contracts.
- Every dollar amount is integer cents (USDCents). $1.00 = 100.
- No ``float`` arithmetic anywhere on prices, quantities, or costs.
- Average fill price is computed as ``round_half_even(total_cost /
  quantity)`` — Python's built-in banker's rounding via
  :func:`decimal.Decimal` so two identical inputs always produce the
  same average even on edge cases like 67.5c.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal

# A fee-calculator returns the integer-cents fee Kalshi would charge
# for ``quantity`` contracts filled at ``price_cents``. See
# :mod:`trumpbot.execution.fees` for the production implementation.
FeeCalculator = Callable[[int, int], int]


@dataclass(frozen=True)
class OrderbookWalkResult:
    """The deterministic outcome of walking an order book for a buy.

    Every field is integer-cents / integer-contracts. Reproducible:
    the same inputs always produce the same instance. Holds the full
    audit trail (``levels_consumed``) so the reasoning text can show
    the user *exactly* which levels we'd touch.
    """

    filled_quantity: int
    """Contracts the walk filled at acceptable prices."""

    total_cost_cents: int
    """Sum of (price_cents * qty) across all levels consumed,
    excluding fees. ``filled_quantity * average_fill_price_cents`` is
    not exactly equal — average is rounded; cost is exact."""

    average_fill_price_cents: int
    """Banker's-rounded mean fill price. ``round_half_even(total_cost
    / quantity)``. Guaranteed deterministic across runs."""

    levels_consumed: list[tuple[int, int]] = field(default_factory=list)
    """Ordered list of ``(price_cents, contracts_taken_at_that_price)``
    pairs. Sums of the second element equal :attr:`filled_quantity`."""

    slippage_cents: int = 0
    """Difference between :attr:`average_fill_price_cents` and the
    best (lowest) acceptable ask. Always non-negative; 0 when the
    walk filled entirely at top of book."""

    estimated_fees_cents: int = 0
    """Sum of the per-level fees from the supplied fee calculator."""

    max_price_reached_cents: int = 0
    """Highest price level the walk consumed any contracts at.
    ``0`` when nothing was filled."""

    @property
    def total_cost_with_fees_cents(self) -> int:
        """Convenience: ``total_cost_cents + estimated_fees_cents``."""
        return self.total_cost_cents + self.estimated_fees_cents

    @property
    def is_empty(self) -> bool:
        """True when the walk filled zero contracts (book empty,
        prices all above ceiling, or budget too small)."""
        return self.filled_quantity == 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def merge_to_yes_asks(
    yes_levels: Iterable[tuple[int, int]],
    no_levels: Iterable[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Convert NO bids into implied YES asks and merge with the YES ask side.

    A NO bid at 35c means somebody will pay 35c for the right to receive
    $1 if NO resolves. Equivalently, you can buy a YES contract from
    them for 65c (= 100 - 35) — they pocket the 35 you pay them, you
    get $1 if YES resolves. So a NO bid at price ``p`` becomes an
    implied YES ask at ``100 - p``.

    Both inputs are ``[(price_cents, quantity_contracts), ...]``. They
    do NOT need to be sorted. The output is sorted ascending by price
    with quantities aggregated when the same price appears in both
    sides.
    """
    merged: dict[int, int] = {}
    for price, qty in yes_levels:
        if qty <= 0 or price <= 0 or price >= 100:
            continue
        merged[price] = merged.get(price, 0) + qty
    for no_price, qty in no_levels:
        if qty <= 0 or no_price <= 0 or no_price >= 100:
            continue
        implied_yes_ask = 100 - no_price
        merged[implied_yes_ask] = merged.get(implied_yes_ask, 0) + qty
    return sorted(merged.items())


def walk_orderbook_for_buy(
    yes_ask_levels: Sequence[tuple[int, int]],
    *,
    target_dollars_cents: int,
    max_price_cents: int = 80,
    fee_calculator: FeeCalculator | None = None,
) -> OrderbookWalkResult:
    """Walk an ascending YES-ask book to figure out how many contracts a
    given dollar budget actually buys.

    Args:
        yes_ask_levels: Sequence of ``(price_cents, contracts_available)``,
            already merged via :func:`merge_to_yes_asks` (or equivalent)
            so the YES side and NO-side-implied-as-YES are unified. Does
            NOT need to be pre-sorted; this function sorts ascending by
            price internally.
        target_dollars_cents: How much we're willing to spend, in
            USDCents.
        max_price_cents: Ceiling — any level priced strictly above this
            is filtered out before walking. Defaults to the locked
            ``80`` per CLAUDE.md.
        fee_calculator: Optional ``(price_cents, quantity) -> fee_cents``
            function. When supplied, the result's ``estimated_fees_cents``
            sums the per-level fees. When ``None``, fees are reported
            as 0.

    Returns:
        :class:`OrderbookWalkResult` with all fields populated. When
        the book is empty (or every level is above the ceiling), the
        result is empty (filled_quantity=0).

    The walk is greedy and strictly within budget: if adding a contract
    at the next level would push ``total_cost_cents`` above
    ``target_dollars_cents``, the walk stops. We never overspend; we
    leave the budget partially unfilled if the book can't absorb it
    cleanly. This matches Kalshi's actual order semantics where you
    submit a price-limited order and accept whatever fills.
    """
    if target_dollars_cents <= 0 or max_price_cents <= 0 or not yes_ask_levels:
        return OrderbookWalkResult(
            filled_quantity=0, total_cost_cents=0, average_fill_price_cents=0
        )

    eligible = sorted(
        ((p, q) for p, q in yes_ask_levels if 0 < p <= max_price_cents and q > 0),
        key=lambda pq: pq[0],
    )
    if not eligible:
        return OrderbookWalkResult(
            filled_quantity=0, total_cost_cents=0, average_fill_price_cents=0
        )

    best_ask = eligible[0][0]
    levels_consumed: list[tuple[int, int]] = []
    filled_quantity = 0
    total_cost_cents = 0
    estimated_fees_cents = 0
    max_price_reached_cents = 0

    for price, available in eligible:
        if total_cost_cents >= target_dollars_cents:
            break
        # How many of this level can we afford?
        remaining_budget = target_dollars_cents - total_cost_cents
        affordable = remaining_budget // price  # int floor div
        if affordable <= 0:
            # Can't even take one at this price — done.
            break
        take = min(affordable, available)
        if take <= 0:
            continue
        levels_consumed.append((price, take))
        filled_quantity += take
        total_cost_cents += price * take
        max_price_reached_cents = max(max_price_reached_cents, price)
        if fee_calculator is not None:
            estimated_fees_cents += fee_calculator(price, take)

    if filled_quantity == 0:
        return OrderbookWalkResult(
            filled_quantity=0, total_cost_cents=0, average_fill_price_cents=0
        )

    average_fill_price_cents = _banker_round_div(total_cost_cents, filled_quantity)
    slippage_cents = max(0, average_fill_price_cents - best_ask)

    return OrderbookWalkResult(
        filled_quantity=filled_quantity,
        total_cost_cents=total_cost_cents,
        average_fill_price_cents=average_fill_price_cents,
        levels_consumed=levels_consumed,
        slippage_cents=slippage_cents,
        estimated_fees_cents=estimated_fees_cents,
        max_price_reached_cents=max_price_reached_cents,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _banker_round_div(numerator_cents: int, denominator: int) -> int:
    """``round_half_even(numerator / denominator)`` in integer cents.

    Why banker's rounding (round-half-to-even) and not regular
    half-up rounding: 67.5c rounding to 68 in one run and 67 in
    another would silently make the same fixture produce different
    averages. With banker's rounding, .5 always goes to the nearest
    EVEN integer, deterministically: 67.5 -> 68, 68.5 -> 68. Two
    identical walks always produce identical averages, byte-for-byte.

    Uses :class:`decimal.Decimal` to side-step Python's float
    representation gotchas (e.g. ``round(67.5)`` is 68 but
    ``round(2.675, 2)`` is 2.67 due to binary float drift).
    """
    if denominator == 0:
        return 0
    quotient = Decimal(numerator_cents) / Decimal(denominator)
    return int(quotient.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


__all__ = [
    "FeeCalculator",
    "OrderbookWalkResult",
    "merge_to_yes_asks",
    "walk_orderbook_for_buy",
]
