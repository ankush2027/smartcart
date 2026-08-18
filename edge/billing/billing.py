import sqlite3


DATABASE = "smartcart.db"


def create_session():
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO sessions
        (start_time, status)
        VALUES (datetime('now'), ?)
    """, ("ACTIVE",))

    connection.commit()

    session_id = cursor.lastrowid

    connection.close()

    return session_id


def add_item(session_id, product_id, confidence=None, weight_delta=None):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT bill_item_id, quantity
        FROM bill_items
        WHERE session_id = ? AND product_id = ?
    """, (session_id, product_id))

    item = cursor.fetchone()

    if item:
        bill_item_id, quantity = item

        cursor.execute("""
            UPDATE bill_items
            SET quantity = ?
            WHERE bill_item_id = ?
        """, (quantity + 1, bill_item_id))

    else:
        cursor.execute("""
            INSERT INTO bill_items
            (session_id, product_id, quantity)
            VALUES (?, ?, ?)
        """, (session_id, product_id, 1))

    cursor.execute("""
        INSERT INTO session_log
        (session_id, product_id, event_type, timestamp, confidence, weight_delta)
        VALUES (?, ?, ?, datetime('now'), ?, ?)
    """, (
        session_id,
        product_id,
        "ADD",
        confidence,
        weight_delta
    ))

    connection.commit()
    connection.close()

def remove_item(session_id, product_id, confidence=None, weight_delta=None):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT bill_item_id, quantity
        FROM bill_items
        WHERE session_id = ? AND product_id = ?
    """, (session_id, product_id))

    item = cursor.fetchone()

    if item:
        bill_item_id, quantity = item

        if quantity > 1:
            cursor.execute("""
                UPDATE bill_items
                SET quantity = ?
                WHERE bill_item_id = ?
            """, (quantity - 1, bill_item_id))

        else:
            cursor.execute("""
                DELETE FROM bill_items
                WHERE bill_item_id = ?
            """, (bill_item_id,))

        cursor.execute("""
            INSERT INTO session_log
            (session_id, product_id, event_type, timestamp, confidence, weight_delta)
            VALUES (?, ?, ?, datetime('now'), ?, ?)
        """, (
            session_id,
            product_id,
            "REMOVE",
            confidence,
            weight_delta
        ))

    connection.commit()
    connection.close()


def calculate_total(session_id):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT products.price, bill_items.quantity
        FROM bill_items
        JOIN products
            ON bill_items.product_id = products.product_id
        WHERE bill_items.session_id = ?
    """, (session_id,))

    items = cursor.fetchall()

    total = 0

    for price, quantity in items:
        total += price * quantity

    connection.close()

    return total

def complete_session(session_id):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        UPDATE sessions
        SET end_time = datetime('now'),
            status = ?
        WHERE session_id = ?
    """, ("COMPLETED", session_id))

    connection.commit()
    connection.close()

def log_event(session_id, product_id, event_type, confidence=None, weight_delta=None):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO session_log
        (session_id, product_id, event_type, timestamp, confidence, weight_delta)
        VALUES (?, ?, ?, datetime('now'), ?, ?)
    """, (
        session_id,
        product_id,
        event_type,
        confidence,
        weight_delta
    ))

    connection.commit()
    connection.close()


def get_session_log(session_id):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            log_id,
            product_id,
            event_type,
            timestamp,
            confidence,
            weight_delta
        FROM session_log
        WHERE session_id = ?
        ORDER BY timestamp
    """, (session_id,))

    logs = cursor.fetchall()

    connection.close()

    return logs

