"""Operator CLI for controlled recovery; never invoked by pairing or runtime."""
import argparse
import sys

from edge.billing.session_projection import ProjectionError, validate_session_id
from edge.firebase.reconcile import ReconciliationError, reconcile_session_to_firebase


def _session_id(value):
    try:
        result = int(value)
        validate_session_id(result)
        return result
    except ValueError:
        raise argparse.ArgumentTypeError('session_id must be a positive SQLite integer') from None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Controlled Firebase metadata/bill recovery from authoritative SQLite. '
                    'Stop relevant API/runtime writers and resolve in-flight work first. '
                    'Quiescence is required but is NOT automatically checked.',
    )
    parser.add_argument('session_id', type=_session_id)
    args = parser.parse_args(argv)
    print('Controlled recovery: writers must be quiesced and in-flight work resolved.', file=sys.stderr)
    try:
        result = reconcile_session_to_firebase(args.session_id)
    except ProjectionError:
        print('Recovery refused: missing/invalid SQLite session or bill. No Firebase write attempted.',
              file=sys.stderr)
        return 1
    except ReconciliationError:
        print('Firebase recovery failed or outcome is uncertain. SQLite is unchanged. '
              'Resolve in-flight work before another controlled reconciliation; do not replay billing.',
              file=sys.stderr)
        return 1
    except Exception:
        # Avoid exposing SDK details, credential paths or source data.
        print('Recovery failed. Check backend configuration and authoritative source; '
              'no billing was replayed.', file=sys.stderr)
        return 1
    state = 'completed' if result.completed else 'active'
    print(f'Reconciled session {result.session_id}: {result.bill_item_count} bill items, '
          f'{state}. Event history preserved; no SQLite changes.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
