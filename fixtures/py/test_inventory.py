"""Behavior-pinning tests for the warehouse inventory module.

These are the regression tests ops runs before every restock-email
deploy. They intentionally assert exact values, messages, and orderings:
if the morning report changes by one character, we want to know.
"""

import pytest

from inventory import (
    FREIGHT_FLAT_FEE,
    LOW_STOCK_UNITS,
    OVERSTOCK_UNITS,
    REORDER_MARGIN,
    InsufficientStockError,
    Inventory,
    InventoryError,
    Order,
    OrderLine,
    Product,
    apply_order,
    busiest_area,
    classify_stock,
    discount_tier,
    format_stock_report,
    order_total,
    pad_center,
    parse_order,
    parse_sku,
    plan_restock,
    priority_for_status,
    reconcile,
    tally_by_flag,
    valuate,
)


def make_inventory():
    inv = Inventory("east")
    inv.add_product(Product("WID-000101", "steel widget", 3.375, 10, 0, 12, 400))
    inv.add_product(Product("PRT-000201", "plastic bracket", 2.5, 3, 1, 12, 200))
    inv.add_product(Product("CON-000301", "console panel", 25.0, 500, 0, 12, 450))
    inv.add_product(Product("WID-000102", "widget, large", 5.0, 0, 0, 12, 400))
    return inv


# --------------------------------------------------------------------------
# module constants (the thresholds are contractual)
# --------------------------------------------------------------------------


def test_module_constants():
    assert LOW_STOCK_UNITS == 12
    assert OVERSTOCK_UNITS == 400
    assert REORDER_MARGIN == 6
    assert FREIGHT_FLAT_FEE == 4.5


# --------------------------------------------------------------------------
# parse_sku
# --------------------------------------------------------------------------


def test_parse_sku_normalizes_case_and_space():
    assert parse_sku("wid-000123") == "WID-000123"
    assert parse_sku("  PRT-000456  ") == "PRT-000456"
    assert parse_sku("con-000301") == "CON-000301"


@pytest.mark.parametrize("bad,message", [
    (None, "missing SKU"),
    (123, "SKU must be a string"),
    (["WID-000123"], "SKU must be a string"),
    ("WID-0001234", "SKU must be 10 characters: 'WID-0001234'"),
    ("WID-0001", "SKU must be 10 characters: 'WID-0001'"),
    ("WID0000123", "SKU must contain exactly one dash: 'WID0000123'"),
    ("W-D-000123", "SKU must contain exactly one dash: 'W-D-000123'"),
    ("XXX-000123", "unknown area code: 'XXX'"),
    ("WID-00012A", "bad SKU number part: '00012A'"),
    ("WID-00012", "SKU must be 10 characters: 'WID-00012'"),
    ("WID-00012a", "bad SKU number part: '00012A'"),
])
def test_parse_sku_rejects(bad, message):
    with pytest.raises(InventoryError) as excinfo:
        parse_sku(bad)
    assert str(excinfo.value) == message


# --------------------------------------------------------------------------
# classify_stock
# --------------------------------------------------------------------------


def test_classify_stock_boundaries():
    assert classify_stock(None) == "UNKNOWN"
    assert classify_stock(0) == "STOCKOUT"
    assert classify_stock(-5) == "STOCKOUT"
    assert classify_stock(1) == "LOW"
    assert classify_stock(12) == "LOW"
    assert classify_stock(13) == "NORMAL"
    assert classify_stock(400) == "NORMAL"
    assert classify_stock(401) == "OVERSTOCK"
    assert classify_stock(9999) == "OVERSTOCK"


def test_classify_stock_custom_level():
    assert classify_stock(2, 2) == "LOW"
    assert classify_stock(3, 2) == "NORMAL"
    assert classify_stock(0, 2) == "STOCKOUT"


# --------------------------------------------------------------------------
# priority_for_status / discount_tier
# --------------------------------------------------------------------------


