"""Kalshi fee-calculator tests.

Pinned to the published 7 % rate from
https://kalshi.com/docs/fees as of 2026-04-25. If Kalshi changes
the schedule, update both :data:`trumpbot.execution.fees.FEE_RATE`
and these expected values together.

Each scenario is hand-computed and verified. Float drift would
silently turn ``$1.75`` into ``$1.7499999...`` and round wrong; the
implementation uses :class:`decimal.Decimal` to avoid this.
"""

from __future__ import annotations

import pytest

from trumpbot.execution.fees import (
    FEE_RATE,
    calculate_entry_fee_cents,
    calculate_exit_fee_cents,
)

# ---------------------------------------------------------------------------
# Fee rate constant
# ---------------------------------------------------------------------------


def test_fee_rate_is_seven_percent() -> None:
    """If this fails, Kalshi has likely updated their schedule. Update
    the constant AND every expected value in this file in the same
    commit."""
    assert float(FEE_RATE) == 0.07


# ---------------------------------------------------------------------------
# Headline scenarios from the spec
# ---------------------------------------------------------------------------


class TestSpecScenarios:
    def test_p_50_x_100_contracts_equals_175_cents(self) -> None:
        # 0.07 x 100 x 50 x 50 / 100 = 175 cents ($1.75) — the peak.
        assert calculate_entry_fee_cents(50, 100) == 175

    def test_p_10_x_100_contracts_equals_63_cents(self) -> None:
        # 0.07 x 100 x 10 x 90 / 100 = 63 cents ($0.63).
        assert calculate_entry_fee_cents(10, 100) == 63

    def test_p_65_x_50_contracts(self) -> None:
        # 0.07 x 50 x 65 x 35 / 100 = 79.625 cents -> ceil to 80 ($0.80).
        assert calculate_entry_fee_cents(65, 50) == 80

    def test_p_99_x_1000_contracts(self) -> None:
        # 0.07 x 1000 x 99 x 1 / 100 = 69.3 cents -> ceil to 70.
        # ($0.70 — much less than $1, despite the spec's
        # "near $1" approximation; the formula tapers at extremes.)
        assert calculate_entry_fee_cents(99, 1000) == 70

    def test_p_01_x_1000_contracts(self) -> None:
        # 0.07 x 1000 x 1 x 99 / 100 = 69.3 cents -> ceil to 70.
        assert calculate_entry_fee_cents(1, 1000) == 70


# ---------------------------------------------------------------------------
# Resolution-extreme edge cases
# ---------------------------------------------------------------------------


class TestExtremes:
    def test_zero_price_pays_no_fee(self) -> None:
        assert calculate_entry_fee_cents(0, 1000) == 0

    def test_one_hundred_price_pays_no_fee(self) -> None:
        # YES contract at $1 means "settled YES" — no fee on settlement.
        assert calculate_entry_fee_cents(100, 1000) == 0

    def test_zero_quantity_pays_no_fee(self) -> None:
        assert calculate_entry_fee_cents(50, 0) == 0

    def test_negative_quantity_pays_no_fee(self) -> None:
        # Defensive — should never happen in real flow.
        assert calculate_entry_fee_cents(50, -10) == 0


# ---------------------------------------------------------------------------
# Tiny trades (effective minimum-fee zone)
# ---------------------------------------------------------------------------


class TestSmallTrades:
    def test_one_contract_at_50c_rounds_up_to_two_cents(self) -> None:
        # 0.07 x 1 x 50 x 50 / 100 = 1.75 cents -> ceil to 2.
        # Kalshi's fee math means a single contract still carries an
        # observable fee.
        assert calculate_entry_fee_cents(50, 1) == 2

    def test_one_contract_at_5c_rounds_up_to_one_cent(self) -> None:
        # 0.07 x 1 x 5 x 95 / 100 = 0.3325 cents -> ceil to 1.
        assert calculate_entry_fee_cents(5, 1) == 1


# ---------------------------------------------------------------------------
# Symmetry around P = 0.50
# ---------------------------------------------------------------------------


class TestSymmetry:
    @pytest.mark.parametrize("price_cents", [10, 25, 40, 49])
    def test_complementary_prices_have_equal_fees(self, price_cents: int) -> None:
        """Fee at P = p equals fee at P = (100 - p) for any quantity —
        the formula is symmetric about P = 0.5."""
        complement = 100 - price_cents
        assert calculate_entry_fee_cents(price_cents, 100) == calculate_entry_fee_cents(
            complement, 100
        )


# ---------------------------------------------------------------------------
# Integer / determinism guards
# ---------------------------------------------------------------------------


class TestIntegerArithmetic:
    def test_returns_int_type(self) -> None:
        out = calculate_entry_fee_cents(65, 50)
        assert isinstance(out, int)

    def test_repeated_calls_byte_identical(self) -> None:
        # Catches any float drift that sneaks in via a future refactor.
        results = [calculate_entry_fee_cents(67, 31) for _ in range(50)]
        assert all(r == results[0] for r in results)

    def test_no_negative_result_anywhere(self) -> None:
        # For every (price, qty) pair in the realistic range, the fee
        # is non-negative.
        for price in range(1, 100):
            for qty in (1, 5, 50, 500):
                assert calculate_entry_fee_cents(price, qty) >= 0


# ---------------------------------------------------------------------------
# Exit fee parity
# ---------------------------------------------------------------------------


class TestExitFee:
    @pytest.mark.parametrize(
        "price_cents,qty",
        [(50, 100), (65, 50), (35, 200), (1, 1000), (99, 1000)],
    )
    def test_exit_fee_matches_entry_fee(self, price_cents: int, qty: int) -> None:
        """Phase 3 Part 1 keeps entry and exit fees symmetric. If
        Kalshi adds a maker/taker split, this test should fail and the
        implementation should diverge them."""
        assert calculate_exit_fee_cents(price_cents, qty) == calculate_entry_fee_cents(
            price_cents, qty
        )


__all__: list[str] = []
