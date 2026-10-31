"""The local launch handshake is private, loopback-only, and redacted."""
import pytest

from edge.cloud.diagnostic import attestation_response, create_attestation_server


def test_attestation_requires_nonce_and_returns_only_fixed_settings():
    settings = {
        "service": "smartcart-local-demo", "status": "ready",
        "listen_host": "127.0.0.1", "port": 5001,
        "verified_customer_admission": True, "demo_payment": False,
    }
    for supplied in ("", "Bearer " + "b" * 64):
        assert attestation_response(path="/v1/attestation", client_ip="127.0.0.1",
                                   supplied=supplied, token="a" * 64,
                                   attestation=settings) == (403, {"status": "forbidden"})
    status, body = attestation_response(path="/v1/attestation", client_ip="127.0.0.1",
                                        supplied="Bearer " + "a" * 64, token="a" * 64,
                                        attestation=settings)
    assert status == 200 and body == settings
    assert "a" * 64 not in repr(body)


@pytest.mark.parametrize("path", ["/", "/health/live", "/v1/attestation/other"])
def test_attestation_only_serves_exact_private_path(path):
    assert attestation_response(path=path, client_ip="127.0.0.1",
                                supplied="Bearer " + "a" * 64, token="a" * 64,
                                attestation={}) == (404, {"status": "not_found"})


@pytest.mark.parametrize("client_ip", ["192.0.2.1", "not-an-ip"])
def test_attestation_rejects_non_loopback_peers(client_ip):
    assert attestation_response(path="/v1/attestation", client_ip=client_ip,
                                supplied="Bearer " + "a" * 64, token="a" * 64,
                                attestation={}) == (403, {"status": "forbidden"})


def test_attestation_server_is_constructed_for_loopback_only(monkeypatch):
    captured = {}

    class FakeServer:
        def __init__(self, address, handler):
            captured["address"] = address
            captured["handler"] = handler

    monkeypatch.setattr("edge.cloud.diagnostic.ThreadingHTTPServer", FakeServer)
    server = create_attestation_server("a" * 64, host="127.0.0.1", port=5001,
                                       verified_customer_admission=True, demo_payment=False)
    assert captured["address"] == ("127.0.0.1", 5002)
    assert server.handshake_token == "a" * 64
    assert server.attestation["demo_payment"] is False


@pytest.mark.parametrize("token,host,port,admission,demo", [
    ("short", "127.0.0.1", 5001, True, False),
    ("a" * 64, "0.0.0.0", 5001, True, False),
    ("a" * 64, "127.0.0.1", 5000, True, False),
    ("a" * 64, "127.0.0.1", 5001, False, False),
    ("a" * 64, "127.0.0.1", 5001, True, True),
])
def test_attestation_refuses_unexpected_local_demo_configuration(token, host, port, admission, demo):
    with pytest.raises(ValueError):
        create_attestation_server(token, host=host, port=port,
                                  verified_customer_admission=admission,
                                  demo_payment=demo)


def test_startup_validator_rejects_bad_or_unsafe_handshake_configuration(tmp_path):
    from edge.cloud.local import local_environment
    from edge.cloud.run import validate_configuration
    from tests.test_local_backend import local_fixture

    config = local_environment(local_fixture(tmp_path))
    config["SMARTCART_DEMO_HANDSHAKE_TOKEN"] = "not-a-random-token"
    with pytest.raises(RuntimeError, match="handshake"):
        validate_configuration(config)

    config["SMARTCART_DEMO_HANDSHAKE_TOKEN"] = "a" * 64
    config["SMARTCART_DEMO_PAYMENT"] = "true"
    config["SMARTCART_DEMO_ALLOWED_UIDS"] = "customer-test"
    with pytest.raises(RuntimeError, match="simulated payment off"):
        validate_configuration(config)
