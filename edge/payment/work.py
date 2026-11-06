"""Bounded, explicitly invoked SQLite work claims. No scheduler or network I/O."""
from contextlib import contextmanager
from dataclasses import dataclass
import time
from uuid import uuid4

from edge.database.database import get_connection
from .store import _one


ERRORS = ('digest_conflict', 'unknown_order', 'scope_unavailable', 'invalid_evidence',
          'payment_conflict', 'provider_unavailable', 'payment_not_captured',
          'processing_error', 'retry_exhausted', 'awaiting_evidence', 'order_unresolved')
TABLES = {'payment_webhooks': 'webhook_id', 'payment_recovery_jobs': 'payment_attempt_id'}
WORK_COLUMNS = f"""
    state TEXT NOT NULL DEFAULT 'RECEIVED'
        CHECK(state IN ('RECEIVED','PROCESSING','PROCESSED','RETRYABLE','QUARANTINED')),
    processing_attempts INTEGER NOT NULL DEFAULT 0 CHECK(typeof(processing_attempts)='integer' AND processing_attempts>=0),
    last_attempted_at INTEGER CHECK(last_attempted_at IS NULL OR (typeof(last_attempted_at)='integer' AND last_attempted_at>=0)),
    next_attempt_at INTEGER NOT NULL DEFAULT 0 CHECK(typeof(next_attempt_at)='integer' AND next_attempt_at>=0),
    claim_token TEXT,
    claim_expires_at INTEGER CHECK(claim_expires_at IS NULL OR (typeof(claim_expires_at)='integer' AND claim_expires_at>=0)),
    error_code TEXT CHECK(error_code IS NULL OR error_code IN {ERRORS}),
    processed_at INTEGER CHECK(processed_at IS NULL OR (typeof(processed_at)='integer' AND processed_at>=0)),
    CHECK((state='PROCESSING' AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)
       OR (state!='PROCESSING' AND claim_token IS NULL AND claim_expires_at IS NULL)),
    CHECK(state!='PROCESSED' OR processed_at IS NOT NULL)
"""


class ClaimLost(Exception):
    """Expired, superseded or quarantined work cannot mutate payment authority."""


@dataclass(frozen=True)
class RetryPolicy:
    lease_seconds: int = 120
    retry_seconds: int = 30
    max_attempts: int = 5

    def __post_init__(self):
        for value in (self.lease_seconds, self.retry_seconds, self.max_attempts):
            if type(value) is not int or not 1 <= value <= 86400:
                raise ValueError('Invalid work policy')


def timestamp(clock=time.time):
    value = clock()
    if type(value) not in (int, float) or not 0 <= value < 2**53:
        raise ValueError('Invalid work clock')
    return int(value)


@contextmanager
def transaction():
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim(table, identity, *, policy, clock=time.time, reclaim_expired=False):
    key = TABLES[table]  # Only constant, allowlisted SQL identifiers.
    now = timestamp(clock)
    with transaction() as conn:
        row = _one(conn, f'SELECT * FROM {table} WHERE {key}=?', (identity,))
        if row is None or row['state'] in ('PROCESSED', 'QUARANTINED'):
            return None
        if row['state'] == 'PROCESSING':
            if not reclaim_expired or row['claim_expires_at'] > now:
                return None
        elif row['next_attempt_at'] > now:
            return None
        if row['processing_attempts'] >= policy.max_attempts:
            conn.execute(f"UPDATE {table} SET state='QUARANTINED',error_code='retry_exhausted',"
                         f'claim_token=NULL,claim_expires_at=NULL WHERE {key}=?', (identity,))
            return None
        conn.execute(f"UPDATE {table} SET state='PROCESSING',processing_attempts=processing_attempts+1,"
                     f'last_attempted_at=?,claim_token=?,claim_expires_at=?,error_code=NULL WHERE {key}=?',
                     (now, uuid4().hex, now + policy.lease_seconds, identity))
        return _one(conn, f'SELECT * FROM {table} WHERE {key}=?', (identity,))


def guard(table, row, *, clock=time.time):
    key = TABLES[table]
    def check(conn):
        current = conn.execute(f'SELECT state,claim_token,claim_expires_at FROM {table} WHERE {key}=?',
                               (row[key],)).fetchone()
        if (current is None or current[0] != 'PROCESSING' or current[1] != row['claim_token']
                or current[2] <= timestamp(clock)):
            raise ClaimLost('Work claim is no longer current')
    return check


def finish(table, row, state, *, error=None, policy, clock=time.time):
    if state not in ('PROCESSED', 'RETRYABLE', 'QUARANTINED') or (error is not None and error not in ERRORS):
        raise ValueError('Invalid work result')
    now = timestamp(clock)
    if state == 'RETRYABLE' and row['processing_attempts'] >= policy.max_attempts:
        state, error = 'QUARANTINED', 'retry_exhausted'
    key = TABLES[table]
    with transaction() as conn:
        guard(table, row, clock=clock)(conn)
        conn.execute(f'UPDATE {table} SET state=?,error_code=?,next_attempt_at=?,processed_at=?, '
                     f'claim_token=NULL,claim_expires_at=NULL WHERE {key}=?',
                     (state, error, now + policy.retry_seconds, now if state == 'PROCESSED' else None, row[key]))
    return state
