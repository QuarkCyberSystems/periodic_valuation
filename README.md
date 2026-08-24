# Periodic Valuation

Periodic **Moving Average** and **Standard Cost** valuation kernels for ERPNext,
built on an **immutable stock ledger**: posted events are never modified or
deleted. Every correction is a new, dated, linked event.

Both kernels are built. The Periodic Moving Average (MAP) kernel is in user
acceptance testing; the Periodic Standard Cost kernel (monthly and
year-to-date variance settlement) plugs into the same foundation and is
under test.

---

## Why this app exists

ERPNext's native valuation engine repairs history. A backdated entry, a landed
cost voucher, or a purchase invoice at a different rate triggers **Repost Item
Valuation**: future Stock Ledger Entries are recomputed and rewritten in place,
and GL entries are deleted and recreated to match. That design has real costs:

- **Auditability**: the ledger you see today is not the ledger that existed
  yesterday. Regulated and audit-heavy environments (IFRS, statutory audits)
  need every posted figure to stay exactly as posted.
- **GL/stock drift**: repost chains can desynchronise inventory value from the
  GL, and reconciliation differences are hard to trace after rows have been
  rewritten.
- **Unbounded propagation**: one backdated document can silently re-value
  months of downstream transactions.

The periodic-settlement model used by the major enterprise ERPs takes the
opposite stance: correct the past explicitly, then value the future. Prices
are period-based; backdated postings update their own period and flow forward
through opening balances; price gaps against negative stock post to a
dedicated difference account (PRD); nothing is ever recomputed retroactively.

This app implements that model for ERPNext:

1. **Immutable events**: append-only movement and valuation logs; corrections
   are reversing events linked to their originals.
2. **Period-based valuation**: posting is allowed only into the current open
   period and (until settled) the previous one. Older corrections post forward
   with an `original_period` reference.
3. **Bounded propagation**: a prior-period posting updates that period and the
   current opening (via a carryover bucket). Nothing else moves.
4. **GL derived from events, never repaired**: every stock GL line carries a
   `valuation_event_id`; the sum of tagged GL always equals the movement table,
   enforced by a hard reconciliation gate at period close.

## How it works

### Routing, not replacement

Nothing changes for existing items. Items opt in per item via
`valuation_method = "Periodic Moving Average"` (or, later, `"Periodic Standard Cost"`).
There is deliberately **no global toggle**.

```
stock voucher submit
        │
StockController.make_sl_entries          (companion fork branch)
        │
        ├── item has a registered kernel? ──► periodic posting kernel (this app)
        │                                       Validate period → Lock balances
        │                                       → Compute → Write, atomically:
        │                                       SME + IVE + period balance
        │                                       + SLE-compatible row + GL
        │
        └── everything else ──────────────► core stock_ledger.py, unchanged
```

The kernel still writes **SLE-compatible rows** (flagged
`posted_via_valuation_kernel`) with correct quantities and values, so Bin, stock
reports and reconciliations keep working, but the core engine never recomputes
them, and no Repost Item Valuation is ever created for routed items.

### The three data layers

| Layer | DocType | Purpose |
|---|---|---|
| Movement | **Stock Movement Event** | Immutable physical quantity log (receipt, issue, transfer, count, cancellation) |
| Valuation | **Inventory Valuation Event** | Immutable value-change log; the source of truth behind every stock GL line |
| Period state | **Inventory Period Balance** | Per item(-warehouse) period buckets: fixed opening, carryover, receipts, issues, adjustments, revaluations, PRD, closing, MAP, negative-stock state |

Control layer: **Inventory Period** (five-state machine, one OPEN period per
company) and **Inventory Period Close** (continuity, event-to-GL identity,
orphan checks, and a strict GL-vs-movement-table reconciliation gate: default
tolerance 0.00, manual resolution only, no automatic write-offs).
An optional **Inventory Period Balance Snapshot** audit log records
Before/After images of every balance mutation.

### MAP rules implemented

- **Receipt** blends: `MAP = (value + qty * cost) / (qty_on_hand + qty)`.
- **Issues** always at the current period MAP, never a user-entered value.
- **Late costs** (landed cost vouchers, purchase-invoice price differences)
  split by **stock coverage** `min(1, on_hand / charged_qty)`, where the
  charged quantity is the invoiced quantity for an invoice difference and the
  receipt row's quantity for a landed cost. The covered share enters inventory
  and moves MAP; the remainder posts to Price Difference. Coverage is measured
  as of the document's own posting period, and each document is judged on its
  own, so two invoices billing one receipt are assessed independently.
