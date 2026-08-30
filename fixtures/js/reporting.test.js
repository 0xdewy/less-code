// Tests for reporting.js — pinned outputs from the close-process fixtures.
// The month-end batch job depends on these exact strings, so assertions are
// strict equality everywhere; do not "update expectations" without ops signoff.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  formatMoney,
  parseMoney,
  parseCsvLine,
  parseCsv,
  normalizeRow,
  validateRow,
  filterByPeriod,
  monthName,
  quarterOfMonth,
  quarterLabel,
  sumColumn,
  creditSummary,
  debitSummary,
  aggregateByCategory,
  buildMonthlySummary,
  buildQuarterlySummary,
  buildRegionSummary,
  statusForDaysLate,
  renderTable,
  topExpenses,
  expenseReport
} from './reporting.js';

const ROWS = [
  { date: '2016-03-14', category: 'travel', region: 'NA', amount: 250.5, description: 'client visit' },
  { date: '2016-03-14', category: 'software', region: 'EMEA', amount: -50, description: 'refund' },
  { date: '2016-07-04', category: 'travel', region: 'NA', amount: 1034.05, description: 'conference' }
];

test('formatMoney formats plain amounts with 2 decimals', () => {
  assert.equal(formatMoney(0), '$0.00');
  assert.equal(formatMoney(12.5), '$12.50');
  assert.equal(formatMoney(123), '$123.00');
  assert.equal(formatMoney(1234.5), '$1,234.50');
  assert.equal(formatMoney(12345), '$12,345.00');
  assert.equal(formatMoney(1234567.891), '$1,234,567.89');
});

test('formatMoney uses accounting parens for negatives', () => {
  assert.equal(formatMoney(-45), '$(45.00)');
  assert.equal(formatMoney(-0.5, { precision: 1 }), '$(0.5)');
  assert.equal(formatMoney(-1234567.89), '$(1,234,567.89)');
});

test('formatMoney honours precision and separator options', () => {
  assert.equal(formatMoney(1000000, { precision: 0 }), '$1,000,000');
  assert.equal(formatMoney(1234.56, { symbol: 'EUR ', thousands: '.', decimal: ',' }), 'EUR 1.234,56');
  assert.equal(formatMoney(7, { symbol: '' }), '7.00');
});

test('formatMoney rejects non-finite input', () => {
  assert.throws(() => formatMoney('5'), { message: 'formatMoney: amount must be a finite number' });
  assert.throws(() => formatMoney(NaN), TypeError);
  assert.throws(() => formatMoney(Infinity), TypeError);
  assert.throws(() => formatMoney(1, { precision: -1 }), RangeError);
  assert.throws(() => formatMoney(1, { precision: '2' }),
    { message: 'formatMoney: precision must be a number' });
});

test('parseMoney parses what formatMoney emits', () => {
  assert.equal(parseMoney('$1,234.56'), 1234.56);
  assert.equal(parseMoney('$(45.00)'), -45);
  assert.equal(parseMoney('(45.00)'), -45);
  assert.equal(parseMoney('  $12.5 '), 12.5);
  assert.equal(parseMoney('-3.25'), -3.25);
  assert.equal(parseMoney('$0'), 0);
  assert.equal(parseMoney(formatMoney(-1234.5)), -1234.5);
});

test('parseMoney rejects malformed strings', () => {
  for (const bad of ['abc', '', '$', '1.2.3', '($5', '-']) {
    assert.throws(() => parseMoney(bad), { message: 'parseMoney: cannot parse "' + bad + '"' });
  }
  assert.throws(() => parseMoney(42), TypeError);
});

test('parseCsvLine splits plain and quoted fields', () => {
  assert.deepEqual(parseCsvLine('a,b,c'), ['a', 'b', 'c']);
  assert.deepEqual(parseCsvLine('a,"b,c",d'), ['a', 'b,c', 'd']);
  assert.deepEqual(parseCsvLine('"x""y",z'), ['x"y', 'z']);
  assert.deepEqual(parseCsvLine('" a ",b'), [' a ', 'b']);
});

