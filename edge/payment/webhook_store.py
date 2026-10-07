"""Durable authenticated evidence. All accepting returns follow a commit."""
from contextlib import closing
import hashlib
import time

from edge.database.database import get_connection
from .provider import AuthenticatedWebhook, InvalidEvidence
from .store import _one, _key
from . import work


def migrate_webhooks(conn):
    conn.execute(f'''CREATE TABLE IF NOT EXISTS payment_webhooks (
        webhook_id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_scope TEXT NOT NULL CHECK(length(provider_scope) BETWEEN 1 AND 128),
        provider_event_id TEXT NOT NULL CHECK(length(provider_event_id) BETWEEN 1 AND 256),
        event_type TEXT NOT NULL CHECK(length(event_type) BETWEEN 1 AND 128),
        payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
        raw_payload BLOB NOT NULL CHECK(typeof(raw_payload)='blob' AND length(raw_payload) BETWEEN 1 AND 1048576),
        provider_order_id TEXT NOT NULL CHECK(length(provider_order_id) BETWEEN 1 AND 256),
        provider_payment_id TEXT NOT NULL CHECK(length(provider_payment_id) BETWEEN 1 AND 256),
        received_at INTEGER NOT NULL CHECK(typeof(received_at)='integer' AND received_at>=0),
        {work.WORK_COLUMNS},
        UNIQUE(provider_scope,provider_event_id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS payment_webhook_conflicts (
        conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
        webhook_id INTEGER NOT NULL REFERENCES payment_webhooks(webhook_id) ON DELETE RESTRICT,
        payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
        raw_payload BLOB NOT NULL CHECK(typeof(raw_payload)='blob' AND length(raw_payload) BETWEEN 1 AND 1048576),
        event_type TEXT NOT NULL,
        provider_order_id TEXT NOT NULL,
        provider_payment_id TEXT NOT NULL,
        received_at INTEGER NOT NULL,
        UNIQUE(webhook_id,payload_digest)
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS webhook_order ON payment_webhooks(provider_scope,provider_order_id,webhook_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS webhook_pending ON payment_webhooks(state,next_attempt_at,webhook_id)')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS webhook_identity_immutable BEFORE UPDATE OF
        webhook_id,provider_scope,provider_event_id,event_type,payload_digest,raw_payload,
        provider_order_id,provider_payment_id,received_at ON payment_webhooks
        BEGIN SELECT RAISE(ABORT,'Webhook evidence is immutable'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS webhook_no_replace BEFORE INSERT ON payment_webhooks
        WHEN EXISTS(SELECT 1 FROM payment_webhooks WHERE webhook_id=NEW.webhook_id
            OR (provider_scope=NEW.provider_scope AND provider_event_id=NEW.provider_event_id))
        BEGIN SELECT RAISE(ABORT,'Webhook evidence cannot be replaced'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS webhook_conflict_no_replace BEFORE INSERT ON payment_webhook_conflicts
        WHEN EXISTS(SELECT 1 FROM payment_webhook_conflicts WHERE conflict_id=NEW.conflict_id
            OR (webhook_id=NEW.webhook_id AND payload_digest=NEW.payload_digest))
        BEGIN SELECT RAISE(ABORT,'Conflicting evidence cannot be replaced'); END''')
    for table in ('payment_webhooks', 'payment_webhook_conflicts'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_retained BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'Webhook evidence is retained'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS webhook_conflict_immutable BEFORE UPDATE ON payment_webhook_conflicts
        BEGIN SELECT RAISE(ABORT,'Conflicting evidence is immutable'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS webhook_terminal BEFORE UPDATE ON payment_webhooks
        WHEN OLD.state='QUARANTINED' OR (OLD.state='PROCESSED' AND NEW.state!='QUARANTINED')
        BEGIN SELECT RAISE(ABORT,'Terminal webhook cannot be retried'); END''')


def validate(event, body):
    if (not isinstance(event, AuthenticatedWebhook) or type(body) is not bytes
            or not 0 < len(body) <= 1048576):
        raise InvalidEvidence('Invalid webhook evidence')
    _key(event.provider_scope)
    for value, limit in ((event.event_id, 256), (event.event_type, 128),
                         (event.order_id, 256), (event.payment_id, 256)):
        if type(value) is not str or not 1 <= len(value) <= limit or any(not 33 <= ord(c) <= 126 for c in value):
            raise InvalidEvidence('Invalid webhook metadata')


def insert_authenticated(event, body, *, clock=time.time):
    """Internal trusted sink. External input must use WebhookIngestor."""
    validate(event, body)
    digest, now = hashlib.sha256(body).hexdigest(), work.timestamp(clock)
    with work.transaction() as conn:
        row = _one(conn, 'SELECT * FROM payment_webhooks WHERE provider_scope=? AND provider_event_id=?',
                    (event.provider_scope, event.event_id))
        if row is None:
            identity = conn.execute('''INSERT INTO payment_webhooks(provider_scope,provider_event_id,event_type,
                payload_digest,raw_payload,provider_order_id,provider_payment_id,received_at)
                VALUES (?,?,?,?,?,?,?,?)''', (event.provider_scope, event.event_id, event.event_type,
                digest, body, event.order_id, event.payment_id, now)).lastrowid
            result = dict(webhook_id=identity, state='RECEIVED', duplicate=False)
        else:
            conflict = (row['payload_digest'] != digest or row['raw_payload'] != body
                        or row['provider_order_id'] != event.order_id
                        or row['provider_payment_id'] != event.payment_id or row['event_type'] != event.event_type)
            if conflict:
                if not conn.execute('SELECT 1 FROM payment_webhook_conflicts WHERE webhook_id=? AND payload_digest=?',
                                    (row['webhook_id'], digest)).fetchone():
                    conn.execute('''INSERT INTO payment_webhook_conflicts(webhook_id,payload_digest,
                        raw_payload,event_type,provider_order_id,provider_payment_id,received_at) VALUES (?,?,?,?,?,?,?)''',
                        (row['webhook_id'], digest, body, event.event_type, event.order_id, event.payment_id, now))
                if row['state'] != 'QUARANTINED':
                    conn.execute("UPDATE payment_webhooks SET state='QUARANTINED',error_code='digest_conflict',"
                                 'claim_token=NULL,claim_expires_at=NULL WHERE webhook_id=?', (row['webhook_id'],))
            result = dict(webhook_id=row['webhook_id'], state='QUARANTINED' if conflict else row['state'], duplicate=True)
    return result  # COMMIT has succeeded, including on the duplicate path.


def read(webhook_id):
    with closing(get_connection()) as conn:
        return _one(conn, 'SELECT * FROM payment_webhooks WHERE webhook_id=?', (webhook_id,))


def pending(*, limit=20, after_id=0, scope=None, order_id=None):
    if type(limit) is not int or not 1 <= limit <= 100 or type(after_id) is not int or after_id < 0:
        raise ValueError('Invalid work bound')
    with closing(get_connection()) as conn:
        return [r[0] for r in conn.execute("SELECT webhook_id FROM payment_webhooks WHERE webhook_id>? "
            "AND state IN ('RECEIVED','RETRYABLE','PROCESSING') AND (? IS NULL OR provider_scope=?) "
            'AND (? IS NULL OR provider_order_id=?) ORDER BY webhook_id LIMIT ?',
            (after_id, scope, scope, order_id, order_id, limit))]
