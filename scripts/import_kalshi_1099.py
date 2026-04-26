"""Kalshi 1099-B reconciliation — compare Kalshi's PDF against the
bot's recorded trades.

Phase 4 Part 2.1.

Usage::

    uv run python -m scripts.import_kalshi_1099 \\
        --file /path/to/Kalshi_1099-B_2026.pdf \\
        --year 2026

Tries to extract proceeds + cost basis totals from the PDF using
:mod:`pypdf` (already a transitive dependency through pydantic-related
tooling, but installed lazily — if the import fails the script falls
back to a "manual paste" mode and asks the operator to type the
numbers from Kalshi's web summary).

Compares Kalshi's totals against the bot's records and writes
``data/exports/annual/<year>/1099_reconciliation.txt`` with a human-
readable diff. Exits non-zero on discrepancies; the operator's job
is then to investigate before filing.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trumpbot.config import load_config  # noqa: E402
from trumpbot.db.connection import Database  # noqa: E402
from trumpbot.exports.tax_exports import TaxExporter, write_export  # noqa: E402
from trumpbot.platform_paths import current_platform_paths, resolve_path  # noqa: E402


@dataclass(frozen=True)
class Kalshi1099Totals:
    proceeds_cents: int
    cost_basis_cents: int
    net_pnl_cents: int


def _dollars_to_cents(s: str) -> int:
    """Parse a string like ``$1,234.56`` or ``-1234.56`` into integer cents.

    Defensive: tolerates dollar signs, commas, parens (accounting
    notation for negatives), and surrounding whitespace.
    """
    raw = s.strip().replace("$", "").replace(",", "").replace(" ", "")
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1]
    if raw.startswith("-"):
        negative = True
        raw = raw[1:]
    amount = Decimal(raw)
    cents = int(amount * 100)
    return -cents if negative else cents


def _parse_pdf_totals(pdf_path: Path) -> Kalshi1099Totals | None:
    """Extract Kalshi 1099-B totals from a PDF. Returns ``None`` when
    parsing fails (the script falls back to manual paste mode)."""
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        print(
            "  pypdf not installed — falling back to manual paste mode.",
            file=sys.stderr,
        )
        return None
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        print(f"  pypdf failed to read {pdf_path}: {exc!r}", file=sys.stderr)
        return None
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    # Kalshi's 1099-B section labels (best-guess, defensive). Operator
    # can refine this after the first real form arrives.
    proceeds_match = re.search(r"(?i)total\s+proceeds[^\d-]*([\$\(\)\-,\d\.]+)", text)
    cost_match = re.search(r"(?i)total\s+(?:cost\s+basis|basis)[^\d-]*([\$\(\)\-,\d\.]+)", text)
    if not proceeds_match or not cost_match:
        print("  could not locate proceeds / cost basis labels in PDF.", file=sys.stderr)
        # Dump raw text so the operator can investigate.
        dump_path = pdf_path.with_suffix(".raw.txt")
        dump_path.write_text(text, encoding="utf-8")
        print(f"  raw extracted text dumped to {dump_path}", file=sys.stderr)
        return None
    proceeds = _dollars_to_cents(proceeds_match.group(1))
    cost = _dollars_to_cents(cost_match.group(1))
    return Kalshi1099Totals(
        proceeds_cents=proceeds,
        cost_basis_cents=cost,
        net_pnl_cents=proceeds - cost,
    )


def _manual_paste_totals() -> Kalshi1099Totals:
    """Fallback when PDF parsing fails. Asks the operator to paste the
    three numbers from Kalshi's web 1099-B summary."""
    print()
    print("Manual paste mode. Find these on Kalshi's 1099-B summary:")
    proceeds_str = input("Total proceeds (e.g. 1234.56 or $1,234.56): ").strip()
    cost_str = input("Total cost basis: ").strip()
    proceeds = _dollars_to_cents(proceeds_str)
    cost = _dollars_to_cents(cost_str)
    return Kalshi1099Totals(
        proceeds_cents=proceeds,
        cost_basis_cents=cost,
        net_pnl_cents=proceeds - cost,
    )


