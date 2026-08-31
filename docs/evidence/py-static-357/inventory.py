"""Warehouse stock management for the WMS-1 replacement (module since 2015).

This file started as a flat-file helper for the nightly WMS-1 sync job
written by ops engineering in 2015. It now also backs the morning restock
email, the order-desk reservation flow, and the cycle-count reconciliation
sheet. stdlib only on purpose: the report host still has no pip access.

House conventions, kept for compatibility:
- SKUs look like 'AREA-######' (3-letter area code, dash, 6 digits) and
  are normalized to upper case everywhere on the way in.
- Quantities are plain ints; floats are money only.
- Money is rounded to 2 decimals on the way out, never internally.
"""

__VERSION__ = "2.4.1"
SKU_AREAS = ("WID", "PRT", "CON")
LOW_STOCK_UNITS = 12
OVERSTOCK_UNITS = 400
REORDER_MARGIN = 6
FREIGHT_FLAT_FEE = 4.5
CRITICAL_FLAG = "STOCKOUT"


def _check_sku(sku):
    if sku is None:
        raise InventoryError('missing SKU')
    if not isinstance(sku, str):
        raise InventoryError('SKU must be a string')
    text = sku.strip().upper()
    if len(text) != 10:
        raise InventoryError(f'SKU must be 10 characters: {sku!r}')
    pieces = text.split('-')
    if len(pieces) != 2:
        raise InventoryError(f'SKU must contain exactly one dash: {sku!r}')
    area = pieces[0]
    digits = pieces[1]
    if area not in SKU_AREAS:
        raise InventoryError(f'unknown area code: {area!r}')
    if len(digits) != 6 or not digits.isdigit():
        raise InventoryError(f'bad SKU number part: {digits!r}')
    return text

def _check_qty(qty, text, self):
    if qty is None:
        raise InventoryError('missing quantity')
    if not isinstance(qty, int) or isinstance(qty, bool):
        raise InventoryError('quantity must be an integer')
    if qty <= 0:
        raise InventoryError('quantity must be positive, got %d' % qty)
    product = self.get(text)
    if product is None:
        raise InventoryError(f'unknown SKU: {text}')
    return product

def _check_sku2(sku, message):
    if sku is None:
        raise InventoryError(message)

def _check_inventory(inventory, message):
    if inventory is None:
        raise ValueError(message)

class InventoryError(Exception):
    """Base error for this module. Message text is load-bearing: the
    morning batch greps for it, so do not reword casually."""


class InsufficientStockError(InventoryError):
    """Raised when an order or allocation exceeds available stock."""


def parse_sku(raw):
    """Return the normalized SKU string, or raise InventoryError.

    Legacy sheets sometimes ship SKUs with surrounding spaces or lower
    case letters; we normalize instead of rejecting because the 2016
    supplier feed could not be trusted.
    """
    return _check_sku(raw)


def classify_stock(on_hand, reorder_level=LOW_STOCK_UNITS):
    """Map an on-hand count to a stock flag.

    Thresholds unchanged since 2016 -- the pickers have the colors
    laminated, so changes need a floor meeting first.
    """
    if on_hand is None:
        return "UNKNOWN"
    if on_hand <= 0:
        return CRITICAL_FLAG
    if on_hand <= reorder_level:
        return "LOW"
    if on_hand <= OVERSTOCK_UNITS:
        return "NORMAL"
    return "OVERSTOCK"


def priority_for_status(status):
    """Restock-desk ticket priority, 1 (drop everything) to 5 (routine).

    Unknown statuses land at 9 so they sort last but stay visible.
    """
    s = status.strip().upper() if isinstance(status, str) else status
    return {'STOCKOUT': 1, 'LOW': 2, 'BACKORDER': 3, 'RECOUNT': 4, 'NORMAL': 5}.get(s, 9)


def discount_tier(qty):
    """Volume discount rate for purchasing, by line quantity."""
    return next((_v for _t, _v in ((100, 0.15), (50, 0.1), (10, 0.05)) if qty >= _t), 0.0)


