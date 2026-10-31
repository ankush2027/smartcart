#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
LABEL_BASE="com.smartcart.demo"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$ROOT/logs"
CONFIG="$ROOT/config/demo-deployment.conf"
TOKEN_FILE="/private/tmp/smartcart-demo-$(id -u)/handshake"
fail(){ printf 'SmartCart: %s\n' "$1" >&2; exit 1; }
[[ "$(git -C "$ROOT" branch --show-current)" == feature/hardware-integration ]] || fail 'expected branch feature/hardware-integration'
if [[ $# -ge 1 ]]; then
  case "$1" in
    demo|status|logs|stop) exec "$HOME/Desktop/smart-cart/.venv/bin/python" -B "$ROOT/scripts/demo_orchestrator.py" "$1" ;;
  esac
fi
case "${1:-}" in
  install|start|restart|build-apk) fail 'named-tunnel mode is disabled for this demo; use scripts/smartcart.sh demo' ;;
esac
command -v launchctl >/dev/null || fail 'launchctl unavailable (macOS required)'
read_config(){
  [[ -f "$CONFIG" && ! -L "$CONFIG" ]] || fail "create $CONFIG from config/demo-deployment.conf.example and fill in your real Cloudflare tunnel name, hostname, and credentials path"
  local parsed
  parsed="$(PYTHONDONTWRITEBYTECODE=1 "$HOME/Desktop/smart-cart/.venv/bin/python" -B - "$ROOT" "$CONFIG" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from edge.cloud.deployment import parse_named_tunnel_config
try:
 c=parse_named_tunnel_config(sys.argv[2])
 print(c['TUNNEL_NAME']+'\t'+c['HOSTNAME']+'\t'+c['CREDENTIALS_FILE'])
except Exception as e:
 raise SystemExit('SmartCart: invalid named tunnel config: '+str(e)) from None
PY
  )" || fail 'named tunnel configuration validation failed'
  IFS=$'\t' read -r TUNNEL_NAME HOSTNAME CREDENTIALS <<< "$parsed"
}
write_plist(){
  local label="$1" program="$2" target="$AGENTS/$1.plist"
  [[ ! -e "$target" && ! -L "$target" ]] || fail "launchd definition already exists at $target; inspect it before replacing anything"
  cat > "$target" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$label</string>
<key>ProgramArguments</key><array><string>$program</string></array>
<key>WorkingDirectory</key><string>$ROOT</string>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>$LOGS/$label.log</string><key>StandardErrorPath</key><string>$LOGS/$label.err.log</string>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
</dict></plist>
PLIST
  chmod 600 "$target"
}
[[ $# -eq 1 ]] || fail 'usage: scripts/smartcart.sh {install|start|stop|restart|status|logs|build-apk}'
action="$1"
case "$action" in
 install)
  read_config
  command -v cloudflared >/dev/null || fail 'install cloudflared and ensure it is on PATH'
  [[ ! -L "$AGENTS" && ! -L "$HOME/.cloudflared" && ! -L "$LOGS" ]] || fail 'refusing symlinked launch agent, Cloudflare, or project log directory'
  [[ ! -e "$HOME/.cloudflared/smartcart-demo.yml" && ! -L "$HOME/.cloudflared/smartcart-demo.yml" ]] || fail 'SmartCart Cloudflare config already exists; inspect and preserve it before installation'
  mkdir -p "$AGENTS" "$LOGS" "$HOME/.cloudflared"
  chmod 700 "$LOGS"
  cat > "$HOME/.cloudflared/smartcart-demo.yml" <<YAML
 tunnel: $TUNNEL_NAME
 credentials-file: $CREDENTIALS
 ingress:
  - hostname: $HOSTNAME
    service: http://127.0.0.1:5001
  - service: http_status:404
YAML
  chmod 600 "$HOME/.cloudflared/smartcart-demo.yml"
  write_plist "$LABEL_BASE.backend" "$ROOT/scripts/demo-backend.sh"
  write_plist "$LABEL_BASE.tunnel" "$ROOT/scripts/demo-named-tunnel.sh"
  printf 'Installed launchd definitions and private local tunnel config. Services are not started.\n'
  ;;
 start|restart)
  read_config
  if [[ "$action" == restart ]]; then "$0" stop; fi
  if [[ "$action" == restart ]]; then
    for _ in {1..30}; do
      if ! lsof -nP -iTCP:5001 -sTCP:LISTEN >/dev/null 2>&1 && ! lsof -nP -iTCP:5002 -sTCP:LISTEN >/dev/null 2>&1; then break; fi
      sleep 1
    done
  fi
  for port in 5001 5002; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      label="$LABEL_BASE.$([[ "$port" == 5001 ]] && echo backend || echo tunnel)"
      if ! launchctl list | awk '{print $3}' | grep -Fxq "$label"; then
        printf 'Port %s is occupied by an unmanaged process:\n' "$port" >&2
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
        fail 'identify it and stop it from its own service/terminal; SmartCart will not kill it'
      fi
    fi
  done
  mkdir -p "$LOGS"
  for service in backend tunnel; do
   label="$LABEL_BASE.$service"
   if launchctl list | awk '{print $3}' | grep -Fxq "$label"; then continue; fi
   launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist" 2>/dev/null || fail "could not start $service; run install first and inspect status/logs"
  done
  printf 'SmartCart managed services requested. Use scripts/smartcart.sh status and logs to verify.\n'
  ;;
 stop)
  for service in tunnel backend; do launchctl bootout "gui/$(id -u)/$LABEL_BASE.$service" 2>/dev/null || true; done
  printf 'Stop requested for SmartCart launch agents. Existing demo sessions and SQLite records are preserved.\n'
  ;;
 status)
  read_config
  for service in backend tunnel; do launchctl list | awk -v l="$LABEL_BASE.$service" '$3==l{print "launchd " l ": running"; found=1} END{if(!found) print "launchd " l ": stopped"}'; done
  curl --noproxy '*' -fsS --max-time 3 http://127.0.0.1:5001/health/ready || printf 'backend readiness unavailable\n'
  if [[ -f "$TOKEN_FILE" ]]; then "$ROOT/scripts/demo-named-tunnel.sh" --check 2>/dev/null || true; fi
  curl --noproxy '*' -fsS --max-time 5 "https://$HOSTNAME/health/ready" || printf 'public tunnel readiness unavailable\n'
  ;;
 logs)
  mkdir -p "$LOGS"; tail -n 80 -F "$LOGS"/*.log "$LOGS"/*.err.log
  ;;
 build-apk)
  read_config
  command -v flutter >/dev/null || fail 'Flutter SDK is required'
  "$ROOT/app/tool/build_release.sh" "https://$HOSTNAME"
  ;;
 *) fail 'unknown operation' ;;
esac
