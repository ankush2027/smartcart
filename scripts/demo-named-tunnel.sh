#!/bin/bash
set -euo pipefail
echo 'Named tunnels are not used by the local demo. Run: bash scripts/smartcart.sh demo' >&2
exit 1
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
CONFIG="$ROOT/config/demo-deployment.conf"
TOKEN_FILE="/private/tmp/smartcart-demo-$(id -u)/handshake"
fail(){ printf 'SmartCart tunnel blocked: %s\n' "$1" >&2; exit 1; }
[[ -f "$CONFIG" ]] || fail 'named tunnel configuration missing; run scripts/smartcart.sh install after Cloudflare setup'
HOSTNAME="$(PYTHONDONTWRITEBYTECODE=1 "$HOME/Desktop/smart-cart/.venv/bin/python" -B - "$ROOT" "$CONFIG" <<'PY'
import sys
sys.path.insert(0,sys.argv[1])
from edge.cloud.deployment import parse_named_tunnel_config
try: print(parse_named_tunnel_config(sys.argv[2])['HOSTNAME'])
except Exception as e: raise SystemExit('SmartCart tunnel blocked: '+str(e)) from None
PY
)" || fail 'stable named-tunnel configuration invalid'
[[ -f "$TOKEN_FILE" && ! -L "$TOKEN_FILE" ]] || fail 'backend private handshake missing; backend is not verified'
"$HOME/Desktop/smart-cart/.venv/bin/python" -B - <<'PY'
import json, os
from pathlib import Path
from urllib.request import Request,urlopen
p=Path(f"/private/tmp/smartcart-demo-{os.getuid()}/handshake")
t=p.read_text().strip()
if len(t)!=64: raise SystemExit('SmartCart tunnel blocked: invalid private handshake')
r=Request('http://127.0.0.1:5002/v1/attestation',headers={'Authorization':'Bearer '+t})
try:
 with urlopen(r,timeout=3) as x:
  a=json.load(x)
  if x.status!=200 or a!={'service':'smartcart-local-demo','status':'ready','listen_host':'127.0.0.1','port':5001,'verified_customer_admission':True,'demo_payment':False}: raise SystemExit('SmartCart tunnel blocked: startup attestation mismatch')
 for u in ('http://127.0.0.1:5001/health/live','http://127.0.0.1:5001/health/ready'):
  with urlopen(u,timeout=3) as x:
   if x.status!=200: raise SystemExit('SmartCart tunnel blocked: backend health check failed')
except Exception as e: raise SystemExit(f'SmartCart tunnel blocked: backend verification failed ({type(e).__name__})') from None
PY
if [[ "${1:-}" == "--check" ]]; then
  printf 'Backend handshake, security settings, liveness, and readiness passed.\n'
  exit 0
fi
CLOUDFLARED="$(command -v cloudflared)" || fail 'cloudflared is not on launchd PATH'
exec "$CLOUDFLARED" tunnel --config "$HOME/.cloudflared/smartcart-demo.yml" run
