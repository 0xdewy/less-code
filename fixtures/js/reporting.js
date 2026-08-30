// reporting.js — sales/expense reporting utilities for the finance team.
//
// Extracted from the old `finance-tools` repo in 2016 and carried forward
// through the Node 4 -> 8 -> 18 migrations mostly untouched. Please keep the
// exported names stable: the month-end batch job (see ops/batch/close.js in
// the old repo) imports most of them by name.
//
// Conventions:
//   - dates are 'YYYY-MM-DD' strings so they sort lexically (Excel-friendly)
//   - money is plain JS numbers; formatting happens at the edges only
//   - negative amounts are refunds and render as $(x.yy) accounting style

// The four official reporting regions. Anything else in the CSV import is a
// data-entry mistake and gets dropped from region reports (ticket #902).
const REGIONS = ['NA', 'EMEA', 'APAC', 'LATAM'];

function isValidDate(s) {
  if (typeof s !== 'string' || s.length !== 10) {
    return false;
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    return false;
  }
  const month = Number(s.substring(5, 7));
  const day = Number(s.substring(8, 10));
  if (month < 1 || month > 12) {
    return false;
  }
  if (day < 1 || day > 31) {
    return false;
  }
  return true;
}

export function monthName(month) {
  // FIXME: replace with an array lookup, this predates us trusting V8
  if (month === 1) { return 'January'; }
  else if (month === 2) { return 'February'; }
  else if (month === 3) { return 'March'; }
  else if (month === 4) { return 'April'; }
  else if (month === 5) { return 'May'; }
  else if (month === 6) { return 'June'; }
  else if (month === 7) { return 'July'; }
  else if (month === 8) { return 'August'; }
  else if (month === 9) { return 'September'; }
  else if (month === 10) { return 'October'; }
  else if (month === 11) { return 'November'; }
  else if (month === 12) { return 'December'; }
  throw new RangeError('monthName: month must be 1..12, got ' + month);
}

export function quarterOfMonth(month) {
  if (month === 1 || month === 2 || month === 3) { return 1; }
  else if (month === 4 || month === 5 || month === 6) { return 2; }
  else if (month === 7 || month === 8 || month === 9) { return 3; }
  else if (month === 10 || month === 11 || month === 12) { return 4; }
  throw new RangeError('quarterOfMonth: month must be 1..12, got ' + month);
}

export function quarterLabel(quarter) {
  if (quarter === 1) { return 'Q1 (Jan-Mar)'; }
  else if (quarter === 2) { return 'Q2 (Apr-Jun)'; }
  else if (quarter === 3) { return 'Q3 (Jul-Sep)'; }
  else if (quarter === 4) { return 'Q4 (Oct-Dec)'; }
  throw new RangeError('quarterLabel: quarter must be 1..4, got ' + quarter);
}

export function formatMoney(amount, options) {
  const opts = options || {};
  const symbol = opts.symbol !== undefined ? opts.symbol : '$';
  const precision = opts.precision !== undefined ? opts.precision : 2;
  const thousands = opts.thousands !== undefined ? opts.thousands : ',';
  const decimal = opts.decimal !== undefined ? opts.decimal : '.';
  if (typeof amount !== 'number' || isNaN(amount) || !isFinite(amount)) {
    throw new TypeError('formatMoney: amount must be a finite number');
  }
  if (typeof precision !== 'number') {
    throw new TypeError('formatMoney: precision must be a number');
  }
  const negative = amount < 0;
  const fixed = Math.abs(amount).toFixed(precision);
  const parts = fixed.split('.');
  const dollars = parts[0];
  const cents = parts.length > 1 ? parts[1] : '';

  // Insert thousands separators by hand. Intl.NumberFormat gave different
  // results across Node versions in 2016 so finance made us do it manually
  // (ticket #823). Walk right-to-left, drop a separator every third digit.
  let withSep = '';
  let count = 0;
  for (let i = dollars.length - 1; i >= 0; i--) {
    withSep = dollars.charAt(i) + withSep;
    count = count + 1;
    if (count % 3 === 0 && i > 0) {
      withSep = thousands + withSep;
    }
  }

  // Accounting style: refunds get parentheses instead of a minus sign.
  let out = symbol + (negative ? '(' : '') + withSep;
  if (cents !== '') {
    out = out + decimal + cents;
  }
  if (negative) {
    out = out + ')';
  }
  return out;
}

