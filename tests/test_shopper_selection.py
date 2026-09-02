"""Frontend multi-product selection stays in memory and never writes inventory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_APP_JS = Path(__file__).resolve().parents[1] / "human_ui" / "app.js"


def _sneakers() -> dict[str, Any]:
    """Return one catalog-shaped selectable product."""
    return {
        "record_id": "rec-sneakers",
        "id": "FAS-001",
        "sku": "FAS-001",
        "name": "Classic Canvas Sneakers",
        "brand": "Northline",
        "price": 59.95,
        "currency": "USD",
        "availability": "in_stock",
        "inventory": 33,
    }


def _jacket() -> dict[str, Any]:
    """Return a second catalog-shaped selectable product."""
    return {
        "record_id": "rec-jacket",
        "id": "FAS-002",
        "sku": "FAS-002",
        "name": "Waterproof Hiking Jacket",
        "brand": "Trailmark",
        "price": 119.0,
        "currency": "USD",
        "availability": "low_stock",
        "inventory": 9,
    }


def _mat() -> dict[str, Any]:
    """Return a third catalog-shaped selectable product."""
    return {
        "record_id": "rec-mat",
        "id": "SPT-001",
        "sku": "SPT-001",
        "name": "Adjustable Yoga Mat",
        "price": 39.99,
        "currency": "USD",
        "inventory": 27,
    }


def _select(items: list[dict[str, Any]], product: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep one entry per record_id; quantity changes only through the stepper."""
    key = product["record_id"]
    if any(item["record_id"] == key for item in items):
        return items
    return [*items, {**product, "quantity": 1}]


def _quantity(items: list[dict[str, Any]], key: str, delta: int) -> list[dict[str, Any]]:
    """Change only frontend quantity and never go below one unit."""
    changed: list[dict[str, Any]] = []
    for item in items:
        if item["record_id"] != key:
            changed.append(item)
            continue
        changed.append({**item, "quantity": max(1, int(item["quantity"]) + delta)})
    return changed


