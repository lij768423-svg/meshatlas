"""Proxy view (optional): the routes of Mihomo / Clash instances, with every hop timed from the machine it leaves.

Topology comes from snapshot files written by ``meshatlas proxy-snapshot``: proxy names, protocols, servers, ports,
``dialer-proxy`` and groups, never passwords or keys. At runtime MeshAtlas does not read Mihomo configs, does not call
the Mihomo API and never sends a byte through a proxy.

Which route is *in use* is inferred the way Mihomo picks it:
  * ``select``: the first option, even one with no route (a manual choice may differ; the page says it is inferred)
  * ``fallback`` / ``load-balance``: the first option with no hop down and no stale endpoint
  * ``url-test``: the lowest total latency among working options, else the first working one
A member with no route (REJECT, a name the snapshot lacks, an empty group, a broken chain) keeps its place as a
placeholder option that is never ok. ``DIRECT`` and proxies of type ``direct`` add no hop; types ``reject`` and ``dns``
behave like ``REJECT``.

Measurement: a TCP connect to the hop's server and port, three times (median and loss), or an ICMP ping for UDP
protocols (Hysteria, Hysteria2, TUIC, WireGuard, and ``udp`` for a ``next`` hop). Only a connection is opened; no proxy
protocol is spoken. Each hop is measured on the machine it really leaves: the hub probes locally, SSH nodes over SSH.
A first hop leaving a node MeshAtlas cannot run commands on (a laptop, say) is measured from the hub instead and flagged
``ref``. A server name that does not resolve counts as down. A name that resolves into 198.18.0.0/15 (fake-IP) means
the measuring machine runs a TUN / fake-IP proxy that would answer the connect itself: that hop is ``intercepted``,
with no time. With ``resolve_hostnames = false`` MeshAtlas resolves no names at all and measures IP literals only.
"""

import base64
import ipaddress
import json
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .derp import region as derp_region

UDP_PROTOS = frozenset({"hysteria", "hysteria2", "tuic", "wireguard"})
ICMP_PROTOS = UDP_PROTOS | {"udp"}            # measured with ICMP ping; everything else with a TCP connect
# Mihomo proxy types a server can forward with; `next.proto` takes these, "tcp" or "udp"
PROXY_TYPES = frozenset({"anytls", "http", "hysteria", "hysteria2", "mieru", "snell", "socks5", "ss", "ssh", "ssr",
                         "trojan", "tuic", "vless", "vmess", "wireguard"})
NEXT_PROTOS = PROXY_TYPES | {"tcp", "udp"}
ROLES = ("relay", "residential", "provider", "stale")
DIRECT = "DIRECT"
REJECTS = frozenset({"REJECT", "REJECT-DROP", "COMPATIBLE"})   # built-in outbounds that drop the connection
DIRECT_TYPES = frozenset({"direct"})                            # proxy types that behave like DIRECT
REJECT_TYPES = frozenset({"reject", "dns"})                     # proxy types that behave like REJECT
NO_RESOLVE = "hostname (resolve_hostnames = false)"
MAX_MEMBERS = 4      # options drawn (and measured) per group
MAX_DEPTH = 6        # nesting guard (groups in groups, dialer chains)
MAX_PASSES = 16      # plan(): passes over a source until its routes stop growing (cycles need a second one)

# The only keys a snapshot may carry. Everything else in a Mihomo proxy (password, uuid, keys, sni, ...) is dropped.
PROXY_KEYS = ("name", "type", "server", "port", "dialer-proxy", "network", "provider")
GROUP_KEYS = ("name", "type", "proxies", "use")

# Places proxy providers use often that are not DERP regions.
BUILTIN_LOCATIONS = {
    "tpe": {"name": "Taipei", "code": "TPE", "lat": 25.03, "lon": 121.57},
    "sel": {"name": "Seoul", "code": "SEL", "lat": 37.57, "lon": 126.98},
}


def _codes(*codes):
    """Match short country / airport codes only as a separate token: "HK 01" and "JP-2" but not "Shkoder"."""
    return r"(?<![a-z])(?:" + "|".join(codes) + r")(?![a-z])"


