"""Slippage / order-book-walking unit tests.

Pinned by the LOCKED Phase-3 strategy. Every numeric outcome is
hand-computed and asserted; if anyone tweaks the walk algorithm and
silently changes a fill price, these tests fail loudly.
"""

from __future__ import annotations

import pytest

from trumpbot.execution.slippage import (
    _banker_round_div,
    merge_to_yes_asks,
    walk_orderbook_for_buy,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _flat_fee(price_cents: int, qty: int) -> int:
    """Trivial fee calculator for tests: 1c per contract regardless of price."""
    del price_cents
    return qty


# ---------------------------------------------------------------------------
# walk_orderbook_for_buy — core scenarios
# ---------------------------------------------------------------------------


class TestWalkBuy:
    def test_empty_book_returns_zero_fill(self) -> None:
        out = walk_orderbook_for_buy([], target_dollars_cents=1000)
        assert out.is_empty
        assert out.filled_quantity == 0
        assert out.total_cost_cents == 0
        assert out.average_fill_price_cents == 0
        assert out.slippage_cents == 0

    def test_single_level_full_fill(self) -> None:
        # 50 contracts at 60c, $20 budget — buys 33 contracts ($19.80).
        out = walk_orderbook_for_buy([(60, 50)], target_dollars_cents=2000)
        assert out.filled_quantity == 33
        assert out.total_cost_cents == 1980
        assert out.average_fill_price_cents == 60
        assert out.slippage_cents == 0
        assert out.max_price_reached_cents == 60
        assert out.levels_consumed == [(60, 33)]

    def test_two_levels_split_fill(self) -> None:
        # 5 at 60c ($3.00) + need 17c more from the next level at 65c.
        # Available at next level: 10. Affordable from $0.17 budget at 65c:
        # 17 // 65 = 0 (can't afford one). Walk stops with just the 5.
        out = walk_orderbook_for_buy([(60, 5), (65, 10)], target_dollars_cents=317)
        assert out.filled_quantity == 5
        assert out.total_cost_cents == 300
        assert out.average_fill_price_cents == 60
        assert out.levels_consumed == [(60, 5)]

    def test_two_levels_genuine_split(self) -> None:
        # 5 at 60c ($3.00) + 10 at 65c ($6.50) -> take all 5 + 4 more at 65c
        # (4 * 65 = 260c). Total = 300 + 260 = 560c. Avg = 560 / 9 = 62.2 -> 62.
        out = walk_orderbook_for_buy([(60, 5), (65, 10)], target_dollars_cents=600)
        assert out.filled_quantity == 9
        assert out.total_cost_cents == 560
        # 560 / 9 = 62.22... -> banker's round to 62.
        assert out.average_fill_price_cents == 62
        assert out.slippage_cents == 2  # 62 - 60
        assert out.max_price_reached_cents == 65
        assert out.levels_consumed == [(60, 5), (65, 4)]

    def test_max_price_ceiling_filters_out_levels(self) -> None:
        # Top-of-book is 60, then 80, then 85. Ceiling 80 keeps the 80
        # but drops the 85.
        out = walk_orderbook_for_buy(
            [(60, 5), (80, 5), (85, 100)], target_dollars_cents=2000, max_price_cents=80
        )
        # Take all 5 at 60 ($3.00), all 5 at 80 ($4.00), exhaust depth
        # under ceiling. Total $7.00, avg = 700 / 10 = 70.
        assert out.filled_quantity == 10
        assert out.total_cost_cents == 700
        assert out.average_fill_price_cents == 70
        assert out.max_price_reached_cents == 80
        assert out.slippage_cents == 10  # 70 - 60
        # 85 level was filtered out — never appears in levels_consumed.
        assert (85, 100) not in out.levels_consumed

    def test_target_below_top_of_book_price(self) -> None:
        # Top ask is 80c. Budget 50c. Can't afford a single contract.
        out = walk_orderbook_for_buy([(80, 100)], target_dollars_cents=50)
        assert out.is_empty

    def test_target_exactly_one_contract(self) -> None:
        out = walk_orderbook_for_buy([(80, 100)], target_dollars_cents=80)
        assert out.filled_quantity == 1
        assert out.total_cost_cents == 80
        assert out.average_fill_price_cents == 80

    def test_budget_exactly_consumes_one_level(self) -> None:
        # 4 contracts at 50c = $2.00 exactly.
        out = walk_orderbook_for_buy([(50, 4), (60, 100)], target_dollars_cents=200)
        assert out.filled_quantity == 4
        assert out.total_cost_cents == 200
        assert out.average_fill_price_cents == 50
        # No bleed into the second level.
        assert out.levels_consumed == [(50, 4)]

    def test_insufficient_liquidity_partial_fill(self) -> None:
        # Only 2 contracts in the entire book under ceiling. Big budget.
        out = walk_orderbook_for_buy(
            [(60, 2), (90, 1000)], target_dollars_cents=10_000, max_price_cents=80
        )
        assert out.filled_quantity == 2
        assert out.total_cost_cents == 120
        assert out.average_fill_price_cents == 60
        # Walk stopped because there's no acceptable inventory left.
        assert out.max_price_reached_cents == 60

    def test_levels_consumed_quantities_sum_to_filled_quantity(self) -> None:
        out = walk_orderbook_for_buy([(50, 3), (55, 3), (60, 10)], target_dollars_cents=1000)
        assert sum(q for _, q in out.levels_consumed) == out.filled_quantity

    def test_average_price_invariant_holds_exactly(self) -> None:
        # Recompute by hand:
        #   3 @ 50 = 150  (running 150c, 3 contracts, 850c budget left)
        #   3 @ 55 = 165  (running 315c, 6 contracts, 685c left)
        #   At 60: affordable = 685 // 60 = 11, available = 10 -> take 10
        #   Cost = 600. Total = 915c, qty = 16, avg = 915/16 = 57.19 -> 57
        # Walks correctly stop when the level is exhausted, not when the
        # budget hits a fractional cent.
        out = walk_orderbook_for_buy([(50, 3), (55, 3), (60, 10)], target_dollars_cents=1000)
        assert out.total_cost_cents == 915
        assert out.filled_quantity == 16
        assert out.average_fill_price_cents == 57

    def test_realistic_putin_book_eight_levels(self) -> None:
        """A realistic Trump-meets-X book with eight ascending levels.

        Manually-computed expected output: with $20 budget at the
        80c ceiling, walk through the cheap levels until budget
        exhausts. Test pins the exact result so any drift from a
        future refactor surfaces immediately."""
        book = [
            (52, 3),  # 3 @ 52  ->  156c
            (54, 4),  # 4 @ 54  ->  216c (running: 372)
            (58, 2),  # 2 @ 58  ->  116c (running: 488)
            (60, 5),  # 5 @ 60  ->  300c (running: 788)
            (65, 4),  # 4 @ 65  ->  260c (running: 1048)
            (70, 6),  # 70c x 6 = 420 — 1048 + 420 = 1468 ≤ 2000, take all
            (75, 8),  # 75c x 8 = 600 — 1468 + 600 = 2068 > 2000; take ((2000-1468)//75)=7
            (80, 100),
        ]
        out = walk_orderbook_for_buy(book, target_dollars_cents=2000, max_price_cents=80)
        # Hand-verified:
        #   3 + 4 + 2 + 5 + 4 + 6 + 7 = 31 contracts
        #   cost = 156 + 216 + 116 + 300 + 260 + 420 + 525 = 1993c
        #   avg = 1993 / 31 = 64.29 -> banker's round to 64
        assert out.filled_quantity == 31
        assert out.total_cost_cents == 1993
        assert out.average_fill_price_cents == 64
        assert out.slippage_cents == 12  # 64 - 52
        assert out.max_price_reached_cents == 75
        assert out.levels_consumed == [
            (52, 3),
            (54, 4),
            (58, 2),
            (60, 5),
            (65, 4),
            (70, 6),
            (75, 7),
        ]

    def test_zero_target_returns_empty(self) -> None:
        out = walk_orderbook_for_buy([(60, 5)], target_dollars_cents=0)
        assert out.is_empty

    def test_negative_target_returns_empty(self) -> None:
        # Defensive — should never happen in real flow.
        out = walk_orderbook_for_buy([(60, 5)], target_dollars_cents=-100)
        assert out.is_empty

    def test_max_price_zero_filters_everything(self) -> None:
        out = walk_orderbook_for_buy([(40, 100)], target_dollars_cents=1000, max_price_cents=0)
        assert out.is_empty


# ---------------------------------------------------------------------------
# Determinism (the float-drift guard)
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_repeated_walks_identical_to_the_byte(self) -> None:
        """Walking the SAME book with the SAME inputs N times must give
        identical OrderbookWalkResults. Catches any float drift that
        sneaks in via a future refactor."""
        book = [(50, 3), (55, 3), (60, 10), (65, 5)]
        results = [walk_orderbook_for_buy(book, target_dollars_cents=750) for _ in range(50)]
        first = results[0]
        for r in results[1:]:
            assert r == first

    def test_banker_rounding_handles_half_cases_evenly(self) -> None:
        # 67.5c rounds toward EVEN: 68 (even) chosen over 67.
        assert _banker_round_div(135, 2) == 68  # 67.5 -> 68
        # 68.5c rounds toward EVEN: 68 (already even). Stays at 68.
        assert _banker_round_div(137, 2) == 68
        # 0.5 -> 0 (even).
        assert _banker_round_div(1, 2) == 0
        # 2.5 -> 2 (even).
        assert _banker_round_div(5, 2) == 2

    def test_banker_div_zero_denom_returns_zero(self) -> None:
        # Defensive — should never happen because callers guard.
        assert _banker_round_div(100, 0) == 0


# ---------------------------------------------------------------------------
# Fee integration
# ---------------------------------------------------------------------------


class TestFeeIntegration:
    def test_fee_calculator_summed_across_levels(self) -> None:
        # Trivial 1c/contract fee. 9 contracts -> 9c total.
        out = walk_orderbook_for_buy(
            [(60, 5), (65, 10)],
            target_dollars_cents=600,
            fee_calculator=_flat_fee,
        )
        assert out.estimated_fees_cents == out.filled_quantity == 9
        assert out.total_cost_with_fees_cents == out.total_cost_cents + 9

    def test_no_fee_calculator_means_zero_fees(self) -> None:
        out = walk_orderbook_for_buy([(60, 5)], target_dollars_cents=300)
        assert out.estimated_fees_cents == 0
        assert out.total_cost_with_fees_cents == out.total_cost_cents


# ---------------------------------------------------------------------------
# merge_to_yes_asks (NO bid -> YES ask inversion)
# ---------------------------------------------------------------------------


class TestMergeToYesAsks:
    def test_pure_yes_side_passes_through_sorted(self) -> None:
        out = merge_to_yes_asks([(70, 5), (60, 3)], no_levels=[])
        assert out == [(60, 3), (70, 5)]

    def test_no_bid_inverts_to_yes_ask(self) -> None:
        # NO bid at 35c ⇒ implied YES ask at 65c.
        out = merge_to_yes_asks(yes_levels=[], no_levels=[(35, 10)])
        assert out == [(65, 10)]

    def test_yes_and_no_at_same_implied_price_aggregate(self) -> None:
        # YES ask 65c for 3 + NO bid 35c for 7 (= implied YES ask 65c)
        # -> single 65c level with 10.
        out = merge_to_yes_asks([(65, 3)], [(35, 7)])
        assert out == [(65, 10)]

    def test_negative_or_zero_quantities_filtered(self) -> None:
        out = merge_to_yes_asks([(50, 0), (60, -5), (70, 3)], [])
        assert out == [(70, 3)]

    def test_extreme_prices_filtered(self) -> None:
        # Prices ≤ 0 or ≥ 100 are nonsense for Kalshi binary contracts.
        out = merge_to_yes_asks([(0, 5), (100, 5), (50, 3)], [(100, 1), (-1, 5)])
        assert out == [(50, 3)]


# ---------------------------------------------------------------------------
# Property-style invariants
# ---------------------------------------------------------------------------


class TestInvariants:
    @pytest.mark.parametrize(
        "book,target",
        [
            ([(50, 100)], 5_000),
            ([(50, 5), (60, 5), (70, 5), (80, 5)], 800),
            ([(45, 1), (47, 1), (49, 1), (51, 1), (53, 1)], 250),
            ([(80, 1)], 80),  # exact-fit single contract
        ],
    )
    def test_levels_consumed_sum_equals_filled_quantity(
        self, book: list[tuple[int, int]], target: int
    ) -> None:
        out = walk_orderbook_for_buy(book, target_dollars_cents=target)
        assert sum(q for _, q in out.levels_consumed) == out.filled_quantity

    @pytest.mark.parametrize(
        "book,target",
        [
            ([(50, 100)], 5_000),
            ([(50, 5), (60, 5), (70, 5), (80, 5)], 800),
            ([(45, 1), (47, 1), (49, 1), (51, 1), (53, 1)], 250),
        ],
    )
    def test_max_price_reached_under_ceiling(
        self, book: list[tuple[int, int]], target: int
    ) -> None:
        out = walk_orderbook_for_buy(book, target_dollars_cents=target, max_price_cents=80)
        if not out.is_empty:
            assert out.max_price_reached_cents <= 80

    @pytest.mark.parametrize(
        "book,target",
        [
            ([(50, 100)], 5_000),
            ([(50, 5), (60, 5), (70, 5), (80, 5)], 800),
            ([(45, 1), (47, 1), (49, 1), (51, 1), (53, 1)], 250),
        ],
    )
    def test_total_cost_never_exceeds_budget(
        self, book: list[tuple[int, int]], target: int
    ) -> None:
        out = walk_orderbook_for_buy(book, target_dollars_cents=target)
        assert out.total_cost_cents <= target


__all__: list[str] = []
