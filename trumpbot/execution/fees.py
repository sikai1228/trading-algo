"""Kalshi fee modeling.

Phase 3 Part 1.

Kalshi charges a per-trade fee proportional to ``P x (1 - P)`` where
``P`` is the fill price as a decimal (price_cents / 100). The formula
peaks at ``P = 0.50`` (1.75 c per contract -> $1.75 on a 100-contract
trade) and tapers toward zero at both extremes — a contract bought at
99 c that resolves YES at 100 c pays roughly the same fee as one
bought at 1 c that resolves NO.

Implications for the strategy:

- The Trump-meets-X markets we trade typically cluster between 40 c
  and 70 c — the steepest fee zone. A $20 buy at 65 c pays roughly
  ``0.07 x 30 x 0.65 x 0.35 ≈ $0.48`` in entry fees, not nothing.
- At settlement the resolution price is 0 c or 100 c, so exit fees
  on a YES win are near-zero. Entry fees dominate.
- The :func:`walk_orderbook_for_buy` walker is fee-aware: pass
  :func:`calculate_entry_fee_cents` as ``fee_calculator`` to roll
  the per-level fees into ``estimated_fees_cents`` on the result.

Source of the constant: Kalshi's published trading-fee schedule at
https://kalshi.com/docs/fees as of 2026-04-25. **If Kalshi changes
their formula, update :data:`FEE_RATE` here and add a regression test
pinning the new expected values; do not silently re-tune.**
"""

from __future__ import annotations

import math
from decimal import Decimal

# Kalshi's per-contract fee multiplier as of 2026-04-25.
# fee_per_contract_cents = ceil( FEE_RATE x 100 x P x (1 - P) )
# where P is the fill price as a decimal in [0, 1].
FEE_RATE: Decimal = Decimal("0.07")
"""Per-contract fee rate. Multiplies P x (1 - P) x 100 (the per-
contract dollar fee, in cents). Verify against
https://kalshi.com/docs/fees periodically — if Kalshi adjusts their
fee schedule, update this constant and the test fixtures together."""


def calculate_entry_fee_cents(price_cents: int, quantity: int) -> int:
    """Total fee, in integer cents, for buying ``quantity`` YES
    contracts at ``price_cents``.

    Uses :class:`decimal.Decimal` so the per-trade total is exact:
    ``ceil(FEE_RATE x Q x P_cents x (100 - P_cents) / 100)``.
    Result is rounded UP to the next whole cent — Kalshi rounds
    fractional fees in their favor.

    Edge cases:

    - ``price_cents`` of 0 or 100 (resolution extremes) -> fee is 0.
    - ``quantity`` ≤ 0 -> fee is 0 (defensive; should never happen).
    - For numerically odd inputs (e.g. quantity=1 at 50 c) the formula
      gives 0.0175 c which rounds up to 1 cent — Kalshi's effective
      minimum fee per contract.
    """
    if quantity <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    p = Decimal(price_cents)
    q = Decimal(quantity)
    one_hundred = Decimal(100)
    # Decimal arithmetic to avoid float drift: total fee in cents =
    # FEE_RATE x Q x P x (100 - P) / 100. The /100 inside the
    # P x (1 - P) factor times the x100 to convert to cents per
    # contract cancel; what remains is FEE_RATE x Q x P x (100 - P) / 100.
    fee = FEE_RATE * q * p * (one_hundred - p) / one_hundred
    return _ceil_decimal_to_int(fee)


def calculate_exit_fee_cents(price_cents: int, quantity: int) -> int:
    """Total fee, in integer cents, for selling ``quantity`` YES
    contracts at ``price_cents``.

    Currently identical to the entry fee — Kalshi applies the same
    formula on entry and exit. Kept as a separate function so a
    future asymmetric fee (e.g. maker rebate, taker premium) is a
    single-call-site change.
    """
    return calculate_entry_fee_cents(price_cents, quantity)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ceil_decimal_to_int(d: Decimal) -> int:
    """Ceiling of a :class:`Decimal` to ``int``. Used to round fees
    up to the next whole cent."""
    return int(math.ceil(d))


__all__ = [
    "FEE_RATE",
    "calculate_entry_fee_cents",
    "calculate_exit_fee_cents",
]