test('parseCsvLine handles empty and trailing fields', () => {
  assert.deepEqual(parseCsvLine(''), ['']);
  assert.deepEqual(parseCsvLine('a,'), ['a', '']);
  assert.deepEqual(parseCsvLine(',b'), ['', 'b']);
});

test('parseCsvLine rejects broken input', () => {
  assert.throws(() => parseCsvLine('a,"bc'), { message: 'parseCsvLine: unterminated quote in line: a,"bc' });
  assert.throws(() => parseCsvLine(5), TypeError);
});

test('parseCsv builds objects from a header row', () => {
  assert.deepEqual(
    parseCsv('date,category,amount\n2016-01-05,travel,100\n2016-02-06,software,50'),
    [
      { date: '2016-01-05', category: 'travel', amount: '100' },
      { date: '2016-02-06', category: 'software', amount: '50' }
    ]
  );
  assert.deepEqual(parseCsv('date,category,amount\r\n2016-01-05,travel,100\r\n'), [
    { date: '2016-01-05', category: 'travel', amount: '100' }
  ]);
});

test('parseCsv skips blank lines and pads short rows', () => {
  assert.deepEqual(parseCsv('a,b\n\n1,2\n'), [{ a: '1', b: '2' }]);
  assert.deepEqual(parseCsv('a,b,c\n1,2'), [{ a: '1', b: '2', c: '' }]);
});

test('parseCsv rejects empty or non-string input', () => {
  assert.throws(() => parseCsv(''), { message: 'parseCsv: no header row' });
  assert.throws(() => parseCsv('\n \n'), { message: 'parseCsv: no header row' });
  assert.throws(() => parseCsv(null), TypeError);
});

test('normalizeRow maps a csv object onto a report row', () => {
  assert.deepEqual(
    normalizeRow({ date: '2016-03-14', category: 'travel', region: 'EMEA', amount: '$1,000.50' }),
    { date: '2016-03-14', category: 'travel', region: 'EMEA', amount: 1000.5, description: '' }
  );
  assert.deepEqual(
    normalizeRow({ date: '2016-03-14', category: 'travel', amount: '$(12.50)', description: 'refund' }),
    { date: '2016-03-14', category: 'travel', region: 'NA', amount: -12.5, description: 'refund' }
  );
  assert.equal(normalizeRow({ date: '2016-03-14', category: 'x', amount: 3 }).amount, 3);
});

test('normalizeRow rejects broken objects', () => {
  assert.throws(() => normalizeRow({ category: 'travel', amount: 1 }), { message: 'normalizeRow: missing date' });
  assert.throws(() => normalizeRow({ date: '2016-3-14', category: 'travel', amount: 1 }),
    { message: 'normalizeRow: bad date "2016-3-14"' });
  assert.throws(() => normalizeRow({ date: '2016-03-14', amount: 1 }), { message: 'normalizeRow: missing category' });
  assert.throws(() => normalizeRow({ date: '2016-03-14', category: 'x', amount: true }), TypeError);
  assert.throws(() => normalizeRow('nope'), TypeError);
});

test('validateRow collects errors in a fixed order', () => {
  assert.deepEqual(validateRow(ROWS[0]), []);
  assert.deepEqual(validateRow({}), ['missing date', 'missing category', 'missing amount']);
  assert.deepEqual(validateRow({ date: '2016-13-01', category: 'travel', amount: 5 }), ['bad date: 2016-13-01']);
  assert.deepEqual(validateRow({ date: '2016-01-01', category: '', amount: '5' }), ['missing category', 'amount is not a finite number']);
  assert.deepEqual(validateRow({ date: '2016-01-01', category: 'travel', region: 'MARS', amount: 5 }), ['unknown region: MARS']);
  assert.deepEqual(validateRow({ date: '2016-01-01', category: 'travel', amount: NaN }), ['amount is not a finite number']);
});

