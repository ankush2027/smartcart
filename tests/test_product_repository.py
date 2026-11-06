"""
Tests for edge.database.product_repository.

Uses a temporary SQLite database via pytest's tmp_path fixture so that
no data is written to the real data/smartcart.db.

Covers:
 16. yolo_class lookup — get_product_by_yolo_class found / missing.
 17. Existing repository behaviour — add_product, get_product_by_barcode,
     get_all_products still work correctly after the yolo_class schema change.
"""

# pyrefly: ignore [missing-import]
import pytest

from edge.database import database as _db_module
from edge.database.database import initialize_database
from edge.database.product_repository import (
    add_product,
    get_all_products,
    get_product_by_barcode,
    get_product_by_yolo_class,
)


# ---------------------------------------------------------------------------
# Fixture: redirect DATABASE_PATH to a temp file for each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """
    Each test gets its own fresh SQLite database in a temp directory.
    The global DATABASE_PATH is monkeypatched so that all repository
    functions use the temp DB instead of data/smartcart.db.
    """
    temp_db = tmp_path / "test_smartcart.db"
    monkeypatch.setattr(_db_module, "DATABASE_PATH", temp_db)
    initialize_database()
    return temp_db


# ---------------------------------------------------------------------------
# 16. yolo_class lookup — get_product_by_yolo_class
# ---------------------------------------------------------------------------

def test_get_product_by_yolo_class_found():
    """
    Adding a product with yolo_class and then looking it up by class name
    must return the correct row.
    """
    add_product(
        name="Maggi ₹10",
        barcode="8901030592632",
        price=10.0,
        expected_weight=75.0,
        yolo_class="Maggi_10",
    )

    row = get_product_by_yolo_class("Maggi_10")

    assert row is not None
    # Returned tuple: (product_id, name, barcode, price, expected_weight, yolo_class)
    assert row[1] == "Maggi ₹10"
    assert row[3] == 10.0
    assert row[4] == 75.0
    assert row[5] == "Maggi_10"


def test_get_product_by_yolo_class_missing():
    """Querying a non-existent yolo_class must return None (not raise)."""
    result = get_product_by_yolo_class("GhostProduct")

    assert result is None


def test_get_product_by_yolo_class_matches_exact_string():
    """yolo_class lookup must be case-sensitive and exact."""
    add_product(
        name="Colgate 150g",
        barcode="8901314005021",
        price=35.0,
        expected_weight=150.0,
        yolo_class="Colgate_150g",
    )

    # Exact match should work.
    assert get_product_by_yolo_class("Colgate_150g") is not None

    # Case variant must NOT match.
    assert get_product_by_yolo_class("colgate_150g") is None
    assert get_product_by_yolo_class("COLGATE_150G") is None


def test_get_product_by_yolo_class_distinguishes_multiple_products():
    """
    With multiple products in the DB, each lookup returns only its own row.
    """
    add_product("Maggi ₹10",  "BC001", 10.0,  75.0,  yolo_class="Maggi_10")
    add_product("Maggi ₹20",  "BC002", 20.0,  100.0, yolo_class="Maggi_20")
    add_product("CocaCola",   "BC003", 35.0,  330.0, yolo_class="CocaCola")

    row_maggi10  = get_product_by_yolo_class("Maggi_10")
    row_maggi20  = get_product_by_yolo_class("Maggi_20")
    row_cocacola = get_product_by_yolo_class("CocaCola")

    assert row_maggi10[5]  == "Maggi_10"
    assert row_maggi20[5]  == "Maggi_20"
    assert row_cocacola[5] == "CocaCola"

    # Cross-check that they have different product_ids.
    ids = {row_maggi10[0], row_maggi20[0], row_cocacola[0]}
    assert len(ids) == 3


# ---------------------------------------------------------------------------
# 17. Existing repository behaviour after schema change
# ---------------------------------------------------------------------------

def test_add_product_without_yolo_class_still_works():
    """
    add_product must still work when yolo_class is omitted (defaults to None).
    This preserves backward compatibility.
    """
    add_product(
        name="Legacy Product",
        barcode="LEGACY_001",
        price=5.0,
        expected_weight=50.0,
        # yolo_class not supplied — should default to None
    )

    row = get_product_by_barcode("LEGACY_001")

    assert row is not None
    assert row[1] == "Legacy Product"
    assert row[5] is None     # yolo_class is NULL


def test_add_product_with_yolo_class_stores_correctly():
    """add_product must persist yolo_class when explicitly provided."""
    add_product(
        name="Lays ₹10",
        barcode="LAYS_001",
        price=10.0,
        expected_weight=50.0,
        yolo_class="Lays",
    )

    row = get_product_by_barcode("LAYS_001")

    assert row is not None
    assert row[5] == "Lays"


def test_get_product_by_barcode_returns_correct_row():
    """get_product_by_barcode still works and returns the yolo_class column."""
    add_product("Bru ₹20", "BRU_020", 20.0, 18.0, yolo_class="Bru_20")

    row = get_product_by_barcode("BRU_020")

    assert row is not None
    # (product_id, name, barcode, price, expected_weight, yolo_class)
    assert row[2] == "BRU_020"
    assert row[5] == "Bru_20"


def test_get_product_by_barcode_missing_returns_none():
    """get_product_by_barcode returns None for unknown barcodes."""
    result = get_product_by_barcode("NO_SUCH_BARCODE")

    assert result is None


def test_get_all_products_returns_all_rows():
    """get_all_products returns every inserted product in product_id order."""
    add_product("Maggi ₹10",  "BC001", 10.0,  75.0,  yolo_class="Maggi_10")
    add_product("Maggi ₹20",  "BC002", 20.0,  100.0, yolo_class="Maggi_20")
    add_product("Lifebuoy",   "BC003", 40.0,  100.0, yolo_class="Lifebuoy_40")

    rows = get_all_products()

    assert len(rows) == 3
    yolo_classes = [r[5] for r in rows]
    assert "Maggi_10"    in yolo_classes
    assert "Maggi_20"    in yolo_classes
    assert "Lifebuoy_40" in yolo_classes


def test_get_all_products_empty_database():
    """get_all_products returns an empty list when no products have been added."""
    rows = get_all_products()

    assert rows == []


def test_initialize_database_migration_is_safe():
    """
    Calling initialize_database() twice must not raise and must not
    duplicate rows or drop data.  This validates the migration guard.
    """
    add_product("TestProduct", "TEST_001", 1.0, 10.0, yolo_class="TestClass")

    # Second call simulates an app restart against an existing DB.
    initialize_database()

    rows = get_all_products()

    # The existing row must still exist and must not be duplicated.
    assert len(rows) == 1
    assert rows[0][5] == "TestClass"
