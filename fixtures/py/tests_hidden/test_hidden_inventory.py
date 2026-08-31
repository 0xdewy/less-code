"""Hidden safety net for the py fixture.

Never shown to the reducer (excluded from the prompt spec and from the
frozen verify gate); run only by `lc bench` after a reduction to detect
behaviour changes the visible suite would miss.
"""

import pytest

from inventory import (
    Inventory,
    InventoryError,
    InsufficientStockError,
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


def stocked(**kw):
    inv = Inventory()
    inv.add_product(Product('WID-000001', 'Widget', 2.0, 30))
    inv.add_product(Product('PRT-000002', 'Part', 1.5, 5))
    inv.add_product(Product('CON-000003', 'Connector', 0.5, 0))
    return inv


class TestParsing:
    def test_lowercase_and_padding_normalized(self):
        assert parse_sku('  wid-000001 ') == 'WID-000001'

    def test_two_dashes_rejected(self):
        with pytest.raises(InventoryError, match='exactly one dash'):
            parse_sku('WI-D-00001')

    def test_non_string_rejected(self):
        with pytest.raises(InventoryError, match='SKU must be a string'):
            parse_sku(12345)

    def test_digit_part_must_be_ascii_digits(self):
        with pytest.raises(InventoryError, match='bad SKU number part'):
            parse_sku('WID-00000X')


class TestLadders:
    def test_classify_boundaries(self):
        assert classify_stock(None) == 'UNKNOWN'
        assert classify_stock(0) == 'STOCKOUT'
        assert classify_stock(-4) == 'STOCKOUT'
        assert classify_stock(12) == 'LOW'
        assert classify_stock(13) == 'NORMAL'
        assert classify_stock(400) == 'NORMAL'
        assert classify_stock(401) == 'OVERSTOCK'

    def test_classify_honours_custom_reorder_level(self):
        assert classify_stock(20, 25) == 'LOW'
        assert classify_stock(26, 25) == 'NORMAL'

    def test_priority_is_case_and_space_insensitive(self):
        assert priority_for_status('  low  ') == 2
        assert priority_for_status('STOCKOUT') == 1
        assert priority_for_status('BACKORDER') == 3
        assert priority_for_status('RECOUNT') == 4
        assert priority_for_status('NORMAL') == 5
        assert priority_for_status('WAT') == 9
        assert priority_for_status(None) == 9

    def test_discount_tier_boundaries(self):
        assert [discount_tier(q) for q in (9, 10, 49, 50, 99, 100)] == [
            0.0, 0.05, 0.05, 0.1, 0.1, 0.15
        ]

    def test_pad_center_odd_split_and_truncation(self):
        assert pad_center('ab', 5) == ' ab  '
        assert pad_center('abc', 3) == 'abc'
        assert pad_center('abcdef', 4) == 'abc+'


class TestInventoryOps:
    def test_receive_rejects_bool_quantity(self):
        inv = stocked()
        with pytest.raises(InventoryError, match='quantity must be an integer'):
            inv.receive('WID-000001', True)

    def test_receive_records_move_and_returns_new_on_hand(self):
        inv = stocked()
        assert inv.receive('wid-000001', 5) == 35
        assert inv.moves == [('IN', 'WID-000001', 5)]

    def test_allocate_exact_fit_then_exhausted(self):
        inv = stocked()
        assert inv.allocate('PRT-000002', 5) == 5
        with pytest.raises(InsufficientStockError, match='want 1, have 0'):
            inv.allocate('PRT-000002', 1)

    def test_release_over_allocation_message(self):
        inv = stocked()
        inv.allocate('WID-000001', 4)
        with pytest.raises(InventoryError, match='only 4 allocated'):
            inv.release('WID-000001', 5)
        assert inv.release('WID-000001', 4) == 0

    def test_adjust_negative_rejected_and_delta_recorded(self):
        inv = stocked()
        with pytest.raises(InventoryError, match='count cannot go negative'):
            inv.adjust('WID-000001', -1)
        inv.adjust('WID-000001', 25)
        assert inv.moves[-1] == ('ADJ', 'WID-000001', -5)

    def test_writeoff_cannot_exceed_on_hand(self):
        inv = stocked()
        with pytest.raises(InventoryError, match='only 30 on hand'):
            inv.writeoff('WID-000001', 31)

    def test_duplicate_product_rejected(self):
        inv = stocked()
        with pytest.raises(InventoryError, match='duplicate SKU'):
            inv.add_product(Product('WID-000001', 'Widget again'))

    def test_get_is_forgiving_and_totals_are_exact(self):
        inv = stocked()
        assert inv.get(None) is None and inv.get(7) is None
        assert inv.get(' wid-000001 ').name == 'Widget'
        assert inv.total_units() == 35
        assert inv.total_value() == 67.5
        assert inv.sorted_skus() == ['CON-000003', 'PRT-000002', 'WID-000001']
        assert inv.low_stock() == ['CON-000003', 'PRT-000002']

    def test_product_available_and_is_low(self):
        p = Product('WID-000001', 'W', 1.0, 20, 5)
        assert p.available() == 15
        assert p.is_low() is False
        assert Product('WID-000001', 'W', 1.0, 12).is_low() is True


class TestOrders:
    def test_from_row_trims_and_uppercases(self):
        line = OrderLine.from_row('  wid-000001 , 4 ')
        assert (line.sku, line.qty) == ('WID-000001', 4)

    def test_from_row_bad_qty_message(self):
        with pytest.raises(InventoryError, match='quantity must be an integer'):
            OrderLine.from_row('WID-000001,x')

    def test_parse_order_skips_comments_and_blanks(self):
        order = parse_order(7, '\n# note\nWID-000001,2\n\nPRT-000002,3\n')
        assert [(l.sku, l.qty) for l in order.lines] == [
            ('WID-000001', 2), ('PRT-000002', 3)
        ]
        assert order.total_units() == 5
        assert order.status == 'NEW'

    def test_parse_order_bad_row_aborts_with_context(self):
        with pytest.raises(InventoryError, match="order 7: bad row 'nope'"):
            parse_order(7, 'WID-000001,2\nnope\n')

    def test_apply_order_is_atomic(self):
        inv = stocked()
        order = parse_order(1, 'WID-000001,2\nPRT-000002,99\n')
        with pytest.raises(InsufficientStockError):
            apply_order(inv, order)
        assert inv.get('WID-000001').allocated == 0
        assert inv.moves == []
        assert order.status == 'NEW'

    def test_apply_order_success_sets_status(self):
        inv = stocked()
        order = parse_order(1, 'WID-000001,2\nPRT-000002,3\n')
        assert apply_order(inv, order) == 5
        assert order.status == 'RESERVED'
        assert inv.get('WID-000001').allocated == 2

    def test_apply_order_requires_arguments(self):
        with pytest.raises(ValueError, match='inventory required'):
            apply_order(None, Order(1))
        with pytest.raises(ValueError, match='order required'):
            apply_order(stocked(), None)

    def test_order_total_freight_boundary(self):
        inv = stocked()
        order = parse_order(1, 'WID-000001,5\n')  # 10.0 -> under threshold
        assert order_total(inv, order) == 14.5
        assert order_total(inv, order, freight_free_above=10.0) == 10.0


class TestReports:
    def test_plan_restock_targets(self):
        inv = stocked()
        assert plan_restock(inv) == [
            ('CON-000003', 0, 406), ('PRT-000002', 5, 401)
        ]
        assert plan_restock(inv, margin=0) == [
            ('CON-000003', 0, 400), ('PRT-000002', 5, 395)
        ]

    def test_valuate_applies_volume_discount(self):
        inv = stocked()
        # 30 * 2.0 * 0.95 + 5 * 1.5 + 0 = 64.5
        assert valuate(inv) == 64.5

    def test_tally_and_busiest_area(self):
        inv = stocked()
        assert tally_by_flag(inv) == {'STOCKOUT': 1, 'LOW': 1, 'NORMAL': 1}
        assert busiest_area(inv) == ('WID', 30)
        assert busiest_area(Inventory()) is None

    def test_reconcile_only_reports_mismatches(self):
        inv = stocked()
        assert reconcile(inv, None) == []
        assert reconcile(inv, {'WID-000001': 28, 'PRT-000002': 5}) == [
            ('WID-000001', 30, 28, -2)
        ]

    def test_format_stock_report_escapes_commas_and_filters_zero(self):
        inv = Inventory()
        inv.add_product(Product('WID-000001', 'Widget, large', 1.0, 20))
        inv.add_product(Product('PRT-000002', 'Empty', 1.0, 0))
        full = format_stock_report(inv)
        assert full.splitlines()[0] == 'sku,name,on_hand,available,flag'
        assert 'Widget; large' in full
        assert len(full.splitlines()) == 3
        assert len(format_stock_report(inv, include_zero=False).splitlines()) == 2

    def test_report_helpers_require_inventory(self):
        for fn in (plan_restock, valuate, tally_by_flag, busiest_area,
                   format_stock_report):
            with pytest.raises(ValueError, match='inventory required'):
                fn(None)
