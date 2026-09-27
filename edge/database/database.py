import sqlite3
from pathlib import Path


DATABASE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "smartcart.db"


def get_connection():
    """
    Create and return a connection to the SmartCart SQLite database.
    """

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)

    return connection


def initialize_database():
    """
    Create the SmartCart database if it does not already exist.

    Session ownership columns
    -------------------------
    ``sessions.cart_id``       — stable physical cart identifier (e.g. QR/NFC ID).
    ``sessions.customer_uid``  — Firebase Auth UID of the assigned customer.

    Both columns are nullable.  A session may exist before a customer is
    assigned and before a cart ID is set.  They are populated via
    :func:`edge.billing.billing.set_session_cart_id` and
    :func:`edge.billing.billing.assign_customer_to_session`.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            barcode TEXT UNIQUE,
            price REAL NOT NULL,
            expected_weight REAL NOT NULL,
            yolo_class TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    """)

    # Migration: add yolo_class to existing databases that were created
    # before this column was introduced.  SQLite does not support
    # "ALTER TABLE … ADD COLUMN IF NOT EXISTS", so we catch the
    # OperationalError that fires when the column already exists.
    try:
        cursor.execute("ALTER TABLE products ADD COLUMN yolo_class TEXT")
    except Exception:
        pass  # column already present — nothing to do

    # Additive lifecycle migration: retain IDs, metadata and historical references.
    if 'is_active' not in {row[1] for row in cursor.execute('PRAGMA table_info(products)')}:
        cursor.execute('ALTER TABLE products ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1')

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bill_items (
            bill_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            total REAL NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            total_amount REAL DEFAULT 0,
            cart_id TEXT,
            customer_uid TEXT
        )
    """)

    # Migration: add session-ownership columns to databases created before
    # cart_id / customer_uid were introduced.
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN cart_id TEXT")
    except Exception:
        pass  # column already present — nothing to do

    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN customer_uid TEXT")
    except Exception:
        pass  # column already present — nothing to do

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            product_id INTEGER,
            details TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS threshold_log (
            threshold_log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            old_threshold REAL,
            new_threshold REAL,
            reason TEXT
        )
    """)

    connection.commit()
    connection.close()


if __name__ == "__main__":
    initialize_database()
    print(f"Database initialized: {DATABASE_PATH}")