# Provider node names -> (location key, label). [[proxy.regions]] from the config are tried first.
DEFAULT_REGIONS = [
    (r"hong ?kong|香港|🇭🇰|" + _codes("hk", "hkg"), "hkg", "Hong Kong"),
    (r"taiwan|taipei|台湾|台灣|🇹🇼|" + _codes("tw", "twn", "tpe"), "tpe", "Taiwan"),
    (r"singapore|新加坡|🇸🇬|" + _codes("sg", "sgp", "sin"), "sin", "Singapore"),
    (r"japan|tokyo|osaka|日本|东京|東京|🇯🇵|" + _codes("jp", "jpn", "tyo", "nrt"), "tok", "Japan"),
    (r"korea|seoul|韩国|韓國|🇰🇷|" + _codes("kr", "kor", "icn"), "sel", "South Korea"),
    (r"united states|america|美国|美國|🇺🇸|" + _codes("us", "usa", "lax", "sjc"), "lax", "United States"),
    (r"united kingdom|britain|london|英国|英國|🇬🇧|" + _codes("uk", "gb", "gbr", "lhr"), "lhr", "United Kingdom"),
    (r"germany|frankfurt|德国|德國|🇩🇪|" + _codes("de", "deu", "fra"), "fra", "Germany"),
    (r"netherlands|amsterdam|荷兰|荷蘭|🇳🇱|" + _codes("nl", "nld", "ams"), "ams", "Netherlands"),
    (r"france|paris|法国|法國|🇫🇷|" + _codes("fr", "par"), "par", "France"),
    (r"canada|toronto|加拿大|🇨🇦|" + _codes("ca", "can", "yyz"), "tor", "Canada"),
    (r"australia|sydney|澳大利亚|澳洲|🇦🇺|" + _codes("au", "aus", "syd"), "syd", "Australia"),
]

# Runs on the measuring machine (fed to `python3 -`, nothing is written to disk). Targets arrive base64-encoded in
# argv[1]; prints {probe key: [ms or null, x3], {"state": "down" | "intercepted", ...} or "error text"}.
# Must stay compatible with Python 3.6.
PROBE_SCRIPT = r'''
import base64, ipaddress, json, re, socket, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
TRIES = 3
WAIT = "2000" if sys.platform == "darwin" else "2"   # ping -W is milliseconds on macOS, seconds on Linux
FAKE_IP = ipaddress.ip_network("198.18.0.0/15")      # fake-IP range of Mihomo / Clash / sing-box
def resolve(host, port, kind):
    # the getaddrinfo entry to measure, or a verdict: the name does not resolve, or it resolves to a fake IP (this
    # machine runs a TUN / fake-IP proxy, which would answer the connect itself)
    try:
        info = socket.getaddrinfo(host, port, 0, kind)
    except (OSError, ValueError):
        info = []
    if not info:
        return {"state": "down", "error": "cannot resolve"}
    if info[0][0] == socket.AF_INET and ipaddress.ip_address(info[0][4][0]) in FAKE_IP:
        return {"state": "intercepted", "addr": info[0][4][0]}
    return info[0]
def tcp(host, port):
    got = resolve(host, int(port), socket.SOCK_STREAM)
    if isinstance(got, dict):
        return got
    fam, kind, proto, _, addr = got
    out = []
    for i in range(TRIES):
        if i:
            time.sleep(0.15)
        s = socket.socket(fam, kind, proto)
        s.settimeout(3)
        t = time.perf_counter()
        try:
            s.connect(addr)
            out.append(round((time.perf_counter() - t) * 1000, 1))
        except OSError:
            out.append(None)
        finally:
            s.close()
    return out
def icmp(host):
    got = resolve(host, None, socket.SOCK_DGRAM)
    if isinstance(got, dict):
        return got
    ip = got[4][0]
    if got[0] == socket.AF_INET6 and sys.platform == "darwin":
        cmd = ["ping6", "-c", str(TRIES), ip]
    else:
        cmd = ["ping", "-c", str(TRIES), "-W", WAIT, "-i", "0.3", ip]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return "ping failed: %s" % exc
    got = [float(x) for x in re.findall(r"time[=<] ?([0-9.]+)", r.stdout)][:TRIES]
    return got + [None] * (TRIES - len(got))
def one(t):
    try:
        return t["k"], tcp(t["h"], t["p"]) if t["m"] == "tcp" else icmp(t["h"])
    except Exception as exc:
        return t["k"], "%s: %s" % (type(exc).__name__, exc)
def main():
    targets = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    with ThreadPoolExecutor(12) as pool:
        print(json.dumps(dict(pool.map(one, targets))))
if __name__ == "__main__":
    main()
'''