def test_priority_for_status_mapping():
    assert priority_for_status("STOCKOUT") == 1
    assert priority_for_status("LOW") == 2
    assert priority_for_status("BACKORDER") == 3
    assert priority_for_status("RECOUNT") == 4
    assert priority_for_status("NORMAL") == 5


def test_priority_for_status_normalizes_and_defaults():
    assert priority_for_status(" low ") == 2
    assert priority_for_status("Backorder") == 3
    assert priority_for_status("MYSTERY") == 9
    assert priority_for_status("") == 9
    assert priority_for_status(123) == 9
    assert priority_for_status(None) == 9


def test_discount_tier_boundaries():
    assert discount_tier(-3) == 0.0
    assert discount_tier(0) == 0.0
    assert discount_tier(9) == 0.0
    assert discount_tier(10) == 0.05
    assert discount_tier(49) == 0.05
    assert discount_tier(50) == 0.10
    assert discount_tier(99) == 0.10
    assert discount_tier(100) == 0.15
    assert discount_tier(1000) == 0.15


# --------------------------------------------------------------------------
# pad_center
# --------------------------------------------------------------------------


def test_pad_center_even_and_odd_padding():
    assert pad_center("AB", 6) == "  AB  "
    assert pad_center("ABC", 6) == " ABC  "
    assert pad_center("", 3) == "   "


def test_pad_center_exact_width_and_truncation():
    assert pad_center("ABCDEF", 6) == "ABCDEF"
    assert pad_center("ABCDEFG", 6) == "ABCDE+"
    assert pad_center("ABCDEFGHIJ", 6) == "ABCDE+"


# --------------------------------------------------------------------------
# Product
# --------------------------------------------------------------------------


def test_product_defaults_and_normalization():
    p = Product("prt-000777", "spacer")
    assert p.sku == "PRT-000777"
    assert p.name == "spacer"
    assert p.unit_cost == 0.0
    assert p.on_hand == 0
    assert p.allocated == 0
    assert p.reorder_level == 12
    assert p.max_level == 400
    assert p.available() == 0
    assert p.is_low() is True


def test_product_available_and_is_low_boundary():
    assert Product("PRT-000778", "x", 1.0, 13).is_low() is False
    assert Product("PRT-000779", "x", 1.0, 12).is_low() is True
    p = Product("PRT-000780", "x", 1.0, 10, 4)
    assert p.available() == 6


@pytest.mark.parametrize("sku,message", [
    (None, "missing SKU"),
    (7, "SKU must be a string"),
    ("SHORT-1", "SKU must be 10 characters: 'SHORT-1'"),
    ("W-D-000123", "SKU must contain exactly one dash: 'W-D-000123'"),
    ("ZZZ-000123", "unknown area code: 'ZZZ'"),
    ("WID-12345", "SKU must be 10 characters: 'WID-12345'"),
    ("WID-00012X", "bad SKU number part: '00012X'"),
])
def test_product_rejects_bad_sku(sku, message):
    with pytest.raises(InventoryError) as excinfo:
        Product(sku, "name")
    assert str(excinfo.value) == message


def test_product_rejects_missing_name():
    with pytest.raises(InventoryError) as excinfo:
        Product("WID-000101", None)
    assert str(excinfo.value) == "missing name"


# --------------------------------------------------------------------------
# Inventory basics
# --------------------------------------------------------------------------


def test_inventory_empty_state():
    inv = Inventory("east")
    assert inv.name == "east"
    assert inv.products == {}
    assert inv.moves == []
    assert inv.sorted_skus() == []
    assert inv.total_units() == 0
    assert inv.total_value() == 0.0
    assert inv.low_stock() == []
    assert busiest_area(inv) is None


def test_add_and_get_products():
    inv = make_inventory()
    assert inv.sorted_skus() == ["CON-000301", "PRT-000201", "WID-000101", "WID-000102"]
    assert inv.get("WID-000101") is inv.products["WID-000101"]
    assert inv.get("wid-000101") is inv.products["WID-000101"]
    assert inv.get("CON-000999") is None
    assert inv.get(None) is None
    assert inv.get(42) is None