test('filterByPeriod is inclusive on both bounds', () => {
  const rows = [
    { date: '2016-02-29', amount: 1 },
    { date: '2016-03-01', amount: 2 },
    { date: '2016-03-31', amount: 3 },
    { date: '2016-04-01', amount: 4 }
  ];
  const kept = filterByPeriod(rows, '2016-03-01', '2016-03-31');
  assert.equal(kept.length, 2);
  assert.deepEqual(kept.map(r => r.date), ['2016-03-01', '2016-03-31']);
  assert.deepEqual(filterByPeriod(rows, '2016-03-14', '2016-03-14'), []);
});

test('filterByPeriod handles empty input and rejects bad arguments', () => {
  assert.deepEqual(filterByPeriod([], '2016-01-01', '2016-12-31'), []);
  assert.throws(() => filterByPeriod(ROWS, '2016-12-31', '2016-01-01'), RangeError);
  assert.throws(() => filterByPeriod(ROWS, '2016/01/01', '2016-12-31'),
    { message: 'filterByPeriod: dates must look like YYYY-MM-DD' });
  assert.throws(() => filterByPeriod([{ date: 'nope' }], '2016-01-01', '2016-12-31'),
    { message: 'filterByPeriod: row date "nope" is malformed' });
});

test('monthName maps every month and rejects out-of-range', () => {
  const expected = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];
  for (let m = 1; m <= 12; m++) {
    assert.equal(monthName(m), expected[m - 1]);
  }
  assert.throws(() => monthName(0), { message: 'monthName: month must be 1..12, got 0' });
  assert.throws(() => monthName(13), { message: 'monthName: month must be 1..12, got 13' });
});

test('quarterOfMonth maps months onto quarters', () => {
  assert.equal(quarterOfMonth(1), 1);
  assert.equal(quarterOfMonth(3), 1);
  assert.equal(quarterOfMonth(4), 2);
  assert.equal(quarterOfMonth(6), 2);
  assert.equal(quarterOfMonth(7), 3);
  assert.equal(quarterOfMonth(9), 3);
  assert.equal(quarterOfMonth(10), 4);
  assert.equal(quarterOfMonth(12), 4);
  assert.throws(() => quarterOfMonth(0), { message: 'quarterOfMonth: month must be 1..12, got 0' });
  assert.throws(() => quarterOfMonth(13), RangeError);
});

test('quarterLabel labels quarters and rejects out-of-range', () => {
  assert.equal(quarterLabel(1), 'Q1 (Jan-Mar)');
  assert.equal(quarterLabel(2), 'Q2 (Apr-Jun)');
  assert.equal(quarterLabel(3), 'Q3 (Jul-Sep)');
  assert.equal(quarterLabel(4), 'Q4 (Oct-Dec)');
  assert.throws(() => quarterLabel(0), RangeError);
  assert.throws(() => quarterLabel(5), { message: 'quarterLabel: quarter must be 1..4, got 5' });
});

test('sumColumn adds a numeric column', () => {
  assert.equal(sumColumn(ROWS, 'amount'), 1234.55);
  assert.equal(sumColumn([], 'amount'), 0);
  assert.equal(sumColumn([{ v: 1.5 }, { v: 2 }, { v: 0.5 }], 'v'), 4);
});

test('sumColumn rejects missing or non-numeric values', () => {
  assert.throws(() => sumColumn([{ amount: 1 }, {}], 'amount'),
    { message: 'sumColumn: "amount" is not a finite number on row 1' });
  assert.throws(() => sumColumn([{ amount: NaN }], 'amount'), TypeError);
});

test('creditSummary and debitSummary split charges from refunds', () => {
  const withZero = ROWS.concat([{ date: '2016-08-01', category: 'travel', region: 'NA', amount: 0 }]);
  assert.deepEqual(creditSummary(withZero), { count: 3, total: 1284.55 });
  assert.deepEqual(debitSummary(withZero), { count: 1, total: -50 });
  assert.deepEqual(creditSummary([]), { count: 0, total: 0 });
  assert.deepEqual(debitSummary([]), { count: 0, total: 0 });
});

