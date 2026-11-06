from edge.database.product_repository import (
    add_product,
    get_product_by_barcode,
    get_all_products
)


def main():

    # Check existing products
    products = get_all_products()

    print("Products in database:")

    for product in products:
        print(product)

    # Test barcode search
    product = get_product_by_barcode("890123456001")

    print("\nProduct found by barcode:")

    print(product)


if __name__ == "__main__":
    main()