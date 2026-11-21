"""``meshatlas proxy-snapshot``: turn a Mihomo (Clash Meta) config into a topology snapshot for the proxy view.

The snapshot keeps structure only, through a whitelist: proxy names, types, servers, ports, ``dialer-proxy`` and
``network``, and group names, types and members. Passwords, UUIDs, keys, SNI, paths, headers and subscription URLs
never reach it. Proxies of ``proxy-providers`` are read from the provider's local cache file (Mihomo's ``path``);
nothing is downloaded.

PyYAML is needed by this command only and is imported lazily (README, "Proxy view (optional)", says how to add it to
each kind of install); the rest of MeshAtlas keeps running on the standard library.

Simplifications: a group's own ``filter`` / ``exclude-filter`` is not applied (all of a provider's proxies count as
members), and subscriptions cached as base64 share links instead of YAML are skipped with a warning.
"""

import hashlib
import json
import re
from pathlib import Path

from .proxy import _scalar, clean_group, clean_proxy


class SnapshotError(Exception):
    pass


class YamlMissing(SnapshotError):
    pass


README_PROXY = "https://github.com/lij768423-svg/meshatlas#proxy-view-optional"
YAML_HELP = ('PyYAML is needed for proxy-snapshot; "Proxy view (optional)" in the README says how to add it to your '
             f"install: {README_PROXY}")


def require_yaml():
    try:
        import yaml
    except ImportError as exc:
        raise YamlMissing(YAML_HELP) from exc
    return yaml


def load_yaml(path):
    """Parse a YAML file. Errors name the line, never the content (a config line may hold a password)."""
    yaml = require_yaml()
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise SnapshotError(f"{path}: not valid YAML{where}") from None


def _list(v):
    return v if isinstance(v, list) else []


def _patterns(text, split):
    """Compile a Mihomo name filter (several patterns separated by a backtick for ``filter``)."""
    out = []
    for part in (text.split("`") if split else [text]):
        if part:
            try:
                out.append(re.compile(part))
            except re.error:
                return None
    return out


def _provider_items(name, pv, home, load):
    """Raw proxy mappings of one provider, plus a warning (or None)."""
    kind = _scalar(pv.get("type")).lower()
    if kind == "inline":
        return _list(pv.get("payload")), None
    path = _scalar(pv.get("path"))
    if not path and kind == "http" and _scalar(pv.get("url")):
        # Mihomo's default cache location: <home>/proxies/<md5 of the url>
        path = str(Path("proxies") / hashlib.md5(_scalar(pv["url"]).encode(), usedforsecurity=False).hexdigest())
    if not path:
        return [], f"provider '{name}' has no path; counted with 0 proxies"
    file = Path(path).expanduser()
    if not file.is_absolute():
        file = home / file
    if not file.is_file():
        return [], (f"provider '{name}': cache file {file} not found (let Mihomo fetch it once, or pass --home "
                    f"with Mihomo's directory); counted with 0 proxies")
    try:
        data = load(file)
    except Exception as exc:  # noqa: BLE001 - report and go on with the other providers
        return [], f"provider '{name}': cannot read {file}: {str(exc)[:120]}; counted with 0 proxies"
    items = data.get("proxies") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], (f"provider '{name}': {file} has no YAML proxies list (share-link subscriptions are not "
                    f"supported); counted with 0 proxies")
    return items, None


def _provider_proxies(name, pv, home, load, warnings):
    items, warn = _provider_items(name, pv, home, load)
    if warn:
        warnings.append(warn)
    keep = _patterns(_scalar(pv.get("filter")), True)
    drop = _patterns(_scalar(pv.get("exclude-filter")), False)
    if keep is None or drop is None:
        warnings.append(f"provider '{name}': filter is not a valid regular expression here; it was ignored")
        keep, drop = keep or [], drop or []
    drop_types = {t.strip().lower() for t in _scalar(pv.get("exclude-type")).split("|") if t.strip()}
    override = pv.get("override") if isinstance(pv.get("override"), dict) else {}
    dialer = _scalar(override.get("dialer-proxy")) or _scalar(pv.get("dialer-proxy"))
    prefix, suffix = (v if isinstance(v, str) else "" for v in (override.get("additional-prefix"),
                                                               override.get("additional-suffix")))
    out = []
    for raw in items:
        p = clean_proxy(raw)
        if not p:
            continue
        if keep and not any(rx.search(p["name"]) for rx in keep):
            continue
        if any(rx.search(p["name"]) for rx in drop) or p.get("type") in drop_types:
            continue
        p["name"] = prefix + p["name"] + suffix
        if dialer:
            p["dialer-proxy"] = dialer
        p["provider"] = name
        out.append(p)
    return out


def snapshot(config, home, load=None):
    """Build a snapshot from a parsed Mihomo config. Returns (snapshot, warnings).

    ``home`` is the directory relative provider paths are resolved against (Mihomo's ``-d``); ``load`` reads a
    provider cache file and defaults to YAML."""
    if not isinstance(config, dict):
        raise SnapshotError("not a Mihomo config (expected a YAML mapping with proxies and proxy-groups)")
    load = load or load_yaml
    home = Path(home)
    warnings = []
    proxies = [p for p in map(clean_proxy, _list(config.get("proxies"))) if p]
    own_names = [p["name"] for p in proxies]
    providers = []
    raw_providers = config.get("proxy-providers") or {}
    if not isinstance(raw_providers, dict):
        warnings.append("proxy-providers is not a mapping; ignored")
        raw_providers = {}
    for name, pv in raw_providers.items():
        name = _scalar(name)
        pv = pv if isinstance(pv, dict) else {}
        got = _provider_proxies(name, pv, home, load, warnings)
        proxies += got
        providers.append({"name": name, "type": _scalar(pv.get("type")).lower(), "count": len(got)})
    provider_names = [p["name"] for p in providers]
    groups = []
    for raw in _list(config.get("proxy-groups")):
        g = clean_group(raw)
        if not g:
            continue
        everything = bool(raw.get("include-all"))
        if everything or raw.get("include-all-proxies"):
            g["proxies"] += [n for n in own_names if n not in g["proxies"]]
        if everything or raw.get("include-all-providers"):
            g["use"] = list(dict.fromkeys(g.get("use", []) + provider_names))
        for used in g.get("use", []):
            if used not in provider_names:
                warnings.append(f"group '{g['name']}' uses provider '{used}', which is not defined")
        groups.append(g)
    return {"proxies": proxies, "groups": groups, "providers": providers}, warnings


def dumps(snap):
    """JSON text with one proxy / group / provider per line, so regenerated snapshots diff cleanly."""
    parts = []
    for key in ("proxies", "groups", "providers"):
        items = ",\n  ".join(json.dumps(x, ensure_ascii=False) for x in snap.get(key) or [])
        parts.append(f' "{key}": [\n  {items}\n ]' if items else f' "{key}": []')
    return "{\n" + ",\n".join(parts) + "\n}\n"


def snapshot_file(path, home=None):
    """Read a Mihomo config file and build its snapshot. Returns (snapshot, warnings)."""
    require_yaml()
    path = Path(path).expanduser()
    if not path.is_file():
        raise SnapshotError(f"{path}: file not found")
    return snapshot(load_yaml(path), Path(home).expanduser() if home else path.parent)
