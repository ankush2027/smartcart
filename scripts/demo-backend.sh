#!/bin/bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$HOME/Desktop/smart-cart/.venv/bin/python"
STATE_DIR="/private/tmp/smartcart-demo-${UID}"
TOKEN_FILE="$STATE_DIR/handshake"

fail() { printf 'SmartCart demo startup blocked: %s\n' "$1" >&2; exit 1; }
listener() { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null; }

cd "$ROOT"
[[ "$(git branch --show-current)" == "feature/hardware-integration" ]] || fail 'wrong branch; expected feature/hardware-integration'
[[ -x "$PYTHON" ]] || fail 'configured Python environment is unavailable'
command -v lsof >/dev/null || fail 'lsof is required for safe port preflight'
command -v openssl >/dev/null || fail 'openssl is required to create the private startup handshake'
[[ -f data/smartcart_demo.db ]] || fail 'existing demo SQLite database is missing; refusing to create a replacement'

for port in 5001 5002; do
  if listener "$port" >/dev/null; then
    printf 'Port %s already has a listener:\n' "$port" >&2
    listener "$port" >&2 || true
    fail 'do not kill an unidentified process; stop a known prior SmartCart process from its original Terminal, then retry'
  fi
done
"$PYTHON" -B - <<'PY'
import socket
for port in (5001, 5002):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        raise SystemExit(f"SmartCart demo startup blocked: port {port} cannot be bound") from None
    finally:
        sock.close()
PY

PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -m edge.cloud.local --check || fail 'local demo configuration validation failed'

if [[ -L "$STATE_DIR" ]]; then fail 'private handshake directory is a symlink'; fi
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
[[ "$(stat -f '%u' "$STATE_DIR")" == "$(id -u)" ]] || fail 'private handshake directory has an unexpected owner'
umask 077
temporary_token=""
cleanup() {
  [[ -z "$temporary_token" ]] || rm -f "$temporary_token"
  rm -f "$TOKEN_FILE"
}
trap cleanup EXIT
temporary_token="$(mktemp "$STATE_DIR/handshake.XXXXXX")"
openssl rand -hex 32 > "$temporary_token" || fail 'could not create local handshake'
chmod 600 "$temporary_token"
mv -f "$temporary_token" "$TOKEN_FILE"
IFS= read -r SMARTCART_DEMO_HANDSHAKE_TOKEN < "$TOKEN_FILE"
[[ "$SMARTCART_DEMO_HANDSHAKE_TOKEN" =~ ^[0-9a-f]{64}$ ]] || fail 'local handshake has invalid format'
export SMARTCART_DEMO_HANDSHAKE_TOKEN

backup="data/smartcart_demo.db.before-demo-$(date +%Y%m%d-%H%M%S)-$$.bak"
cp -p data/smartcart_demo.db "$backup" || fail 'SQLite backup failed; backend not started'
printf 'Preflight passed. SQLite backup saved at %s\n' "$backup"
printf 'Starting SmartCart on 127.0.0.1:5001; private startup attestation is on 127.0.0.1:5002. Demo payment is disabled.\n'
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -m edge.cloud.local
