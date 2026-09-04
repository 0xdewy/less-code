# JS fixture: legacy-reporting

Single-file ESM module (`reporting.js`) styled as an internal finance-team
utility maintained since 2016: money formatting/parsing, CSV import,
period filtering, aggregation, and text-table report rendering. Node stdlib
only, fully deterministic.

## Baseline (as built)

| Metric | Value |
|---|---|
| code-LOC (`uv run lc analyze fixtures/js`) | **571** (lang=javascript) |
| historical mutation audit (retired command) | **0.85** (34/40 killed) |
| tests (`node --test`) | **36 tests green**, 134 assertions (41 throw-paths) |

Remaining audit survivors: 4 mutations inside comment text (inert), 1 in the
dead-export zone (tests must not touch it), 1 equivalent mutant
(`hasStart && hasEnd` guarded by the earlier `hasStart !== hasEnd` check).

## Embedded reduction opportunities (line refs into reporting.js)

### Dead exports (2–3, never imported by tests)
- `formatPercent` — L605, orphaned by the 2019 dashboard rewrite
- `deepCloneRow` — L611, pre-`structuredClone` helper with no callers
- `pivotByRegion` — L630, abandoned 2017 offsite experiment

### Copy-pasted near-duplicate blocks (mergeable into helpers)
- `creditSummary` (L305) vs `debitSummary` (L317): identical shape, differ
  only in the `amount >= 0` / `amount < 0` predicate (the file even carries a
  TODO admitting it)
- `buildMonthlySummary` (L353) vs `buildQuarterlySummary` (L374) vs
  `buildRegionSummary` (L398): the same bucket-loop three times over
  (12 month / 4 quarter / 4 region keys), differing only in key derivation

### Manual for-loops replaceable by map/filter/reduce/find/some/every
- `isValidDate` L17, `filterByPeriod` L272, `sumColumn` L293,
  `aggregateByCategory` bucket-search loop L330 (TODO says "use .find"),
  `columnIsNumeric` L449 (trivially `.every`), `summaryTableRows` L533,
  `topExpenses` L541 (slice+loop = `.sort().slice()`)

### Verbose if/else chains replaceable by lookup tables / early returns
- `monthName` L35 (12-branch chain, FIXME in source), `quarterOfMonth` L52,
  `quarterLabel` L60, `statusForDaysLate` L417 (aging buckets)

### String-concat loops replaceable by join / repeat / template literals / Intl
- `formatMoney` thousands-separator loop L84 (hand-rolled; comment cites
  Intl inconsistencies — `Intl.NumberFormat` covers it today)
- `renderTable` L459 (per-cell `out = out + ...` accumulation)
- `expenseReport` L558 (title bar `'='` loop + `+=` report assembly)

## Files
- `reporting.js` — 658 lines / 571 code-LOC
- `reporting.test.js` — node:test + node:assert/strict; covers happy paths,
  boundaries (empty arrays, zero, negative, malformed input) and error paths
  (`assert.throws` with exact messages); does NOT test the dead exports
- `package.json` — `{"type":"module"}`, `npm test` = `node --test`

## Reproduce
```
cd fixtures/js && node --test                     # 36 pass
uv run lc analyze fixtures/js                     # javascript, code=571
# Historical mutation score before the audit command was retired: 0.85
```
