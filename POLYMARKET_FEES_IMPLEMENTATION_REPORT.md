# Polymarket Fees Implementation Report

Date: 2026-07-21

## Official Research Summary

Sources checked:

- Polymarket official trading fees documentation: https://docs.polymarket.com/trading/fees
- Polymarket official builder fees documentation: https://docs.polymarket.com/builders/fees
- Polymarket official changelog: https://docs.polymarket.com/changelog

Findings:

- Platform fees are per-market and are applied at match time, not when an order is merely submitted.
- Markets with active platform fees expose `feesEnabled=true`; official docs say market fee parameters should be queried with `getClobMarketInfo(conditionID)`.
- The platform fee formula is `fee = shares * feeRate * price * (1 - price)`.
- Makers are not charged platform fees; takers pay platform fees.
- Current documented Crypto taker fee rate is `0.07`.
- Sports taker fee changed on 2026-07-10 from `0.03` to `0.05`.
- Fees are rounded to 5 decimal places; values below the minimum rounded fee become zero.
- Builder fees are separate and additive. This demo change implements platform fees only and does not attach a builder code.

## Previous Logic

The demo previously calculated profitability from entry and exit prices only:

```text
shares = investment_usd / entry_price
gross_exit_value_usd = shares * exit_price
pnl_usd = gross_exit_value_usd - investment_usd
roi_percent = pnl_usd / investment_usd * 100
```

There was no stored fee snapshot, no Maker/Taker role, and dashboard/Excel profitability treated gross P&L as the headline result.

## New Demo Policy

The regular demo system now uses a conservative simulation policy:

- Entry liquidity role: `TAKER`
- Stop loss exit: `TAKER`
- Take profit exit: `TAKER`
- Event-resolution exit: `TAKER`
- Default BTC/Crypto platform fee rate: `0.07`
- Fee calculation source when no market fee snapshot exists: `SIMULATED_CRYPTO_DEFAULT`
- Fee calculation version: `polymarket-platform-fee-v2-2026-07-21`

If market data later provides a fee snapshot, the event can store it and deals can preserve the rate used at the time. Closed historical deals are not retroactively rewritten.

## Example: $1 Deal

Entry price `0.77`, exit price `0.90`, taker on both sides:

```text
investment_usd = 1.00
shares = 1 / 0.77 = 1.29870130
entry_fee = shares * 0.07 * 0.77 * 0.23 = 0.01610
exit_fee = shares * 0.07 * 0.90 * 0.10 = 0.00818
gross_pnl = shares * 0.90 - 1 = 0.16883117
total_fees = 0.02428
net_pnl = 0.14455117
```

## Schema Changes

Added event fee snapshot fields:

- `fees_enabled`
- `fee_rate`
- `fee_calculation_source`
- `fee_calculation_version`

Added deal financial snapshot fields:

- `investment_usd`
- `shares`
- `entry_gross_value_usd`
- `entry_liquidity_role`
- `entry_fee_rate`
- `entry_fee_usd`
- `exit_gross_value_usd`
- `exit_liquidity_role`
- `exit_fee_rate`
- `exit_fee_usd`
- `total_fees_usd`
- `gross_pnl_usd`
- `net_pnl_usd`
- `gross_roi_percent`
- `net_roi_percent`
- `fee_calculation_source`
- `fee_calculation_version`

All fields are added through `ensure_column`, so existing SQLite databases migrate in place.

## Dashboard And Excel

Dashboard headline profitability now uses `net_pnl_usd`. Gross P&L and fees are still shown separately.

Excel changes:

- `events` includes fee snapshot fields.
- `deals` includes fee, gross/net P&L, ROI, Maker/Taker, and fee source/version fields.
- New `fee_summary` sheet includes dashboard-aligned fee totals.

## Tests

Command run:

```bash
python -m unittest discover polymarket-collector\tests
```

Result:

```text
Ran 33 tests
OK
```

Coverage added:

- Taker entry and taker exit fee calculation.
- Maker entry and maker exit with zero platform fee.
- No-fee market behavior through `fee_rate=0`.
- Dashboard KPI updates from gross P&L to net P&L.
- Excel sheet expansion with `fee_summary`.