def test_add_product_rejects_duplicate_and_none():
    inv = make_inventory()
    with pytest.raises(InventoryError) as excinfo:
        inv.add_product(Product("WID-000101", "again"))
    assert str(excinfo.value) == "duplicate SKU: WID-000101"
    with pytest.raises(InventoryError) as excinfo:
        inv.add_product(None)
    assert str(excinfo.value) == "product required"


def test_totals_and_low_stock():
    inv = make_inventory()
    assert inv.total_units() == 513
    assert inv.total_value() == 12541.25
    assert inv.low_stock() == ["PRT-000201", "WID-000101", "WID-000102"]


def test_total_value_rounds_to_cents():
    inv = Inventory("odd")
    inv.add_product(Product("PRT-000801", "third-cent part", 3.375, 3))
    assert inv.total_value() == 10.12  # 10.125 rounds down to cents
    inv.add_product(Product("WID-000802", "third-cent part b", 3.375, 1))
    assert inv.total_value() == 13.5   # 10.125 + 3.375 = 13.5 exactly


# --------------------------------------------------------------------------
# receive / allocate / release / adjust / writeoff
# --------------------------------------------------------------------------


def test_receive_books_stock_and_move():
    inv = make_inventory()
    assert inv.receive("wid-000101", 5) == 15
    assert inv.products["WID-000101"].on_hand == 15
    assert inv.moves == [("IN", "WID-000101", 5)]
    assert inv.receive("CON-000301", 1) == 501


@pytest.mark.parametrize("sku,message", [
    (None, "missing SKU"),
    (9, "SKU must be a string"),
    ("WID-0001", "SKU must be 10 characters: 'WID-0001'"),
    ("W-D-000123", "SKU must contain exactly one dash: 'W-D-000123'"),
    ("QQQ-000123", "unknown area code: 'QQQ'"),
    ("WID-0001Z", "SKU must be 10 characters: 'WID-0001Z'"),
    ("WID-0001ZZ", "bad SKU number part: '0001ZZ'"),
    ("CON-000999", "unknown SKU: CON-000999"),
])
def test_receive_rejects_bad_sku(sku, message):
    inv = make_inventory()
    with pytest.raises(InventoryError) as excinfo:
        inv.receive(sku, 1)
    assert str(excinfo.value) == message


@pytest.mark.parametrize("qty,message", [
    (None, "missing quantity"),
    ("5", "quantity must be an integer"),
    (2.5, "quantity must be an integer"),
    (True, "quantity must be an integer"),
    (0, "quantity must be positive, got 0"),
    (-2, "quantity must be positive, got -2"),
])
def test_receive_rejects_bad_qty(qty, message):
    inv = make_inventory()
    with pytest.raises(InventoryError) as excinfo:
        inv.receive("WID-000101", qty)
    assert str(excinfo.value) == message


def test_allocate_reserves_and_rejects_over_allocation():
    inv = make_inventory()
    assert inv.allocate("PRT-000201", 2) == 3  # exact fit: available was 2
    assert inv.products["PRT-000201"].available() == 0
    assert inv.moves == [("OUT", "PRT-000201", 2)]
    with pytest.raises(InsufficientStockError) as excinfo:
        inv.allocate("PRT-000201", 1)
    assert str(excinfo.value) == "insufficient stock for PRT-000201: want 1, have 0"
    with pytest.raises(InsufficientStockError) as excinfo:
        inv.allocate("WID-000101", 11)
    assert str(excinfo.value) == "insufficient stock for WID-000101: want 11, have 10"
    with pytest.raises(InventoryError) as excinfo:
        inv.allocate("CON-000999", 1)
    assert str(excinfo.value) == "unknown SKU: CON-000999"
    with pytest.raises(InventoryError) as excinfo:
        inv.allocate("WID-000101", 0)
    assert str(excinfo.value) == "quantity must be positive, got 0"
    assert inv.allocate("WID-000101", 1) == 1  # qty of exactly 1 is legal


