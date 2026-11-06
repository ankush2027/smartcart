import sqlite3


DATABASE = "smartcart.db"
DEFAULT_THRESHOLD = 0.80


def get_threshold(product_id):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT new_threshold
        FROM threshold_log
        WHERE product_id = ?
        ORDER BY threshold_id DESC
        LIMIT 1
    """, (product_id,))

    result = cursor.fetchone()

    connection.close()

    if result:
        return result[0]

    return DEFAULT_THRESHOLD


def is_confident(confidence, product_id):
    threshold = get_threshold(product_id)

    return confidence >= threshold


def update_threshold(product_id, new_threshold):
    old_threshold = get_threshold(product_id)

    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO threshold_log
        (product_id, old_threshold, new_threshold, timestamp)
        VALUES (?, ?, ?, datetime('now'))
    """, (
        product_id,
        old_threshold,
        new_threshold
    ))

    connection.commit()
    connection.close()