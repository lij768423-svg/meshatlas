"""Configuration: every key is optional. Without a config file MeshAtlas discovers the tailnet by itself."""

import copy
import json
import math
import os
import re
from pathlib import Path

from . import proxy

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - older Pythons get the tomli backport (installed automatically)
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

DEFAULTS = {
    "server": {
        "host": "auto",          # auto = this machine's Tailscale IPv4, falling back to 127.0.0.1
        "port": 8765,
        "token": "",             # optional shared secret (?token=... once, then a cookie)
    },
    "collect": {
        "interval": 120,         # seconds between node rounds (tailscale status + SSH collection)
        "latency_interval": 120, # seconds between latency rounds
        "ping_count": 3,
        "mesh": True,            # nodes with SSH also ping every other node -> full latency matrix
        "collect_self": True,    # read docker / systemd / system stats on the machine running MeshAtlas
        "ssh_timeout": 30,
        "keep_days": 7,
    },
    "display": {
        "title": "MESHATLAS",
        "subtitle": "TAILNET OPS",
        "hide_offline_after_days": 30,
        "hide": [],
    },
    "locations": {},             # key -> {name, code, lat, lon}
    "nodes": {},                 # node id -> overrides {location, role, where, notes, ssh, statsEvery, hidden, parent}
    "external": [],              # servers outside the tailnet, watched with ICMP ping
    "kuma": {},                  # optional Uptime Kuma status page {url, slug}
    "proxy": {                   # optional proxy view (Mihomo / Clash routes); on when `sources` is not empty
        "interval": 300,         # seconds between measurement rounds (at least 60)
        "resolve_hostnames": True,  # false: never resolve server names; only IP literals are measured
        "sources": [],           # [{id, label, origin, origin_label, note, files, entries}]
        "endpoints": {},         # "host" or "host:port" -> {label, loc, node, role, next}
        "locations": {},         # extra map places, same shape as [locations]
        "regions": [],           # [{match, loc, label}]: provider node name -> location
    },
}

SEARCH_PATHS = [
    "meshatlas.toml",
    "meshatlas.json",
    "~/.config/meshatlas/meshatlas.toml",
    "~/.config/meshatlas/meshatlas.json",
    "/etc/meshatlas/meshatlas.toml",
]


class ConfigError(Exception):
    pass


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("locations", "nodes"):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def find_config(explicit=None):
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        return p
    env = os.environ.get("MESHATLAS_CONFIG")
    if env:
        return find_config(env)
    for cand in SEARCH_PATHS:
        p = Path(cand).expanduser()
        if p.exists():
            return p
    return None


def parse_file(path):
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith(".toml"):
        if tomllib is None:
            raise ConfigError("reading TOML on Python < 3.11 needs the tomli package (pip install tomli), or use a .json config")
        try:
            return tomllib.loads(text)
        except Exception as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _check_places(table, section):
    for key, loc in table.items():
        if not isinstance(loc, dict) or not all(_number(loc.get(f)) for f in ("lat", "lon")):
            raise ConfigError(f"[{section}.{key}] needs numeric lat and lon")


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text(table, keys, where, nonempty=()):
    """Optional fields that must be strings when present (they reach the page as they are); ``nonempty`` ones must
    also not be empty."""
    for k in keys:
        v = table.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or (k in nonempty and not v.strip()):
            raise ConfigError(f"{where}: {k} must be {'a non-empty' if k in nonempty else 'a'} string (in quotes)")


def _unknown(table, allowed, where):
    extra = set(table) - set(allowed)
    if extra:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(extra))} (allowed: {', '.join(allowed)})")


