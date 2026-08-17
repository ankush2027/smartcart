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


def add_item(session_id, product_id):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    # Check whether this product is already in the bill
    cursor.execute("""
        SELECT bill_item_id, quantity
        FROM bill_items
        WHERE session_id = ? AND product_id = ?
    """, (session_id, product_id))

    item = cursor.fetchone()

    if item:
        # Product already exists → increase quantity
        bill_item_id, quantity = item

        cursor.execute("""
            UPDATE bill_items
            SET quantity = ?
            WHERE bill_item_id = ?
        """, (quantity + 1, bill_item_id))

    else:
        # Product not in bill → create new bill item
        cursor.execute("""
            INSERT INTO bill_items
            (session_id, product_id, quantity)
            VALUES (?, ?, ?)
        """, (session_id, product_id, 1))

    connection.commit()
    connection.close()


def remove_item(session_id, product_id):
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
            # More than one item → reduce quantity
            cursor.execute("""
                UPDATE bill_items
                SET quantity = ?
                WHERE bill_item_id = ?
            """, (quantity - 1, bill_item_id))

        else:
            # Only one item → remove the bill row
            cursor.execute("""
                DELETE FROM bill_items
                WHERE bill_item_id = ?
            """, (bill_item_id,))

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