def _remove(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Drop one selected product and leave the rest untouched."""
    return [item for item in items if item["record_id"] != key]


def test_selecting_one_product_stays_in_selection_state() -> None:
    """Select creates one frontend row and does not invent a database write."""
    stock = _sneakers()["inventory"]
    selected = _select([], _sneakers())
    assert len(selected) == 1
    assert selected[0]["quantity"] == 1
    assert selected[0]["record_id"] == "rec-sneakers"
    assert selected[0]["inventory"] == stock


def test_selecting_two_and_three_products_keeps_every_choice() -> None:
    """A later Select must not replace earlier selections."""
    selected = _select([], _sneakers())
    selected = _select(selected, _jacket())
    assert [item["name"] for item in selected] == [
        "Classic Canvas Sneakers",
        "Waterproof Hiking Jacket",
    ]
    selected = _select(selected, _mat())
    assert [item["record_id"] for item in selected] == [
        "rec-sneakers",
        "rec-jacket",
        "rec-mat",
    ]


def test_selecting_many_products_has_no_two_item_cap() -> None:
    """Selection is unbounded aside from existing chat source page limits."""
    selected: list[dict[str, Any]] = []
    for index in range(10):
        selected = _select(
            selected,
            {"record_id": f"rec-{index}", "name": f"Product {index}", "price": 1},
        )
    assert len(selected) == 10


def test_selecting_the_same_product_does_not_duplicate() -> None:
    """A second click keeps the existing row so quantity is changed only by +/−."""
    selected = _select([], _sneakers())
    selected[0]["quantity"] = 2
    selected = _select(selected, _sneakers())
    assert len(selected) == 1
    assert selected[0]["quantity"] == 2


def test_quantity_changes_are_frontend_only() -> None:
    """Plus and minus update quantity in memory; catalog inventory is unchanged."""
    selected = _select([], _sneakers())
    catalog_stock = selected[0]["inventory"]
    selected = _quantity(selected, "rec-sneakers", 1)
    selected = _quantity(selected, "rec-sneakers", 1)
    assert selected[0]["quantity"] == 3
    assert selected[0]["inventory"] == catalog_stock
    selected = _quantity(selected, "rec-sneakers", -1)
    assert selected[0]["quantity"] == 2
    selected = _quantity(selected, "rec-sneakers", -5)
    assert selected[0]["quantity"] == 1


def test_remove_drops_only_the_chosen_product() -> None:
    """Remove is per-item and does not clear the rest of the selection."""
    selected = _select(_select(_select([], _sneakers()), _jacket()), _mat())
    selected = _remove(selected, "rec-sneakers")
    assert [item["name"] for item in selected] == [
        "Waterproof Hiking Jacket",
        "Adjustable Yoga Mat",
    ]


def test_shopper_script_has_unbounded_selection_without_inventory_writes() -> None:
    """The chat script owns selection; it must not call payment or stock APIs."""
    script = _APP_JS.read_text(encoding="utf-8")
    assert "selectedProducts" in script
    assert "state.selectedProducts.push" in script
    assert "function changeQuantity" in script
    assert "function removeSelectedProduct" in script
    assert "length >= 2" not in script
    assert "length > 2" not in script
    assert "Razorpay" not in script
    assert "razorpay" not in script
    for name in ("selectProduct", "changeQuantity", "removeSelectedProduct", "addToCart"):
        body = script.split(f"function {name}", 1)[1].split("\nfunction ", 1)[0]
        assert "fetchJson" not in body
        assert "fetch(" not in body
        assert "/inventory" not in body
        assert "update_many" not in body
        assert "stock_quantity" not in body
    review = script.split("function reviewPurchase", 1)[1].split("\nfunction ", 1)[0]
    confirm = script.split("function confirmPurchase", 1)[1].split("\nfunction ", 1)[0]
    cancel = script.split("function cancelPurchase", 1)[1].split("\nfunction ", 1)[0]
    assert "/purchases/review" in review
    assert "/authorize" in confirm
    assert "confirm" in confirm and "true" in confirm
    assert "/cancel" in cancel
    assert "/inventory" not in review
    assert "/inventory" not in confirm
    assert "Razorpay" not in script
    assert (
        "record_id"
        not in script.split("function appendSummaryCard", 1)[1].split("\nfunction ", 1)[0]
    )


def _line_total(item: dict[str, Any]) -> float:
    """Mirror the frontend cent-rounded subtotal used in the purchase summary."""
    return (round(float(item["price"]) * 100) * int(item["quantity"])) / 100


def test_purchase_summary_includes_one_product_quantity_and_total() -> None:
    """A single selected product produces name, quantity, unit price, subtotal, and total."""
    selected = _select([], _sneakers())
    selected[0]["quantity"] = 2
    subtotal = _line_total(selected[0])
    assert selected[0]["name"] == "Classic Canvas Sneakers"
    assert selected[0]["quantity"] == 2
    assert selected[0]["price"] == 59.95
    assert subtotal == 119.90
    assert sum(_line_total(item) for item in selected) == 119.90


def test_purchase_summary_keeps_three_or_more_products_and_quantities() -> None:
    """Multi-product selection remains unbounded and totals every line."""
    selected = _select([], _sneakers())
    selected = _select(selected, _jacket())
    selected = _select(selected, _mat())
    selected[0]["quantity"] = 2
    selected[2]["quantity"] = 3
    totals = [_line_total(item) for item in selected]
    assert [item["name"] for item in selected] == [
        "Classic Canvas Sneakers",
        "Waterproof Hiking Jacket",
        "Adjustable Yoga Mat",
    ]
    assert [item["quantity"] for item in selected] == [2, 1, 3]
    assert totals == [119.90, 119.00, 119.97]
    assert sum(totals) == 358.87


def test_cart_and_review_helpers_do_not_change_catalog_inventory_fields() -> None:
    """Cart is a local copy; review/cancel live in purchase functions, not stock writes."""
    selected = _select(_select([], _sneakers()), _jacket())
    cart = [{**item} for item in selected]
    assert cart[0]["inventory"] == 33
    selected[0]["quantity"] = 5
    assert cart[0]["quantity"] == 1
    assert selected[0]["inventory"] == 33