# ---------- snapshots ----------

def as_port(v):
    """A TCP/UDP port as int, or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    return v if isinstance(v, int) and 0 < v < 65536 else None


def _scalar(v):
    return str(v).strip() if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def clean_proxy(p):
    """Keep only the whitelisted keys of one proxy, as plain strings (port: int). None without a name."""
    if not isinstance(p, dict):
        return None
    out = {}
    for k in PROXY_KEYS:
        v = as_port(p.get(k)) if k == "port" else _scalar(p.get(k))
        if k == "type" and v:
            v = v.lower()
        if v not in (None, ""):
            out[k] = v
    return out if out.get("name") else None


def clean_group(g):
    """Keep only name, type, proxies and use of one proxy group. None without a name."""
    if not isinstance(g, dict) or not _scalar(g.get("name")):
        return None
    out = {"name": _scalar(g["name"]), "type": (_scalar(g.get("type")) or "select").lower(), "proxies": []}
    for k in ("proxies", "use"):
        items = [_scalar(x) for x in g.get(k) or [] if _scalar(x)] if isinstance(g.get(k), list) else []
        if items or k == "proxies":
            out[k] = items
    return out


def read_snapshot(path):
    """Load a snapshot file, keeping whitelisted keys only. Raises ValueError with a readable message."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("proxies") or [], list) \
            or not isinstance(data.get("groups") or [], list):
        raise ValueError('expected a JSON object {"proxies": [...], "groups": [...]} from `meshatlas proxy-snapshot`')
    providers = [{"name": _scalar(p.get("name")), "type": _scalar(p.get("type")), "count": p.get("count")}
                 for p in data.get("providers") or [] if isinstance(p, dict)]
    return {"proxies": [p for p in map(clean_proxy, data.get("proxies") or []) if p],
            "groups": [g for g in map(clean_group, data.get("groups") or []) if g],
            "providers": providers}


# ---------- locations and regions ----------

def resolve_location(key, *tables):
    """(key, {name, code, lat, lon}) for a location key or DERP code, or (None, None) when unknown.

    ``tables`` are {key: {name, code, lat, lon}} dicts tried in order, then the DERP table, then BUILTIN_LOCATIONS."""
    if not key or not isinstance(key, str):
        return None, None
    for k in dict.fromkeys((key, key.lower())):
        for table in tables:
            v = (table or {}).get(k)
            if isinstance(v, dict) and all(isinstance(v.get(f), (int, float)) for f in ("lat", "lon")):
                return k, {"name": v.get("name") or k, "code": v.get("code") or k.upper(), "lat": v["lat"], "lon": v["lon"]}
    low = key.lower()
    reg = derp_region(low)
    if reg:
        return low, {"name": reg[0], "code": low.upper(), "lat": reg[1], "lon": reg[2]}
    if low in BUILTIN_LOCATIONS:
        return low, dict(BUILTIN_LOCATIONS[low])
    return None, None


def edge_key(frm, p):
    """Key of the hop to proxy ``p`` leaving ``frm`` (a node id for the first hop, else the previous server)."""
    return f"{frm}|{p.get('type') or ''}|{p.get('server') or ''}|{p.get('port') or ''}"


def _end_key(hop):
    """Key of a route end (the last hop, or None while still at the origin)."""
    return None if hop is None else edge_key("", hop)


def is_ip(host):
    """True for an IPv4 / IPv6 literal (``[...]`` and a ``%zone`` allowed): measuring it resolves no name."""
    try:
        ipaddress.ip_address(str(host).strip("[]").split("%")[0])
    except ValueError:
        return False
    return True