- **Foreign-currency invoice differences** (IFRS): the price component is
  measured at the *receipt* exchange rate and split by coverage; the FX
  movement goes to Exchange Gain/Loss and never touches inventory.
- **Negative stock** (configurable): MAP freezes; every receipt against
  negative stock posts `PRD = (price - frozen MAP) * qty` immediately; the
  receipt that crosses zero re-prices MAP to its own rate.
- **Backdated postings** update the prior open period and flow into the
  current period through the carryover bucket. Backdating into a period that
  closed negative additionally posts a system-generated cross-period absorb
  entry so GL and stock stay identical (both the current-positive and
  current-negative sub-cases).
- **Returns with reference** are valued at the original document's unit cost
  (excluding landed-cost shares) and re-blend MAP; returns without reference
  post at current MAP.
- **Cancellation** is never `docstatus 2`. A *Create Cancellation* action posts
  a same-doctype reversal document that mirrors the original's GL on its own
  posting date; originals stay submitted; double reversals and cancellations
  into settled periods are blocked.
- **Quantity vs value separation**: **Stock Count** accepts quantities only
  and values them at period MAP; **Stock Revaluation** changes value only.
- **Zero-quantity cleanup** clears residual rounding value to a dedicated
  account. The MAP itself is retained at zero quantity, so an issue taken from
  an empty balance is still costed at the last known price.

## Installation & requirements

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/QuarkCyberSystems/periodic_valuation
bench --site <site> install-app periodic_valuation
```

- Requires the companion ERPNext fork branch carrying the (deliberately
  minimal, ~190 line) core integration: the valuation-method options, the
  kernel-registry dispatch, the `posted_via_valuation_kernel` guards, repost opt-out
  and stock-GL suppression for routed items. Every fork change is a no-op when
  this app is not installed.
- Per company, before the first posting: a **Periodic Moving Average Settings**
  record (difference/variance accounts, precision, tolerances, negative-stock
  policy, return-valuation policy) and an **OPEN Inventory Period**.
- For the invoice-difference flow: Buying Settings → *Maintain Same Rate* off,
  and an adequate over-billing allowance.

## Extending (Standard Cost, or your own kernel)

Kernels register through hooks, with no further core edits:

```python
# hooks.py
valuation_kernels = {
    "Periodic Moving Average": "periodic_valuation.periodic_moving_average.kernel.post_via_pma_kernel",
}
valuation_incoming_rate = "periodic_valuation.shared.routing.get_incoming_rate"
valuation_landed_cost = "periodic_valuation.periodic_moving_average.landed_cost.handle_landed_cost"
```

A kernel receives the submitting controller and its SLE dicts, and owns the
complete atomic write (events, period balances, SLE-compatible rows, GL).

## Testing

The behavioral spec is executable. Every rule above is enforced by:

- a **pure-Python reference simulator**
  (`periodic_valuation/periodic_moving_average/reference/`) with conformance
  tests reproducing the approved sample-entries workbook to the penny. Run
  with `pytest periodic_valuation/periodic_moving_average/reference/`.
- end-to-end smoke suites that drive **real vouchers** on a site and roll
  back, plus replay suites that re-run the workbook scenarios end to end:

```bash
bench --site <site> execute periodic_valuation.tests.smoke_kernel.run    # core posting flow
bench --site <site> execute periodic_valuation.tests.smoke_matrix.run    # full approved test matrix
bench --site <site> execute periodic_valuation.tests.smoke_edges.run     # backdated/negative/FX/transfer edges
bench --site <site> execute periodic_valuation.tests.replay_map_v9.run   # workbook replay, Moving Average
bench --site <site> execute periodic_valuation.tests.replay_mtd_v205.run # workbook replay, Standard Cost MTD
bench --site <site> execute periodic_valuation.tests.replay_ytd_v203.run # workbook replay, Standard Cost YTD
```

All suites assert the GL-inventory identity (sum of tagged GL = period-balance
closing value) after every scenario.

## Status

| Component | State |
|---|---|
| Shared foundation (events, periods, close gate, settings, routing) | Complete |
| Periodic Moving Average kernel, revaluation, count, cancellation, UI | Complete, in user acceptance testing |
| Periodic Standard Cost kernel (MTD/YTD settlement) | Complete, under test |

## Contributing

This app uses `pre-commit` (ruff, eslint, prettier, pyupgrade):

```bash
cd apps/periodic_valuation
pre-commit install
```

## License

GPL-3.0
