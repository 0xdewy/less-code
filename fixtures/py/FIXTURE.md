# Python fixture: legacy warehouse inventory module

`inventory.py` — internal business code "maintained since 2015": SKU
validation, stock receiving/allocation, restock planning, cycle-count
reconciliation, and CSV report rendering for a warehouse ops team.
stdlib-only, deterministic, %-formatting era style.

## Measured

| check | command | result |
|---|---|---|
| tests | `uv run python -m pytest fixtures/py -q` | 97 passed (192 assert statements) |
| LOC | `uv run lc analyze fixtures/py` | lang=python, **525 code-LOC** (95 comment, 85 blank) |
| historical mutation audit (retired command) | previous measurement | **score 0.925** (37/40 killed) |

The only surviving mutants (py-site-242/248/255) sit inside the dead
legacy section — unkillable by design because the tests must not touch
dead symbols.

Note: `conftest.py` sets `sys.dont_write_bytecode = True`. CPython
validates `.pyc` by whole-second mtime + size; the audit rewrites the
module with same-size mutants faster than the clock ticks, so stale
bytecode otherwise gets tested instead of the mutant (produced false
survivors during fixture bring-up).

## Status 2026-08-31 — pristine baseline restored

`inventory.py` on disk is the ORIGINAL fixture again, byte-identical to
`6ef9a1a`. Between `8177df1` and this commit it was not: an in-place `lc reduce`
run had been committed over it, deleting the whole legacy section (five public
symbols) and dropping it to 500 canonical code-LOC. The C7 review flagged that
as defect D1 — a tool-modified baseline is not a baseline — and it is now
reverted. `lc reduce` grew a `--copy-to DIR` flag and an in-place warning
(review D10) so the same accident cannot recur.

Baseline as it stands, all re-measured on the restored file:

| check | command | result |
|---|---|---|
| tests | `uv run python -m pytest fixtures/py -q` | 97 passed |
| hidden | `uv run python -m pytest fixtures/py/tests_hidden -q` | 32 passed |
| LOC | `uv run lc analyze fixtures/py` | **537 canonical code-LOC** (525 raw) |
| historical mutation audit (retired command) | previous measurement | **score 0.925** (37/40) |

The "Measured" table above quotes 525 because it predates canonical-formatted
counting (B1); 525 raw and 537 canonical are the same file under two metrics,
and every C3 number is canonical. The three surviving mutants are the ones the
note describes, inside the dead legacy section.

C3 evidence off this baseline: `lc reduce <copy> --static-only` gives
537 -> 357 = **33.5 %** with both suites green
(`docs/evidence/py-static-357/`). The legacy section is again removed by the
static pass at reduce time — where it belongs — rather than pre-removed in the
fixture; the report now lists those five symbols under
`static_removed_symbols`.

The deterministic rule library (`less_code/rules.py`) additionally takes
`Product.is_low`, `Inventory.sorted_skus`, both `total_units` accumulation
loops and the pointless `try/except InventoryError: raise` in `receive`, and
guard-block outlining (`less_code/outline.py`) takes 107 code lines across four
helpers. Everything else in the list below is still present and unclaimed: the
pasted SKU/qty validators the LLM layer targets, `low_stock` / `tally_by_flag`
/ `busiest_area`, `total_value` and `valuate` (whose loop bodies read a temp
twice, so the sum rule deliberately refuses them), and the unreachable
`from_row` guard.

## Embedded reduction opportunities

Dead legacy code (static pass will remove; unreferenced anywhere,
including tests):
- `legacy_ledger_rows` — inventory.py:657, retired WMS-1 flat-file sync
- `old_flag_code` — inventory.py:674, 2015 spreadsheet-macro flag codes
- `LegacyReorderCalculator` — inventory.py:685, pre-2018 EOQ math

Copy-pasted near-duplicate blocks (merge into one `parse_sku` call /
shared validators):
- SKU shape validation pasted 6x: `parse_sku` :34, `Product.__init__`
  :131, `Inventory.receive` :208, `Inventory.allocate` :243,
  `OrderLine.__init__` :376, `OrderLine.from_row` :413
- qty guard block (`None`/int/bool/positive) pasted in receive, allocate,
  release, adjust, writeoff, `OrderLine.__init__`, `from_row`
- `if inventory is None: raise ValueError("inventory required")` pasted
  8x (:494, :524, :541, :571, :589, :617, :637, plus order-None :496/:556)
- `Order.total_units` :459 duplicates `Inventory.total_units` :346 shape

Verbose if/elif chains (dict lookup / bisect candidates):
- `priority_for_status` :77 — five-way status -> int chain
- `classify_stock` :60 — threshold ladder
- `discount_tier` :99 — quantity tier ladder
- both legacy flag ladders :660/:676

Manual loops replaceable by comprehensions / sum / sorted / max:
- `sorted_skus` :196 (append loop + `.sort()` -> `sorted()`)
- `total_units` :346 and `total_value` :354 (-> `sum()`)
- `low_stock` :362, `tally_by_flag` :569 (-> `collections.Counter`)
- `busiest_area` :584 (two hand-rolled loops -> `max()` over a dict)

Over-defensive leftovers:
- pointless `try/except InventoryError: raise` in `receive` :234
- unreachable `if sku_part is None` guard in `from_row` :422 (split()
  never returns None; kept "since the 2016 null-sheet incident")
- `Product.is_low` :163 — `if cond: return True / return False`

The test suite (`test_inventory.py`) pins exact outputs, orderings, and
error messages for every public entry point, including boundary cases
(zero/empty/negative, exact thresholds, qty=1, exact-fit allocations).
