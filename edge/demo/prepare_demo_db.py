"""
edge/demo/prepare_demo_db.py — Explicit, safe demo database and catalog preparation.

SAFETY RULES
------------
1. NEVER overwrites or corrupts the canonical user database (data/smartcart.db).
2. Sets up an isolated demo database (default: data/smartcart_demo.db).
3. Migrates and initializes all required tables (sessions, checkouts, payments, webhooks, recovery).
4. Populates prototype catalog from configs/prototype_catalog.json for software demo testing only.
5. Emits clear warnings that the 6 prototype products are temporary prototype items,
   NOT the finalized product catalog.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sqlite3

from edge.database import database
from edge.database.catalog_install import DEFAULT_CATALOG, validate_catalog

logger = logging.getLogger(__name__)

DEFAULT_DEMO_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "smartcart_demo.db"


class DemoDbError(RuntimeError):
    """Raised when demo database preparation cannot proceed safely."""


def prepare_demo_database(
    db_path: Path | str | None = None,
    catalog_path: Path | str | None = None,
    *,
    force_clean: bool = False,
) -> Path:
    """
    Safely initialize an isolated SQLite database and seed it with the prototype catalog.

    Parameters
    ----------
    db_path:
        Target SQLite database path. Defaults to data/smartcart_demo.db.
        Must NOT be the existing production/canonical database path unless isolated.
    catalog_path:
        Path to catalog JSON. Defaults to configs/prototype_catalog.json.
    force_clean:
        If True and target database exists, resets it cleanly for fresh demo.

    Returns
    -------
    Path
        Absolute path to prepared demo database.
    """
    target_path = Path(db_path or DEFAULT_DEMO_DB_PATH).resolve()
    canonical_user_db = (Path(__file__).resolve().parents[2] / "data" / "smartcart.db").resolve()

    # Safety rule: Protect user's original smartcart.db from destructive overwrite
    if target_path == canonical_user_db and not force_clean:
        raise DemoDbError(
            f"Safety guard: Refusing to initialize demo directly on canonical database {canonical_user_db}. "
            f"Use the default isolated demo database at {DEFAULT_DEMO_DB_PATH} instead."
        )

    if force_clean and target_path.exists():
        if target_path == canonical_user_db:
            raise DemoDbError(f"Safety guard: Refusing to force-clean canonical database {canonical_user_db}.")
        target_path.unlink()

    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Set SMARTCART_DATABASE_PATH temporarily to target demo DB
    prev_env = os.environ.get("SMARTCART_DATABASE_PATH")
    os.environ["SMARTCART_DATABASE_PATH"] = str(target_path)

    try:
        # Initialize schema and all migrations
        database.initialize_database()

        # Load and validate prototype catalog
        c_path = Path(catalog_path or DEFAULT_CATALOG).resolve()
        if not c_path.is_file():
            raise DemoDbError(f"Catalog file not found: {c_path}")

        definition = json.loads(c_path.read_text())
        rows, pending, warnings = validate_catalog(definition)
        if pending:
            raise DemoDbError(f"Catalog contains pending items: {pending}")

        # Install products into demo database
        conn = sqlite3.connect(target_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            # Check if products already installed
            existing = conn.execute("SELECT product_id, yolo_class FROM products WHERE is_active = 1").fetchall()
            if not existing:
                conn.executemany(
                    "INSERT INTO products (name, barcode, price, expected_weight, yolo_class, is_active) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    rows,
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

        logger.info(
            "Demo database successfully prepared at %s (%d prototype products installed)",
            target_path,
            len(rows),
        )
        return target_path

    finally:
        if prev_env is not None:
            os.environ["SMARTCART_DATABASE_PATH"] = prev_env
        else:
            os.environ.pop("SMARTCART_DATABASE_PATH", None)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Prepare an isolated SmartCart demo SQLite database.")
    parser.add_argument("--db-path", default=str(DEFAULT_DEMO_DB_PATH), help="Target SQLite database path")
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="Catalog JSON file path")
    parser.add_argument("--clean", action="store_true", help="Remove existing demo DB before preparation")
    args = parser.parse_args(argv)

    try:
        path = prepare_demo_database(args.db_path, args.catalog, force_clean=args.clean)
        print("============================================================")
        print("DEMO DATABASE READY")
        print(f"Path: {path}")
        print("Installed 6 prototype products for software integration demo:")
        print("  - 1 to 3 Noodles Rs 10 (50 g)")
        print("  - 1 to 3 Noodles Rs 5 (25 g)")
        print("  - Bru (3.7 g)")
        print("  - Coca Cola (250 g - prototype assumption)")
        print("  - Lays (25 g)")
        print("  - Lifebuoy (40 g)")
        print("NOTE: The final six physical products are NOT yet finalized.")
        print("These classes are for software integration testing only.")
        print("============================================================")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
