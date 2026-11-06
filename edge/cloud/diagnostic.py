"""Authenticated, loopback-only startup attestation for the local demo launcher."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json


class _AttestationHandler(BaseHTTPRequestHandler):
    server_version = "SmartCartLocalAttestation"
    sys_version = ""

    def do_GET(self):
        status, payload = attestation_response(
            path=self.path, client_ip=self.client_address[0],
            supplied=self.headers.get("Authorization", ""),
            token=self.server.handshake_token, attestation=self.server.attestation)
        self._reply(status, payload)

    def _reply(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Never log the authorization header or local attestation traffic.
        return


def create_attestation_server(token, *, host, port, verified_customer_admission,
                              demo_payment):
    """Bind a private attestation listener; it is never part of Flask/Cloudflare."""
    if not isinstance(token, str) or len(token) != 64 or any(
            char not in "0123456789abcdef" for char in token):
        raise ValueError("Local demo handshake token must be 32-byte lowercase hex")
    if host != "127.0.0.1" or port != 5001 or verified_customer_admission is not True or demo_payment is not False:
        raise ValueError("Local demo attestation requires loopback, open verified customers, and demo payment off")
    server = ThreadingHTTPServer(("127.0.0.1", 5002), _AttestationHandler)
    server.daemon_threads = True
    server.handshake_token = token
    server.attestation = {
        "service": "smartcart-local-demo",
        "status": "ready",
        "listen_host": host,
        "port": port,
        "verified_customer_admission": verified_customer_admission,
        "demo_payment": demo_payment,
    }
    return server


def attestation_response(*, path, client_ip, supplied, token, attestation):
    """Pure request decision, separately testable without opening a socket."""
    if path != "/v1/attestation":
        return 404, {"status": "not_found"}
    try:
        peer = ipaddress.ip_address(client_ip)
    except ValueError:
        peer = None
    expected = "Bearer " + token
    if peer is None or not peer.is_loopback or not hmac.compare_digest(supplied, expected):
        return 403, {"status": "forbidden"}
    return 200, attestation