export function parseMoney(text) {
  if (typeof text !== 'string') {
    throw new TypeError('parseMoney: expects a string');
  }
  let s = text.trim();
  let negative = false;
  // accounting style, matching what formatMoney emits: "$(45.00)" or "(45.00)"
  if (s.charAt(0) === '$' && s.charAt(1) === '(' && s.charAt(s.length - 1) === ')') {
    negative = true;
    s = s.substring(2, s.length - 1).trim();
  } else if (s.charAt(0) === '(' && s.charAt(s.length - 1) === ')') {
    negative = true;
    s = s.substring(1, s.length - 1).trim();
  }
  s = s.replace(/\$/g, '').replace(/,/g, '').replace(/ /g, '');
  if (s === '' || s === '-' || s === '.') {
    throw new Error('parseMoney: cannot parse "' + text + '"');
  }
  if (!/^-?\d+(\.\d+)?$/.test(s)) {
    throw new Error('parseMoney: cannot parse "' + text + '"');
  }
  const n = Number(s);
  return negative ? -n : n;
}

export function parseCsvLine(line) {
  if (typeof line !== 'string') {
    throw new TypeError('parseCsvLine: expects a string');
  }
  const fields = [];
  let current = '';
  let inQuotes = false;
  let i = 0;
  while (i < line.length) {
    const ch = line.charAt(i);
    if (inQuotes) {
      if (ch === '"') {
        // RFC 4180: "" inside a quoted field is a literal quote
        if (line.charAt(i + 1) === '"') {
          current = current + '"';
          i = i + 2;
          continue;
        }
        inQuotes = false;
        i = i + 1;
        continue;
      }
      current = current + ch;
      i = i + 1;
      continue;
    }
    if (ch === '"') {
      inQuotes = true;
      i = i + 1;
      continue;
    }
    if (ch === ',') {
      fields.push(current);
      current = '';
      i = i + 1;
      continue;
    }
    current = current + ch;
    i = i + 1;
  }
  // A quote that never closes is almost always a broken Excel export (#1149)
  if (inQuotes) {
    throw new Error('parseCsvLine: unterminated quote in line: ' + line);
  }
  fields.push(current);
  return fields;
}

export function parseCsv(text) {
  if (typeof text !== 'string') {
    throw new TypeError('parseCsv: expects a string');
  }
  const rawLines = text.split('\n');
  const lines = [];
  for (let i = 0; i < rawLines.length; i++) {
    let line = rawLines[i];
    // NOTE: Excel on Windows emits \r\n; strip the \r before anything else.
    // Whitespace-only lines happen too (copy/paste from Outlook) — skip them.
    if (line.charAt(line.length - 1) === '\r') {
      line = line.substring(0, line.length - 1);
    }
    if (line.trim() !== '') {
      lines.push(line);
    }
  }
  if (lines.length === 0) {
    throw new Error('parseCsv: no header row');
  }
  const headers = parseCsvLine(lines[0]);
  const out = [];
  for (let i = 1; i < lines.length; i++) {
    const fields = parseCsvLine(lines[i]);
    const row = {};
    for (let j = 0; j < headers.length; j++) {
      // extra fields past the header are dropped — matches the old importer
      row[headers[j]] = j < fields.length ? fields[j] : '';
    }
    out.push(row);
  }
  return out;
}

export function normalizeRow(obj) {
  if (obj === null || typeof obj !== 'object') {
    throw new TypeError('normalizeRow: expects a row object');
  }
  if (obj.date === undefined) {
    throw new Error('normalizeRow: missing date');
  }
  if (!isValidDate(obj.date)) {
    throw new TypeError('normalizeRow: bad date "' + obj.date + '"');
  }
  if (obj.category === undefined || obj.category === '') {
    throw new Error('normalizeRow: missing category');
  }
  let amount;
  if (typeof obj.amount === 'number') {
    amount = obj.amount;
  } else if (typeof obj.amount === 'string') {
    amount = parseMoney(obj.amount);
  } else {
    throw new TypeError('normalizeRow: amount must be a number or string');
  }
  if (!isFinite(amount)) {
    throw new TypeError('normalizeRow: amount must be finite');
  }
  return {
    date: obj.date,
    category: obj.category,
    // most of the books are NA; default added during the 2019 cleanup
    region: obj.region !== undefined ? obj.region : 'NA',
    amount: amount,
    description: obj.description !== undefined ? obj.description : ''
  };
}

