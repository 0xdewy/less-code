// Hidden safety net for the js fixture.
//
// Deliberately named so that plain `node --test` (the frozen gate) does NOT
// discover it: it is excluded from the prompt spec too, and is executed only
// by `lc bench` after a reduction, as an independent behaviour check.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  monthName,
  quarterOfMonth,
  quarterLabel,
  formatMoney,
  parseMoney,
  parseCsvLine,
  parseCsv,
  normalizeRow,
  validateRow,
  filterByPeriod,
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
} from '../reporting.js';

const ROWS = [
  { date: '2023-01-15', category: 'Travel', region: 'NA', amount: 1200.5, description: '' },
  { date: '2023-02-03', category: 'Travel', region: 'EMEA', amount: -100, description: '' },
  { date: '2023-04-20', category: 'Software', region: 'APAC', amount: 99.99, description: '' },
  { date: '2022-12-31', category: 'Travel', region: 'NA', amount: 5, description: '' }
];

test('monthName covers every month and rejects out of range', () => {
  const names = [];
  for (let m = 1; m <= 12; m++) { names.push(monthName(m)); }
  assert.deepEqual(names.slice(0, 3), ['January', 'February', 'March']);
  assert.equal(names[11], 'December');
  assert.throws(() => monthName(0), RangeError);
  assert.throws(() => monthName(13), /month must be 1\.\.12, got 13/);
});

test('quarter helpers agree with each other', () => {
  assert.deepEqual([1, 4, 7, 10].map(quarterOfMonth), [1, 2, 3, 4]);
  assert.deepEqual([3, 6, 9, 12].map(quarterOfMonth), [1, 2, 3, 4]);
  assert.equal(quarterLabel(2), 'Q2 (Apr-Jun)');
  assert.throws(() => quarterOfMonth(13), RangeError);
  assert.throws(() => quarterLabel(5), RangeError);
});

test('formatMoney thousands separators and accounting negatives', () => {
  assert.equal(formatMoney(0), '$0.00');
  assert.equal(formatMoney(999), '$999.00');
  assert.equal(formatMoney(1000), '$1,000.00');
  assert.equal(formatMoney(1234567.891), '$1,234,567.89');
  assert.equal(formatMoney(-45), '$(45.00)');
  assert.equal(formatMoney(1234, { symbol: '', precision: 0, thousands: ' ' }), '1 234');
  assert.equal(formatMoney(1.5, { decimal: ',' }), '$1,50');
  assert.throws(() => formatMoney('5'), TypeError);
  assert.throws(() => formatMoney(Infinity), TypeError);
  assert.throws(() => formatMoney(NaN), TypeError);
});

test('parseMoney round-trips formatMoney', () => {
  for (const n of [0, 12.34, 1000, -45, -1234567.89]) {
    assert.equal(parseMoney(formatMoney(n)), n);
  }
  assert.equal(parseMoney(' (45.00) '), -45);
  assert.throws(() => parseMoney('abc'), /cannot parse "abc"/);
  assert.throws(() => parseMoney('$'), /cannot parse/);
  assert.throws(() => parseMoney(5), TypeError);
});

test('parseCsvLine handles quotes, escapes and empties', () => {
  assert.deepEqual(parseCsvLine('a,b,c'), ['a', 'b', 'c']);
  assert.deepEqual(parseCsvLine('a,,c'), ['a', '', 'c']);
  assert.deepEqual(parseCsvLine('"a,b",c'), ['a,b', 'c']);
  assert.deepEqual(parseCsvLine('"say ""hi""",c'), ['say "hi"', 'c']);
  assert.deepEqual(parseCsvLine(''), ['']);
  assert.throws(() => parseCsvLine('"open'), /unterminated quote/);
  assert.throws(() => parseCsvLine(null), TypeError);
});

test('parseCsv strips CR, skips blank lines and pads short rows', () => {
  const rows = parseCsv('date,amount\r\n2023-01-01,5\r\n\n2023-01-02\n');
  assert.deepEqual(rows, [
    { date: '2023-01-01', amount: '5' },
    { date: '2023-01-02', amount: '' }
  ]);
  assert.throws(() => parseCsv('   \n'), /no header row/);
  assert.throws(() => parseCsv(42), TypeError);
});

test('normalizeRow defaults region and parses money strings', () => {
  assert.deepEqual(normalizeRow({ date: '2023-01-01', category: 'X', amount: '$(5.00)' }), {
    date: '2023-01-01', category: 'X', region: 'NA', amount: -5, description: ''
  });
  assert.throws(() => normalizeRow({ category: 'X', amount: 1 }), /missing date/);
  assert.throws(() => normalizeRow({ date: '2023-13-01', category: 'X', amount: 1 }), TypeError);
  assert.throws(() => normalizeRow({ date: '2023-01-01', category: '', amount: 1 }), /missing category/);
  assert.throws(() => normalizeRow({ date: '2023-01-01', category: 'X', amount: true }), TypeError);
  assert.throws(() => normalizeRow(null), TypeError);
});

test('validateRow accumulates every problem', () => {
  assert.deepEqual(validateRow({ date: '2023-01-01', category: 'X', amount: 1 }), []);
  assert.deepEqual(
    validateRow({ date: 'nope', region: 'MARS' }),
    ['bad date: nope', 'missing category', 'unknown region: MARS', 'missing amount']
  );
  assert.deepEqual(validateRow({ date: '2023-01-01', category: 'X', amount: Infinity }),
    ['amount is not a finite number']);
});

