from edge.database.database import get_connection
from edge.database.product_repository import get_product_by_barcode


def create_session():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO sessions (start_time, total_amount)
        VALUES (datetime('now'), 0)
    """)

    session_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return session_id


def add_item(session_id, product_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT price
        FROM products
        WHERE product_id = ?
    """, (product_id,))

    product = cursor.fetchone()

    if product is None:
        connection.close()
        return False

    price = product[0]

    cursor.execute("""
        SELECT bill_item_id, quantity
        FROM bill_items
        WHERE session_id = ? AND product_id = ?
    """, (session_id, product_id))

    item = cursor.fetchone()

    if item:
        bill_item_id, quantity = item

        new_quantity = quantity + 1
        new_total = price * new_quantity

        cursor.execute("""
            UPDATE bill_items
            SET quantity = ?,
                price = ?,
                total = ?
            WHERE bill_item_id = ?
        """, (
            new_quantity,
            price,
            new_total,
            bill_item_id
        ))

    else:
        cursor.execute("""
            INSERT INTO bill_items
            (session_id, product_id, quantity, price, total)
            VALUES (?, ?, ?, ?, ?)
        """, (
            session_id,
            product_id,
            1,
            price,
            price
        ))

    cursor.execute("""
        INSERT INTO session_log
        (session_id, timestamp, event_type, product_id, details)
        VALUES (?, datetime('now'), ?, ?, ?)
    """, (
        session_id,
        "ADD",
        product_id,
        "Product added to bill"
    ))

    connection.commit()
    connection.close()

    return True


def remove_item(session_id, product_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT bill_item_id, quantity
        FROM bill_items
        WHERE session_id = ? AND product_id = ?
    """, (session_id, product_id))

    item = cursor.fetchone()

    if item is None:
        connection.close()
        return False

    bill_item_id, quantity = item

    if quantity > 1:
        cursor.execute("""
            SELECT price
            FROM bill_items
            WHERE bill_item_id = ?
        """, (bill_item_id,))

        price = cursor.fetchone()[0]
        new_quantity = quantity - 1
        new_total = price * new_quantity

        cursor.execute("""
            UPDATE bill_items
            SET quantity = ?,
                total = ?
            WHERE bill_item_id = ?
        """, (
            new_quantity,
            new_total,
            bill_item_id
        ))

    else:
        cursor.execute("""
            DELETE FROM bill_items
            WHERE bill_item_id = ?
        """, (bill_item_id,))

    cursor.execute("""
        INSERT INTO session_log
        (session_id, timestamp, event_type, product_id, details)
        VALUES (?, datetime('now'), ?, ?, ?)
    """, (
        session_id,
        "REMOVE",
        product_id,
        "Product removed from bill"
    ))

    connection.commit()
    connection.close()

    return True


def calculate_total(session_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT COALESCE(SUM(total), 0)
        FROM bill_items
        WHERE session_id = ?
    """, (session_id,))

    total = cursor.fetchone()[0]

    cursor.execute("""
        UPDATE sessions
        SET total_amount = ?
        WHERE session_id = ?
    """, (total, session_id))

    connection.commit()
    connection.close()

    return total


def get_bill(session_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            products.product_id,
            products.name,
            bill_items.quantity,
            bill_items.price,
            bill_items.total
        FROM bill_items
        JOIN products
            ON bill_items.product_id = products.product_id
        WHERE bill_items.session_id = ?
        ORDER BY products.product_id
    """, (session_id,))

    bill = cursor.fetchall()

    connection.close()

    return bill


def complete_session(session_id):
    total = calculate_total(session_id)

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        UPDATE sessions
        SET end_time = datetime('now'),
            total_amount = ?
        WHERE session_id = ?
    """, (total, session_id))

    connection.commit()
    connection.close()

    return total


def get_session_log(session_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            log_id,
            session_id,
            timestamp,
            event_type,
            product_id,
            details
        FROM session_log
        WHERE session_id = ?
        ORDER BY log_id
    """, (session_id,))

    logs = cursor.fetchall()

    connection.close()

    return logs
