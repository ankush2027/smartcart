from pathlib import Path
import pytest

from edge.cloud.deployment import parse_named_tunnel_config, validate_api_base_url


def _config(tmp_path, *, tunnel="12345678-1234-1234-1234-123456789abc", host="cart.demo.org", cred=None):
    credential = cred or tmp_path / "credentials.json"
    if cred is None:
        credential.write_text("{}"); credential.chmod(0o600)
    path = tmp_path / "deployment.conf"
    path.write_text(f"TUNNEL_NAME={tunnel}\nHOSTNAME={host}\nCREDENTIALS_FILE={credential}\n")
    return path


def test_named_tunnel_config_requires_actual_uuid_domain_and_private_credentials(tmp_path):
    parsed = parse_named_tunnel_config(_config(tmp_path))
    assert parsed["HOSTNAME"] == "cart.demo.org"
    assert parsed["TUNNEL_NAME"].endswith("9abc")


@pytest.mark.parametrize("host", ["cart.example.com", "abc.trycloudflare.com", "not a hostname", "-bad.org"])
def test_rejects_placeholder_or_ephemeral_hostname(tmp_path, host):
    with pytest.raises(ValueError):
        parse_named_tunnel_config(_config(tmp_path, host=host))


def test_rejects_insecure_credentials_permissions(tmp_path):
    credential = tmp_path / "credentials.json"
    credential.write_text("{}"); credential.chmod(0o644)
    with pytest.raises(ValueError, match="mode"):
        parse_named_tunnel_config(_config(tmp_path, cred=credential))


def test_build_requires_exact_configured_stable_url():
    assert validate_api_base_url("https://cart.demo.org", "cart.demo.org") == "https://cart.demo.org"
    for value in ("https://old.demo.org", "https://x.trycloudflare.com", "http://cart.demo.org"):
        with pytest.raises(ValueError):
            validate_api_base_url(value, "cart.demo.org")
