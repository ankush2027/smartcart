"""Durable, coalescing session projections. Exactly one cloud publisher process.

All cloud writers use the same database. Triggers enqueue in their transaction.
A failed/uncertain Firebase update leaves a retryable entry; no billing is replayed.
"""
from threading import RLock
from edge.database.database import get_connection
from edge.billing.session_projection import read_session_projection
from edge.firebase.sync import sync_session_projection


def install():
    connection = get_connection()
    try:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS cloud_sync_outbox (
                session_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL,
                queued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            for table in ('sessions', 'bill_items'):
                for operation in ('INSERT', 'UPDATE', 'DELETE'):
                    row = 'OLD' if operation == 'DELETE' else 'NEW'
                    # Table/operation names are fixed internal constants, not input.
                    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS cloud_sync_{table}_{operation}
                        AFTER {operation} ON {table} BEGIN
                        INSERT INTO cloud_sync_outbox(session_id,revision)
                        VALUES ({row}.session_id,1)
                        ON CONFLICT(session_id) DO UPDATE SET revision=revision+1;
                        END""")
            connection.execute("""INSERT OR IGNORE INTO cloud_sync_outbox(session_id,revision)
                SELECT session_id,1 FROM sessions""")
    finally:
        connection.close()


class Publisher:
    def __init__(self, ref=None):
        self.ref = ref
        self.lock = RLock()

    def flush(self, *_):
        # Serializes the entire read/publish/ack cycle, including request callbacks.
        # Revisions prevent acknowledging a concurrent database change.
        with self.lock:
            connection = get_connection()
            try:
                pending = connection.execute(
                    'SELECT session_id,revision FROM cloud_sync_outbox ORDER BY session_id LIMIT 100'
                ).fetchall()
                for session_id, revision in pending:
                    snapshot = read_session_projection(session_id)
                    sync_session_projection(snapshot, _ref=self.ref)
                    with connection:
                        connection.execute('DELETE FROM cloud_sync_outbox WHERE session_id=? AND revision=?',
                                           (session_id, revision))
            finally:
                connection.close()