def pad_center(text, width):
    """Center-pad for the fixed-width restock fax (yes, still a fax).

    Overlong text is truncated to width and marked with '+' so the
    night shift can see it was cut.
    """
    if len(text) > width:
        return text[: width - 1] + "+"
    if len(text) == width:
        return text
    total = width - len(text)
    left = total // 2
    right = total - left
    return " " * left + text + " " * right


class Product:
    """A stock item exactly as the WMS sees it."""

    def __init__(
        self,
        sku,
        name,
        unit_cost=0.0,
        on_hand=0,
        allocated=0,
        reorder_level=LOW_STOCK_UNITS,
        max_level=OVERSTOCK_UNITS,
    ):
        text = _check_sku(sku)
        _check_sku2(name, 'missing name')
        self.sku = text
        self.name = name
        self.unit_cost = unit_cost
        self.on_hand = on_hand
        self.allocated = allocated
        self.reorder_level = reorder_level
        self.max_level = max_level

    def available(self):
        """Units free to promise: on hand minus already allocated."""
        return self.on_hand - self.allocated

    def is_low(self):
        """True when at or below the reorder level."""
        return self.on_hand <= self.reorder_level


class Inventory:
    """The book of record for one warehouse."""

    def __init__(self, name="main"):
        self.name = name
        self.products = {}
        self.moves = []

    def add_product(self, product):
        _check_sku2(product, 'product required')
        if product.sku in self.products:
            raise InventoryError(f"duplicate SKU: {product.sku}")
        self.products[product.sku] = product
        return self

    def get(self, sku):
        if sku is None:
            return None
        if not isinstance(sku, str):
            return None
        key = sku.strip().upper()
        if key in self.products:
            return self.products[key]
        return None

    def sorted_skus(self):
        """SKUs in shelf order; pickers walk the aisles alphabetically."""
        return sorted(self.products.keys())

    def receive(self, sku, qty):
        """Book incoming stock; returns the new on-hand count."""
        text = _check_sku(sku)
        product = _check_qty(qty, text, self)
        product.on_hand = product.on_hand + qty
        self.moves.append(("IN", text, qty))
        return product.on_hand

    def allocate(self, sku, qty):
        """Reserve qty against available stock for a customer order."""
        text = _check_sku(sku)
        product = _check_qty(qty, text, self)
        if product.available() < qty:
            raise InsufficientStockError(
                "insufficient stock for %s: want %d, have %d"
                % (text, qty, product.available())
            )
        product.allocated = product.allocated + qty
        self.moves.append(("OUT", text, qty))
        return product.allocated

    def release(self, sku, qty):
        """Give back an allocation (order cancelled before shipping)."""
        _check_sku2(sku, 'missing SKU')
        if not isinstance(sku, str):
            raise InventoryError("SKU must be a string")
        text = sku.strip().upper()
        product = _check_qty(qty, text, self)
        if qty > product.allocated:
            raise InventoryError(
                "cannot release %d for %s: only %d allocated"
                % (qty, text, product.allocated)
            )
        product.allocated = product.allocated - qty
        self.moves.append(("REL", text, qty))
        return product.allocated

    def adjust(self, sku, new_on_hand):
        """Cycle-count correction: set on-hand to the recounted number."""
        _check_sku2(sku, 'missing SKU')
        text = sku.strip().upper() if isinstance(sku, str) else sku
        _check_sku2(new_on_hand, 'missing count')
        if not isinstance(new_on_hand, int) or isinstance(new_on_hand, bool):
            raise InventoryError("count must be an integer")
        if new_on_hand < 0:
            raise InventoryError("count cannot go negative")
        product = self.get(text)
        if product is None:
            raise InventoryError(f"unknown SKU: {text}")
        old = product.on_hand
        product.on_hand = new_on_hand
        self.moves.append(("ADJ", text, new_on_hand - old))
        return product.on_hand

    def writeoff(self, sku, qty):
        """Damage/shrinkage removal straight from on-hand."""
        _check_sku2(sku, 'missing SKU')
        text = sku.strip().upper() if isinstance(sku, str) else sku
        product = _check_qty(qty, text, self)
        if qty > product.on_hand:
            raise InventoryError(
                "cannot write off %d for %s: only %d on hand"
                % (qty, text, product.on_hand)
            )
        product.on_hand = product.on_hand - qty
        self.moves.append(("WR", text, qty))
        return product.on_hand

    def total_units(self):
        """Sum of on-hand across all SKUs (morning email KPI #1)."""
        return sum(self.products[sku].on_hand for sku in self.products)

    def total_value(self):
        """Sum of on-hand times unit cost, no discounts (KPI #2)."""
        total = 0.0
        for sku in self.products:
            product = self.products[sku]
            total = total + product.on_hand * product.unit_cost
        return round(total, 2)

    def low_stock(self):
        """SKUs at or below their reorder level, in shelf order."""
        return [sku for sku in self.sorted_skus() if (product := self.products[sku]).on_hand <= product.reorder_level]


