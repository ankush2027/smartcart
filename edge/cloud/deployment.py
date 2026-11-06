"""Validation helpers for the local macOS named-tunnel deployment."""
from pathlib import Path
import re
import stat

_UUID = re.compile(r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$")
_HOST = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


def parse_named_tunnel_config(path):
    """Read strict KEY=value config; values cannot contain shell/YAML syntax."""
    values = {}
    for line_no, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in {"TUNNEL_NAME", "HOSTNAME", "CREDENTIALS_FILE"} or key in values:
            raise ValueError(f"invalid deployment config at line {line_no}")
        values[key] = value
    if set(values) != {"TUNNEL_NAME", "HOSTNAME", "CREDENTIALS_FILE"}:
        raise ValueError("deployment config requires tunnel UUID, stable hostname and credentials path")
    tunnel, host, credentials = values["TUNNEL_NAME"], values["HOSTNAME"], values["CREDENTIALS_FILE"]
    if not _UUID.fullmatch(tunnel):
        raise ValueError("TUNNEL_NAME must be the Cloudflare tunnel UUID")
    host = host.lower().rstrip(".")
    if (not _HOST.fullmatch(host) or host.endswith((".trycloudflare.com", ".example.com", ".example.org", ".example.net", ".invalid", ".test"))):
        raise ValueError("HOSTNAME must be a real stable hostname on your Cloudflare-managed domain")
    credential_path = Path(credentials)
    if not credential_path.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_./-]+", credentials):
        raise ValueError("CREDENTIALS_FILE must be an absolute path without whitespace or config metacharacters")
    if credential_path.is_symlink() or not credential_path.is_file():
        raise ValueError("Cloudflare credentials file is missing or is a symlink")
    if stat.S_IMODE(credential_path.stat().st_mode) not in (0o600, 0o400):
        raise ValueError("Cloudflare credentials file must have mode 600 or 400")
    return {"TUNNEL_NAME": tunnel, "HOSTNAME": host, "CREDENTIALS_FILE": str(credential_path)}


def validate_api_base_url(value, hostname):
    expected = "https://" + hostname
    if value != expected:
        raise ValueError("API URL must exactly match the configured stable HTTPS hostname")
    return expected
