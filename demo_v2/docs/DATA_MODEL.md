# DEMO V2 data model

All tables use the `demo_v2_` namespace in the dedicated database. Versioned,
checksummed migrations run explicitly in a transaction, with foreign keys, WAL,
busy timeout and lifecycle indexes. No migration touches a LIVE file.

- Definitions: all 21 asset/timeframe keys, provider family, enabled flag, expected
  duration, provenance. A catalog definition does not assert provider availability.
- Instances: immutable verified external/condition/token identities and metadata
  start/end; independent discovery and official-resolution provenance.
- Rules: mutable lifecycle/current-version pointer; immutable version JSON holds
  Decimal prices/shares, selected markets, schedules/windows and movement filters.
  Versions cannot be updated or deleted except during an explicit factory reset.
- Entry counts: one counter per rule+instance, shared between sides. Side runtime
  stores last ask, version, generation, ordinal and observation time. No open-position
  uniqueness constraint exists. Connection generations are durable across restarts.
- Positions: independent IDs, immutable entry-version reference, entry/exit quote
  provenance, gross/net shares, cash debit, share/cash fee evidence, TP/SL snapshots,
  proceeds and closed net P&L classification. Archived rules remain attributable.
- Ledger: immutable signed cash movements, unique position/action references.
  Entry debits cash; exit/settlement credits cash. Global/per-rule wallet is an
  exact Decimal sum starting at zero without a balance floor.
- Underlying/fee evidence/incidents: research observations, source IDs, timestamps,
  context, readiness and provenance. Raw telemetry is not decision input.

All financial SQL columns use canonical decimal TEXT. No SQL SUM/REAL arithmetic
is used for money. If a buy fee is deducted in shares, cash debit is gross notional;
buy fee USD-equivalent is recorded, not subtracted from cash a second time.

Entry, count, side runtime, version reference and ledger insertion share one
`BEGIN IMMEDIATE` transaction. Exit and official resolution atomically update
positions and ledger; repeated idempotency keys/settlements never add cash twice.
Exceptions roll back all effects. A schema/version mismatch fails closed.

Reset Trading Data atomically deletes simulation positions (including open ones),
runtime, ledger and trade-linked observations without inventing closing trades;
rules and versions remain. Factory reset additionally removes rules/versions.
System catalog and verified market metadata remain. Both resets advance the
connection generation; the runtime must discard queued observations on reset.