class OrderLine:
    """One SKU line on a customer order."""

    def __init__(self, sku, qty):
        text = _check_sku(sku)
        _check_sku2(qty, 'missing quantity')
        if not isinstance(qty, int) or isinstance(qty, bool):
            raise InventoryError("quantity must be an integer")
        if qty <= 0:
            raise InventoryError("quantity must be positive, got %d" % qty)
        self.sku = text
        self.qty = qty

    @classmethod
    def from_row(cls, row):
        """Build a line from an order-desk 'SKU,qty' text row."""
        _check_sku2(row, 'missing row')
        if not isinstance(row, str):
            raise InventoryError("row must be a string")
        parts = row.split(",")
        if len(parts) != 2:
            raise InventoryError(f"row must look like 'SKU,qty': {row!r}")
        sku_part = parts[0].strip()
        qty_part = parts[1].strip()
        text = _check_sku(sku_part)
        try:
            qty = int(qty_part)
        except ValueError:
            raise InventoryError(f"quantity must be an integer: {qty_part!r}")
        if qty <= 0:
            raise InventoryError("quantity must be positive, got %d" % qty)
        return cls(text, qty)


class Order:
    """A customer order: header plus SKU lines."""

    def __init__(self, number, status="NEW"):
        _check_sku2(number, 'missing order number')
        self.number = number
        self.status = status
        self.lines = []

    def add_line(self, line):
        _check_sku2(line, 'missing line')
        self.lines.append(line)
        return self

    def total_units(self):
        return sum(line.qty for line in self.lines)


def parse_order(number, text):
    """Build an Order from order-desk CSV, one 'SKU,qty' row per line.

    Blank lines and '#' comments are skipped. Any bad row aborts the
    whole parse: half-parsed orders caused the March incident.
    """
    order = Order(number)
    if text is None:
        return order
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        try:
            order.add_line(OrderLine.from_row(line))
        except InventoryError:
            raise InventoryError(f"order {number}: bad row {raw!r}")
    return order


def apply_order(inventory, order):
    """Reserve stock for every line, or raise and change nothing.

    Two passes on purpose so a failure half-way never leaves phantom
    allocations behind (see RETRO-2017-04).
    """
    _check_inventory(inventory, 'inventory required')
    _check_inventory(order, 'order required')
    for line in order.lines:
        product = inventory.get(line.sku)
        if product is None:
            raise InventoryError(f"unknown SKU: {line.sku}")
        if product.available() < line.qty:
            raise InsufficientStockError(
                "insufficient stock for %s: want %d, have %d"
                % (line.sku, line.qty, product.available())
            )
    reserved = 0
    for line in order.lines:
        product = inventory.get(line.sku)
        product.allocated = product.allocated + line.qty
        inventory.moves.append(("OUT", line.sku, line.qty))
        reserved = reserved + line.qty
    order.status = "RESERVED"
    return reserved