def test_release_returns_allocation():
    inv = make_inventory()
    inv.allocate("WID-000101", 4)
    assert inv.release("WID-000101", 4) == 0
    inv.allocate("WID-000101", 1)
    assert inv.release("WID-000101", 1) == 0  # qty of exactly 1 is legal
    assert inv.products["WID-000101"].available() == 10
    assert inv.moves[-1] == ("REL", "WID-000101", 1)


def test_release_rejects_over_release_and_bad_qty():
    inv = make_inventory()
    inv.allocate("WID-000101", 2)
    with pytest.raises(InventoryError) as excinfo:
        inv.release("WID-000101", 3)
    assert str(excinfo.value) == "cannot release 3 for WID-000101: only 2 allocated"
    with pytest.raises(InventoryError) as excinfo:
        inv.release("WID-000101", 0)
    assert str(excinfo.value) == "quantity must be positive, got 0"
    with pytest.raises(InventoryError) as excinfo:
        inv.release("WID-000101", None)
    assert str(excinfo.value) == "missing quantity"
    with pytest.raises(InventoryError) as excinfo:
        inv.release("CON-000999", 1)
    assert str(excinfo.value) == "unknown SKU: CON-000999"


def test_adjust_sets_count_and_logs_delta():
    inv = make_inventory()
    assert inv.adjust("WID-000101", 8) == 8
    assert inv.products["WID-000101"].on_hand == 8
    assert inv.moves == [("ADJ", "WID-000101", -2)]
    assert inv.adjust("WID-000101", 0) == 0
    assert inv.moves[-1] == ("ADJ", "WID-000101", -8)


def test_adjust_rejects_negative_none_bool_and_unknown():
    inv = make_inventory()
    with pytest.raises(InventoryError) as excinfo:
        inv.adjust("WID-000101", -1)
    assert str(excinfo.value) == "count cannot go negative"
    with pytest.raises(InventoryError) as excinfo:
        inv.adjust("WID-000101", None)
    assert str(excinfo.value) == "missing count"
    with pytest.raises(InventoryError) as excinfo:
        inv.adjust("WID-000101", True)
    assert str(excinfo.value) == "count must be an integer"
    with pytest.raises(InventoryError) as excinfo:
        inv.adjust("CON-000999", 1)
    assert str(excinfo.value) == "unknown SKU: CON-000999"


def test_writeoff_removes_stock():
    inv = make_inventory()
    assert inv.writeoff("PRT-000201", 1) == 2  # qty of exactly 1 is legal
    assert inv.writeoff("WID-000101", 9) == 1
    assert inv.products["WID-000101"].on_hand == 1
    assert inv.moves == [("WR", "PRT-000201", 1), ("WR", "WID-000101", 9)]


def test_writeoff_rejects_too_much_and_bad_qty():
    inv = make_inventory()
    with pytest.raises(InventoryError) as excinfo:
        inv.writeoff("WID-000101", 11)
    assert str(excinfo.value) == "cannot write off 11 for WID-000101: only 10 on hand"
    with pytest.raises(InventoryError) as excinfo:
        inv.writeoff("WID-000101", 0)
    assert str(excinfo.value) == "quantity must be positive, got 0"
    with pytest.raises(InventoryError) as excinfo:
        inv.writeoff("CON-000999", 1)
    assert str(excinfo.value) == "unknown SKU: CON-000999"


# --------------------------------------------------------------------------
# orders: OrderLine, Order, parse_order
# --------------------------------------------------------------------------


def test_order_line_normalizes_and_validates():
    line = OrderLine("wid-000101", 4)
    assert line.sku == "WID-000101"
    assert line.qty == 4
    with pytest.raises(InventoryError) as excinfo:
        OrderLine("WID-000101", None)
    assert str(excinfo.value) == "missing quantity"
    with pytest.raises(InventoryError) as excinfo:
        OrderLine("WID-000101", True)
    assert str(excinfo.value) == "quantity must be an integer"
    with pytest.raises(InventoryError) as excinfo:
        OrderLine("WID-000101", 0)
    assert str(excinfo.value) == "quantity must be positive, got 0"
    with pytest.raises(InventoryError) as excinfo:
        OrderLine(None, 1)
    assert str(excinfo.value) == "missing SKU"
    with pytest.raises(InventoryError) as excinfo:
        OrderLine("bad", 1)
    assert str(excinfo.value) == "SKU must be 10 characters: 'bad'"