def _validate_proxy(cfg, base):
    """Check [proxy] and turn snapshot paths into absolute ones (relative to the config file's directory)."""
    px = cfg["proxy"]
    if not isinstance(px, dict):
        raise ConfigError("[proxy] must be a table")
    _unknown(px, ("interval", "resolve_hostnames", "sources", "endpoints", "locations", "regions"), "[proxy]")
    if not _number(px["interval"]) or not math.isfinite(px["interval"]) or px["interval"] < 60:
        raise ConfigError("[proxy] interval must be a number of seconds, at least 60")
    if not isinstance(px["resolve_hostnames"], bool):
        raise ConfigError("[proxy] resolve_hostnames must be true or false")
    for key in ("endpoints", "locations"):
        if not isinstance(px[key], dict):
            raise ConfigError(f"[proxy.{key}] must be a table")
    for key in ("sources", "regions"):
        if not isinstance(px[key], list):
            raise ConfigError(f"[[proxy.{key}]] must be a list of tables")
    _check_places(px["locations"], "proxy.locations")
    for key, loc in px["locations"].items():
        _text(loc, ("name", "code"), f"[proxy.locations.{key}]")

    def check_loc(where, key):
        if not isinstance(key, str) or proxy.resolve_location(key, px["locations"], cfg["locations"])[0] is None:
            raise ConfigError(f"{where}: unknown location {key!r} (use a [locations] or [proxy.locations] key, "
                              f"or a DERP code such as \"fra\")")

    for i, r in enumerate(px["regions"]):
        where = f"[[proxy.regions]] #{i + 1}"
        if not isinstance(r, dict) or not isinstance(r.get("match"), str) or not r["match"] or not r.get("loc"):
            raise ConfigError(f"{where} needs match (a regular expression) and loc")
        _unknown(r, ("match", "loc", "label"), where)
        _text(r, ("label",), where)
        try:
            re.compile(r["match"], re.I)
        except re.error as exc:
            raise ConfigError(f"{where}: match is not a valid regular expression ({exc})") from exc
        check_loc(where, r["loc"])
    for host, e in px["endpoints"].items():
        where = f'[proxy.endpoints."{host}"]'
        if not isinstance(e, dict):
            raise ConfigError(f"{where} must be a table")
        _unknown(e, ("label", "loc", "node", "role", "next"), where)
        _text(e, ("label", "node"), where, nonempty=("node",))
        if e.get("loc") is not None:
            check_loc(where, e["loc"])
        if e.get("role") is not None and e["role"] not in proxy.ROLES:
            raise ConfigError(f"{where}: role must be one of {', '.join(proxy.ROLES)}")
        nx = e.get("next")
        if nx is not None:
            if not isinstance(nx, dict) or not isinstance(nx.get("host"), str) or not nx["host"].strip() \
                    or proxy.as_port(nx.get("port")) is None:
                raise ConfigError(f"{where}: next needs host and a numeric port")
            _unknown(nx, ("host", "port", "label", "proto"), f"{where} next")
            _text(nx, ("label", "proto"), f"{where} next")
            if nx.get("proto") is not None and nx["proto"].lower() not in proxy.NEXT_PROTOS:
                raise ConfigError(f"{where} next: proto must be \"tcp\", \"udp\" or a Mihomo proxy type "
                                  f"({', '.join(sorted(proxy.PROXY_TYPES))}); udp, hysteria, hysteria2, tuic and "
                                  f"wireguard are measured with ICMP ping, the rest with a TCP connect")
    seen = set()
    for i, s in enumerate(px["sources"]):
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not s["id"]:
            raise ConfigError(f"[[proxy.sources]] #{i + 1} needs an id")
        where = f"[[proxy.sources]] {s['id']!r}"
        if s["id"] in seen:
            raise ConfigError(f"{where}: duplicate id")
        seen.add(s["id"])
        _unknown(s, ("id", "label", "origin", "origin_label", "note", "files", "entries"), where)
        if not isinstance(s.get("origin"), str) or not s["origin"]:
            raise ConfigError(f"{where} needs origin (the node id its traffic starts from, or \"hub\")")
        _text(s, ("label", "origin_label", "note"), where)
        files = s.get("files")
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
            raise ConfigError(f"{where} needs files = [\"<snapshot>.json\", ...] (made with `meshatlas proxy-snapshot`)")
        names, resolved = {proxy.DIRECT, *proxy.REJECTS}, []
        for f in files:
            path = Path(f).expanduser()
            if not path.is_absolute():
                path = base / path
            if not path.is_file():
                raise ConfigError(f"{where}: snapshot file not found: {path} (make it with `meshatlas proxy-snapshot`)")
            try:
                snap = proxy.read_snapshot(path)
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                raise ConfigError(f"{where}: {path}: {exc}") from exc
            names.update(p["name"] for p in snap["proxies"])
            names.update(g["name"] for g in snap["groups"])
            resolved.append(str(path))
        s["files"] = resolved
        entries = s.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ConfigError(f"{where} needs entries = [[\"label\", \"group or proxy name\"], ...]")
        for en in entries:
            if not (isinstance(en, list) and len(en) == 2 and all(isinstance(x, str) and x for x in en)):
                raise ConfigError(f"{where}: each entry must be [label, group or proxy name], got {en!r}")
            if en[1] not in names:
                raise ConfigError(f"{where}: entry {en[1]!r} is not a group or proxy in its snapshot file(s)")


def validate(cfg, base=None):
    """Check the merged config. ``base`` is the config file's directory (relative snapshot paths start there)."""
    _check_places(cfg["locations"], "locations")
    for i, ext in enumerate(cfg["external"]):
        if not ext.get("id") or not ext.get("address"):
            raise ConfigError(f"[[external]] #{i + 1} needs id and address")
    kuma = cfg["kuma"]
    if kuma and (not kuma.get("url") or not kuma.get("slug")):
        raise ConfigError("[kuma] needs both url and slug")
    _validate_proxy(cfg, Path(base) if base else Path.cwd())
    return cfg


def load(path=None):
    """Return (config dict, path or None)."""
    found = find_config(path)
    raw = parse_file(found) if found else {}
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown top-level section(s): {', '.join(sorted(unknown))}")
    return validate(_merge(DEFAULTS, raw), found.parent if found else None), found
