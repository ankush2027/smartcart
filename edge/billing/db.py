import sqlite3

# Connect to the SmartCart database
connection = sqlite3.connect("smartcart.db")

# Create cursor
cursor = connection.cursor()


# --------------------------------------------------
# 1. PRODUCTS
# --------------------------------------------------

cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        product_id INTEGER PRIMARY KEY AUTOINCREMENT,
        yolo_class TEXT NOT NULL,
        barcode TEXT UNIQUE,
        product_name TEXT NOT NULL,
        price REAL NOT NULL,
        weight_g REAL NOT NULL
    )
""")


# --------------------------------------------------
# 2. SESSIONS
# --------------------------------------------------

cursor.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        session_id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_time TEXT NOT NULL,
        end_time TEXT,
        status TEXT NOT NULL
    )
""")


# --------------------------------------------------
# 3. BILL ITEMS
# --------------------------------------------------

cursor.execute("""
    CREATE TABLE IF NOT EXISTS bill_items (
        bill_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,

        FOREIGN KEY (session_id)
            REFERENCES sessions(session_id),

        FOREIGN KEY (product_id)
            REFERENCES products(product_id)
    )
""")


# --------------------------------------------------
# 4. SESSION LOG
# --------------------------------------------------

cursor.execute("""
    CREATE TABLE IF NOT EXISTS session_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        confidence REAL,
        weight_delta REAL,

        FOREIGN KEY (session_id)
            REFERENCES sessions(session_id),

        FOREIGN KEY (product_id)
            REFERENCES products(product_id)
    )
""")


# --------------------------------------------------
# 5. THRESHOLD LOG
# --------------------------------------------------

cursor.execute("""
    CREATE TABLE IF NOT EXISTS threshold_log (
        threshold_id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        old_threshold REAL NOT NULL,
        new_threshold REAL NOT NULL,
        timestamp TEXT NOT NULL,

        FOREIGN KEY (product_id)
            REFERENCES products(product_id)
    )
""")


cursor.execute("SELECT * FROM products")

products = cursor.fetchall()

for product in products:
    print(product)

    
# Save changes
connection.commit()

print("All SmartCart tables created successfully!")


# Close database connection
connection.close()