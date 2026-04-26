"""Phase 4 Part 2.1 — tax tracking and data export.

The bot captures tax-relevant data on every trade lifecycle (Phase 4
Part 2.1, migration 008). This package turns that captured data into
filing-ready exports without recomputing anything from raw rows.

Modules:

- :mod:`trumpbot.exports.tax_exports` — :class:`TaxExporter` with
  yearly summary, full trade log (CSV / JSON), IRS Form 8949 layout,
  and Kalshi 1099-B reconciliation report.
"""

from __future__ import annotations
