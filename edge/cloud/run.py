"""Single-instance cloud entry point behind a managed HTTPS reverse proxy."""
import fcntl
import logging
import ipaddress
import os
import sys
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlparse
from edge.cloud.preview import preview_settings


def server_options(environment):
    host = environment.get('SMARTCART_API_HOST', '127.0.0.1').strip()
    try:
        ipaddress.ip_address(host)
        port = int(environment.get('PORT', '5001'))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise RuntimeError('Configure a literal listen IP and port from 1 to 65535') from None
    options = dict(host=host, port=port, threads=4, max_request_body_size=16384,
                   clear_untrusted_proxy_headers=True, expose_tracebacks=False)
    proxy = environment.get('SMARTCART_TRUSTED_PROXY', '').strip()
    if proxy:
        try:
            # No wildcard, subnet, DNS name or header-supplied peer identity.
            ipaddress.ip_address(proxy)
        except ValueError:
            raise RuntimeError('Trusted proxy must be one explicit IP address; wildcard trust is disabled') from None
        options.update(trusted_proxy=proxy, trusted_proxy_count=1,
                       trusted_proxy_headers={'x-forwarded-proto'})
    return options


def validate_configuration(environment):
    path = Path(environment.get('SMARTCART_DATABASE_PATH', ''))
    if not path.is_absolute() or not path.parent.is_dir() or not path.is_file():
        raise RuntimeError('Provision the approved SQLite database on persistent disk before starting; refusing an empty replacement')
    firebase_url = urlparse(environment.get('SMARTCART_FIREBASE_URL', ''))
    if (firebase_url.scheme != 'https' or not firebase_url.hostname or firebase_url.username
            or firebase_url.password or firebase_url.query or firebase_url.fragment):
        raise RuntimeError('Firebase requires its HTTPS database URL')
    if not environment.get('SMARTCART_ALLOWED_CARTS'):
        raise RuntimeError('Explicit cart allowlist required')
    if not Path(environment.get('GOOGLE_APPLICATION_CREDENTIALS', '')).is_file():
        raise RuntimeError('Mount the approved Firebase service account as a secret file')
    demo = environment.get('SMARTCART_DEMO_PAYMENT', 'false')
    if demo not in ('true', 'false'):
        raise RuntimeError('Demo payment flag must be true or false')
    if demo == 'true' and not environment.get('SMARTCART_DEMO_ALLOWED_UIDS', '').strip():
        raise RuntimeError('Simulated payment requires an explicit test-customer allowlist')
    users, _ = preview_settings(environment)
    allow_all_customers = environment.get('SMARTCART_ALLOW_ALL_VERIFIED_CUSTOMERS', 'false')
    if allow_all_customers not in ('true', 'false'):
        raise RuntimeError('Verified-customer admission flag must be true or false')
    handshake_token = environment.get('SMARTCART_DEMO_HANDSHAKE_TOKEN')
    if handshake_token is not None and (len(handshake_token) != 64 or
            any(char not in '0123456789abcdef' for char in handshake_token)):
        raise RuntimeError('Invalid local demo handshake configuration')
    if handshake_token is not None:
        options = server_options(environment)
        if (options['host'] != '127.0.0.1' or options['port'] != 5001
                or allow_all_customers != 'true' or demo != 'false'):
            raise RuntimeError('Local demo handshake requires loopback, verified-customer admission, and simulated payment off')
    software = environment.get('SMARTCART_SOFTWARE_INPUT', 'false')
    if software not in ('true', 'false'):
        raise RuntimeError('Software input flag must be true or false')
    if software == 'true':
        demo_users = {uid.strip() for uid in environment.get('SMARTCART_DEMO_ALLOWED_UIDS', '').split(',') if uid.strip()}
        if demo != 'true' or not demo_users or not demo_users <= users:
            raise RuntimeError('Software demo requires preview-approved demo customers')
        if 'CART-001' not in environment['SMARTCART_ALLOWED_CARTS'].split(','):
            raise RuntimeError('Software demo requires CART-001 in the cart allowlist')
    return path


def main():
    path = validate_configuration(os.environ)
    options = server_options(os.environ)
    preview_users, limits = preview_settings(os.environ)
    with (path.parent / 'smartcart-cloud.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Do not log legacy exception payloads, IDs, tokens or SDK credential errors.
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        for handler in logging.getLogger().handlers:
            handler.addFilter(lambda record: record.name == 'smartcart.cloud')
        log = logging.getLogger('smartcart.cloud')
        log.setLevel(logging.INFO)
        from edge.database.database import initialize_database
        from edge.firebase.client import initialize
        from edge.cloud.outbox import install, Publisher
        from edge.cloud.app import create_cloud_app
        from waitress import serve
        handshake_token = os.getenv('SMARTCART_DEMO_HANDSHAKE_TOKEN')
        attestation = None
        if handshake_token:
            from edge.cloud.diagnostic import create_attestation_server
            # Bind the diagnostic socket before Firebase or SQLite startup work.
            attestation = create_attestation_server(
                handshake_token, host=options['host'], port=options['port'],
                verified_customer_admission=os.getenv('SMARTCART_ALLOW_ALL_VERIFIED_CUSTOMERS') == 'true',
                demo_payment=os.getenv('SMARTCART_DEMO_PAYMENT') == 'true')
        # Validate SDK setup before any additive local schema changes.
        initialize()
        initialize_database()
        install()
        publisher = Publisher()
        stop = Event()
        def publish():
            while not stop.is_set():
                try:
                    publisher.flush()
                except Exception:
                    log.warning('{"event":"projection_retry_pending"}')
                stop.wait(5)
        app = create_cloud_app(preview_uids=preview_users, allow_all_verified_customers=os.getenv('SMARTCART_ALLOW_ALL_VERIFIED_CUSTOMERS') == 'true', rate_limits=limits, publisher=publisher, demo=os.getenv('SMARTCART_DEMO_PAYMENT') == 'true',
                               demo_allowed_uids=frozenset(uid.strip() for uid in
                                   os.getenv('SMARTCART_DEMO_ALLOWED_UIDS', '').split(',') if uid.strip()))
        supervisor = None
        supervisor_thread = None
        if os.getenv('SMARTCART_SOFTWARE_INPUT') == 'true':
            from edge.cloud.software import create_software_supervisor
            supervisor, console = create_software_supervisor(publisher)
            Thread(target=console.read, daemon=True).start()
            supervisor_thread = Thread(target=supervisor.run_forever, daemon=True)
            supervisor_thread.start()
        worker = Thread(target=publish, daemon=True)
        worker.start()
        attestation_thread = None
        try:
            if attestation is not None:
                attestation_thread = Thread(target=attestation.serve_forever, daemon=True)
                attestation_thread.start()
            serve(app, **options)
        finally:
            if attestation is not None:
                attestation.shutdown()
                attestation.server_close()
                attestation_thread.join(timeout=5)
            if supervisor is not None:
                supervisor.request_stop()
                supervisor_thread.join(timeout=10)
            stop.set()
            worker.join(timeout=10)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Backend startup failed; check approved configuration, database access and Firebase setup.', file=sys.stderr)
        raise SystemExit(1) from None
