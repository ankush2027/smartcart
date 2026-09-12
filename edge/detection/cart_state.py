"""
In-memory cart state for SmartCart.

Tracks which products (by product_id / yolo_class) are currently
believed to be physically inside the cart.

Responsibilities:
    - Maintain a quantity-aware set of CartItems.
    - Provide a safe snapshot for use as BEFORE state during REMOVE resolution.

Non-responsibilities:
    - Does NOT touch SQLite or billing.
    - Does NOT perform YOLO, weight, or camera logic.
    - Does NOT contain global or singleton state — callers construct
      their own CartState instances.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass
class CartItem:
    """
    Represents one distinct product line inside the cart.

    product_id  – database product identifier (used as the primary key).
    yolo_class  – YOLO class name used later by the ProductResolver.
    quantity    – how many units of this product are currently in the cart.
    """
    product_id: int
    yolo_class: str
    quantity: int


class CartState:
    """
    Pure in-memory representation of what is currently inside the cart.

    Each product_id maps to exactly one CartItem whose `quantity` field
    tracks how many units are present.  When quantity reaches zero the
    item is removed entirely.

    Usage::

        state = CartState()

        state.add(1, "Maggi_10")
        state.add(1, "Maggi_10")   # quantity → 2

        state.remove(1)             # quantity → 1

        before = state.snapshot()   # deep-copied list, safe to mutate

        item = state.get_by_class("Maggi_10")
    """

    def __init__(self) -> None:
        # Internal store: product_id → CartItem
        self._items: dict[int, CartItem] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def add(self, product_id: int, yolo_class: str) -> None:
        """
        Add one unit of a product to the cart.

        If a CartItem for *product_id* already exists its quantity is
        incremented by 1.  Otherwise a new CartItem with quantity=1 is
        created.  Duplicate CartItem records are never created for the
        same product_id.
        """
        if product_id in self._items:
            self._items[product_id].quantity += 1
        else:
            self._items[product_id] = CartItem(
                product_id=product_id,
                yolo_class=yolo_class,
                quantity=1,
            )

    def remove(self, product_id: int) -> bool:
        """
        Remove one unit of a product from the cart.

        Decrements the CartItem's quantity by 1.  When quantity reaches
        zero the CartItem is deleted from the internal store.

        Returns:
            True  – if the product existed and was decremented / removed.
            False – if product_id is not currently in the cart.
                    (No exception is raised for this normal case.)
        """
        if product_id not in self._items:
            return False

        self._items[product_id].quantity -= 1

        if self._items[product_id].quantity == 0:
            del self._items[product_id]

        return True

    def snapshot(self) -> list[CartItem]:
        """
        Return a deep copy of the current cart contents.

        The caller may freely modify the returned list or its CartItem
        objects without affecting the internal CartState.  This snapshot
        is intended to serve as the BEFORE state for REMOVE resolution
        by a future ProductResolver.
        """
        return copy.deepcopy(list(self._items.values()))

    def get_by_class(self, yolo_class: str) -> CartItem | None:
        """
        Return the CartItem whose yolo_class matches *yolo_class*.

        Returns None if no such item exists.

        Note: yolo_class values are assumed to be unique across
        product_ids (one class → one product line).
        """
        for item in self._items.values():
            if item.yolo_class == yolo_class:
                return item
        return None

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Total number of distinct product lines in the cart."""
        return len(self._items)

    def __repr__(self) -> str:  # pragma: no cover
        items = list(self._items.values())
        return f"CartState({items!r})"