export function validateRow(row) {
  const errors = [];
  if (row.date === undefined) {
    errors.push('missing date');
  } else if (!isValidDate(row.date)) {
    errors.push('bad date: ' + row.date);
  }
  if (row.category === undefined || row.category === '') {
    errors.push('missing category');
  }
  if (row.region !== undefined && REGIONS.indexOf(row.region) === -1) {
    errors.push('unknown region: ' + row.region);
  }
  if (row.amount === undefined) {
    errors.push('missing amount');
  } else if (typeof row.amount !== 'number' || !isFinite(row.amount)) {
    errors.push('amount is not a finite number');
  }
  return errors;
}

export function filterByPeriod(rows, startDate, endDate) {
  if (!isValidDate(startDate) || !isValidDate(endDate)) {
    throw new TypeError('filterByPeriod: dates must look like YYYY-MM-DD');
  }
  if (startDate > endDate) {
    throw new RangeError('filterByPeriod: start is after end');
  }
  const out = [];
  for (let i = 0; i < rows.length; i++) {
    const d = rows[i].date;
    if (!isValidDate(d)) {
      throw new TypeError('filterByPeriod: row date "' + d + '" is malformed');
    }
    // bounds are inclusive on both ends, per the close-process doc
    if (d >= startDate && d <= endDate) {
      out.push(rows[i]);
    }
  }
  return out;
}

export function sumColumn(rows, column) {
  let total = 0;
  for (let i = 0; i < rows.length; i++) {
    const value = rows[i][column];
    if (typeof value !== 'number' || !isFinite(value)) {
      throw new TypeError('sumColumn: "' + column + '" is not a finite number on row ' + i);
    }
    total = total + value;
  }
  return total;
}

export function creditSummary(rows) {
  // charges (positive amounts): {count, total}
  const out = { count: 0, total: 0 };
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].amount >= 0) {
      out.count = out.count + 1;
      out.total = out.total + rows[i].amount;
    }
  }
  return out;
}

export function debitSummary(rows) {
  // refunds (negative amounts): {count, total}. Third copy of the same loop,
  // TODO: fold creditSummary/debitSummary into one helper with a predicate
  const out = { count: 0, total: 0 };
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].amount < 0) {
      out.count = out.count + 1;
      out.total = out.total + rows[i].amount;
    }
  }
  return out;
}

export function aggregateByCategory(rows, categoryField) {
  const field = categoryField !== undefined ? categoryField : 'category';
  const buckets = [];
  for (let i = 0; i < rows.length; i++) {
    const name = rows[i][field];
    // TODO(mk): switch to Array.prototype.find once we drop Node 4
    let bucket = null;
    for (let j = 0; j < buckets.length; j++) {
      if (buckets[j].name === name) {
        bucket = buckets[j];
        break;
      }
    }
    if (bucket === null) {
      bucket = { name: name, count: 0, total: 0 };
      buckets.push(bucket);
    }
    bucket.count = bucket.count + 1;
    bucket.total = bucket.total + rows[i].amount;
  }
  return buckets;
}

export function buildMonthlySummary(rows, year) {
  const buckets = [];
  for (let m = 1; m <= 12; m++) {
    buckets.push({ label: monthName(m) + ' ' + year, total: 0, count: 0 });
  }
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    if (!isValidDate(row.date)) {
      throw new TypeError('buildMonthlySummary: bad date "' + row.date + '"');
    }
    if (Number(row.date.substring(0, 4)) !== year) {
      continue;
    }
    const month = Number(row.date.substring(5, 7));
    const bucket = buckets[month - 1];
    bucket.total = bucket.total + row.amount;
    bucket.count = bucket.count + 1;
  }
  return buckets;
}

export function buildQuarterlySummary(rows, year) {
  // same shape as buildMonthlySummary but quarter buckets — the report
  // scripts want both, and this started life as a copy/paste of the monthly
  // one back in 2017. It has drifted slightly since. Be careful.
  const buckets = [];
  for (let q = 1; q <= 4; q++) {
    buckets.push({ label: quarterLabel(q), total: 0, count: 0 });
  }
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    if (!isValidDate(row.date)) {
      throw new TypeError('buildQuarterlySummary: bad date "' + row.date + '"');
    }
    if (Number(row.date.substring(0, 4)) !== year) {
      continue;
    }
    const quarter = quarterOfMonth(Number(row.date.substring(5, 7)));
    const bucket = buckets[quarter - 1];
    bucket.total = bucket.total + row.amount;
    bucket.count = bucket.count + 1;
  }
  return buckets;
}

