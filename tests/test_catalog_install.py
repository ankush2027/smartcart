"""All installation tests use isolated SQLite; no real catalog mutation."""
import copy
import json
import sqlite3

import pytest
from edge.database import database
from edge.database import catalog_install as catalog
from edge.billing import billing
from edge.database.product_repository import add_product, get_all_products
from edge.runtime.cart_runtime import validated_catalog


@pytest.fixture
def definition():
    return json.loads(catalog.DEFAULT_CATALOG.read_text())


@pytest.fixture
def complete(definition):
    return definition


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATABASE_PATH', tmp_path / 'catalog.db')
    database.initialize_database()
    return database.DATABASE_PATH


def test_exact_approved_definition_and_assumption_warning(definition):
    products = definition['products']
    assert tuple(p['yolo_class'] for p in products) == catalog.MODEL_CLASSES
    assert [p['name'] for p in products] == [
        '1 to 3 Noodles', '1 to 3 Noodles', 'BRU New Rich Aroma',
        'Coca-Cola Zero Sugar 250 ml', 'Lays American Style Cream & Onion', 'Lifebuoy Neem & Aloe']
    assert [p['price'] for p in products] == [10, 5, 5, 10, 10, 10]
    assert [p['expected_weight'] for p in products] == [50, 25, 3.7, 250, 25, 40]
    assert all(p['barcode'] is None for p in products)
    rows, pending, warnings = catalog.validate_catalog(definition)
    assert len(rows) == 6 and pending == []
    assert products[3]['weight_basis'] == 'temporary_prototype_assumption'
    assert any('NOT measured packaged weight' in warning and 'Stage 11' in warning for warning in warnings)
    assert any('Bru: below current 10 g' in warning for warning in warnings)
    assert sum('printed NET' in warning for warning in warnings) == 5


@pytest.mark.parametrize('change', ['duplicate', 'extra', 'missing', 'renamed'])
def test_vocabulary_rejected(definition, change):
    if change == 'duplicate':
        definition['products'].append(copy.deepcopy(definition['products'][0]))
    elif change == 'extra':
        row = copy.deepcopy(definition['products'][0]); row['yolo_class'] = 'Extra'
        definition['products'].append(row)
    elif change == 'missing':
        definition['products'].pop()
    else:
        definition['products'][0]['yolo_class'] += ' '
    with pytest.raises(catalog.CatalogError, match='six-class'):
        catalog.validate_catalog(definition)


@pytest.mark.parametrize('field,value', [('price', -1), ('price', float('nan')),
    ('price', float('inf')), ('price', True), ('price', '10'),
    ('expected_weight', 0), ('expected_weight', -1), ('expected_weight', float('nan')),
    ('expected_weight', float('inf')), ('expected_weight', True), ('expected_weight', '50')])
def test_invalid_numbers(definition, field, value):
    definition['products'][0][field] = value
    with pytest.raises(catalog.CatalogError):
        catalog.validate_catalog(definition)


def test_pending_blocks_before_any_connection(definition, monkeypatch):
    definition['products'][3].update(expected_weight=None, weight_basis='pending_packaged_measurement')
    monkeypatch.setattr(catalog, 'get_connection', lambda: pytest.fail('Database accessed'))
    with pytest.raises(catalog.CatalogError, match='pending.*Coca Cola'):
        catalog.install_catalog(definition)



def test_coca_cola_cannot_use_net_weight_basis(definition):
    definition['products'][3].update(expected_weight=250, weight_basis='printed_net_initial_calibration')
    with pytest.raises(catalog.CatalogError, match='not volume'):
        catalog.validate_catalog(definition)


def test_atomic_install_nullable_barcodes_and_safe_rerun(db, complete):
    assert catalog.install_catalog(complete) == 'installed'
    before = get_all_products()
    assert len(before) == 6 and all(row[2] is None for row in before)
    assert len(validated_catalog(catalog.MODEL_CLASSES)) == 6
    assert catalog.install_catalog(complete) == 'unchanged'
    assert get_all_products() == before
    complete['products'][0]['price'] += 1
    with pytest.raises(catalog.CatalogError, match='refuse replacement'):
        catalog.install_catalog(complete)
    assert get_all_products() == before


def test_stage8_history_is_never_reassigned_or_deleted(db, complete):
    add_product('Stage 8 Test Product', 'STAGE8-001', 25, 100, 'Stage8_Test_Product')
    sid = billing.create_session('CART-A')
    billing.add_item(sid, 1)
    billing.remove_item(sid, 1)
    before = db.read_bytes()
    with pytest.raises(catalog.CatalogError, match='refuse replacement'):
        catalog.install_catalog(complete)
    assert db.read_bytes() == before
    assert billing.get_session_meta(sid)['end_time'] is None
    assert len(billing.get_session_log(sid)) == 2


def test_empty_catalog_with_history_refused(db, complete):
    billing.create_session('CART-A')
    with pytest.raises(catalog.CatalogError, match='session/history'):
        catalog.install_catalog(complete)
    assert get_all_products() == []


def test_insert_failure_rolls_back_entire_catalog(db, complete):
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON products WHEN NEW.yolo_class = 'Lays' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        catalog.install_catalog(complete)
    assert get_all_products() == []


def test_absent_database_is_not_created(tmp_path, monkeypatch, complete):
    path = tmp_path / 'absent.db'
    monkeypatch.setattr(database, 'DATABASE_PATH', path)
    with pytest.raises(catalog.CatalogError, match='absent'):
        catalog.install_catalog(complete)
    assert not path.exists()


def test_complete_validation_cli_never_accesses_database(monkeypatch, capsys):
    monkeypatch.setattr(catalog, 'get_connection', lambda: pytest.fail('Database accessed'))
    assert catalog.main([]) == 0
    output = capsys.readouterr().out
    assert 'temporary prototype assumption' in output and 'VALID: complete catalog' in output


@pytest.mark.parametrize('index,weight', [(3, 251), (0, 250)])
def test_temporary_assumption_is_narrowly_scoped(definition, index, weight):
    definition['products'][index].update(expected_weight=weight, weight_basis='temporary_prototype_assumption')
    with pytest.raises(catalog.CatalogError, match='approved only'):
        catalog.validate_catalog(definition)


def test_future_measured_coca_cola_weight_supported(definition):
    # Synthetic future measurement solely for this validation test.
    definition['products'][3].update(expected_weight=321, weight_basis='measured_packaged')
    rows, pending, warnings = catalog.validate_catalog(definition)
    assert len(rows) == 6 and not pending
    assert not any('temporary prototype assumption' in warning for warning in warnings)