def test_from_row_parses_and_normalizes():
    line = OrderLine.from_row("wid-000101, 4")
    assert line.sku == "WID-000101"
    assert line.qty == 4
    assert OrderLine.from_row("CON-000301,12").qty == 12
    assert OrderLine.from_row("WID-000101,1").qty == 1


@pytest.mark.parametrize("row,message", [
    (None, "missing row"),
    (5, "row must be a string"),
    ("WID-000101", "row must look like 'SKU,qty': 'WID-000101'"),
    ("WID-000101,2,3", "row must look like 'SKU,qty': 'WID-000101,2,3'"),
    ("XXX-000123,1", "unknown area code: 'XXX'"),
    ("WID-00012A,1", "bad SKU number part: '00012A'"),
    ("WID-000101,abc", "quantity must be an integer: 'abc'"),
    ("WID-000101,0", "quantity must be positive, got 0"),
    ("WID-000101,-4", "quantity must be positive, got -4"),
])
def test_from_row_rejects(row, message):
    with pytest.raises(InventoryError) as excinfo:
        OrderLine.from_row(row)
    assert str(excinfo.value) == message


def test_order_build_and_total_units():
    order = Order("SO-1")
    assert order.status == "NEW"
    assert order.lines == []
    assert order.total_units() == 0
    order.add_line(OrderLine("WID-000101", 3))
    order.add_line(OrderLine("WID-000102", 2))
    assert order.total_units() == 5
    with pytest.raises(InventoryError) as excinfo:
        Order(None)
    assert str(excinfo.value) == "missing order number"
    with pytest.raises(InventoryError) as excinfo:
        order.add_line(None)
    assert str(excinfo.value) == "missing line"


def test_parse_order_skips_comments_and_blanks():
    text = "# nightly batch\n\nwid-000101, 3\n  WID-000102,2  \n# end"
    order = parse_order("SO-1", text)
    assert order.number == "SO-1"
    assert [(l.sku, l.qty) for l in order.lines] == [("WID-000101", 3), ("WID-000102", 2)]
    assert order.total_units() == 5


def test_parse_order_empty_and_none_text():
    assert parse_order("SO-3", "").lines == []
    assert parse_order("SO-4", None).lines == []
    assert parse_order("SO-4", None).total_units() == 0


@pytest.mark.parametrize("row", ["WID-000101,x", "WID-000101", "WID-000101,0", "XXX-000123,1"])
def test_parse_order_aborts_on_bad_row(row):
    with pytest.raises(InventoryError) as excinfo:
        parse_order("SO-9", row)
    assert str(excinfo.value) == "order SO-9: bad row %r" % row


# --------------------------------------------------------------------------
# apply_order
# --------------------------------------------------------------------------


def test_apply_order_reserves_all_or_nothing():
    inv = make_inventory()
    order = Order("SO-20")
    order.add_line(OrderLine("WID-000101", 4))
    order.add_line(OrderLine("PRT-000201", 2))
    assert apply_order(inv, order) == 6
    assert order.status == "RESERVED"
    assert inv.products["WID-000101"].allocated == 4
    assert inv.products["PRT-000201"].allocated == 3
    assert inv.moves == [("OUT", "WID-000101", 4), ("OUT", "PRT-000201", 2)]


def test_apply_order_exact_fit_allowed():
    inv = make_inventory()
    order = Order("SO-21")
    order.add_line(OrderLine("WID-000101", 10))  # exactly all available
    assert apply_order(inv, order) == 10
    assert inv.products["WID-000101"].available() == 0


