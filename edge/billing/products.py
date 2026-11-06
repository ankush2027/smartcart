import sqlite3


DATABASE = "smartcart.db"


def get_product(product_id):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute(
        "SELECT * FROM products WHERE product_id = ?",
        (product_id,)
    )

    product = cursor.fetchone()

    connection.close()

    return product


def get_all_products():
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("SELECT * FROM products")

    products = cursor.fetchall()

    connection.close()

    return products


def add_product(yolo_class, barcode, product_name, price, weight_g):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO products
        (yolo_class, barcode, product_name, price, weight_g)
        VALUES (?, ?, ?, ?, ?)
    """, (yolo_class, barcode, product_name, price, weight_g))

    connection.commit()

    product_id = cursor.lastrowid

    connection.close()

    return product_id