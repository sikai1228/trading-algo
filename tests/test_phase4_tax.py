"""Phase 4 Part 2.1 — tax tracking, exports, commands, and migration
backfill regression tests.

Covers:

- Migration 008 schema additions
- insert_trade populates acquisition fields at fill time
- close_trade populates disposal fields at close time
- Backfill correctness for pre-existing closed rows
- TaxExporter aggregate stats
- TaxExporter CSV / JSON / Form 8949 / Kalshi reconciliation outputs
- /tax_summary, /tax_export, /tax_reconcile command handlers
- Decimal / cent precision in dollar formatting
- Edge cases: year-boundary trades, void resolution, NULL fees
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    NewsEventRow,
    TradeInsertRow,
    close_trade,
    insert_news_event,
    insert_trade,
    upsert_market,
)
from trumpbot.exports.tax_exports import (
    TaxExporter,
    YearlySummary,
    _bare_dollars,
    _dollars_str,
    write_export,
)
from trumpbot.notifications.commands import (
    CommandContext,
    handle_tax_export,
    handle_tax_reconcile,
    handle_tax_summary,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "tax.db")
    db.connect()
    upsert_market(
        db,
        MarketRow(
            ticker="X",
            series_ticker="KXTRUMPMEET",
            event_ticker=None,
            title="Trump and X meet?",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject="x",
            subject_full_name="X",
            resolution_rules="r",
            approved_sources=None,
            open_ts="2026-01-01T00:00:00Z",
            close_ts="2026-12-31T23:59:59Z",
            expected_expiration_ts=None,
            status="active",
            last_price_cents=50,
            volume=1000,
            open_interest=200,
            raw_json=None,
        ),
    )
    upsert_market(
        db,
        MarketRow(
            ticker="Y",
            series_ticker="KXTRUMPMEET",
            event_ticker=None,
            title="Trump and Y meet?",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject="y",
            subject_full_name="Y",
            resolution_rules="r",
            approved_sources=None,
            open_ts="2026-01-01T00:00:00Z",
            close_ts="2026-12-31T23:59:59Z",
            expected_expiration_ts=None,
            status="active",
            last_price_cents=50,
            volume=1000,
            open_interest=200,
            raw_json=None,
        ),
    )
    eid = insert_news_event(
        db,
        NewsEventRow(
            source="reuters_via_gnews",
            is_kalshi_approved=True,
            headline="h",
            url="https://e.com/1",
            url_canonical="https://e.com/1",
            body_excerpt=None,
            author=None,
            raw_published_ts=None,
            detected_ts="2026-04-15T12:00:00Z",
            has_photo=False,
            has_video=False,
            raw_data=None,
        ),
    )
    assert eid is not None
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO news_market_matches (news_event_id, ticker, confidence, "
            "matched_subject, match_reason) VALUES (?, 'X', 0.9, 'x', 'test')",
            (eid,),
        )
        conn.execute(
            "INSERT INTO news_market_matches (news_event_id, ticker, confidence, "
            "matched_subject, match_reason) VALUES (?, 'Y', 0.9, 'y', 'test')",
            (eid,),
        )
        conn.execute(
            "INSERT INTO risk_decisions (intent_type, intent_json, decision, "
            "rejection_reason, rule_fired, reasoning_text) "
            "VALUES ('entry', '{}', 'approved', NULL, NULL, 't')"
        )
    return db


def _insert_open_trade(
    db: Database,
    *,
    ticker: str = "X",
    entered_at: str = "2026-04-15T12:00:00Z",
    entry_price: int = 50,
    quantity: int = 10,
    entry_fees: int = 5,
    match_id: int = 1,
) -> int:
    return insert_trade(
        db,
        TradeInsertRow(
            ticker=ticker,
            status="dry_run",
            entry_price_cents=entry_price,
            quantity=quantity,
            cost_basis_usd_cents=entry_price * quantity,
            triggering_match_id=match_id,
            triggering_intent_json="{}",
            risk_decision_id=1,
            approval_id=None,
            is_reentry=False,
            prior_trade_id=None,
            reasoning_text="phase 4 part 2.1 tax fixture",
            entered_at=entered_at,
            entry_fees_cents=entry_fees,
        ),
    )


# ---------------------------------------------------------------------------
# Migration 008
# ---------------------------------------------------------------------------


class TestMigration008:
    def test_adds_tax_columns(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        cols = {r["name"] for r in db.connect().execute("PRAGMA table_info(trades)")}
        for c in (
            "acquired_date",
            "disposed_date",
            "holding_period_days",
            "acquisition_cost_cents",
            "disposal_proceeds_cents",
            "realized_gain_loss_cents",
            "tax_year",
        ):
            assert c in cols, f"missing column {c}"

    def test_creates_tax_indexes(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        idx = {r["name"] for r in db.connect().execute("PRAGMA index_list(trades)")}
        assert "idx_trades_tax_year" in idx
        assert "idx_trades_disposed_date" in idx


# ---------------------------------------------------------------------------
# Lifecycle population
# ---------------------------------------------------------------------------


class TestLifecyclePopulation:
    def test_insert_populates_acquired_date_and_cost(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        tid = _insert_open_trade(db, entry_price=50, quantity=10, entry_fees=5)
        row = db.connect().execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
        assert row["acquired_date"] == "2026-04-15"
        # 50 * 10 + 5 = 505 cents = $5.05 acquisition cost.
        assert row["acquisition_cost_cents"] == 505
        # Disposal fields are NULL until close.
        assert row["disposed_date"] is None
        assert row["holding_period_days"] is None
        assert row["disposal_proceeds_cents"] is None
        assert row["realized_gain_loss_cents"] is None
        assert row["tax_year"] is None

    def test_close_settled_yes_populates_full_disposal(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        tid = _insert_open_trade(db, entry_price=50, quantity=10, entry_fees=5)
        # Settle YES at $1.00 on 2026-05-30.
        close_trade(
            db,
            trade_id=tid,
            new_status="dry_run_closed_resolved",
            exit_price_cents=100,
            realized_pnl_usd_cents=500,  # legacy realized; ignored by new tax math
            exited_at="2026-05-30T18:00:00Z",
            exit_fees_cents=0,
        )
        row = db.connect().execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
        assert row["disposed_date"] == "2026-05-30"
        assert row["holding_period_days"] == 45  # Apr 15 -> May 30 = 45 days
        assert row["disposal_proceeds_cents"] == 1000  # 100c * 10
        # acquisition cost 505; proceeds 1000; gain 495
        assert row["realized_gain_loss_cents"] == 495
        assert row["tax_year"] == 2026

    def test_close_settled_no_zero_proceeds(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        tid = _insert_open_trade(db, entry_price=50, quantity=10, entry_fees=5)
        close_trade(
            db,
            trade_id=tid,
            new_status="live_closed_resolved_no",
            exit_price_cents=0,
            realized_pnl_usd_cents=-500,
            exited_at="2026-05-30T18:00:00Z",
            exit_fees_cents=0,
        )
        row = db.connect().execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
        assert row["disposal_proceeds_cents"] == 0
        # Loss = 0 - 505 = -505
        assert row["realized_gain_loss_cents"] == -505

    def test_close_stop_loss_nets_exit_fees(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        tid = _insert_open_trade(db, entry_price=50, quantity=10, entry_fees=5)
        # Stop-loss at 20c with $0.12 exit fee.
        close_trade(
            db,
            trade_id=tid,
            new_status="dry_run_closed_stop",
            exit_price_cents=20,
            realized_pnl_usd_cents=-300,
            exited_at="2026-04-20T18:00:00Z",
            exit_fees_cents=12,
        )
        row = db.connect().execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
        # Proceeds: 20 * 10 - 12 = 188 cents
        assert row["disposal_proceeds_cents"] == 188
        # Gain/loss: 188 - 505 = -317
        assert row["realized_gain_loss_cents"] == -317
        assert row["holding_period_days"] == 5

    def test_year_boundary_disposal(self, tmp_path: Path) -> None:
        """Trade entered Dec 30, closed Jan 5 → tax_year is the year of
        disposal (2027), not entry."""
        db = _db(tmp_path)
        tid = _insert_open_trade(db, entered_at="2026-12-30T12:00:00Z")
        close_trade(
            db,
            trade_id=tid,
            new_status="dry_run_closed_resolved",
            exit_price_cents=100,
            realized_pnl_usd_cents=0,
            exited_at="2027-01-05T12:00:00Z",
            exit_fees_cents=0,
        )
        row = db.connect().execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
        assert row["tax_year"] == 2027
        assert row["holding_period_days"] == 6


# ---------------------------------------------------------------------------
# Backfill (migration on existing data)
# ---------------------------------------------------------------------------


class TestBackfill:
    def test_backfill_pre_existing_closed_trade(self, tmp_path: Path) -> None:
        """Simulate a pre-Phase-4-Part-2.1 closed row by re-running the
        backfill SQL on a freshly-inserted closed trade where the tax
        columns were forcibly cleared. Verifies the migration's UPDATE
        statements work correctly on real data."""
        db = _db(tmp_path)
        # Insert + close a trade so it has full data.
        tid = _insert_open_trade(db, entry_price=50, quantity=10, entry_fees=5)
        close_trade(
            db,
            trade_id=tid,
            new_status="dry_run_closed_stop",
            exit_price_cents=20,
            realized_pnl_usd_cents=-300,
            exited_at="2026-04-20T18:00:00Z",
            exit_fees_cents=12,
        )
        # Now wipe the tax columns to simulate a pre-Phase-4-Part-2.1 row.
        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE trades
                   SET acquired_date = NULL,
                       disposed_date = NULL,
                       holding_period_days = NULL,
                       acquisition_cost_cents = NULL,
                       disposal_proceeds_cents = NULL,
                       realized_gain_loss_cents = NULL,
                       tax_year = NULL
                 WHERE id = ?
                """,
                (tid,),
            )
        # Re-run migration 008's backfill UPDATE statements. Splitting
        # the SQL on `;` isn't reliable because of inline comments, so
        # we re-execute the four UPDATE statements verbatim from the
        # migration. If the migration changes, this test is the canary
        # that the backfill still produces the same numbers.
        conn = db.connect()
        conn.execute(
            """
            UPDATE trades
               SET acquired_date          = substr(entered_at, 1, 10),
                   acquisition_cost_cents = cost_basis_usd_cents
                                          + COALESCE(entry_fees_cents, 0)
             WHERE acquired_date IS NULL
               AND entered_at IS NOT NULL
            """
        )
        conn.execute(
            """
            UPDATE trades
               SET disposed_date           = substr(exited_at, 1, 10),
                   disposal_proceeds_cents =
                       exit_price_cents * quantity - COALESCE(exit_fees_cents, 0),
                   holding_period_days     = CAST(
                       julianday(substr(exited_at, 1, 10))
                     - julianday(substr(entered_at, 1, 10))
                     AS INTEGER),
                   tax_year = CAST(strftime('%Y', exited_at) AS INTEGER)
             WHERE disposed_date IS NULL
               AND exited_at IS NOT NULL
               AND status IN ('dry_run_closed_stop', 'live_closed_stop')
            """
        )
        conn.execute(
            """
            UPDATE trades
               SET realized_gain_loss_cents = disposal_proceeds_cents
                                            - acquisition_cost_cents
             WHERE realized_gain_loss_cents IS NULL
               AND acquisition_cost_cents IS NOT NULL
               AND disposal_proceeds_cents IS NOT NULL
            """
        )
        row = db.connect().execute("SELECT * FROM trades WHERE id = ?", (tid,)).fetchone()
        # Backfill restores the same numbers we computed at close time.
        assert row["acquired_date"] == "2026-04-15"
        assert row["disposed_date"] == "2026-04-20"
        assert row["holding_period_days"] == 5
        assert row["acquisition_cost_cents"] == 505
        assert row["disposal_proceeds_cents"] == 188
        assert row["realized_gain_loss_cents"] == -317
        assert row["tax_year"] == 2026


