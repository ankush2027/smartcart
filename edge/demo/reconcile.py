"""Explicit operator recovery for a dedicated demo cart, before writers start.

All writers sharing this Firebase sessions namespace must be stopped. This is
not a public API and must never be used against a cart owned by another database.
"""
from contextlib import closing
from copy import deepcopy

from edge.billing.session_projection import read_session_projection
from edge.database.database import get_connection
from edge.firebase.sync import sync_session_projection


class DemoReconciliationError(RuntimeError):
    pass


def _sessions(value):
    if value is None:
        return {}
    if isinstance(value, list):
        value = {str(i): row for i, row in enumerate(value) if row is not None}
    if not isinstance(value, dict):
        raise DemoReconciliationError('Invalid sessions projection; operator review required')
    for key, row in value.items():
        if (not isinstance(key, str) or not key.isascii() or not key.isdigit()
                or str(int(key)) != key or not 0 < int(key) < 9223372036854775807
                or not isinstance(row, dict)):
            raise DemoReconciliationError('Invalid session identity; operator review required')
    return value


class _Payload:
    """Reuse the approved projection serializer, without performing a write."""
    def child(self, path):
        return self

    def update(self, data):
        self.data = data


def reconcile_demo_cart(cart_id, *, root):
    """Remove only active orphan projections of the explicitly selected cart.

    Preserve other carts and completed orphan history. Rebuild matching local
    sessions, retaining event logs. Reserve IDs above *all* observed remote IDs
    so a freshly prepared database cannot reuse a previous Firebase identity.
    Remote failure stops startup; no bill/payment/session row is changed.
    """
    if not isinstance(cart_id, str) or not cart_id.strip():
        raise DemoReconciliationError('Explicit demo cart required')
    reference = root.child('sessions')
    before = deepcopy(_sessions(reference.get()))
    with closing(get_connection()) as connection:
        local = dict(connection.execute('SELECT session_id, cart_id FROM sessions'))
    snapshots = {str(sid): read_session_projection(sid)
                 for sid, owner in local.items() if owner == cart_id}
    updates = {}
    for key, snapshot in snapshots.items():
        old = before.get(key)
        if old is not None and any(old.get(field) != getattr(snapshot, field)
                                   for field in ('cart_id', 'customer_uid', 'start_time')):
            raise DemoReconciliationError('Session identity collision; operator review required')
        payload = _Payload()
        sync_session_projection(snapshot, _ref=payload)
        updates[key] = payload.data
    orphans = {key for key, row in before.items()
               if row.get('cart_id') == cart_id and row.get('end_time') is None
               and int(key) not in local}
    # A conflicting local row belonging to another cart must not be deleted.
    if any(row.get('cart_id') == cart_id and int(key) in local
           and local[int(key)] != cart_id for key, row in before.items()):
        raise DemoReconciliationError('Session identity collision; operator review required')
    highwater = max((int(key) for key in before), default=0)
    with closing(get_connection()) as connection:
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='sessions'").fetchone()
            if row is None:
                connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('sessions',?)", (highwater,))
            elif row[0] < highwater:
                connection.execute("UPDATE sqlite_sequence SET seq=? WHERE name='sessions'", (highwater,))

    def publish(current):
        current = _sessions(current)
        # Fail closed rather than overwriting a concurrent writer. Firebase may
        # invoke this callback again; it has no local side effects.
        if current != before:
            raise DemoReconciliationError('Projection changed during repair; stop writers and retry')
        result = deepcopy(current)
        for key in orphans:
            del result[key]
        for key, payload in updates.items():
            row = result.setdefault(key, {})
            for field, value in payload.items():
                if value is None:
                    row.pop(field, None)
                else:
                    row[field] = value
        return result or None

    try:
        reference.transaction(publish)
    except Exception:
        raise DemoReconciliationError('Demo projection repair failed or is uncertain; startup stopped') from None
    return len(orphans), len(updates)
