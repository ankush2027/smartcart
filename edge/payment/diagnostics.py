"""Read-only local report: python -m edge.payment.diagnostics [--database PATH]."""
import argparse
import json
from pathlib import Path
import sqlite3

from edge.database import database


def report(path=None, *, limit=100, after_attempt='', after_webhook=0):
    if (type(limit) is not int or not 1 <= limit <= 1000 or not isinstance(after_attempt, str)
            or type(after_webhook) is not int or after_webhook < 0):
        raise ValueError('Invalid report bound')
    target = Path(path if path is not None else database.DATABASE_PATH).resolve()
    conn = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        payments = [dict(r) for r in conn.execute('''SELECT p.payment_attempt_id,p.checkout_id,p.provider_scope,
            p.state AS payment_state,COALESCE(j.state,'NOT_SCHEDULED') AS recovery_status,
            COALESCE(j.error_code,p.error_code) AS error_code
            FROM payment_attempts p LEFT JOIN payment_recovery_jobs j USING(payment_attempt_id)
            WHERE p.payment_attempt_id>? ORDER BY p.payment_attempt_id LIMIT ?''', (after_attempt, limit))]
        inbox = [dict(r) for r in conn.execute('''SELECT w.webhook_id,w.provider_scope,w.state,
            w.processing_attempts,w.last_attempted_at,w.error_code,p.payment_attempt_id,p.checkout_id
            FROM payment_webhooks w LEFT JOIN payment_attempts p
            ON p.provider_scope=w.provider_scope AND p.provider_order_id=w.provider_order_id
            WHERE w.webhook_id>? ORDER BY w.webhook_id LIMIT ?''', (after_webhook, limit))]
        return dict(payments=payments, webhooks=inbox)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='Read-only payment recovery diagnostics')
    parser.add_argument('--database')
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--after-attempt', default='')
    parser.add_argument('--after-webhook', type=int, default=0)
    args = parser.parse_args()
    try:
        result = report(args.database, limit=args.limit, after_attempt=args.after_attempt, after_webhook=args.after_webhook)
    except Exception:
        parser.exit(1, 'Payment diagnostics unavailable; check the database and migration state.\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
