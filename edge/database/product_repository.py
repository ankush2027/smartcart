from edge.database.database import get_connection


def add_product(name, barcode, price, expected_weight, yolo_class=None):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO products (
            name,
            barcode,
            price,
            expected_weight,
            yolo_class
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        name,
        barcode,
        price,
        expected_weight,
        yolo_class
    ))

    connection.commit()
    connection.close()


def get_product_by_barcode(barcode):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            product_id,
            name,
            barcode,
            price,
            expected_weight,
            yolo_class
        FROM products
        WHERE barcode = ?
    """, (barcode,))

    product = cursor.fetchone()

    connection.close()

    return product


def get_product_by_yolo_class(yolo_class):
    """
    Return the product row whose yolo_class matches the supplied label.

    Returns a tuple:
        (product_id, name, barcode, price, expected_weight, yolo_class)

    or None if no matching product exists.
    """
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            product_id,
            name,
            barcode,
            price,
            expected_weight,
            yolo_class
        FROM products
        WHERE yolo_class = ?
    """, (yolo_class,))

    product = cursor.fetchone()

    connection.close()

    return product


def get_all_products():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            product_id,
            name,
            barcode,
            price,
            expected_weight,
            yolo_class
        FROM products
        ORDER BY product_id
    """)

    products = cursor.fetchall()

    connection.close()

    return products


def get_active_products():
    """Runtime catalog only; preserve the existing six-column tuple contract."""
    connection = get_connection()
    try:
        return connection.execute(
            'SELECT product_id, name, barcode, price, expected_weight, yolo_class '
            'FROM products WHERE is_active = 1 ORDER BY product_id').fetchall()
    finally:
        connection.close()


def get_product_by_id(product_id):
    """Historical lookup includes retired products; returns six columns or None."""
    connection = get_connection()
    try:
        return connection.execute(
            'SELECT product_id, name, barcode, price, expected_weight, yolo_class '
            'FROM products WHERE product_id = ?', (product_id,)).fetchone()
    finally:
        connection.close()