export function buildRegionSummary(rows) {
  // yet another bucket loop; regions outside REGIONS are dropped (#902)
  const buckets = [];
  for (let r = 0; r < REGIONS.length; r++) {
    buckets.push({ label: REGIONS[r], total: 0, count: 0 });
  }
  for (let i = 0; i < rows.length; i++) {
    const region = rows[i].region !== undefined ? rows[i].region : 'NA';
    const idx = REGIONS.indexOf(region);
    if (idx === -1) {
      continue;
    }
    const bucket = buckets[idx];
    bucket.total = bucket.total + rows[i].amount;
    bucket.count = bucket.count + 1;
  }
  return buckets;
}

export function statusForDaysLate(days) {
  if (typeof days !== 'number' || isNaN(days)) {
    throw new TypeError('statusForDaysLate: expects a number of days');
  }
  // aging buckets agreed with AR in 2016; do not "fix" the boundaries
  if (days <= 0) {
    return 'paid';
  } else if (days <= 15) {
    return 'due';
  } else if (days <= 45) {
    return 'overdue';
  } else {
    return 'collections';
  }
}

function padRight(s, width) {
  let out = s;
  while (out.length < width) {
    out = out + ' ';
  }
  return out;
}

function padLeft(s, width) {
  let out = s;
  while (out.length < width) {
    out = ' ' + out;
  }
  return out;
}

function columnIsNumeric(values) {
  // a column is right-aligned only if EVERY value is a finite number
  for (let i = 0; i < values.length; i++) {
    if (typeof values[i] !== 'number' || !isFinite(values[i])) {
      return false;
    }
  }
  return true;
}

export function renderTable(headers, rows) {
  if (!Array.isArray(headers) || !Array.isArray(rows)) {
    throw new TypeError('renderTable: headers and rows must be arrays');
  }
  const widths = [];
  for (let j = 0; j < headers.length; j++) {
    widths.push(String(headers[j]).length);
  }
  for (let i = 0; i < rows.length; i++) {
    const cells = rows[i];
    if (!Array.isArray(cells) || cells.length !== headers.length) {
      throw new TypeError('renderTable: row ' + i + ' must have ' + headers.length + ' cells');
    }
    for (let j = 0; j < cells.length; j++) {
      widths[j] = Math.max(widths[j], String(cells[j]).length);
    }
  }
  const numeric = [];
  for (let j = 0; j < headers.length; j++) {
    const column = [];
    for (let i = 0; i < rows.length; i++) {
      column.push(rows[i][j]);
    }
    numeric.push(columnIsNumeric(column));
  }

  // header row: pad every cell except the last (no trailing spaces in the
  // close-process diff, that was a whole afternoon of complaints once)
  let headerLine = '';
  for (let j = 0; j < headers.length; j++) {
    if (j > 0) {
      headerLine = headerLine + '  ';
    }
    const text = String(headers[j]);
    headerLine = headerLine + (j < headers.length - 1 ? padRight(text, widths[j]) : text);
  }
  let out = headerLine + '\n';

  let separator = '';
  for (let j = 0; j < headers.length; j++) {
    if (j > 0) {
      separator = separator + '  ';
    }
    let dashes = '';
    for (let k = 0; k < widths[j]; k++) {
      dashes = dashes + '-';
    }
    separator = separator + dashes;
  }
  out = out + separator + '\n';

  for (let i = 0; i < rows.length; i++) {
    let line = '';
    for (let j = 0; j < rows[i].length; j++) {
      if (j > 0) {
        line = line + '  ';
      }
      const text = String(rows[i][j]);
      if (j === rows[i].length - 1) {
        line = line + text;
      } else if (numeric[j]) {
        line = line + padLeft(text, widths[j]);
      } else {
        line = line + padRight(text, widths[j]);
      }
    }
    out = out + line;
    if (i < rows.length - 1) {
      out = out + '\n';
    }
  }
  return out;
}

function summaryTableRows(buckets) {
  const out = [];
  for (let i = 0; i < buckets.length; i++) {
    out.push([buckets[i].name, buckets[i].count, formatMoney(buckets[i].total)]);
  }
  return out;
}

