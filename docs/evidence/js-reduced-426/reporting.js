// reporting.js — sales/expense reporting utilities for the finance team.

const REGIONS = ['NA', 'EMEA', 'APAC', 'LATAM'];

function isValidDate(s) {
  return typeof s === 'string' && /^(\d{4}-\d{2}-\d{2})$/.test(s) &&
    Number(s.substring(5, 7)) >= 1 && Number(s.substring(5, 7)) <= 12 &&
    Number(s.substring(8, 10)) >= 1 && Number(s.substring(8, 10)) <= 31;
}

export function monthName(month) {
  if (month < 1 || month > 12) throw new RangeError('monthName: month must be 1..12, got ' + month);
  return ['January', 'February', 'March', 'April', 'May', 'June', 
          'July', 'August', 'September', 'October', 'November', 'December'][month - 1];
}

export function quarterOfMonth(month) {
  if (month < 1 || month > 12) throw new RangeError('quarterOfMonth: month must be 1..12, got ' + month);
  return Math.ceil(month / 3);
}

export function quarterLabel(quarter) {
  if (quarter < 1 || quarter > 4) throw new RangeError('quarterLabel: quarter must be 1..4, got ' + quarter);
  return ['Q1 (Jan-Mar)', 'Q2 (Apr-Jun)', 'Q3 (Jul-Sep)', 'Q4 (Oct-Dec)'][quarter - 1];
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
  if (typeof text !== 'string') throw new TypeError('parseMoney: expects a string');
  let s = text.trim();
  let negative = false;
  if (s.charAt(0) === '$' && s.charAt(1) === '(' && s.charAt(s.length - 1) === ')') {
    negative = true;
    s = s.substring(2, s.length - 1).trim();
  } else if (s.charAt(0) === '(' && s.charAt(s.length - 1) === ')') {
    negative = true;
    s = s.substring(1, s.length - 1).trim();
  }
  s = s.replace(/\$/g, '').replace(/,/g, '').replace(/ /g, '');
  if (s === '' || s === '-' || s === '.') throw new Error('parseMoney: cannot parse "' + text + '"');
  if (!/^-?\d+(\.\d+)?$/.test(s)) throw new Error('parseMoney: cannot parse "' + text + '"');
  const n = Number(s);
  return negative ? -n : n;
}

export function parseCsvLine(line) {
  if (typeof line !== 'string') throw new TypeError('parseCsvLine: expects a string');
  const fields = [];
  let current = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line.charAt(i);
    if (inQuotes) {
      if (ch === '"' && line.charAt(i + 1) === '"') {
        current += '"';
        i++;
        continue;
      }
      if (ch === '"') inQuotes = false;
      else current += ch;
    } else if (ch === ',') fields.push(current), current = '';
    else if (ch === '"') inQuotes = true;
    else current += ch;
  }
  if (inQuotes) throw new Error('parseCsvLine: unterminated quote in line: ' + line);
  return [...fields, current];
}

export function parseCsv(text) {
  if (typeof text !== 'string') throw new TypeError('parseCsv: expects a string');
  const rawLines = text.split('\n');
  const lines = [];
  for (let i = 0; i < rawLines.length; i++) {
    let line = rawLines[i];
    if (line.charAt(line.length - 1) === '\r') line = line.substring(0, line.length - 1);
    if (line.trim() !== '') lines.push(line);
  }
  if (lines.length === 0) throw new Error('parseCsv: no header row');
  const headers = parseCsvLine(lines[0]);
  const out = [];
  for (let i = 1; i < lines.length; i++) {
    const fields = parseCsvLine(lines[i]);
    const row = {};
    for (let j = 0; j < headers.length; j++) {
      row[headers[j]] = j < fields.length ? fields[j] : '';
    }
    out.push(row);
  }
  return out;
}

