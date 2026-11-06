"""Cloud composition: retains existing routes, ownership checks and billing."""
import json
import logging
import time
import uuid
from flask import g, jsonify, request
from werkzeug.exceptions import HTTPException
from edge.api.server import create_app
from edge.demo.composition import create_demo_app
from edge.database.database import get_connection
from edge.payment.service import PaymentService
from edge.cloud.demo_provider import PersistentDemoProvider
from edge.cloud.outbox import Publisher
from edge.cloud.preview import approved_uids, PreviewLimiter

logger = logging.getLogger('smartcart.cloud')


def verify_claims(token):
    from firebase_admin import auth
    return auth.verify_id_token(token, check_revoked=True)


def create_cloud_app(*, verifier=None, publisher=None, demo=False, demo_allowed_uids=(),
                     preview_uids=(), allow_all_verified_customers=False,
                     rate_limits=None, rate_clock=time.monotonic):
    preview_users = approved_uids(preview_uids)
    limiter = PreviewLimiter(preview_users, rate_limits, clock=rate_clock)
    allowed_demo_users = frozenset(demo_allowed_uids)
    verifier = verifier or verify_claims
    publisher = publisher or Publisher()
    # Claims are verified once by the boundary before existing route handlers.
    def cached_verifier(token):
        return g.claims
    kwargs = dict(token_verifier=cached_verifier, firebase_sync_fn=publisher.flush)
    if demo:
        provider = PersistentDemoProvider()
        service = PaymentService(provider=provider, completion_sync=publisher.flush)
        app = create_demo_app(**kwargs, payment_service=service, fake_provider=provider)
    else:
        app = create_app(**kwargs)
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024, PROPAGATE_EXCEPTIONS=False)

    def rate_limited(retry):
        response = jsonify(error='rate_limited', message='Too many requests. Please retry later.')
        response.status_code = 429
        response.headers['Retry-After'] = str(retry)
        return response

    @app.before_request
    def boundary():
        g.started = time.monotonic()
        g.request_id = uuid.uuid4().hex
        retry = limiter.global_retry_after()
        if retry:
            return rate_limited(retry)
        if request.path in ('/health/live', '/health/ready'):
            return None
        if not request.is_secure:
            return jsonify(error='https_required', message='HTTPS is required.'), 400
        # Native Android needs no CORS. Reject browser-origin requests by default.
        if request.headers.get('Origin'):
            return jsonify(error='origin_not_allowed', message='Browser access is disabled.'), 403
        if request.url_rule is None:
            return None
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer ') or not header[7:]:
            return jsonify(error='authentication_required', message='Sign in to continue.'), 401
        if len(header) > 8192:
            return jsonify(error='invalid_token', message='Please sign in again.'), 401
        try:
            claims = verifier(header[7:])
            if not isinstance(claims, dict) or not isinstance(claims.get('uid'), str) or not claims['uid']:
                raise ValueError()
            g.claims = claims
        except Exception:
            return jsonify(error='invalid_token', message='Please sign in again.'), 401
        if not allow_all_verified_customers and claims['uid'] not in preview_users:
            return jsonify(error='preview_access_denied', message='This account is not approved for this preview.'), 403
        retry = limiter.user_retry_after(claims['uid'])
        if retry:
            return rate_limited(retry)
        if request.method == 'POST':
            if not request.is_json:
                return jsonify(error='invalid_request', message='JSON is required.'), 415
            body = request.get_json(silent=True)
            fields = ({'cart_id'} if request.path == '/pair-cart' else
                      {'payment_id', 'signature'} if request.path.endswith('/payment/evidence') else set())
            if not isinstance(body, dict) or set(body) != fields:
                return jsonify(error='invalid_request', message='Unexpected request fields.'), 400
        role = claims.get('role', 'customer')
        required = 'manager' if request.path.startswith('/manager/') else 'customer'
        if role != required:
            return jsonify(error='forbidden', message='This account cannot perform this action.'), 403
        # Gate all fake-provider payment paths, including client evidence: protecting
        # only the simulate endpoint would leave the fake signature path available.
        if demo and ('/payment' in request.path or request.path.startswith('/demo/')):
            if claims['uid'] not in allowed_demo_users:
                return jsonify(error='demo_not_allowed', message='Demo payment is restricted to approved test customers.'), 403

    @app.get('/health/live')
    def live():
        return jsonify(status='alive')

    @app.get('/health/ready')
    def ready():
        connection = None
        try:
            connection = get_connection()
            connection.execute('SELECT session_id FROM sessions LIMIT 1').fetchall()
            stale = connection.execute("""SELECT count(*) FROM cloud_sync_outbox
                WHERE queued_at < datetime('now','-5 minutes')""").fetchone()[0]
            return jsonify(status='not_ready' if stale else 'ready'), 503 if stale else 200
        except Exception:
            return jsonify(status='not_ready'), 503
        finally:
            if connection is not None: connection.close()

    @app.get('/manager/service-status')
    def manager_status():
        return jsonify(edge_ingress='not_implemented', demo_payment=demo)

    @app.post('/customer/register')
    def register_customer():
        """Idempotently provision the Firebase-verified customer UID only."""
        uid = g.claims['uid']
        connection = None
        try:
            connection = get_connection()
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('''INSERT INTO customers(customer_uid) VALUES (?)
                ON CONFLICT(customer_uid) DO UPDATE SET updated_at=CURRENT_TIMESTAMP''', (uid,))
            connection.commit()
            return jsonify(status='registered'), 200
        except Exception:
            if connection is not None:
                connection.rollback()
            logger.warning('{"event":"customer_registration_failed"}')
            return jsonify(error='registration_unavailable', message='Customer registration could not be completed.'), 503
        finally:
            if connection is not None:
                connection.close()

    @app.errorhandler(Exception)
    def safe_error(error):
        status = error.code if isinstance(error, HTTPException) else 500
        return jsonify(error='request_failed', message='Request could not be completed.'), status

    @app.after_request
    def observe(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Request-ID'] = g.get('request_id', '')
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        logger.info(json.dumps(dict(event='http_request', request_id=g.get('request_id'),
            method=request.method, route=request.url_rule.rule if request.url_rule else 'unmatched',
            status=response.status_code, duration_ms=round(1000*(time.monotonic()-g.get('started',time.monotonic()))))))
        return response
    return app
