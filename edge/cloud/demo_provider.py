"""Persistent DEMO ledger only. No money moves and no real payment gateway.

Wrap the existing demo provider, persisting each public operation atomically.
Separate instances/process restarts read the same ledger from authoritative disk.
"""
from dataclasses import asdict
import json
from edge.database.database import get_connection
from edge.payment.fake import FakeLedger, FakeProvider
from edge.payment.provider import ProviderOrder, ProviderPayment


class PersistentDemoProvider:
    scope = FakeProvider.scope

    def __init__(self):
        connection = get_connection()
        try:
            with connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS cloud_demo_ledger (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)""")
        finally:
            connection.close()

    def _call(self, method, *args, **kwargs):
        connection = get_connection()
        try:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT payload FROM cloud_demo_ledger WHERE id=1').fetchone()
            data = json.loads(row[0]) if row else {'orders': {}, 'payments': {}, 'rejected': []}
            ledger = FakeLedger(
                orders={key: ProviderOrder(**value) for key,value in data['orders'].items()},
                payments={key: ProviderPayment(**value) for key,value in data['payments'].items()},
                rejected=set(data['rejected']))
            result = getattr(FakeProvider(ledger, recover_missing=True), method)(*args, **kwargs)
            payload = json.dumps({'orders': {k: asdict(v) for k,v in ledger.orders.items()},
                                  'payments': {k: asdict(v) for k,v in ledger.payments.items()},
                                  'rejected': sorted(ledger.rejected)})
            connection.execute('INSERT OR REPLACE INTO cloud_demo_ledger VALUES(1,?)', (payload,))
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_order(self, request): return self._call('create_order', request)
    def recover_order(self, request): return self._call('recover_order', request)
    def fetch_order(self, order_id): return self._call('fetch_order', order_id)
    def fetch_payment(self, payment_id): return self._call('fetch_payment', payment_id)
    def record_payment(self, order_id, **kwargs): return self._call('record_payment', order_id, **kwargs)
    def evidence(self, payment_id): return self._call('evidence', payment_id)
    def verify_client_evidence(self, order_id, evidence):
        return self._call('verify_client_evidence', order_id, evidence)