export function normalizeRow(obj) {
  if (obj === null || typeof obj !== 'object') throw new TypeError('normalizeRow: expects a row object');
  if (obj.date === undefined) throw new Error('normalizeRow: missing date');
  if (!isValidDate(obj.date)) throw new TypeError('normalizeRow: bad date "' + obj.date + '"');
  if (obj.category === undefined || obj.category === '') throw new Error('normalizeRow: missing category');
  let amount;
  if (typeof obj.amount === 'number') amount = obj.amount;
  else if (typeof obj.amount === 'string') amount = parseMoney(obj.amount);
  else throw new TypeError('normalizeRow: amount must be a number or string');
  if (!isFinite(amount)) throw new TypeError('normalizeRow: amount must be finite');
  return {
    date: obj.date,
    category: obj.category,
    region: obj.region !== undefined ? obj.region : 'NA',
    amount: amount,
    description: obj.description !== undefined ? obj.description : ''
  };
}

export function validateRow(row) {
  const errors = [];
  if (row.date === undefined) errors.push('missing date');
  else if (!isValidDate(row.date)) errors.push('bad date: ' + row.date);
  if (row.category === undefined || row.category === '') errors.push('missing category');
  if (row.region !== undefined && REGIONS.indexOf(row.region) === -1) errors.push('unknown region: ' + row.region);
  if (row.amount === undefined) errors.push('missing amount');
  else if (typeof row.amount !== 'number' || !isFinite(row.amount)) errors.push('amount is not a finite number');
  return errors;
}

export function filterByPeriod(rows, startDate, endDate) {
  if (!isValidDate(startDate) || !isValidDate(endDate)) throw new TypeError('filterByPeriod: dates must look like YYYY-MM-DD');
  if (startDate > endDate) throw new RangeError('filterByPeriod: start is after end');
  const out = [];
  for (let i = 0; i < rows.length; i++) {
    const d = rows[i].date;
    if (!isValidDate(d)) throw new TypeError('filterByPeriod: row date "' + d + '" is malformed');
    if (d >= startDate && d <= endDate) out.push(rows[i]);
  }
  return out;
}

export function sumColumn(rows, column) {
  let total = 0;
  for (let i = 0; i < rows.length; i++) {
    const value = rows[i][column];
    if (typeof value !== 'number' || !isFinite(value)) throw new TypeError('sumColumn: "' + column + '" is not a finite number on row ' + i);
    total += value;
  }
  return total;
}

export function creditSummary(rows) {
  const out = { count: 0, total: 0 };
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].amount >= 0) {
      out.count++;
      out.total += rows[i].amount;
    }
  }
  return out;
}

export function debitSummary(rows) {
  const out = { count: 0, total: 0 };
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].amount < 0) {
      out.count++;
      out.total += rows[i].amount;
    }
  }
  return out;
}

export function aggregateByCategory(rows, categoryField = 'category') {
  const buckets = [];
  for (let i = 0; i < rows.length; i++) {
    const name = rows[i][categoryField];
    let bucket = null;
    for (let j = 0; j < buckets.length; j++) {
      if (buckets[j].name === name) {
        bucket = buckets[j];
        break;
      }
    }
    if (bucket === null) {
      bucket = { name, count: 0, total: 0 };
      buckets.push(bucket);
    }
    bucket.count++;
    bucket.total += rows[i].amount;
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
  const buckets = REGIONS.map(region => ({ label: region, total: 0, count: 0 }));
  for (let i = 0; i < rows.length; i++) {
    const region = rows[i].region || 'NA';
    if (!REGIONS.includes(region)) continue;
    buckets[REGIONS.indexOf(region)].total += rows[i].amount;
    buckets[REGIONS.indexOf(region)].count++;
  }
  return buckets;
}

export function statusForDaysLate(days) {
  if (typeof days !== 'number' || isNaN(days)) throw new TypeError('statusForDaysLate: expects a number of days');
  if (days <= 0) return 'paid';
  if (days <= 15) return 'due';
  if (days <= 45) return 'overdue';
  return 'collections';
}

function padRight(s, width) {
  while (s.length < width) s += ' ';
  return s;
}

function padLeft(s, width) {
  while (s.length < width) s = ' ' + s;
  return s;
}

function columnIsNumeric(values) {
  return values.every(value => typeof value === 'number' && isFinite(value));
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
export function topExpenses(rows, n = 5) {
  if (typeof n !== 'number' || n < 0) throw new RangeError('topExpenses: n must be a non-negative number');
  const sorted = rows.slice().sort((a, b) => b.amount - a.amount);
  return sorted.slice(0, Math.min(n, sorted.length));
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