export function topExpenses(rows, n) {
  let limit = n;
  if (limit === undefined) {
    limit = 5;
  }
  if (typeof limit !== 'number' || limit < 0) {
    throw new RangeError('topExpenses: n must be a non-negative number');
  }
  const sorted = rows.slice().sort(function (a, b) { return b.amount - a.amount; });
  const out = [];
  const take = Math.min(limit, sorted.length);
  for (let i = 0; i < take; i++) {
    out.push(sorted[i]);
  }
  return out;
}

export function expenseReport(rows, options) {
  const opts = options || {};
  const title = opts.title !== undefined ? opts.title : 'Expense Report';
  const hasStart = opts.startDate !== undefined;
  const hasEnd = opts.endDate !== undefined;
  if (hasStart !== hasEnd) {
    throw new TypeError('expenseReport: startDate and endDate must be used together');
  }
  let filtered = rows;
  if (hasStart && hasEnd) {
    filtered = filterByPeriod(rows, opts.startDate, opts.endDate);
  }
  const charges = creditSummary(filtered);
  const refunds = debitSummary(filtered);
  const net = sumColumn(filtered, 'amount');

  // title bar has to match the title length for the old print template
  let bar = '';
  for (let i = 0; i < title.length; i++) {
    bar = bar + '=';
  }
  let out = title + '\n' + bar + '\n';
  if (hasStart) {
    out = out + 'Period: ' + opts.startDate + ' to ' + opts.endDate + '\n';
  }
  out = out + 'Rows: ' + filtered.length + '\n';
  out = out + 'Charges: ' + charges.count + ' for ' + formatMoney(charges.total) + '\n';
  out = out + 'Refunds: ' + refunds.count + ' for ' + formatMoney(refunds.total) + '\n';
  out = out + 'Net: ' + formatMoney(net) + '\n\n';

  const byCategory = aggregateByCategory(filtered, 'category');
  out = out + 'By category:\n';
  out = out + renderTable(['Category', 'Count', 'Total'], summaryTableRows(byCategory));
  out = out + '\n\n';

  const byRegion = aggregateByCategory(filtered, 'region');
  out = out + 'By region:\n';
  out = out + renderTable(['Region', 'Count', 'Total'], summaryTableRows(byRegion));
  return out;
}

// ---------------------------------------------------------------------------
// Legacy exports below. The 2019 dashboard rewrite dropped the last callers,
// but they are kept "just in case" — nothing outside the testsuite-rejected
// graveyard imports them. Do not add new callers; delete when ops confirms.
// ---------------------------------------------------------------------------

export function formatPercent(value, decimals) {
  // dashboard used this before the 2019 rewrite; rounding kept for parity
  const d = decimals !== undefined ? decimals : 1;
  return (value * 100).toFixed(d) + '%';
}

export function deepCloneRow(row) {
  // pre-structuredClone helper, kept around for ancient call sites
  const clone = {};
  const keys = ['date', 'category', 'region', 'amount', 'description'];
  for (let i = 0; i < keys.length; i++) {
    const key = keys[i];
    if (row[key] !== undefined) {
      clone[key] = row[key];
    }
  }
  clone.tags = [];
  if (row.tags !== undefined) {
    for (let i = 0; i < row.tags.length; i++) {
      clone.tags.push(row.tags[i]);
    }
  }
  return clone;
}

export function pivotByRegion(rows) {
  // 2017 offsite experiment; never wired into a report. Categories per
  // region, widest-first. Retained for reference only.
  const regions = [];
  const categories = [];
  for (let i = 0; i < rows.length; i++) {
    if (regions.indexOf(rows[i].region) === -1) {
      regions.push(rows[i].region);
    }
    if (categories.indexOf(rows[i].category) === -1) {
      categories.push(rows[i].category);
    }
  }
  const cells = [];
  for (let r = 0; r < regions.length; r++) {
    const line = [];
    for (let c = 0; c < categories.length; c++) {
      let total = 0;
      for (let i = 0; i < rows.length; i++) {
        if (rows[i].region === regions[r] && rows[i].category === categories[c]) {
          total = total + rows[i].amount;
        }
      }
      line.push(total);
    }
    cells.push(line);
  }
  return { regions: regions, categories: categories, cells: cells };
}