test('filterByPeriod is inclusive on both ends', () => {
  const got = filterByPeriod(ROWS, '2023-01-15', '2023-04-20');
  assert.equal(got.length, 3);
  assert.deepEqual(filterByPeriod(ROWS, '2023-03-01', '2023-03-31'), []);
  assert.throws(() => filterByPeriod(ROWS, '2023-04-20', '2023-01-15'), RangeError);
  assert.throws(() => filterByPeriod(ROWS, 'x', '2023-01-15'), TypeError);
  assert.throws(() => filterByPeriod([{ date: 'bad' }], '2023-01-01', '2023-12-31'), TypeError);
});

test('column sums and credit/debit split', () => {
  assert.equal(sumColumn([], 'amount'), 0);
  assert.equal(Math.round(sumColumn(ROWS, 'amount') * 100) / 100, 1205.49);
  assert.throws(() => sumColumn([{ amount: 'x' }], 'amount'), TypeError);
  assert.deepEqual(creditSummary(ROWS), { count: 3, total: 1305.49 });
  assert.deepEqual(debitSummary(ROWS), { count: 1, total: -100 });
  assert.deepEqual(creditSummary([{ amount: 0 }]), { count: 1, total: 0 });
  assert.deepEqual(debitSummary([{ amount: 0 }]), { count: 0, total: 0 });
});

test('aggregateByCategory keeps first-seen order and supports any field', () => {
  const byCat = aggregateByCategory(ROWS);
  assert.deepEqual(byCat.map((b) => b.name), ['Travel', 'Software']);
  assert.deepEqual(byCat[0], { name: 'Travel', count: 3, total: 1105.5 });
  assert.deepEqual(aggregateByCategory(ROWS, 'region').map((b) => b.name),
    ['NA', 'EMEA', 'APAC']);
  assert.deepEqual(aggregateByCategory([]), []);
});

test('summary builders bucket by year, month, quarter and region', () => {
  const monthly = buildMonthlySummary(ROWS, 2023);
  assert.equal(monthly.length, 12);
  assert.equal(monthly[0].label, 'January 2023');
  assert.deepEqual([monthly[0].count, monthly[1].count, monthly[3].count], [1, 1, 1]);
  assert.equal(monthly[11].count, 0);
  const quarterly = buildQuarterlySummary(ROWS, 2023);
  assert.deepEqual(quarterly.map((b) => b.count), [2, 1, 0, 0]);
  assert.equal(quarterly[0].label, 'Q1 (Jan-Mar)');
  const regional = buildRegionSummary(ROWS);
  assert.deepEqual(regional.map((b) => b.label), ['NA', 'EMEA', 'APAC', 'LATAM']);
  assert.deepEqual(regional.map((b) => b.count), [2, 1, 1, 0]);
  assert.deepEqual(buildRegionSummary([{ region: 'MARS', amount: 1 }]).map((b) => b.count),
    [0, 0, 0, 0]);
  assert.throws(() => buildMonthlySummary([{ date: 'x', amount: 1 }], 2023), TypeError);
  assert.throws(() => buildQuarterlySummary([{ date: 'x', amount: 1 }], 2023), TypeError);
});

test('aging buckets keep their agreed boundaries', () => {
  assert.deepEqual([-1, 0, 1, 15, 16, 45, 46].map(statusForDaysLate),
    ['paid', 'paid', 'due', 'due', 'overdue', 'overdue', 'collections']);
  assert.throws(() => statusForDaysLate('3'), TypeError);
  assert.throws(() => statusForDaysLate(NaN), TypeError);
});

test('renderTable aligns numbers right and never pads the last column', () => {
  const out = renderTable(['Name', 'Qty'], [['ab', 1], ['longer', 22]]);
  assert.equal(out, 'Name    Qty\n------  ---\nab      1\nlonger  22');
  const mixed = renderTable(['A', 'B'], [[1, 'x'], [22, 'y']]);
  assert.equal(mixed.split('\n')[2], ' 1  x');
  assert.equal(renderTable(['A'], []), 'A\n-\n');
  assert.throws(() => renderTable('A', []), TypeError);
  assert.throws(() => renderTable(['A', 'B'], [['only']]), /row 0 must have 2 cells/);
});

test('topExpenses sorts descending and clamps to length', () => {
  assert.deepEqual(topExpenses(ROWS, 2).map((r) => r.amount), [1200.5, 99.99]);
  assert.equal(topExpenses(ROWS).length, 4);
  assert.deepEqual(topExpenses(ROWS, 0), []);
  const copy = ROWS.slice();
  topExpenses(ROWS, 4);
  assert.deepEqual(ROWS, copy, 'must not sort the caller array in place');
  assert.throws(() => topExpenses(ROWS, -1), RangeError);
});

test('expenseReport renders the full document', () => {
  const report = expenseReport(ROWS, { title: 'Q1', startDate: '2023-01-01', endDate: '2023-06-30' });
  const lines = report.split('\n');
  assert.equal(lines[0], 'Q1');
  assert.equal(lines[1], '==');
  assert.equal(lines[2], 'Period: 2023-01-01 to 2023-06-30');
  assert.equal(lines[3], 'Rows: 3');
  assert.equal(lines[4], 'Charges: 2 for $1,300.49');
  assert.equal(lines[5], 'Refunds: 1 for $(100.00)');
  assert.equal(lines[6], 'Net: $1,200.49');
  assert.ok(report.includes('By category:'));
  assert.ok(report.includes('By region:'));
  const plain = expenseReport(ROWS);
  assert.equal(plain.split('\n')[0], 'Expense Report');
  assert.ok(!plain.includes('Period:'));
  assert.throws(() => expenseReport(ROWS, { startDate: '2023-01-01' }), TypeError);
});
