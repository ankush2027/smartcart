"""Explicit prototype catalog validation and all-or-nothing installation.

No runtime imports, model loading, automatic initialization, or replacement of data.
"""
import argparse
import json
import math
import sqlite3
from pathlib import Path

from edge.database import database
from edge.database.database import get_connection
from edge.weight.weight_event import detect_weight_change

DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / 'configs/prototype_catalog.json'
MODEL_SHA256 = '13413bbf0a7c774b6b037bce10be2e16a9d3bbfd30dc2a365840f600e08fd0b3'
MODEL_CLASSES = ('1 to 3 Noodles Rs 10', '1 to 3 Noodles Rs 5', 'Bru',
                 'Coca Cola', 'Lays', 'Lifebuoy')


class CatalogError(ValueError):
    pass


def validate_catalog(definition):
    """Return (ordered SQL rows, pending classes, warnings); never open SQLite."""
    if not isinstance(definition, dict) or set(definition) != {'currency', 'model_sha256', 'products'}:
        raise CatalogError('Expected currency, model_sha256 and products')
    if definition['currency'] != 'INR' or definition['model_sha256'] != MODEL_SHA256:
        raise CatalogError('Expected approved INR catalog and model SHA-256')
    products = definition['products']
    if not isinstance(products, list) or not all(isinstance(p, dict) for p in products):
        raise CatalogError('Products must be a list of records')
    labels = [p.get('yolo_class') for p in products]
    if (any(not isinstance(c, str) for c in labels) or len(set(labels)) != len(labels)
            or set(labels) != set(MODEL_CLASSES)):
        raise CatalogError('Require exact six-class vocabulary without duplicates, extras or omissions')
    rows, pending, warnings, barcodes = [], [], [], set()
    by_class = {p['yolo_class']: p for p in products}
    for label in MODEL_CLASSES:
        p = by_class[label]
        if set(p) != {'yolo_class', 'name', 'barcode', 'price', 'expected_weight', 'weight_basis'}:
            raise CatalogError(f'{label}: unexpected or missing fields')
        if not isinstance(p['name'], str) or not p['name'].strip():
            raise CatalogError(f'{label}: name is required')
        barcode = p['barcode']
        if barcode is not None:
            if not isinstance(barcode, str) or not barcode.strip() or barcode in barcodes:
                raise CatalogError(f'{label}: barcode must be NULL or a unique nonempty string')
            barcodes.add(barcode)
        price, weight, basis = p['price'], p['expected_weight'], p['weight_basis']
        if type(price) not in (int, float) or not math.isfinite(price) or price < 0:
            raise CatalogError(f'{label}: price must be finite and nonnegative')
        if weight is None:
            if basis != 'pending_packaged_measurement':
                raise CatalogError(f'{label}: missing weight must explicitly be pending')
            pending.append(label)
            continue
        if type(weight) not in (int, float) or not math.isfinite(weight) or weight <= 0:
            raise CatalogError(f'{label}: expected_weight must be positive finite grams')
        if basis not in ('printed_net_initial_calibration', 'measured_packaged', 'temporary_prototype_assumption'):
            raise CatalogError(f'{label}: invalid weight provenance')
        if basis == 'temporary_prototype_assumption':
            if label != 'Coca Cola' or weight != 250:
                raise CatalogError('Temporary assumption is approved only for Coca Cola at 250 g')
            warnings.append('Coca Cola: 250 g is a temporary prototype assumption, NOT measured packaged weight; replace during Stage 11 physical load-cell calibration')
        if label == 'Coca Cola' and basis not in ('measured_packaged', 'temporary_prototype_assumption'):
            raise CatalogError('Coca Cola requires measured weight or the approved temporary assumption, not volume')
        if basis == 'printed_net_initial_calibration':
            warnings.append(f'{label}: printed NET weight is initial calibration, not packaged measurement')
        if detect_weight_change(0, weight) is None:
            warnings.append(f'{label}: below current 10 g event threshold; single-unit automatic events unsupported')
        rows.append((p['name'], barcode, price, weight, label))
    return rows, pending, warnings


def install_catalog(definition, *, retire_stage8=False):
    """Install a complete active catalog atomically, or return unchanged.

    Explicit Stage 8 transition retains ID 1 and requires all sessions completed.
    Default installation still refuses replacement of existing active products.
    """
    rows, pending, _ = validate_catalog(definition)
    if pending:
        raise CatalogError('Installation blocked by pending packaged weight: ' + ', '.join(pending))
    if not database.DATABASE_PATH.is_file():
        raise CatalogError("Canonical database is absent; explicit initialization required")
    connection = get_connection()
    try:
        connection.execute('BEGIN IMMEDIATE')
        existing = connection.execute(
            'SELECT name, barcode, price, expected_weight, yolo_class FROM products WHERE is_active = 1').fetchall()
        if sorted(existing, key=lambda r: str(r[4])) == sorted(rows, key=lambda r: r[4]):
            connection.rollback()
            return 'unchanged'
        if retire_stage8:
            # Deliberately narrow transition, not a general product retirement tool.
            all_products = connection.execute(
                'SELECT product_id, name, barcode, price, expected_weight, yolo_class, is_active '
                'FROM products ORDER BY product_id').fetchall()
            expected = [(1, 'Stage 8 Test Product', 'STAGE8-001', 25.0, 100.0, 'Stage8_Test_Product', 1)]
            if all_products != expected:
                raise CatalogError('Stage 8 transition requires the original, unchanged product ID 1 only')
            if connection.execute('SELECT 1 FROM sessions WHERE end_time IS NULL LIMIT 1').fetchone():
                raise CatalogError('Complete active sessions through CartSession before transition')
            connection.execute('UPDATE products SET is_active = 0 WHERE product_id = 1')
        else:
            if existing:
                raise CatalogError('Existing catalog differs; refuse replacement or historical product reassignment')
            if any(connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
                   for table in ('products', 'sessions', 'bill_items', 'session_log')):
                raise CatalogError('Database contains session/history data; explicit transition required')
        connection.executemany(
            'INSERT INTO products (name, barcode, price, expected_weight, yolo_class) VALUES (?, ?, ?, ?, ?)', rows)
        connection.commit()
        return 'installed'
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=DEFAULT_CATALOG)
    parser.add_argument('--install', action='store_true', help='Explicitly install into canonical SQLite')
    parser.add_argument('--retire-stage8', action='store_true', help='Explicitly retire the original Stage 8 product after session completion')
    args = parser.parse_args(argv)
    if args.retire_stage8 and not args.install:
        parser.error('--retire-stage8 requires --install')
    try:
        definition = json.loads(args.catalog.read_text())
        _, pending, warnings = validate_catalog(definition)
        for warning in warnings:
            print('WARNING:', warning)
        if pending:
            raise CatalogError('Installation blocked by pending packaged weight: ' + ', '.join(pending))
        print(install_catalog(definition, retire_stage8=args.retire_stage8) if args.install else 'VALID: complete catalog; database not accessed')
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print('BLOCKED:', error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
