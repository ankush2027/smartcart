from edge.database.database import get_connection


def add_product(name, barcode, price, expected_weight):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO products (
            name,
            barcode,
            price,
            expected_weight
        )
        VALUES (?, ?, ?, ?)
    """, (
        name,
        barcode,
        price,
        expected_weight
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
            expected_weight
        FROM products
        WHERE barcode = ?
    """, (barcode,))

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
            expected_weight
        FROM products
        ORDER BY product_id
    """)

    products = cursor.fetchall()

    connection.close()

    return products