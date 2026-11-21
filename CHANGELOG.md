# Changelog

## 0.1.0 — unreleased

First public release.

- Zero-config discovery from `tailscale status`; locations from the DERP map, with a `netcheck` fallback for hubs
  on custom DERP regions.
- Built-in latency: `tailscale ping` from the hub, full mesh from SSH nodes, ICMP for external servers.
- Read-only SSH collection: containers, systemd units, CPU, memory, disks.
- Map with direct / relayed / ICMP links, mesh view and latency matrix; node detail pages; ⌘K search; an issues list
  whose alerts open the failing unit, container or monitor; per-browser settings; browser notifications; English and Chinese,
  switched from the top bar with a decrypt-style text transition.
- SQLite history (7 days): latency, CPU / memory, availability, events.
- `meshatlas run | init | demo | check`, Docker image, systemd unit, static demo site.
- Optional proxy view: the routes of Mihomo / Clash instances from secret-free snapshots (`meshatlas proxy-snapshot`),
  each hop timed (TCP connect or ICMP) on the machine it leaves when MeshAtlas can run commands there, the option in
  use inferred with Mihomo's rules; hops whose hostname resolves to a local fake-IP are flagged `intercepted`, and
  `resolve_hostnames = false` keeps proxy hostnames away from the measuring machines' DNS. `[proxy]` in the config,
  `api/proxy`, a Proxy map mode. Off unless configured.