def plan_restock(inventory, margin=REORDER_MARGIN):
    """Return [(sku, on_hand, target_qty)] for every product at or below
    its reorder level, in shelf order.

    target = max_level - on_hand + margin (margin covers the sales week
    between ordering and the truck arriving).
    """
    _check_inventory(inventory, 'inventory required')
    plan = []
    for sku in inventory.sorted_skus():
        product = inventory.products[sku]
        on_hand = product.on_hand
        if on_hand > product.reorder_level:
            continue
        target = product.max_level - on_hand + margin
        if target <= 0:
            continue
        plan.append((sku, on_hand, target))
    return plan


def valuate(inventory):
    """Wholesale value of on-hand stock with volume discounts applied."""
    _check_inventory(inventory, 'inventory required')
    total = 0.0
    for sku in inventory.sorted_skus():
        product = inventory.products[sku]
        gross = product.on_hand * product.unit_cost
        rate = discount_tier(product.on_hand)
        total = total + gross * (1.0 - rate)
    return round(total, 2)


def order_total(inventory, order, freight_free_above=250.0):
    """Order subtotal in dollars; small orders carry the flat freight fee."""
    _check_inventory(inventory, 'inventory required')
    _check_inventory(order, 'order required')
    subtotal = 0.0
    for line in order.lines:
        product = inventory.get(line.sku)
        if product is None:
            raise InventoryError(f"unknown SKU: {line.sku}")
        subtotal = subtotal + line.qty * product.unit_cost
    if subtotal >= freight_free_above:
        return round(subtotal, 2)
    return round(subtotal + FREIGHT_FLAT_FEE, 2)


def tally_by_flag(inventory):
    """Count products per stock flag; keys follow classify_stock()."""
    _check_inventory(inventory, 'inventory required')
    counts = {}
    for sku in inventory.sorted_skus():
        product = inventory.products[sku]
        flag = classify_stock(product.on_hand, product.reorder_level)
        if flag in counts:
            counts[flag] = counts[flag] + 1
        else:
            counts[flag] = 1
    return counts


def busiest_area(inventory):
    """(area, units) for the area with most units on hand.

    Ties break alphabetically by area code -- left aisle first.
    """
    _check_inventory(inventory, 'inventory required')
    counts = {}
    for sku in inventory.sorted_skus():
        product = inventory.products[sku]
        area = sku.split("-")[0]
        if area in counts:
            counts[area] = counts[area] + product.on_hand
        else:
            counts[area] = product.on_hand
    best = None
    best_units = -1
    for area in sorted(counts.keys()):
        if counts[area] > best_units:
            best_units = counts[area]
            best = area
    if best is None:
        return None
    return (best, best_units)


def reconcile(inventory, counted):
    """Compare system stock to cycle counts.

    Returns [(sku, system, counted, delta)] for every mismatch, shelf
    order. SKUs missing from the count sheet are treated as recounted
    equal (the sheet only lists what was actually counted).
    """
    _check_inventory(inventory, 'inventory required')
    if counted is None:
        counted = {}
    rows = []
    for sku in inventory.sorted_skus():
        product = inventory.products[sku]
        system_qty = product.on_hand
        shelf = counted.get(sku, system_qty)
        delta = shelf - system_qty
        if delta != 0:
            rows.append((sku, system_qty, shelf, delta))
    return rows


def format_stock_report(inventory, include_zero=True):
    """Render the CSV attached to the morning stock email."""
    _check_inventory(inventory, 'inventory required')
    lines = ["sku,name,on_hand,available,flag"]
    for sku in inventory.sorted_skus():
        product = inventory.products[sku]
        if not include_zero and product.on_hand == 0:
            continue
        flag = classify_stock(product.on_hand, product.reorder_level)
        name = product.name.replace(",", ";")
        lines.append(
            "%s,%s,%d,%d,%s" % (sku, name, product.on_hand, product.available(), flag)
        )
    return "\n".join(lines)