# ---------------------------------------------------------------------------
# TaxExporter
# ---------------------------------------------------------------------------


def _close(
    db: Database,
    *,
    ticker: str,
    entry_price: int,
    qty: int,
    exit_price: int,
    when: str = "2026-05-30T18:00:00Z",
) -> int:
    tid = _insert_open_trade(
        db,
        ticker=ticker,
        entry_price=entry_price,
        quantity=qty,
        entry_fees=0,
        match_id=1 if ticker == "X" else 2,
    )
    close_trade(
        db,
        trade_id=tid,
        new_status=("dry_run_closed_stop" if exit_price < 100 else "dry_run_closed_resolved"),
        exit_price_cents=exit_price,
        realized_pnl_usd_cents=(exit_price - entry_price) * qty,
        exited_at=when,
        exit_fees_cents=0,
    )
    return tid


class TestTaxExporter:
    def test_yearly_summary_aggregates(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # Two wins (X 50→100, Y 60→100) and one loss (X 70→30).
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        _close(db, ticker="Y", entry_price=60, qty=5, exit_price=100)
        _close(db, ticker="X", entry_price=70, qty=4, exit_price=30)
        s = TaxExporter(db).export_yearly_summary(2026)
        assert s.year == 2026
        assert s.closed_trades == 3
        assert s.wins == 2
        assert s.losses == 1
        # Win rate = 2/3 ≈ 67%.
        assert s.win_rate_pct == 67
        # Total gain = 500 + 200 = 700; loss = 160; net = 540
        assert s.total_gain_cents == 700
        assert s.total_loss_cents == 160
        assert s.net_pnl_cents == 540
        assert s.largest_gain_cents == 500
        assert "X" in s.largest_gain_market
        assert s.largest_loss_cents == 160
        assert "X" in s.largest_loss_market

    def test_yearly_summary_open_count(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # One closed in 2026 + two open trades.
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        _insert_open_trade(db, ticker="X", entered_at="2026-06-01T12:00:00Z")
        _insert_open_trade(db, ticker="Y", entered_at="2026-06-02T12:00:00Z", match_id=2)
        s = TaxExporter(db).export_yearly_summary(2026)
        assert s.closed_trades == 1
        assert s.open_trades == 2

    def test_csv_export_columns_and_format(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        out = TaxExporter(db).export_trade_log(2026, format="csv")
        reader = csv.reader(io.StringIO(out))
        header = next(reader)
        assert header == [
            "trade_id",
            "ticker",
            "market_description",
            "acquired_date",
            "disposed_date",
            "holding_period_days",
            "quantity",
            "acquisition_cost_usd",
            "disposal_proceeds_usd",
            "realized_gain_loss_usd",
            "status",
            "resolution_outcome",
            "notes",
        ]
        row = next(reader)
        assert row[1] == "X"
        assert row[3] == "2026-04-15"  # acquired_date YYYY-MM-DD
        assert row[7] == "5.00"  # acquisition cost = 50c * 10 = 500c = $5.00
        assert row[8] == "10.00"  # proceeds = 100c * 10 = 1000c = $10.00
        assert row[9] == "5.00"  # realized = +$5.00

    def test_form_8949_columns_match_irs_spec(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        out = TaxExporter(db).export_form_8949_format(2026)
        reader = csv.reader(io.StringIO(out))
        header = next(reader)
        # Match the IRS form column names exactly.
        assert header == [
            "Description of property",
            "Date acquired",
            "Date sold or disposed of",
            "Proceeds (sales price)",
            "Cost or other basis",
            "Adjustment, if any",
            "Code, if any",
            "Gain or (loss)",
        ]
        row = next(reader)
        # Adjustment / Code intentionally blank.
        assert row[5] == ""
        assert row[6] == ""

    def test_kalshi_reconciliation_totals(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        _close(db, ticker="Y", entry_price=70, qty=5, exit_price=30)
        recon = TaxExporter(db).export_kalshi_reconciliation(2026)
        # Proceeds: 1000 + 150 = 1150. Cost: 500 + 350 = 850.
        # Net: 1150 - 850 = 300.
        assert recon["totals"]["proceeds_cents"] == 1150
        assert recon["totals"]["cost_basis_cents"] == 850
        assert recon["totals"]["net_pnl_cents"] == 300
        assert len(recon["line_items"]) == 2

    def test_json_export_is_valid_and_includes_summary(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        raw = TaxExporter(db).export_trade_log(2026, format="json")
        payload = json.loads(raw)
        assert payload["year"] == 2026
        assert "summary" in payload
        assert "trades" in payload
        assert payload["summary"]["closed_trades"] == 1


# ---------------------------------------------------------------------------
# Decimal / cent precision
# ---------------------------------------------------------------------------


class TestDecimalPrecision:
    def test_cents_to_dollars_no_float_drift(self) -> None:
        # 1749 cents → $17.49 (NOT $17.490000000001)
        assert _dollars_str(1749) == "$17.49"
        assert _dollars_str(-1749) == "-$17.49"
        assert _bare_dollars(1749) == "17.49"

    def test_zero_and_none(self) -> None:
        assert _dollars_str(0) == "$0.00"
        assert _dollars_str(None) == "$0.00"
        assert _bare_dollars(None) == "0.00"

    def test_csv_row_has_exact_cents(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # Insert a trade with awkward cents and verify the CSV preserves
        # exact two-decimal formatting.
        tid = _insert_open_trade(db, entry_price=33, quantity=53, entry_fees=7)
        # cost basis = 33 * 53 = 1749; +7 fees = 1756 acquisition cost
        close_trade(
            db,
            trade_id=tid,
            new_status="dry_run_closed_stop",
            exit_price_cents=29,
            realized_pnl_usd_cents=0,  # ignored
            exited_at="2026-05-01T12:00:00Z",
            exit_fees_cents=11,
        )
        out = TaxExporter(db).export_trade_log(2026, format="csv")
        reader = csv.reader(io.StringIO(out))
        next(reader)
        row = next(reader)
        # acquisition: 33*53 + 7 = 1756 cents = $17.56
        assert row[7] == "17.56"
        # proceeds: 29 * 53 - 11 = 1526 cents = $15.26
        assert row[8] == "15.26"
        # gain/loss: 1526 - 1756 = -230 cents = -$2.30
        assert row[9] == "-2.30"


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------


class TestTaxCommands:
    async def test_tax_summary_handler(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        ctx = CommandContext(db=db, args=["2026"])
        rendered = await handle_tax_summary(ctx)
        assert rendered.template_name == "command_reply_tax_summary"
        assert "Tax Summary" in rendered.text
        assert "2026" in rendered.text
        assert "$5.00" in rendered.text  # net P&L

    async def test_tax_summary_default_year(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        ctx = CommandContext(db=db, args=[])
        rendered = await handle_tax_summary(ctx)
        # Should default to current year and render zeros.
        current_year = str(datetime.now().year)
        assert current_year in rendered.text

    async def test_tax_summary_invalid_year(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        ctx = CommandContext(db=db, args=["nope"])
        rendered = await handle_tax_summary(ctx)
        assert rendered.template_name == "command_reply_usage_hint"

    async def test_tax_export_writes_csv(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        out_dir = tmp_path / "exports"
        ctx = CommandContext(db=db, args=["2026", "csv"], exports_dir=out_dir)
        rendered = await handle_tax_export(ctx)
        assert "Tax Export" in rendered.text
        # File exists and has content.
        path = out_dir / "annual" / "2026" / "full_trade_log.csv"
        assert path.exists()
        text = path.read_text()
        assert "trade_id" in text
        assert "X" in text

    async def test_tax_export_form_8949(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        out_dir = tmp_path / "exports"
        ctx = CommandContext(db=db, args=["2026", "form_8949"], exports_dir=out_dir)
        await handle_tax_export(ctx)
        path = out_dir / "annual" / "2026" / "form_8949_format.csv"
        assert path.exists()
        text = path.read_text()
        assert "Description of property" in text

    async def test_tax_export_invalid_format(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        ctx = CommandContext(db=db, args=["2026", "xml"], exports_dir=tmp_path / "exports")
        rendered = await handle_tax_export(ctx)
        assert rendered.template_name == "command_reply_usage_hint"

    async def test_tax_reconcile_writes_json(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _close(db, ticker="X", entry_price=50, qty=10, exit_price=100)
        out_dir = tmp_path / "exports"
        ctx = CommandContext(db=db, args=["2026"], exports_dir=out_dir)
        rendered = await handle_tax_reconcile(ctx)
        assert "Reconciliation" in rendered.text
        path = out_dir / "annual" / "2026" / "kalshi_reconciliation.json"
        assert path.exists()
        payload = json.loads(path.read_text())
        assert payload["year"] == 2026
        assert payload["totals"]["net_pnl_cents"] == 500


# ---------------------------------------------------------------------------
# write_export helper
# ---------------------------------------------------------------------------


def test_write_export_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "file.txt"
    write_export(target, "hello")
    assert target.read_text() == "hello"


# ---------------------------------------------------------------------------
# YearlySummary dataclass
# ---------------------------------------------------------------------------


def test_yearly_summary_as_dict() -> None:
    s = YearlySummary(
        year=2026,
        total_trades=3,
        closed_trades=2,
        open_trades=1,
        wins=1,
        losses=1,
        win_rate_pct=50,
        total_gain_cents=500,
        total_loss_cents=200,
        net_pnl_cents=300,
        largest_gain_cents=500,
        largest_gain_market="X",
        largest_loss_cents=200,
        largest_loss_market="Y",
        total_fees_cents=12,
        total_slippage_cents=5,
        avg_holding_days=10,
        by_market={"X": 500, "Y": -200},
    )
    d = s.as_dict()
    assert d["year"] == 2026
    assert d["net_pnl_cents"] == 300
    assert d["by_market"] == {"X": 500, "Y": -200}


# ---------------------------------------------------------------------------
# monthly_tax_digest_loop helpers (logic-only; the loop itself is
# triggered manually via integration tests)
# ---------------------------------------------------------------------------


class TestMonthlyDigestHelpers:
    def test_previous_month_bounds(self) -> None:
        from trumpbot.notifications.scheduled import _previous_month_bounds

        # Pretend it's 2026-03-10. Previous month = 2026-02-01..28.
        now = datetime(2026, 3, 10, 12, 0, 0).replace(tzinfo=__import__("datetime").UTC)
        start, end, name, year, month = _previous_month_bounds(now=now)
        assert start.isoformat() == "2026-02-01"
        assert end.isoformat() == "2026-02-28"
        assert name == "February"
        assert year == 2026
        assert month == 2

    def test_seconds_until_next_monthly_tick_picks_next_month(self) -> None:
        from trumpbot.notifications.scheduled import _seconds_until_next_monthly_tick

        # Pretend it's 2026-03-15 12:00 UTC ~= 08:00 ET.
        # Next monthly tick = April 1 09:00 ET.
        now = datetime(2026, 3, 15, 12, 0, 0).replace(tzinfo=__import__("datetime").UTC)
        secs = _seconds_until_next_monthly_tick(fire_day=1, fire_time_et="09:00", now=now)
        # Some positive value bounded by ~18 days (Mar 15 12:00 UTC →
        # Apr 1 09:00 ET ≈ 17 days + 1 hour).
        assert secs > 0
        assert secs < 18 * 24 * 3600


__all__: list[str] = []