test('aggregateByCategory buckets by first-seen order', () => {
  assert.deepEqual(aggregateByCategory(ROWS), [
    { name: 'travel', count: 2, total: 1284.55 },
    { name: 'software', count: 1, total: -50 }
  ]);
  assert.deepEqual(aggregateByCategory(ROWS, 'region'), [
    { name: 'NA', count: 2, total: 1284.55 },
    { name: 'EMEA', count: 1, total: -50 }
  ]);
  assert.deepEqual(aggregateByCategory([]), []);
});

test('buildMonthlySummary fills twelve buckets and keeps other years out', () => {
  const rows = ROWS.concat([{ date: '2015-03-14', category: 'travel', region: 'NA', amount: 999 }]);
  const buckets = buildMonthlySummary(rows, 2016);
  assert.equal(buckets.length, 12);
  assert.deepEqual(buckets[2], { label: 'March 2016', total: 200.5, count: 2 });
  assert.deepEqual(buckets[6], { label: 'July 2016', total: 1034.05, count: 1 });
  assert.deepEqual(buckets[0], { label: 'January 2016', total: 0, count: 0 });
  assert.deepEqual(buildMonthlySummary([], 2017).map(b => b.count),
    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
  assert.throws(() => buildMonthlySummary([{ date: 'nope' }], 2016),
    { message: 'buildMonthlySummary: bad date "nope"' });
});

test('buildQuarterlySummary fills four buckets', () => {
  const buckets = buildQuarterlySummary(ROWS, 2016);
  assert.deepEqual(buckets, [
    { label: 'Q1 (Jan-Mar)', total: 200.5, count: 2 },
    { label: 'Q2 (Apr-Jun)', total: 0, count: 0 },
    { label: 'Q3 (Jul-Sep)', total: 1034.05, count: 1 },
    { label: 'Q4 (Oct-Dec)', total: 0, count: 0 }
  ]);
  assert.deepEqual(buildQuarterlySummary(ROWS, 2015).map(b => b.total), [0, 0, 0, 0]);
  assert.throws(() => buildQuarterlySummary([{ date: '2016-1-1' }], 2016),
    { message: 'buildQuarterlySummary: bad date "2016-1-1"' });
});

test('buildRegionSummary keeps official regions and drops unknown ones', () => {
  const rows = ROWS.concat([{ date: '2016-08-01', category: 'travel', region: 'MARS', amount: 7 }]);
  assert.deepEqual(buildRegionSummary(rows), [
    { label: 'NA', total: 1284.55, count: 2 },
    { label: 'EMEA', total: -50, count: 1 },
    { label: 'APAC', total: 0, count: 0 },
    { label: 'LATAM', total: 0, count: 0 }
  ]);
  assert.deepEqual(buildRegionSummary([{ date: '2016-08-01', amount: 5 }]),
    [{ label: 'NA', total: 5, count: 1 }, { label: 'EMEA', total: 0, count: 0 },
     { label: 'APAC', total: 0, count: 0 }, { label: 'LATAM', total: 0, count: 0 }]);
});

test('statusForDaysLate uses the AR aging boundaries', () => {
  assert.equal(statusForDaysLate(-3), 'paid');
  assert.equal(statusForDaysLate(0), 'paid');
  assert.equal(statusForDaysLate(1), 'due');
  assert.equal(statusForDaysLate(15), 'due');
  assert.equal(statusForDaysLate(16), 'overdue');
  assert.equal(statusForDaysLate(45), 'overdue');
  assert.equal(statusForDaysLate(46), 'collections');
  assert.throws(() => statusForDaysLate('soon'), TypeError);
  assert.throws(() => statusForDaysLate(null), TypeError);
  assert.throws(() => statusForDaysLate(NaN), TypeError);
});

test('renderTable pads columns and right-aligns numeric columns', () => {
  assert.equal(
    renderTable(['Category', 'Count'], [['travel', 2], ['software', 12]]),
    'Category  Count\n--------  -----\ntravel    2\nsoftware  12'
  );
  assert.equal(
    renderTable(['Qty', 'Item'], [[3, 'paper'], [12, 'toner']]),
    'Qty  Item\n---  -----\n  3  paper\n 12  toner'
  );
});

test('renderTable left-aligns mixed columns and renders headers only', () => {
  assert.equal(renderTable(['A', 'B'], [[1, 'x'], ['y', 2]]), 'A  B\n-  -\n1  x\ny  2');
  assert.equal(renderTable(['A', 'B'], []), 'A  B\n-  -\n');
});

test('renderTable rejects malformed arguments', () => {
  assert.throws(() => renderTable('A', []), TypeError);
  assert.throws(() => renderTable(['A'], 'x'), TypeError);
  assert.throws(() => renderTable(['A', 'B'], [[1]]),
    { message: 'renderTable: row 0 must have 2 cells' });
  assert.throws(() => renderTable(['A', 'B'], [[1, 2, 3]]), TypeError);
});

test('topExpenses sorts descending and slices', () => {
  const seven = [
    { amount: 5 }, { amount: 30 }, { amount: 10 }, { amount: 25 },
    { amount: 15 }, { amount: 20 }, { amount: 1 }
  ];
  const top = topExpenses(seven);
  assert.equal(top.length, 5);
  assert.deepEqual(top.map(r => r.amount), [30, 25, 20, 15, 10]);
  assert.deepEqual(topExpenses(seven, 2).map(r => r.amount), [30, 25]);
  assert.deepEqual(topExpenses(seven, 0), []);
  assert.deepEqual(topExpenses(seven, 99).length, 7);
  assert.throws(() => topExpenses(seven, -1), RangeError);
  assert.throws(() => topExpenses(seven, 'x'), RangeError);
});

test('expenseReport renders the full close-process layout', () => {
  const out = expenseReport(ROWS, { startDate: '2016-01-01', endDate: '2016-12-31', title: 'FY16 Expenses' });
  assert.equal(out, [
    'FY16 Expenses',
    '=============',
    'Period: 2016-01-01 to 2016-12-31',
    'Rows: 3',
    'Charges: 2 for $1,284.55',
    'Refunds: 1 for $(50.00)',
    'Net: $1,234.55',
    '',
    'By category:',
    'Category  Count  Total',
    '--------  -----  ---------',
    'travel        2  $1,284.55',
    'software      1  $(50.00)',
    '',
    'By region:',
    'Region  Count  Total',
    '------  -----  ---------',
    'NA          2  $1,284.55',
    'EMEA        1  $(50.00)'
  ].join('\n'));
});

test('expenseReport applies the period filter before aggregating', () => {
  const out = expenseReport(ROWS, { startDate: '2016-03-01', endDate: '2016-03-31' });
  assert.equal(out, [
    'Expense Report',
    '==============',
    'Period: 2016-03-01 to 2016-03-31',
    'Rows: 2',
    'Charges: 1 for $250.50',
    'Refunds: 1 for $(50.00)',
    'Net: $200.50',
    '',
    'By category:',
    'Category  Count  Total',
    '--------  -----  --------',
    'travel        1  $250.50',
    'software      1  $(50.00)',
    '',
    'By region:',
    'Region  Count  Total',
    '------  -----  --------',
    'NA          1  $250.50',
    'EMEA        1  $(50.00)'
  ].join('\n'));
});

test('expenseReport on empty input renders a zero report', () => {
  assert.equal(expenseReport([], {}), [
    'Expense Report',
    '==============',
    'Rows: 0',
    'Charges: 0 for $0.00',
    'Refunds: 0 for $0.00',
    'Net: $0.00',
    '',
    'By category:',
    'Category  Count  Total',
    '--------  -----  -----',
    '',
    '',
    'By region:',
    'Region  Count  Total',
    '------  -----  -----',
    ''
  ].join('\n'));
});

test('expenseReport requires startDate and endDate together', () => {
  assert.throws(() => expenseReport(ROWS, { startDate: '2016-01-01' }),
    { message: 'expenseReport: startDate and endDate must be used together' });
  assert.throws(() => expenseReport(ROWS, { endDate: '2016-12-31' }), TypeError);
  assert.throws(() => expenseReport(ROWS, { startDate: '2016-12-31', endDate: '2016-01-01' }), RangeError);
});
