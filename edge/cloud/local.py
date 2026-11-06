"""Local-only launcher. Loads configuration as data; never sources shell code.

The legacy .env database setting is deliberately NOT used: select the existing
hardware-workspace database. No database or credential file is copied/reset.
"""
import argparse
import os
from pathlib import Path
import shlex
import sys
from edge.cloud.preview import PREVIEW_KEYS

FIREBASE_KEYS = {'SMARTCART_FIREBASE_URL', 'GOOGLE_APPLICATION_CREDENTIALS',
                 'SMARTCART_ALLOWED_CARTS'}


def local_environment(root, *, demo_uids=()):
    root = Path(root).resolve()
    values = {}
    for number, line in enumerate((root / '.env').read_text().splitlines(), 1):
        line = line.strip().removeprefix('export ')
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        key = key.strip()
        if key not in FIREBASE_KEYS:
            continue
        try:
            parts = shlex.split(value, comments=True, posix=True)
            if not separator or len(parts) != 1 or not parts[0] or key in values:
                raise ValueError()
        except ValueError:
            raise RuntimeError(f'Invalid local configuration at line {number}; values omitted') from None
        values[key] = parts[0]
    if set(values) != FIREBASE_KEYS:
        raise RuntimeError('Local .env is missing required Firebase/cart settings')
    database = root / 'data' / 'smartcart_demo.db'
    if not database.is_file() or database.resolve().parent != (root / 'data').resolve():
        raise RuntimeError('Expected an existing local database; refusing creation or external redirection')
    # Also reject an external data directory symlink.
    if not database.resolve().is_relative_to(root):
        raise RuntimeError('Local database must remain inside the hardware workspace')
    credential = Path(values['GOOGLE_APPLICATION_CREDENTIALS']).expanduser()
    if not credential.is_absolute():
        credential = root / credential
    if not credential.is_file():
        raise RuntimeError('Configured Firebase Admin credential file is unavailable')
    values['GOOGLE_APPLICATION_CREDENTIALS'] = str(credential.resolve())
    uids = tuple(demo_uids)
    if any(not isinstance(uid, str) or not uid.strip() or ',' in uid or len(uid) > 128 for uid in uids):
        raise RuntimeError('Demo customer IDs must be nonempty individual Firebase UIDs')
    values.update(SMARTCART_DATABASE_PATH=str(database), SMARTCART_API_HOST='127.0.0.1',
                  PORT='5001', SMARTCART_TRUSTED_PROXY='127.0.0.1',
                  SMARTCART_DEMO_PAYMENT='true' if uids else 'false',
                  SMARTCART_DEMO_ALLOWED_UIDS=','.join(uids))
    # Preview configuration comes only from explicit process environment.
    for key in PREVIEW_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    # The local project-demo launcher explicitly admits any verified Firebase
    # customer. This does not relax token verification, roles, or ownership.
    values['SMARTCART_ALLOW_ALL_VERIFIED_CUSTOMERS'] = 'true'
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description='Start loopback-only SmartCart backend; no tunnel is started.')
    parser.add_argument('--software-input', action='store_true', help='Enable local simulated product console for CART-001; no physical recognition')
    parser.add_argument('--check', action='store_true', help='Validate config without SDK, database writes or sockets')
    parser.add_argument('--demo-customer-uid', action='append', default=[],
                        help='Explicitly allow this test customer to use simulated payment (repeatable)')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    values = local_environment(root, demo_uids=args.demo_customer_uid)
    if args.software_input and not args.demo_customer_uid:
        parser.error('--software-input requires --demo-customer-uid')
    values['SMARTCART_SOFTWARE_INPUT'] = 'true' if args.software_input else 'false'
    from edge.cloud.run import server_options, validate_configuration
    validate_configuration(values)
    server_options(values)
    if args.check:
        print('Local configuration valid; no service, SDK or database writes started. All protected access still requires a valid Firebase ID token and role; cart/session ownership remains enforced.')
        return
    os.environ.update(values)
    from edge.cloud.run import main as run
    run()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Local backend check/start failed; verify local database and approved Firebase configuration.', file=sys.stderr)
        raise SystemExit(1) from None