def test_apply_order_failure_leaves_no_trace():
    inv = make_inventory()
    order = Order("SO-22")
    order.add_line(OrderLine("WID-000101", 2))
    order.add_line(OrderLine("CON-000301", 501))  # 500 on hand, want 501
    with pytest.raises(InsufficientStockError) as excinfo:
        apply_order(inv, order)
    assert str(excinfo.value) == "insufficient stock for CON-000301: want 501, have 500"
    assert order.status == "NEW"
    assert inv.products["WID-000101"].allocated == 0
    assert inv.moves == []


def test_apply_order_guards_and_unknown_sku():
    inv = make_inventory()
    with pytest.raises(ValueError) as excinfo:
        apply_order(None, Order("SO-23"))
    assert str(excinfo.value) == "inventory required"
    with pytest.raises(ValueError) as excinfo:
        apply_order(inv, None)
    assert str(excinfo.value) == "order required"
    order = Order("SO-24")
    order.add_line(OrderLine("CON-000999", 1))
    with pytest.raises(InventoryError) as excinfo:
        apply_order(inv, order)
    assert str(excinfo.value) == "unknown SKU: CON-000999"


# --------------------------------------------------------------------------
# plan_restock / valuate / order_total
# --------------------------------------------------------------------------


def test_plan_restock_targets_and_order():
    inv = make_inventory()
    assert plan_restock(inv) == [
        ("PRT-000201", 3, 203),
        ("WID-000101", 10, 396),
        ("WID-000102", 0, 406),
    ]


def test_plan_restock_margin_and_skip_rules():
    inv = Inventory("west")
    inv.add_product(Product("WID-000501", "tight item", 1.0, 5, 0, 5, 5))
    assert plan_restock(inv, margin=0) == []  # target would be 0
    assert plan_restock(inv, margin=1) == [("WID-000501", 5, 1)]
    assert plan_restock(inv) == [("WID-000501", 5, 6)]
    inv.add_product(Product("WID-000502", "full shelf", 1.0, 9, 0, 5, 40))
    # 9 > reorder 5, so the full shelf is never planned; with margin 3
    # the tight item targets 5 - 5 + 3 = 3:
    assert plan_restock(inv, margin=3) == [("WID-000501", 5, 3)]


def test_plan_restock_requires_inventory():
    with pytest.raises(ValueError) as excinfo:
        plan_restock(None)
    assert str(excinfo.value) == "inventory required"


def test_valuate_applies_volume_discounts():
    inv = make_inventory()
    # WID-000101: 10 * 3.375 * 0.95 = 32.0625
    # PRT-000201: 3 * 2.5 * 1.0 = 7.5
    # CON-000301: 500 * 25.0 * 0.85 = 10625.0
    # WID-000102: 0
    assert valuate(inv) == 10664.56
    assert valuate(Inventory()) == 0.0


def test_valuate_requires_inventory():
    with pytest.raises(ValueError) as excinfo:
        valuate(None)
    assert str(excinfo.value) == "inventory required"


def make_priced_inventory():
    inv = Inventory("priced")
    inv.add_product(Product("WID-000901", "plain", 100.0, 50))
    inv.add_product(Product("WID-000902", "odd", 3.345, 50))
    inv.add_product(Product("WID-000903", "flat", 125.0, 50))
    inv.add_product(Product("WID-000904", "exact3", 125.0625, 50))
    return inv


def one_line_order(sku, qty):
    order = Order("SO-PR")
    order.add_line(OrderLine(sku, qty))
    return order


def test_order_total_below_threshold_adds_freight():
    inv = make_priced_inventory()
    assert order_total(inv, one_line_order("WID-000901", 1)) == 104.5
    assert order_total(inv, one_line_order("WID-000902", 3)) == 14.54


def test_order_total_at_and_above_threshold_is_free():
    inv = make_priced_inventory()
    assert order_total(inv, one_line_order("WID-000903", 2)) == 250.0
    assert order_total(inv, one_line_order("WID-000904", 2)) == 250.12


