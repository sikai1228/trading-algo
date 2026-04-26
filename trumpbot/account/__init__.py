"""Phase 4 account module: bankroll syncing + reconciliation + settlement detection.

These three responsibilities all touch the same Kalshi REST surface
(``/portfolio/balance``, ``/portfolio/positions``, ``/portfolio/orders``,
``/portfolio/settlements``) so they share a module.

- :mod:`trumpbot.account.bankroll_sync` — periodic sync from
  ``KalshiClient.get_balance`` into a local cache the engine consults
  for sizing decisions. No more hardcoded ``starting_amount_usd`` once
  live trading is on.

- :mod:`trumpbot.account.reconcile` — startup reconciliation: cross-
  reference Kalshi positions with our local trades table, surface any
  drift to the user, and recover orphaned ``pending`` rows whose
  network response was lost.

- :mod:`trumpbot.account.settlement_detector` — every 5 minutes,
  poll ``GET /portfolio/settlements`` and close out any open live
  trades whose markets have resolved.
"""

from __future__ import annotations
