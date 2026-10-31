# SmartCart persistent local demo

> Superseded by [smartcart_demo_workflow.md](smartcart_demo_workflow.md). The current requested workflow uses one terminal and Cloudflare Quick Tunnel; do not follow the named-tunnel/launchd steps below.

The intended daily control is one command: `scripts/smartcart.sh start` (or `status`, `stop`, `restart`, `logs`, `build-apk`). macOS launchd runs the backend and Cloudflare named tunnel as background agents, restarts failed processes, and keeps logs in `logs/`. Installation and status do not start a tunnel. Do not use the Quick Tunnel script for the persistent demo.

## One-time Cloudflare setup

A stable public URL requires (1) a Cloudflare account, (2) a domain whose DNS is managed by that account, (3) a named tunnel and DNS hostname such as `cart.your-domain.tld`, and (4) that tunnel's credentials JSON on this Mac. No hostname, account, tunnel, or credential is supplied by this repository. Follow Cloudflare's named-tunnel setup to create the tunnel, route the DNS hostname to it, and download the tunnel credentials file. Keep the credential file private (mode 600 or 400); never commit it.

Copy `config/demo-deployment.conf.example` to `config/demo-deployment.conf` and enter the actual tunnel UUID, domain hostname, and absolute credentials-file path. The config is local and should not contain tokens. Confirm `.env` and Firebase Admin setup are already present. The existing `.env` allowlist includes CART-001 and CART-002; the active CART-001 customer assignment is preserved. Demo payment stays disabled.

## Install once

From the repository:

```sh
cd $HOME/Desktop/smart-cart-hardware && bash scripts/smartcart.sh install
```

This creates two per-user launchd agents and a local cloudflared routing config. It does not start them. It refuses missing/invalid named-tunnel configuration and never falls back to a random URL. Before first `start`, identify any current listener on 5001/5002. The script refuses unmanaged listeners rather than killing them. Stop only a confirmed prior SmartCart process from its original Terminal/service.

## Daily operation

```sh
cd $HOME/Desktop/smart-cart-hardware && bash scripts/smartcart.sh start
```

Check operation and logs:

```sh
cd $HOME/Desktop/smart-cart-hardware && bash scripts/smartcart.sh status
cd $HOME/Desktop/smart-cart-hardware && bash scripts/smartcart.sh logs
```

Status reports launchd agents, local readiness, private attestation/security settings, and public `https://<configured-hostname>/health/ready`. The named tunnel forwards only to `http://127.0.0.1:5001`; port 5002 remains loopback-only. Use `https://<configured-hostname>` as the app API URL. The hostname is stable as long as the Cloudflare named tunnel and DNS route remain configured.

Stop/restart:

```sh
cd $HOME/Desktop/smart-cart-hardware && bash scripts/smartcart.sh stop
cd $HOME/Desktop/smart-cart-hardware && bash scripts/smartcart.sh restart
```

Logs are `logs/com.smartcart.demo.backend.log`, `.err.log`, `logs/com.smartcart.demo.tunnel.log`, and `.err.log`. launchd starts the services when the user logs in and restarts failed processes. It cannot keep a laptop reachable while powered off/asleep or logged out; the Mac must be awake and on a working network for phone access.

## Release APK

The release embeds `SMARTCART_API_BASE_URL` at build time. Run this only after the real hostname is configured and public readiness passes:

```sh
cd $HOME/Desktop/smart-cart-hardware && bash scripts/smartcart.sh build-apk
```

The validator rejects Quick Tunnel and placeholder URLs. Rebuild/reinstall only when app code or stable API hostname changes. Stable hostname avoids routine rebuilds.

## Troubleshooting

- **Port occupied:** inspect `lsof -nP -iTCP:5001 -sTCP:LISTEN` and port 5002. Do not kill unknown processes. Stop the identified SmartCart service cleanly, then retry.
- **Backend/attestation not ready:** run `scripts/smartcart.sh logs`; verify local `.env`, Firebase Admin credential file, database path, and unchanged database presence. Do not replace or reset SQLite.
- **Tunnel agent fails:** verify the account's named tunnel UUID, DNS hostname route, credentials file path and permissions, cloudflared installation, then `restart` and inspect logs. Missing named-tunnel settings block startup; no Quick Tunnel fallback occurs.
- **Public readiness fails:** compare the configured hostname with the Cloudflare tunnel's DNS route and check network connectivity. Private attestation is never sent through Cloudflare.
- **Phone cannot connect:** check public health, then confirm installed APK was built with the configured stable URL and reinstall the latest release.