def test_order_total_guards_and_unknown_sku():
    inv = make_priced_inventory()
    with pytest.raises(ValueError) as excinfo:
        order_total(None, one_line_order("WID-000901", 1))
    assert str(excinfo.value) == "inventory required"
    with pytest.raises(ValueError) as excinfo:
        order_total(inv, None)
    assert str(excinfo.value) == "order required"
    with pytest.raises(InventoryError) as excinfo:
        order_total(inv, one_line_order("CON-000999", 1))
    assert str(excinfo.value) == "unknown SKU: CON-000999"


# --------------------------------------------------------------------------
# tally_by_flag / busiest_area / reconcile
# --------------------------------------------------------------------------


def test_tally_by_flag_counts():
    inv = make_inventory()
    assert tally_by_flag(inv) == {"LOW": 2, "STOCKOUT": 1, "OVERSTOCK": 1}
    assert tally_by_flag(Inventory()) == {}


def test_tally_by_flag_requires_inventory():
    with pytest.raises(ValueError) as excinfo:
        tally_by_flag(None)
    assert str(excinfo.value) == "inventory required"


def test_busiest_area_picks_largest_and_breaks_ties_left():
    inv = make_inventory()
    assert busiest_area(inv) == ("CON", 500)
    tie = Inventory("tie")
    tie.add_product(Product("PRT-000601", "a", 1.0, 7))
    tie.add_product(Product("WID-000602", "b", 1.0, 7))
    assert busiest_area(tie) == ("PRT", 7)  # tie: first area code in sort order wins
    zero = Inventory("zero")
    zero.add_product(Product("WID-000603", "c", 1.0, 0))
    assert busiest_area(zero) == ("WID", 0)


def test_busiest_area_requires_inventory():
    with pytest.raises(ValueError) as excinfo:
        busiest_area(None)
    assert str(excinfo.value) == "inventory required"


def test_reconcile_reports_mismatches_only():
    inv = make_inventory()
    assert reconcile(inv, None) == []
    assert reconcile(inv, {}) == []
    assert reconcile(inv, {"WID-000101": 12, "WID-000102": 0}) == [("WID-000101", 10, 12, 2)]
    assert reconcile(inv, {"PRT-000201": 1}) == [("PRT-000201", 3, 1, -2)]


def test_reconcile_requires_inventory():
    with pytest.raises(ValueError) as excinfo:
        reconcile(None, {})
    assert str(excinfo.value) == "inventory required"


# --------------------------------------------------------------------------
# format_stock_report
# --------------------------------------------------------------------------


def test_format_stock_report_exact_csv():
    inv = make_inventory()
    assert format_stock_report(inv) == (
        "sku,name,on_hand,available,flag\n"
        "CON-000301,console panel,500,500,OVERSTOCK\n"
        "PRT-000201,plastic bracket,3,2,LOW\n"
        "WID-000101,steel widget,10,10,LOW\n"
        "WID-000102,widget; large,0,0,STOCKOUT"
    )


def test_format_stock_report_hides_zero_rows_on_demand():
    inv = make_inventory()
    assert format_stock_report(inv, include_zero=False) == (
        "sku,name,on_hand,available,flag\n"
        "CON-000301,console panel,500,500,OVERSTOCK\n"
        "PRT-000201,plastic bracket,3,2,LOW\n"
        "WID-000101,steel widget,10,10,LOW"
    )
    assert format_stock_report(Inventory()) == "sku,name,on_hand,available,flag"


def test_format_stock_report_requires_inventory():
    with pytest.raises(ValueError) as excinfo:
        format_stock_report(None)
    assert str(excinfo.value) == "inventory required"


# --------------------------------------------------------------------------
# error hierarchy
# --------------------------------------------------------------------------


def test_insufficient_stock_is_inventory_error():
    assert issubclass(InsufficientStockError, InventoryError)
    inv = make_inventory()
    try:
        inv.allocate("WID-000101", 999)
    except InventoryError as exc:
        assert isinstance(exc, InsufficientStockError)
    else:
        pytest.fail("expected InsufficientStockError")
