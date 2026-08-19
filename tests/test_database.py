import sqlite3

from edge.database.database import get_connection, initialize_database


def main():
    initialize_database()

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
    """)

    tables = cursor.fetchall()

    print("Tables in SmartCart database:")

    for table in tables:
        print("-", table[0])

    connection.close()


if __name__ == "__main__":
    main()