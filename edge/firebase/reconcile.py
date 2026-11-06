"""Trusted one-session recovery. Relevant writers must already be quiesced."""
from dataclasses import dataclass

from edge.billing.session_projection import read_session_projection
from edge.firebase import client
from edge.firebase.sync import sync_session_projection


class ReconciliationError(RuntimeError):
    """Firebase publication failed; remote application may be uncertain."""


@dataclass(frozen=True)
class ReconciliationResult:
    session_id: int
    bill_item_count: int
    completed: bool


def reconcile_session_to_firebase(session_id: int, *, _ref=None) -> ReconciliationResult:
    """Rebuild metadata/bill from SQLite without billing or event replay.

    Internal/operator API, not customer authorization. Accepts no replacement
    ownership or payload. _ref injects an offline Firebase reference for tests.
    Source errors propagate before Firebase initialization or reference access.
    A remote error never mutates SQLite and does not prove the write was absent.
    No automatic retry, lock, quiescence detection or stale-write fencing exists.
    """
    snapshot = read_session_projection(session_id)
    try:
        if _ref is None:
            client.initialize()
        sync_session_projection(snapshot, _ref=_ref)
    except Exception as error:
        raise ReconciliationError(
            'Firebase reconciliation failed or its outcome is uncertain; SQLite is unchanged'
        ) from error
    return ReconciliationResult(snapshot.session_id, len(snapshot.bill_items),
                                snapshot.end_time is not None)