def outcome(got):
    """Edge fields for what the probe returned for one target: a list of three times (ms or None), a verdict
    ({"state": "down", "error": "cannot resolve"} or {"state": "intercepted", "addr": fake IP}) or an error text."""
    if isinstance(got, list) and got:
        ok = [x for x in got if isinstance(x, (int, float)) and not isinstance(x, bool)]
        return {"ms": round(statistics.median(ok), 1) if ok else None, "loss": round(1 - len(ok) / len(got), 2),
                "state": "down" if not ok else "lossy" if len(ok) < len(got) else "ok"}
    if isinstance(got, dict) and got.get("state") == "down":          # the name does not resolve: Mihomo fails too
        return {"state": "down", "error": str(got.get("error") or "cannot resolve")[:160]}
    if isinstance(got, dict) and got.get("state") == "intercepted":   # a fake IP: any time would be the local TUN's
        return {"state": "intercepted", "error": f"resolved to the fake IP {str(got.get('addr') or '?')[:64]}"}
    return {"state": "error", "error": got[:160] if isinstance(got, str) else "no result from the probe"}


def pick(kind, opts):
    """Index of the option a group of type ``kind`` most likely uses (Mihomo's rules), or None without options.

    ``select`` takes the first option even when it is a placeholder; the others skip options that are not ok."""
    if not opts:
        return None
    if kind == "select":
        return 0
    ok = [i for i, o in enumerate(opts) if o["ok"]]
    if not ok:
        return 0
    if kind == "url-test":
        timed = [i for i in ok if opts[i]["total"] is not None]
        return min(timed, key=lambda i: opts[i]["total"]) if timed else ok[0]
    return ok[0]


def _node_at(host, nodes):
    """Node id whose id, Tailscale IP or external address is ``host``."""
    if not host:
        return None
    if host in nodes:
        return host
    return next((nid for nid, n in nodes.items() if host in (n.get("tailscaleIp"), n.get("address"))), None)


def _last_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:160] if lines else ""