def _format_diff(label: str, kalshi_cents: int, bot_cents: int) -> str:
    """Format a labeled diff line for the report. Pads for visual
    alignment across the three lines."""
    diff = bot_cents - kalshi_cents
    sign = "+" if diff > 0 else ""
    return (
        f"  {label:18s}  Kalshi: ${kalshi_cents/100:>10,.2f}  "
        f"Bot: ${bot_cents/100:>10,.2f}  Diff: {sign}${diff/100:.2f}"
    )


def _amain(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config).expanduser())
    paths = current_platform_paths()
    db_path = resolve_path(cfg.database.path, paths.database_path)
    db = Database(db_path)
    db.connect()
    exporter = TaxExporter(db)
    pdf_path = Path(args.file).expanduser()

    kalshi: Kalshi1099Totals
    if not pdf_path.is_file() and args.allow_manual_paste:
        print(f"  PDF not found at {pdf_path}; entering manual paste mode.")
        kalshi = _manual_paste_totals()
    else:
        parsed = _parse_pdf_totals(pdf_path) if pdf_path.is_file() else None
        if parsed is None:
            if args.allow_manual_paste:
                kalshi = _manual_paste_totals()
            else:
                print(
                    "  PDF parse failed and --allow-manual-paste not set. "
                    "Re-run with --allow-manual-paste to type the totals.",
                    file=sys.stderr,
                )
                return 2
        else:
            kalshi = parsed

    bot_recon = exporter.export_kalshi_reconciliation(args.year)
    bot_totals = bot_recon["totals"]

    out_dir = (
        Path(args.out_dir).expanduser()
        if args.out_dir
        else db_path.parent / "exports" / "annual" / str(args.year)
    )
    report_path = out_dir / "1099_reconciliation.txt"

    discrepancies: list[str] = []
    lines: list[str] = []
    lines.append(f"Kalshi 1099-B reconciliation — tax year {args.year}\n")
    lines.append(_format_diff("Proceeds", kalshi.proceeds_cents, int(bot_totals["proceeds_cents"])))
    lines.append(
        _format_diff("Cost basis", kalshi.cost_basis_cents, int(bot_totals["cost_basis_cents"]))
    )
    lines.append(_format_diff("Net P&L", kalshi.net_pnl_cents, int(bot_totals["net_pnl_cents"])))
    lines.append("")
    lines.append(f"Bot trade count: {len(bot_recon['line_items'])}")

    if abs(int(bot_totals["proceeds_cents"]) - kalshi.proceeds_cents) > 100:
        discrepancies.append(
            f"  - Proceeds differ by more than $1.00 "
            f"(${(int(bot_totals['proceeds_cents']) - kalshi.proceeds_cents)/100:+.2f})"
        )
    if abs(int(bot_totals["cost_basis_cents"]) - kalshi.cost_basis_cents) > 100:
        discrepancies.append(
            f"  - Cost basis differs by more than $1.00 "
            f"(${(int(bot_totals['cost_basis_cents']) - kalshi.cost_basis_cents)/100:+.2f})"
        )

    if discrepancies:
        lines.append("\n⚠️  Discrepancies detected:")
        lines.extend(discrepancies)
        lines.append("\nNext step: investigate trades that may not appear on Kalshi's 1099-B")
        lines.append("(e.g. void-resolved markets, fee accounting differences). Compare the")
        lines.append("per-trade detail in kalshi_reconciliation.json against Kalshi's PDF.")
    else:
        lines.append("\n✅  Totals match within $1.00 tolerance.")

    write_export(report_path, "\n".join(lines) + "\n")
    print(f"\nReconciliation report: {report_path}")
    for line in lines:
        print(line)

    db.close()
    return 1 if discrepancies else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="import_kalshi_1099")
    parser.add_argument(
        "--config",
        default=str(Path("~/.config/trumpbot/config.yaml").expanduser()),
    )
    parser.add_argument("--file", required=True, help="Path to Kalshi 1099-B PDF")
    parser.add_argument("--year", type=int, required=True, help="Tax year on the form")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Override output directory (default: <db_dir>/exports/annual/<year>/)",
    )
    parser.add_argument(
        "--allow-manual-paste",
        action="store_true",
        help="Fall back to typing totals if PDF parsing fails",
    )
    args = parser.parse_args()
    return _amain(args)


if __name__ == "__main__":
    sys.exit(main())
