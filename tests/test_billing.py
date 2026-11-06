
from edge.billing.billing import (
    create_session,
    add_item,
    remove_item,
    calculate_total,
    get_bill,
    complete_session,
    get_session_log
)


def main():
    print("Starting billing test...")

    # 1. Create a new shopping session
    session_id = create_session()
    print("Session created:", session_id)

    # 2. Add Maggi ₹10 (product_id = 1)
    add_item(session_id, 1)

    # 3. Add Maggi ₹10 again
    add_item(session_id, 1)

    # 4. Add Maggi ₹20 (product_id = 2)
    add_item(session_id, 2)

    # 5. Display bill
    print("\nCurrent Bill:")

    for item in get_bill(session_id):
        print(item)

    # 6. Calculate total
    total = calculate_total(session_id)
    print("\nCurrent Total:", total)

    # 7. Remove one Maggi ₹10
    remove_item(session_id, 1)

    # 8. Display updated bill
    print("\nBill After Removing One Item:")

    for item in get_bill(session_id):
        print(item)

    # 9. Calculate updated total
    total = calculate_total(session_id)
    print("\nUpdated Total:", total)

    # 10. Complete session
    final_total = complete_session(session_id)
    print("\nSession completed.")
    print("Final Total:", final_total)

    # 11. Display session log
    print("\nSession Log:")

    for log in get_session_log(session_id):
        print(log)


if __name__ == "__main__":
    main()
