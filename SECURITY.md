# Security

## Model

MeshAtlas is a **read-only** observer.

- **Network exposure.** By default it binds to the machine's Tailscale IPv4 address, so only devices your tailnet ACLs
  allow can reach it. `host = "0.0.0.0"` exposes it on every interface; do that only behind a firewall and with a
  token. For HTTPS and identity, put it behind `tailscale serve`.
- **Optional token.** `token` / `MESHATLAS_TOKEN` requires the token once per browser (`?token=…`), then a
  `HttpOnly; SameSite=Strict` cookie. API clients can send `Authorization: Bearer <token>`.
- **Commands on the hub.** `tailscale status --json`, `tailscale ping`, `tailscale debug derp-map`,
  `tailscale netcheck`, `ping`, `ssh` with `BatchMode=yes`, and `python3 -` for the same scripts the SSH targets get
  (when the hub collects itself or pings `[[external]]` servers) and, with the proxy view on, the proxy probe below.
- **Commands on SSH targets.** A Python script fed to `python3 -` on stdin; nothing is written to the remote disk.
  It runs `docker ps`, `docker stats`, `systemctl list-units`, reads `/proc`, `/etc/os-release` and filesystem usage,
  and (with mesh enabled) `tailscale ping`. It tries `sudo -n docker` only if plain `docker` is denied, and never
  prompts for a password. It never runs `docker inspect` and never reads environment variables.
- **Proxy view (only when `[proxy]` has sources).** The hub and every SSH node that measures a proxy hop also run a
  short probe script on `python3 -` (`PROBE_SCRIPT` in [`src/meshatlas/proxy.py`](src/meshatlas/proxy.py); nothing is
  written to disk). It resolves proxy server hostnames with the system resolver (not with `resolve_hostnames = false`),
  opens a TCP connection to each server's port and closes it without sending data, and runs `ping` / `ping6` for UDP
  protocols. It never speaks a proxy protocol, and MeshAtlas never reads Mihomo's config or API while running.
  Snapshot files (`meshatlas proxy-snapshot`) contain no passwords, UUIDs, keys or subscription URLs, but they do list
  your proxy servers' addresses and ports: keep them outside any git checkout. If a measuring machine runs a TUN /
  transparent proxy, the probe connection goes through that proxy (see the README).
- **What reaches the browser.** Node names, Tailscale IPs, OS, container names/images/ports, unit names and
  descriptions, metrics and events. The map API never sends SSH targets or external IP addresses of tailnet
  peers. With the proxy view on, `/api/proxy` also sends proxy and group names, protocols, proxy server addresses
  (IPs or hostnames) and ports, the labels, origins and notes of `[[proxy.sources]]`, the labels, roles, locations and
  nodes from `[proxy.endpoints]`, which node measured each hop, and the results. A server mapped to a tailnet node
  with `node = ...` is sent with its address, which may be that node's public IP.
- **Headers.** A strict Content-Security-Policy (`script-src 'self'`), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `frame-ancestors 'none'`.
- **No telemetry.** MeshAtlas reports nothing to anyone. Besides the tailnet itself it only contacts what you
  configure: the optional Uptime Kuma status page, ICMP pings to `[[external]]` servers and, with the proxy view, DNS
  lookups of proxy server hostnames plus TCP connects and pings to the proxy servers, once per `[proxy] interval`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Security" → "Report a vulnerability") rather than a public
issue. You will get a reply within a week.
