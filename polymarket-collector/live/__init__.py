"""Isolated LIVE trading support.

The package is intentionally fail-closed. It can expose read-only and mock
workflows, but real Polymarket submission remains blocked unless future work
adds credentials and passes every safety gate.
"""

