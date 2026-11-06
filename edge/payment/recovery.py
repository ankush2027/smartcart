"""Controlled recovery of existing attempts only; no background activation."""
import time
from contextlib import closing

from edge.database.database import get_connection

from . import store, webhook_store, work
from .webhook_processor import WebhookProcessor, classify


def migrate_recovery(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS payment_recovery_jobs (
        payment_attempt_id TEXT PRIMARY KEY NOT NULL
            REFERENCES payment_attempts(payment_attempt_id) ON DELETE RESTRICT,
        {work.WORK_COLUMNS}
    )''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS recovery_identity_immutable
        BEFORE UPDATE OF payment_attempt_id ON payment_recovery_jobs
        BEGIN SELECT RAISE(ABORT,'Recovery identity is immutable'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS recovery_retained BEFORE DELETE ON payment_recovery_jobs
        BEGIN SELECT RAISE(ABORT,'Recovery history is retained'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS recovery_no_replace BEFORE INSERT ON payment_recovery_jobs
        WHEN EXISTS(SELECT 1 FROM payment_recovery_jobs WHERE payment_attempt_id=NEW.payment_attempt_id)
        BEGIN SELECT RAISE(ABORT,'Recovery history cannot be replaced'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS recovery_terminal BEFORE UPDATE ON payment_recovery_jobs
        WHEN OLD.state IN ('PROCESSED','QUARANTINED')
        BEGIN SELECT RAISE(ABORT,'Terminal recovery cannot be retried'); END''')


class RecoveryWorker:
    def __init__(self, services, *, policy=None, clock=time.time):
        self.processor = WebhookProcessor(services, policy=policy, clock=clock)
        self.services = self.processor.services
        self.policy, self.clock = self.processor.policy, clock

    def run_once(self, attempt_id, *, reclaim_expired=False):
        attempt = store.read_attempt(attempt_id)
        if attempt['state'] in ('COMPLETED', 'FAILED'):
            # A crash can leave work PROCESSING after the payment commit.
            # Settle an existing eligible job without creating work or replaying
            # any provider/payment/completion operation.
            with closing(get_connection()) as conn:
                exists = conn.execute('SELECT 1 FROM payment_recovery_jobs WHERE payment_attempt_id=?',
                                      (attempt_id,)).fetchone()
            if exists:
                row = work.claim('payment_recovery_jobs', attempt_id, policy=self.policy, clock=self.clock,
                                 reclaim_expired=reclaim_expired)
                if row is not None:
                    work.finish('payment_recovery_jobs', row, 'PROCESSED', policy=self.policy, clock=self.clock)
            return attempt['state']  # No new dispatch, verification or completion.
        with work.transaction() as conn:
            if not conn.execute('SELECT 1 FROM payment_recovery_jobs WHERE payment_attempt_id=?', (attempt_id,)).fetchone():
                conn.execute('INSERT INTO payment_recovery_jobs(payment_attempt_id) VALUES (?)', (attempt_id,))
        row = work.claim('payment_recovery_jobs', attempt_id, policy=self.policy, clock=self.clock,
                         reclaim_expired=reclaim_expired)
        if row is None:
            return 'SKIPPED'
        fence = work.guard('payment_recovery_jobs', row, clock=self.clock)
        state, error = 'PROCESSED', None
        try:
            attempt = store.read_attempt(attempt_id)
            service = self.services.get(attempt['provider_scope'])
            if service is None:
                state, error = 'RETRYABLE', 'scope_unavailable'
            else:
                if attempt['state'] in ('RESERVED', 'CREATING'):
                    service.recover_creation(attempt_id, guard=fence)
                    attempt = store.read_attempt(attempt_id)
                if attempt['state'] == 'CREATING':
                    state, error = 'RETRYABLE', 'order_unresolved'
                elif attempt['state'] == 'ORDER_READY':
                    # Only authenticated durable hints supply a payment ID.
                    # Each invocation handles at most 20 inbox records.
                    for identity in webhook_store.pending(limit=20, scope=attempt['provider_scope'],
                                                           order_id=attempt['provider_order_id']):
                        self.processor.run_once(identity, reclaim_expired=reclaim_expired, guard=fence)
                    attempt = store.read_attempt(attempt_id)
                    if attempt['state'] == 'ORDER_READY':
                        state, error = 'RETRYABLE', 'awaiting_evidence'
                if attempt['state'] == 'VERIFIED':
                    service.complete(attempt['checkout_id'], attempt_id, guard=fence)
        except work.ClaimLost:
            return 'CLAIM_LOST'
        except Exception as exc:
            state, error = classify(exc)
        try:
            return work.finish('payment_recovery_jobs', row, state, error=error, policy=self.policy, clock=self.clock)
        except work.ClaimLost:
            return 'CLAIM_LOST'

    def run_batch(self, attempt_ids, *, reclaim_expired=False):
        if type(attempt_ids) not in (list, tuple) or not 1 <= len(attempt_ids) <= 100:
            raise ValueError('Supply 1 to 100 existing attempt IDs')
        return [(identity, self.run_once(identity, reclaim_expired=reclaim_expired)) for identity in attempt_ids]
