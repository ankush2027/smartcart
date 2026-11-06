"""Lifecycle migration/transition tests; every database is temporary."""
import json
import sqlite3

import pytest
from edge.database import database
from edge.database.catalog_install import DEFAULT_CATALOG, MODEL_CLASSES, CatalogError, install_catalog
from edge.database.product_repository import (add_product, get_all_products,
    get_active_products, get_product_by_id, get_product_by_barcode, get_product_by_yolo_class)
from edge.billing import billing
from edge.cart_session import CartSession
from edge.detection.product_resolver import ProductResolver, Detection
from edge.runtime.cart_runtime import validated_catalog, resume_session
from tests.test_runtime_firebase_sync import _FakeReference, _ErrorReference


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / 'cart.db'
    monkeypatch.setattr(database, 'DATABASE_PATH', path)
    return path


def definition():
    return json.loads(DEFAULT_CATALOG.read_text())


def stage8():
    database.initialize_database()
    add_product('Stage 8 Test Product', 'STAGE8-001', 25, 100, 'Stage8_Test_Product')
    sid = billing.create_session('CART-001')
    billing.assign_customer_to_session(sid, 'test-owner')
    for _ in range(2):
        billing.add_item(sid, 1)
        billing.remove_item(sid, 1)
    return CartSession(ProductResolver(validated_catalog(('Stage8_Test_Product',))), session_id=sid)


@pytest.mark.parametrize('old_schema', [False, True])
def test_migration_fresh_old_and_idempotent(db, old_schema):
    if old_schema:
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE products (product_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, barcode TEXT UNIQUE, price REAL NOT NULL, expected_weight REAL NOT NULL, yolo_class TEXT)')
            c.execute("INSERT INTO products VALUES (7, 'old', NULL, 10, 50, 'old')")
    database.initialize_database()
    if not old_schema:
        add_product('old', None, 10, 50, 'old')
    before = get_all_products()
    database.initialize_database()
    with sqlite3.connect(db) as c:
        columns = c.execute('PRAGMA table_info(products)').fetchall()
        col = next(r for r in columns if r[1] == 'is_active')
        assert col[2:5] == ('INTEGER', 1, '1')
        assert c.execute('SELECT is_active FROM products').fetchall() == [(1,)]
    assert get_all_products() == before == get_active_products()
    assert all(len(row) == 6 for row in before)


def test_stage8_completion_transition_history_and_runtime(db):
    session = stage8()
    logs, product = session.get_session_log(), get_product_by_id(1)
    root = _FakeReference()
    session._firebase_ref = root
    assert session.complete() == 0
    meta = billing.get_session_meta(1)
    assert meta['end_time'] and meta['customer_uid'] == 'test-owner'
    remote = root.node('sessions/1').last_update_data
    assert remote['end_time'] == meta['end_time'] and remote['total_amount'] == 0
    assert remote['customer_uid'] == 'test-owner'
    assert install_catalog(definition(), retire_stage8=True) == 'installed'
    assert get_product_by_id(1) == product
    assert get_product_by_barcode('STAGE8-001') == product
    assert get_product_by_yolo_class('Stage8_Test_Product') == product
    assert session.get_session_log() == logs and len(logs) == 4
    assert billing.get_bill(1) == []
    assert len(get_all_products()) == 7
    active = get_active_products()
    assert len(active) == 6 and {p[0] for p in active} == set(range(2, 8))
    assert {p[5] for p in active} == set(MODEL_CLASSES)
    assert all(p[2] is None for p in active)
    before = get_all_products()
    assert install_catalog(definition(), retire_stage8=True) == 'unchanged'
    assert install_catalog(definition()) == 'unchanged'
    assert get_all_products() == before
    database.initialize_database()
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT is_active FROM products WHERE product_id=1').fetchone() == (0,)
        assert c.execute('SELECT COUNT(*) FROM session_log l LEFT JOIN products p ON p.product_id=l.product_id WHERE p.product_id IS NULL').fetchone() == (0,)
    catalog = validated_catalog(MODEL_CLASSES)
    new_sid = billing.create_session('NEW-CART')
    billing.add_item(new_sid, active[0][0])
    detector = type('Detector', (), {'class_names': MODEL_CLASSES})()
    resumed = resume_session('NEW-CART', detector, None)
    assert resumed.cart_state.snapshot()[0].quantity == 1
    resolver = ProductResolver(catalog)
    assert not resolver.resolve_add([Detection('Stage8_Test_Product', .99)], 100, resumed.cart_state).resolved
    assert not resolver.resolve_remove([], -100, resumed.cart_state).resolved


def test_active_session_blocks_retirement(db):
    stage8()
    before = db.read_bytes()
    with pytest.raises(CatalogError, match='Complete active sessions'):
        install_catalog(definition(), retire_stage8=True)
    assert db.read_bytes() == before


def test_transition_failure_rolls_back_retirement_and_inserts(db):
    session = stage8(); session.complete()
    with sqlite3.connect(db) as c:
        c.execute("CREATE TRIGGER fail BEFORE INSERT ON products WHEN NEW.yolo_class='Lays' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        install_catalog(definition(), retire_stage8=True)
    assert len(get_all_products()) == len(get_active_products()) == 1
    assert len(billing.get_session_log(1)) == 4
    assert billing.get_session_meta(1)['end_time']


def test_completion_firebase_failure_preserves_sqlite(db):
    session = stage8(); session._firebase_ref = _ErrorReference()
    with pytest.raises(Exception):
        session.complete()
    assert billing.get_session_meta(1)['end_time']
    assert billing.get_session_meta(1)['total_amount'] == 0
    assert len(billing.get_session_log(1)) == 4
    assert get_product_by_id(1)[5] == 'Stage8_Test_Product'


def test_unexpected_product_metadata_blocks_transition(db):
    session = stage8(); session.complete()
    with sqlite3.connect(db) as c:
        c.execute("UPDATE products SET name='changed' WHERE product_id=1")
    with pytest.raises(CatalogError, match='unchanged product'):
        install_catalog(definition(), retire_stage8=True)
    assert len(get_active_products()) == 1