class ProxyView:
    """Loaded snapshots plus the last measurement round. ``collect()`` and ``api()`` may run in different threads.

    ``context()`` returns ``(nodes, hub, locations)``: the current node table (id -> node with ``loc``, ``ssh``,
    ``local``, ``tailscaleIp``...), the hub's node id and the known locations. It is called every round, so nodes
    discovered later are used as soon as they appear."""

    def __init__(self, cfg, context, run_local, ssh, base_locations=None):
        self.interval = max(60, int(cfg.get("interval") or 300))
        self.resolve_hostnames = bool(cfg.get("resolve_hostnames", True))
        self.endpoints = {str(k): v for k, v in (cfg.get("endpoints") or {}).items()}
        self.own_locations = dict(cfg.get("locations") or {})
        self.base_locations = dict(base_locations or {})
        self.regions = []
        for r in cfg.get("regions") or []:
            label = r.get("label") or (resolve_location(r["loc"], self.own_locations, self.base_locations)[1]
                                       or {}).get("name") or r["loc"]
            self.regions.append((re.compile(r["match"], re.I), r["loc"], label))
        self.regions += [(re.compile(rx, re.I), loc, label) for rx, loc, label in DEFAULT_REGIONS]
        self.context, self.run_local, self.ssh = context, run_local, ssh
        self.sources = [self._load(s) for s in cfg.get("sources") or []]
        self.lock = threading.Lock()
        self.results, self.at, self.elapsed, self.error = {}, None, None, None

    def _load(self, s):
        proxies, groups = {}, {}
        for f in s.get("files") or []:          # later files override earlier names
            snap = read_snapshot(f)
            proxies.update((p["name"], p) for p in snap["proxies"])
            groups.update((g["name"], g) for g in snap["groups"])
        by_provider = {}
        for p in proxies.values():
            if p.get("provider"):
                by_provider.setdefault(p["provider"], []).append(p["name"])
        for g in groups.values():               # "use": explicit members first, then the providers' proxies
            members = list(dict.fromkeys(g["proxies"]))
            for used in g.get("use") or []:
                members += [n for n in by_provider.get(used, []) if n not in members]
            g["proxies"] = members
        return {"id": str(s["id"]), "label": s.get("label") or str(s["id"]), "origin": str(s["origin"]),
                "originLabel": s.get("origin_label") or None, "note": s.get("note") or None,
                "entries": [(str(a), str(b)) for a, b in s.get("entries") or []],
                "proxies": proxies, "groups": groups}

    @staticmethod
    def _resolved(src, hub):
        """``src`` with origin "hub" replaced by the hub's node id (the alias probe_from accepts too), once known."""
        return dict(src, origin=hub) if src["origin"] == "hub" and hub else src

    @staticmethod
    def _what(src, name):
        """How ``name`` behaves: ("direct" | "reject" | "group" | "proxy", its group / proxy or None), or (None, None)
        for a name the snapshot does not know (PASS among them)."""
        if name == DIRECT:
            return "direct", None
        if name in REJECTS:
            return "reject", None
        g = src["groups"].get(name)
        if g is not None:
            return "group", g
        p = src["proxies"].get(name)
        if p is None:
            return None, None
        kind = p.get("type") or ""
        return ("direct" if kind in DIRECT_TYPES else "reject" if kind in REJECT_TYPES else "proxy"), p

    # ---------- topology ----------

    def endpoint(self, host, port=None):
        """The [proxy.endpoints] entry for host:port, else for host alone ({} when none)."""
        if not host:
            return {}
        host = str(host)
        if port is not None:
            for key in (f"{host}:{port}", f"[{host}]:{port}"):
                if key in self.endpoints:
                    return self.endpoints[key]
        return self.endpoints.get(host) or {}

    def _tail(self, p):
        """Hops a server forwards to by itself (``next`` on its endpoint), added so those legs are measured too."""
        out, seen, cur = [], set(), p
        while len(out) < 3:
            e = self.endpoint(cur.get("server"), cur.get("port"))
            nx = e.get("next")
            if not isinstance(nx, dict) or not nx.get("host"):
                break
            spot = (str(nx["host"]), nx.get("port"))
            if spot in seen:
                break
            seen.add(spot)
            cur = {"name": f"{e.get('label') or cur.get('server')} → {nx.get('label') or nx['host']}",
                   "type": str(nx.get("proto") or "tcp").lower(), "server": str(nx["host"]),
                   "port": as_port(nx.get("port")), "synthetic": True}
            out.append(cur)
        return out

    def hop_edges(self, src, route):
        """(edge key, leaves from, previous hop or None, hop) for every hop of a route."""
        out, frm, prev = [], src["origin"], None
        for p in route:
            out.append((edge_key(frm, p), frm, prev, p))
            frm, prev = p.get("server"), p
        return out

    def measurer(self, frm, prev, nodes, hub):
        """(node id that measures a hop leaving ``frm``, ref). (None, False) when nothing can measure it.

        The first hop (``prev`` None) leaves the origin node: it is measured there when that node is the hub, local or
        has SSH, else from the hub as a stand-in (ref). Every later hop leaves a server: it is measured on the node that
        server is (its endpoint's ``node``, else a node whose id or address the server is) if MeshAtlas can run
        commands there. A server has no stand-in, even when its address happens to be a node id."""
        if prev is None:
            if not frm or (frm not in nodes and frm != hub):
                return None, False
            node_id = frm
        else:
            node_id = self.endpoint(prev.get("server"), prev.get("port")).get("node") or _node_at(frm, nodes)
        if not node_id:
            return None, False
        n = nodes.get(node_id) or {}
        if node_id == hub or n.get("local") or n.get("ssh"):
            return node_id, False
        if prev is None and hub:
            return hub, True
        return None, False

    def region(self, name):
        """(location key, label) guessed from a provider node name, or (None, None)."""
        for rx, loc, label in self.regions:
            if rx.search(name or ""):
                return loc, label
        return None, None

    # ---------- measurement ----------

    def plan(self, hub=None):
        """{edge key: (from, previous hop or None, hop)} for every hop the inference can choose: all options of the
        first MAX_MEMBERS members of each group, followed through dialer-proxy, relay groups and endpoint ``next``.

        ``hub`` is the hub's node id, for sources whose origin is "hub". Each source is walked until its routes stop
        growing, so the result does not depend on the order of the entries, cycles included (a proxy in a group
        that the proxy dials through, as ``include-all`` makes)."""
        edges = {}
        for src in self.sources:
            src, memo = self._resolved(src, hub), {}
            for _ in range(MAX_PASSES):
                before = self._shape(edges, memo)
                seen = set()
                for _, name in src["entries"]:
                    self._reach(src, name, None, edges, memo, seen)
                if self._shape(edges, memo) == before:
                    break
        return edges

    @staticmethod
    def _shape(edges, memo):
        return len(edges), {state: frozenset(map(_end_key, ends)) for state, ends in memo.items()}

    def _reach(self, src, name, last, edges, memo, seen):
        """Possible last hops after taking ``name`` from hop ``last`` (None: still at the origin), recording edges.

        Edges depend only on where a hop leaves from, so walking (name, last hop) states covers every route without
        listing them all. A state is walked once per pass (``seen``); one met again while it is still being walked is
        a cycle and answers with what the previous pass found, so plan() repeats the passes until nothing changes."""
        state = (name, _end_key(last))
        if state in seen:
            return memo.get(state, [])
        seen.add(state)
        kind, obj = self._what(src, name)
        ends = {}
        if kind == "group" and obj["type"] == "relay":      # each member is dialed through the one before it
            cur = {state[1]: last}
            for m in obj["proxies"]:
                nxt = {}
                for hop in cur.values():
                    nxt.update((_end_key(e), e) for e in self._reach(src, m, hop, edges, memo, seen))
                cur = nxt
            ends = cur
        elif kind == "group":
            for m in obj["proxies"][:MAX_MEMBERS]:
                ends.update((_end_key(e), e) for e in self._reach(src, m, last, edges, memo, seen))
        elif kind in ("direct", "proxy"):
            d = obj.get("dialer-proxy") if obj else None
            heads = self._reach(src, d, last, edges, memo, seen) if d else [last]
            if kind == "direct":                             # DIRECT or a direct-type proxy: no hop of its own
                ends.update((_end_key(e), e) for e in heads)
            else:
                # no loop check here: a route that dials a proxy through itself only adds a hop nothing shows (best()
                # drops such routes), while checking by key would also drop real routes through a look-alike server
                route = [obj] + self._tail(obj)
                for head in heads:
                    prev = head
                    for hop in route:
                        frm = src["origin"] if prev is None else prev.get("server")
                        edges.setdefault(edge_key(frm, hop), (frm, prev, hop))
                        prev = hop
                    ends[_end_key(prev)] = prev
        # "reject" (REJECT, reject / dns proxies) and unknown names have no route
        for e in memo.get(state, []):                        # what earlier passes found stays (the walk only grows)
            ends.setdefault(_end_key(e), e)
        memo[state] = list(ends.values())
        return memo[state]

    def _probe(self, who, targets, nodes, hub):
        arg = base64.b64encode(json.dumps(targets).encode()).decode()
        timeout = 30 + 10 * (len(targets) // 12 + 1)
        n = nodes.get(who) or {}
        try:
            if who == hub or n.get("local"):
                rc, so, se = self.run_local(["python3", "-", arg], stdin=PROBE_SCRIPT.encode(), timeout=timeout)
            elif n.get("ssh"):
                rc, so, se = self.ssh(n["ssh"], "python3 - " + arg, stdin=PROBE_SCRIPT.encode(), timeout=timeout + 30)
            else:
                return None, "no way to run commands there"
            if rc != 0:
                return None, _last_line(se) or _last_line(so) or f"exit {rc}"
            return json.loads(so.strip().splitlines()[-1]), None
        except Exception as exc:  # noqa: BLE001 - one unreachable machine must not stop the round
            return None, f"{type(exc).__name__}: {exc}"[:160]

    def collect(self):
        """One measurement round over every hop of every candidate route."""
        t0 = time.time()
        nodes, hub, _ = self.context()
        edges = self.plan(hub)
        tasks, plan = {}, {}
        for key, (frm, prev, p) in edges.items():
            host, port = p.get("server"), p.get("port")
            method = "icmp" if (p.get("type") or "") in ICMP_PROTOS else "tcp"
            if host and not self.resolve_hostnames and not is_ip(host):
                plan[key] = (None, None, False, None, NO_RESOLVE)   # nothing may resolve the name: not measured
                continue
            who, ref = self.measurer(frm, prev, nodes, hub)
            if not who or not host or (method == "tcp" and not port):
                plan[key] = (None, None, False, None, None)
                continue
            pk = f"{method}|{host}|{port if method == 'tcp' else ''}"
            tasks.setdefault(who, {})[pk] = {"k": pk, "h": host, "p": port, "m": method}
            plan[key] = (who, pk, ref, method, None)
        got, errors = {}, {}
        if tasks:
            with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
                futures = {who: pool.submit(self._probe, who, list(t.values()), nodes, hub) for who, t in tasks.items()}
            for who, fut in futures.items():
                out, err = fut.result()
                if err:
                    errors[who] = err
                got[who] = out if isinstance(out, dict) else {}
        at = time.time()
        results = {}
        for key, (frm, _, p) in edges.items():
            who, pk, ref, method, note = plan[key]
            r = {"from": frm, "to": p.get("server"), "port": p.get("port"), "proto": p.get("type"), "method": None,
                 "measuredFrom": who, "ref": ref, "ms": None, "loss": None, "state": "unmeasured"}
            if note:
                r["error"] = note
            if who in errors:                    # the measuring machine itself failed (SSH, python3, the probe)
                r.update(method=method, state="error", error=errors[who])
            elif who:
                r.update(outcome(got.get(who, {}).get(pk)), method=method)
                if r["state"] != "error":
                    r["at"] = at
            results[key] = r
        with self.lock:
            self.results, self.at, self.elapsed = results, at, round(time.time() - t0, 1)
            self.error = "; ".join(f"{who}: {err}" for who, err in errors.items()) or None

    def run_round(self):
        """collect(), keeping the error visible in the API if the round itself crashes."""
        try:
            self.collect()
        except Exception as exc:
            with self.lock:
                self.error = f"{type(exc).__name__}: {exc}"[:200]
            raise

    # ---------- inference + API ----------

    def _loc(self, key, known):
        """Resolved location key: [proxy.locations], then [locations], the hub's known places, DERP, built-ins."""
        return resolve_location(key, self.own_locations, self.base_locations, known)[0]

    def _place(self, p, ctx):
        """Label, location, node and role of the server a hop goes to."""
        nodes, _, known = ctx
        e = self.endpoint(p.get("server"), p.get("port"))
        node = e.get("node") or _node_at(p.get("server"), nodes)
        loc = self._loc(e.get("loc"), known)
        if loc is None and node in nodes:
            loc = self._loc(nodes[node].get("loc"), known)
        if loc is None and not p.get("synthetic"):      # e.g. a provider node: guess from its name
            loc = self._loc(self.region(p["name"])[0], known)
        role = e.get("role") or ("provider" if p.get("provider") else None)
        return {"label": e.get("label") or node or p.get("server") or p["name"], "loc": loc, "node": node, "role": role}

    def _landing(self, src, route, hops, ctx):
        nodes, _, known = ctx
        if not route:
            origin_loc = (nodes.get(src["origin"]) or {}).get("loc")
            return {"kind": "direct", "label": "Direct (no proxy)", "loc": self._loc(origin_loc, known)}
        last = hops[-1]
        if last["role"] == "stale":
            return {"kind": "stale", "label": "Old address, probably dead", "loc": last["loc"]}
        if last["role"] == "residential":
            return {"kind": "residential", "label": "Residential IP", "loc": last["loc"]}
        if last["role"] == "provider":
            real = next((p for p in reversed(route) if not p.get("synthetic")), route[-1])
            loc, name = self.region(real["name"])
            out = {"kind": "provider", "label": f"Provider · {name}" if name else "Provider",
                   "loc": self._loc(loc, known) or last["loc"]}
            if name:
                out["region"] = name
            return out
        return {"kind": "internet", "label": "Internet", "loc": last["loc"]}

    def _view(self, src, route, label, res, ctx):
        hops, total, complete, ok = [], 0.0, True, True
        for key, _, _, p in self.hop_edges(src, route):
            r = res.get(key) or {}
            place = self._place(p, ctx)
            if r.get("state") == "down" or place["role"] == "stale":
                ok = False
            if r.get("ms") is None:
                complete = False
            else:
                total += r["ms"]
            hops.append({"name": p["name"], "proto": p.get("type"), "host": p.get("server"), "port": p.get("port"),
                         "label": place["label"], "loc": place["loc"], "node": place["node"], "role": place["role"],
                         "edge": key, "synthetic": bool(p.get("synthetic"))})
        return {"label": label, "ok": ok, "total": round(total, 1) if route and complete else None,
                "landing": self._landing(src, route, hops, ctx), "hops": hops}

    @staticmethod
    def _stub(label, kind="unknown"):
        """(placeholder view, None) for a route that cannot be drawn: REJECT (kind "reject"), or a name the snapshot
        lacks, an empty group, a broken or looping chain (kind "unknown"). It is never ok."""
        text = "Rejected (REJECT)" if kind == "reject" else "Unknown route"
        return {"label": label, "ok": False, "total": None, "hops": [], "landing": {"kind": kind, "label": text}}, None

    def best(self, src, name, res, ctx, prefix=(), depth=0):
        """(view, route) for the route ``name`` most likely takes now; (placeholder view, None) when it has none."""
        prefix = list(prefix)
        kind, obj = self._what(src, name)
        if kind == "reject":
            return self._stub(name, "reject")
        if kind is None or depth > MAX_DEPTH or (kind == "group" and not obj["proxies"]):
            return self._stub(name)
        if kind == "group" and obj["type"] == "relay":
            route = prefix
            for m in obj["proxies"]:
                view, route = self.best(src, m, res, ctx, route, depth + 1)
                if route is None:
                    return self._stub(name, view["landing"]["kind"])
            return self._view(src, route, name, res, ctx), route
        if kind == "group":
            opts = [self.best(src, m, res, ctx, prefix, depth + 1) for m in obj["proxies"][:MAX_MEMBERS]]
            view, route = opts[pick(obj["type"], [o[0] for o in opts])]
            return dict(view, label=f"{name} › {view['label']}"), route
        head, route = None, prefix
        d = obj.get("dialer-proxy") if obj else None
        if d:
            head, route = self.best(src, d, res, ctx, prefix, depth + 1)
            if route is None:
                return self._stub(name, head["landing"]["kind"])
        if kind == "proxy":
            if any(h is obj for h in route):          # dialed through itself: a loop Mihomo cannot dial
                return self._stub(name)
            route = route + [obj] + self._tail(obj)
        view = self._view(src, route, "Direct" if name == DIRECT else name, res, ctx)
        if head is not None:
            view["dialer"] = head["label"]
        return view, route

    def _entry(self, src, label, name, res, ctx):
        kind, obj = self._what(src, name)
        if kind == "group" and obj["type"] != "relay":
            opts = [dict(self.best(src, m, res, ctx)[0], member=m) for m in obj["proxies"][:MAX_MEMBERS]]
            return {"label": label, "group": name, "type": obj["type"], "options": opts, "active": pick(obj["type"], opts),
                    "more": max(0, len(obj["proxies"]) - MAX_MEMBERS)}
        view = self.best(src, name, res, ctx)[0]
        return {"label": label, "group": name, "type": obj["type"] if kind == "group" else kind or "proxy",
                "options": [dict(view, member=name)], "active": 0, "more": 0}

    def api(self):
        """The /api/proxy payload."""
        with self.lock:
            res, at, elapsed, error = dict(self.results), self.at, self.elapsed, self.error
        ctx = self.context()
        nodes, hub, known = ctx
        sources, used = [], []
        for src in self.sources:
            src = self._resolved(src, hub)
            loc = self._loc((nodes.get(src["origin"]) or {}).get("loc"), known)
            entries = [self._entry(src, label, name, res, ctx) for label, name in src["entries"]]
            used.append(loc)
            for en in entries:
                for o in en["options"]:
                    used += [h["loc"] for h in o["hops"]] + [o["landing"].get("loc")]
            sources.append({"id": src["id"], "label": src["label"], "origin": src["origin"],
                            "originLabel": src["originLabel"] or src["origin"], "loc": loc, "note": src["note"],
                            "entries": entries})
        locations = {}
        for key in dict.fromkeys(k for k in used if k):
            k, v = resolve_location(key, self.own_locations, self.base_locations, known)
            if k:
                locations[k] = v
        return {"at": at, "elapsed": elapsed, "error": error, "interval": self.interval, "sources": sources,
                "edges": res, "locations": locations}
