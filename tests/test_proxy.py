import base64
import hashlib
import json
import socket
import subprocess
import sys

import pytest

from meshatlas import cli, config, core, proxy, proxy_snapshot, tailscale
from meshatlas.proxy import ProxyView, edge_key, pick

# Fictional topology, documentation addresses only.
SNAPSHOT = {
    "proxies": [
        {"name": "fra-anytls", "type": "anytls", "server": "203.0.113.10", "port": 443},
        {"name": "fra-hy2", "type": "hysteria2", "server": "203.0.113.10", "port": 8443},
        {"name": "home-exit", "type": "ss", "server": "198.51.100.7", "port": 8388, "dialer-proxy": "Relay"},
        {"name": "old-box", "type": "vless", "server": "198.51.100.99", "port": 443},
        {"name": "HK 01", "type": "trojan", "server": "hk1.example.com", "port": 443, "provider": "sub"},
        {"name": "JP 01", "type": "trojan", "server": "jp1.example.com", "port": 443, "provider": "sub"},
    ],
    "groups": [
        {"name": "PROXY", "type": "select", "proxies": ["Auto", "DIRECT"]},
        {"name": "Auto", "type": "url-test", "proxies": ["fra-anytls"], "use": ["sub"]},
        {"name": "Relay", "type": "fallback", "proxies": ["fra-hy2", "fra-anytls"]},
        {"name": "AI", "type": "select", "proxies": ["home-exit"]},
        {"name": "Backup", "type": "fallback", "proxies": ["old-box", "HK 01"]},
    ],
    "providers": [{"name": "sub", "type": "http", "count": 2}],
}

ENDPOINTS = {
    "203.0.113.10": {"label": "Frankfurt relay", "loc": "fra", "node": "vps-fra", "role": "relay"},
    "198.51.100.7:8388": {"label": "Bridge", "loc": "home", "node": "homelab",
                          "next": {"host": "192.0.2.50", "port": 443, "label": "Residential exit", "proto": "tcp"}},
    "192.0.2.50": {"loc": "sfo", "role": "residential"},
    "198.51.100.99": {"label": "Retired box", "loc": "ams", "role": "stale"},
}

NODES = {
    "homelab": {"id": "homelab", "loc": "sfo", "local": True, "ssh": None, "tailscaleIp": "100.64.0.1"},
    "vps-fra": {"id": "vps-fra", "loc": "fra", "local": False, "ssh": "root@vps-fra", "tailscaleIp": "100.64.1.1"},
    "my-laptop": {"id": "my-laptop", "loc": "sfo", "local": False, "ssh": None, "tailscaleIp": "100.64.0.10"},
    "nas": {"id": "nas", "loc": "home", "local": False, "ssh": None, "tailscaleIp": "100.64.0.20"},
}
HOME = {"home": {"name": "Home", "code": "HOME", "lat": 37.4, "lon": -122.1}}

SAMPLES = {   # probe key -> what the fake probe returns
    "tcp|203.0.113.10|443": [20.0, 21.0, 22.0],
    "icmp|203.0.113.10|": [None, None, None],
    "tcp|hk1.example.com|443": [50.0, None, 51.0],
    "tcp|jp1.example.com|443": [None, None, None],
    "tcp|198.51.100.7|8388": [9.0, 9.0, 9.0],
    "tcp|192.0.2.50|443": [3.0, 3.0, 3.0],
}


def write_snapshot(tmp_path, snap=None, name="laptop.json"):
    p = tmp_path / name
    p.write_text(json.dumps(snap or SNAPSHOT))
    return p


def proxy_cfg(files, entries=None, **extra):
    cfg = {"interval": 300, "endpoints": ENDPOINTS, "locations": {}, "regions": [],
           "sources": [{"id": "laptop", "label": "Laptop · Clash", "origin": "my-laptop", "origin_label": "Laptop",
                        "files": [str(f) for f in files],
                        "entries": entries or [["Everything else", "PROXY"], ["AI", "AI"], ["Backup", "Backup"]]}]}
    cfg.update(extra)
    return cfg


def fake_runners(calls, samples=SAMPLES, fail=()):
    def answer(where, arg):
        targets = json.loads(base64.b64decode(arg))
        calls.append((where, sorted(t["k"] for t in targets)))
        if where in fail:
            return 255, "", "ssh: connect to host vps-fra port 22: Connection refused\n"
        return 0, json.dumps({t["k"]: samples.get(t["k"], [None, None, None]) for t in targets}) + "\n", ""

    def run_local(cmd, timeout=30, stdin=None):
        assert cmd[:2] == ["python3", "-"] and stdin == proxy.PROBE_SCRIPT.encode()
        return answer("local", cmd[2])

    def ssh(target, cmd, stdin=None, timeout=60):
        return answer(target, cmd.split()[-1])

    return run_local, ssh


@pytest.fixture
def view(tmp_path):
    calls = []
    run_local, ssh = fake_runners(calls)
    pv = ProxyView(proxy_cfg([write_snapshot(tmp_path)]), lambda: (NODES, "homelab", {}), run_local, ssh,
                   base_locations=HOME)
    pv.calls = calls
    return pv


# ---------- topology ----------

def test_use_expands_explicit_members_first(view):
    src = view.sources[0]
    assert src["groups"]["Auto"]["proxies"] == ["fra-anytls", "HK 01", "JP 01"]
    assert src["originLabel"] == "Laptop"


def test_edge_keys_and_plan(view):
    assert edge_key("my-laptop", SNAPSHOT["proxies"][0]) == "my-laptop|anytls|203.0.113.10|443"
    edges = view.plan()
    assert set(edges) == {
        "my-laptop|anytls|203.0.113.10|443",          # Auto, and Relay's second member for the AI route
        "my-laptop|hysteria2|203.0.113.10|8443",      # Relay's first member
        "my-laptop|trojan|hk1.example.com|443",       # provider proxies pulled in by "use"
        "my-laptop|trojan|jp1.example.com|443",
        "203.0.113.10|ss|198.51.100.7|8388",          # home-exit, dialed through Relay (same key from either member)
        "198.51.100.7|tcp|192.0.2.50|443",            # synthetic hop from the Bridge endpoint's `next`
        "my-laptop|vless|198.51.100.99|443",          # Backup's stale first member
    }
    frm, prev, hop = edges["198.51.100.7|tcp|192.0.2.50|443"]
    assert frm == "198.51.100.7" and prev["name"] == "home-exit" and hop["synthetic"] is True
    assert edges["my-laptop|anytls|203.0.113.10|443"][1] is None    # first hop: leaves the origin


def test_nested_groups_are_planned_completely(tmp_path):
    # every option the inference may pick through nested groups and a dialer group gets measured
    proxies = [{"name": f"p{i}", "type": "trojan", "server": f"192.0.2.{i}", "port": 443} for i in range(1, 9)]
    proxies.append({"name": "exit", "type": "ss", "server": "198.51.100.7", "port": 8388, "dialer-proxy": "Outer"})
    groups = [{"name": "Outer", "type": "url-test", "proxies": ["A", "B"]},
              {"name": "A", "type": "fallback", "proxies": ["p1", "p2", "p3", "p4"]},
              {"name": "B", "type": "fallback", "proxies": ["p5", "p6", "p7", "p8"]}]
    pv = ProxyView(proxy_cfg([write_snapshot(tmp_path, {"proxies": proxies, "groups": groups})],
                             entries=[["Exit", "exit"]]), lambda: (NODES, "homelab", {}), None, None)
    froms = {k.split("|")[0] for k in pv.plan() if k.endswith("|198.51.100.7|8388")}
    assert froms == {f"192.0.2.{i}" for i in range(1, 9)}


def test_relay_group_chains_members(tmp_path):
    snap = {"proxies": SNAPSHOT["proxies"][:3], "groups": [{"name": "Chain", "type": "relay",
                                                             "proxies": ["fra-anytls", "home-exit"]},
                                                            {"name": "Relay", "type": "select", "proxies": ["fra-hy2"]}]}
    pv = ProxyView(proxy_cfg([write_snapshot(tmp_path, snap)], entries=[["Chain", "Chain"]]),
                   lambda: (NODES, "homelab", {}), None, None)
    view, route = pv.best(pv.sources[0], "Chain", {}, (NODES, "homelab", {}))
    # home-exit dials through Relay (fra-hy2), which is itself dialed through fra-anytls
    assert [h["edge"] for h in view["hops"]] == [
        "my-laptop|anytls|203.0.113.10|443", "203.0.113.10|hysteria2|203.0.113.10|8443",
        "203.0.113.10|ss|198.51.100.7|8388", "198.51.100.7|tcp|192.0.2.50|443"]
    assert view["label"] == "Chain" and set(pv.plan()) == {h["edge"] for h in view["hops"]}


def test_measurer_rules(view):
    nodes, hub = NODES, "homelab"
    m = view.measurer
    assert m("homelab", None, nodes, hub) == ("homelab", False)            # the hub probes locally
    assert m("vps-fra", None, nodes, hub) == ("vps-fra", False)            # SSH node probes itself
    assert m("my-laptop", None, nodes, hub) == ("homelab", True)           # unreachable node: hub as a stand-in
    assert m("ghost", None, nodes, hub) == (None, False)                   # origin not discovered (yet)
    prev = {"server": "203.0.113.10", "port": 443}
    assert m("203.0.113.10", prev, nodes, hub) == ("vps-fra", False)       # endpoint says it is vps-fra
    prev = {"server": "100.64.1.1", "port": 443}
    assert m("100.64.1.1", prev, nodes, hub) == ("vps-fra", False)         # a node's Tailscale IP
    prev = {"server": "100.64.0.20", "port": 443}
    assert m("100.64.0.20", prev, nodes, hub) == (None, False)             # node without SSH: no stand-in
    prev = {"server": "hk1.example.com", "port": 443}
    assert m("hk1.example.com", prev, nodes, hub) == (None, False)


def test_pick_rules():
    def o(ok, total):
        return {"ok": ok, "total": total}

    opts = [o(False, None), o(True, 80.0), o(True, 30.0), o(True, None)]
    assert pick("select", opts) == 0
    assert pick("fallback", opts) == 1 and pick("load-balance", opts) == 1
    assert pick("url-test", opts) == 2
    assert pick("url-test", [o(False, 5.0), o(True, None)]) == 1          # nothing timed: first working one
    assert pick("fallback", [o(False, 1.0), o(False, 2.0)]) == 0
    assert pick("url-test", []) is None


def make_view(tmp_path, snap, entries=None, samples=SAMPLES, hub="homelab", **extra):
    calls = []
    run_local, ssh = fake_runners(calls, samples=samples)
    pv = ProxyView(proxy_cfg([write_snapshot(tmp_path, snap)], entries=entries, **extra),
                   lambda: (NODES, hub, {}), run_local, ssh, base_locations=HOME)
    pv.calls = calls
    return pv


def entries_of(pv):
    return {en["label"]: en for en in pv.api()["sources"][0]["entries"]}


# PROXY holds a proxy that dials through PROXY itself (include-all makes such groups); Mihomo dials it via fra-anytls
CYCLE = {"proxies": [SNAPSHOT["proxies"][0],
                     {"name": "home-exit", "type": "ss", "server": "198.51.100.7", "port": 8388, "dialer-proxy": "PROXY"}],
         "groups": [{"name": "PROXY", "type": "select", "proxies": ["fra-anytls", "home-exit"]}]}


def test_dialer_back_into_its_own_group(tmp_path):
    plans = []
    for entries in ([["All", "PROXY"], ["AI", "home-exit"]], [["AI", "home-exit"], ["All", "PROXY"]]):
        pv = make_view(tmp_path, CYCLE, entries)
        plans.append(set(pv.plan()))
        pv.collect()
        by = entries_of(pv)
        (ai,) = by["AI"]["options"]
        assert [h["name"] for h in ai["hops"]] == ["fra-anytls", "home-exit", "Bridge → Residential exit"]
        assert ai["total"] == 33.0 and ai["dialer"] == "PROXY › fra-anytls"
        assert [o["total"] for o in by["All"]["options"]] == [21.0, 33.0]
    # the same plan in either entry order (it may also hold a hop no route shows: home-exit dialed through itself)
    assert plans[0] == plans[1] and {"my-laptop|anytls|203.0.113.10|443", "203.0.113.10|ss|198.51.100.7|8388",
                                     "198.51.100.7|tcp|192.0.2.50|443"} <= plans[0]


def test_dialer_through_a_look_alike_server(tmp_path):
    # two proxies on the same server and port (two accounts, say), one dialed through the other: a real route
    snap = {"proxies": [{"name": "acct-a", "type": "ss", "server": "192.0.2.9", "port": 8388},
                        {"name": "acct-b", "type": "ss", "server": "192.0.2.9", "port": 8388, "dialer-proxy": "acct-a"}],
            "groups": []}
    pv = make_view(tmp_path, snap, [["B", "acct-b"]])
    (b,) = entries_of(pv)["B"]["options"]
    assert [h["edge"] for h in b["hops"]] == ["my-laptop|ss|192.0.2.9|8388", "192.0.2.9|ss|192.0.2.9|8388"]
    assert {h["edge"] for h in b["hops"]} <= set(pv.plan())


def test_select_keeps_members_without_a_route_in_place(tmp_path):
    snap = {"proxies": [SNAPSHOT["proxies"][0], {"name": "block", "type": "reject"}],
            "groups": [{"name": "Ads", "type": "select", "proxies": ["REJECT", "DIRECT"]},
                       {"name": "Sub-Auto", "type": "url-test", "proxies": [], "use": ["sub"]},   # provider cache missing
                       {"name": "PROXY", "type": "select", "proxies": ["Sub-Auto", "DIRECT"]},
                       {"name": "Outer", "type": "select", "proxies": ["PROXY", "fra-anytls"]},
                       {"name": "FB", "type": "fallback", "proxies": ["REJECT", "gone", "block", "fra-anytls"]}]}
    pv = make_view(tmp_path, snap, [["Ads", "Ads"], ["Main", "PROXY"], ["Outer", "Outer"], ["FB", "FB"]])
    pv.collect()
    by = entries_of(pv)
    ads = by["Ads"]
    assert ads["options"][0] == {"member": "REJECT", "label": "REJECT", "ok": False, "total": None, "hops": [],
                                 "landing": {"kind": "reject", "label": "Rejected (REJECT)"}}
    assert ads["options"][1]["landing"]["kind"] == "direct" and ads["active"] == 0     # select: the first, as Mihomo
    main = by["Main"]
    assert [o["member"] for o in main["options"]] == ["Sub-Auto", "DIRECT"] and main["active"] == 0
    assert main["options"][0]["ok"] is False and main["options"][0]["landing"]["kind"] == "unknown"
    outer = by["Outer"]
    assert outer["active"] == 0 and outer["options"][0]["label"] == "PROXY › Sub-Auto"
    assert outer["options"][0]["landing"]["kind"] == "unknown" and outer["options"][1]["ok"] is True
    fb = by["FB"]                                       # fallback skips them
    assert [o["landing"]["kind"] for o in fb["options"]] == ["reject", "unknown", "reject", "internet"]
    assert fb["active"] == 3
    json.dumps(pv.api())


def test_entry_types(tmp_path):
    snap = {"proxies": [SNAPSHOT["proxies"][0], {"name": "WAN2", "type": "direct"}, {"name": "block", "type": "reject"},
                        {"name": "dns-out", "type": "dns"}],
            "groups": [{"name": "Chain", "type": "relay", "proxies": ["fra-anytls"]},
                       {"name": "Empty", "type": "select", "proxies": []}]}
    names = ["DIRECT", "WAN2", "REJECT", "block", "dns-out", "fra-anytls", "Chain", "nope", "Empty"]
    pv = make_view(tmp_path, snap, [[n, n] for n in names])
    by = entries_of(pv)
    assert [by[n]["type"] for n in names] == ["direct", "direct", "reject", "reject", "reject", "proxy", "relay",
                                              "proxy", "select"]
    assert [by[n]["options"][0]["landing"]["kind"] for n in names[:-1]] == [
        "direct", "direct", "reject", "reject", "reject", "internet", "internet", "unknown"]
    assert all(by[n]["active"] == 0 for n in names[:-1])
    assert by["Empty"]["options"] == [] and by["Empty"]["active"] is None


def test_direct_type_proxy_adds_no_hop(tmp_path):
    snap = {"proxies": [SNAPSHOT["proxies"][0], {"name": "WAN2", "type": "direct"},
                        {"name": "exit", "type": "ss", "server": "192.0.2.9", "port": 8388, "dialer-proxy": "WAN2"},
                        {"name": "exit2", "type": "ss", "server": "192.0.2.9", "port": 8389}],
            "groups": [{"name": "PROXY", "type": "select", "proxies": ["exit", "WAN2", "DIRECT"]},
                       {"name": "Chain", "type": "relay", "proxies": ["fra-anytls", "WAN2", "exit2"]}]}
    samples = dict(SAMPLES, **{"tcp|192.0.2.9|8388": [7.0, 7.0, 7.0]})
    pv = make_view(tmp_path, snap, [["All", "PROXY"], ["Chain", "Chain"]], samples=samples)
    # exit leaves the origin, as through DIRECT; in the chain, exit2 leaves the hop before WAN2
    assert set(pv.plan()) == {"my-laptop|ss|192.0.2.9|8388", "my-laptop|anytls|203.0.113.10|443",
                              "203.0.113.10|ss|192.0.2.9|8389"}
    pv.collect()
    assert pv.results["my-laptop|ss|192.0.2.9|8388"]["state"] == "ok"
    by = entries_of(pv)
    exit_, wan2, direct = by["All"]["options"]
    assert exit_["total"] == 7.0 and exit_["dialer"] == "WAN2"
    assert [h["edge"] for h in exit_["hops"]] == ["my-laptop|ss|192.0.2.9|8388"]
    assert wan2["label"] == "WAN2" and wan2["hops"] == [] and wan2["landing"] == direct["landing"]
    assert wan2["landing"] == {"kind": "direct", "label": "Direct (no proxy)", "loc": "sfo"}
    assert [h["name"] for h in by["Chain"]["options"][0]["hops"]] == ["fra-anytls", "exit2"]


def test_origin_hub_alias(tmp_path):
    calls = []
    run_local, ssh = fake_runners(calls)
    cfg = proxy_cfg([write_snapshot(tmp_path)], entries=[["Everything else", "PROXY"]])
    cfg["sources"][0].update(origin="hub", origin_label=None)
    pv = ProxyView(cfg, lambda: (NODES, "homelab", {}), run_local, ssh, base_locations=HOME)
    pv.collect()
    first = pv.results["homelab|anytls|203.0.113.10|443"]           # the hub's real id, like probe_from
    assert first["measuredFrom"] == "homelab" and first["ref"] is False and first["state"] == "ok"
    (src,) = pv.api()["sources"]
    assert (src["origin"], src["originLabel"], src["loc"]) == ("homelab", "homelab", "sfo")
    assert src["entries"][0]["options"][0]["hops"][0]["edge"] == "homelab|anytls|203.0.113.10|443"


def test_measurer_server_named_like_a_node(tmp_path):
    # a server written as a MagicDNS short name is still a server: no stand-in, and its endpoint's node wins
    pv = make_view(tmp_path, SNAPSHOT, endpoints={"nas:8388": {"node": "vps-fra"}})
    m = pv.measurer
    assert m("nas", {"server": "nas", "port": 443}, NODES, "homelab") == (None, False)
    assert m("nas", {"server": "nas", "port": 8388}, NODES, "homelab") == ("vps-fra", False)
    assert m("vps-fra", {"server": "vps-fra", "port": 443}, NODES, "homelab") == ("vps-fra", False)
    assert m("nas", None, NODES, "homelab") == ("homelab", True)            # as the origin, the hub stands in


# ---------- measurement ----------

def test_collect_measures_each_hop_where_it_leaves(view):
    view.collect()
    calls = dict(view.calls)
    assert set(calls) == {"local", "root@vps-fra"}
    assert "tcp|198.51.100.7|8388" in calls["root@vps-fra"]                  # leaves the Frankfurt relay
    assert "tcp|192.0.2.50|443" in calls["local"]                            # leaves the bridge on the hub
    assert "icmp|203.0.113.10|" in calls["local"]                            # hysteria2 -> ICMP
    e = view.results
    first = e["my-laptop|anytls|203.0.113.10|443"]
    assert first["ref"] is True and first["measuredFrom"] == "homelab" and first["method"] == "tcp"
    assert first["ms"] == 21.0 and first["state"] == "ok" and first["loss"] == 0
    assert e["my-laptop|hysteria2|203.0.113.10|8443"]["method"] == "icmp"
    assert e["my-laptop|hysteria2|203.0.113.10|8443"]["state"] == "down"
    hk = e["my-laptop|trojan|hk1.example.com|443"]
    assert hk["state"] == "lossy" and hk["loss"] == 0.33 and hk["ms"] == 50.5
    relay = e["203.0.113.10|ss|198.51.100.7|8388"]
    assert relay["measuredFrom"] == "vps-fra" and relay["ref"] is False
    assert view.error is None and view.at is not None


def test_collect_reports_probe_errors(tmp_path):
    calls = []
    run_local, ssh = fake_runners(calls, fail=("root@vps-fra",))
    pv = ProxyView(proxy_cfg([write_snapshot(tmp_path)]), lambda: (NODES, "homelab", {}), run_local, ssh,
                   base_locations=HOME)
    pv.collect()
    r = pv.results["203.0.113.10|ss|198.51.100.7|8388"]
    assert r["state"] == "error" and "Connection refused" in r["error"]
    assert pv.error.startswith("vps-fra: ")
    # nodes without any way in stay unmeasured
    nodes = dict(NODES, homelab=dict(NODES["homelab"], local=False))
    pv = ProxyView(proxy_cfg([write_snapshot(tmp_path)]), lambda: (nodes, None, {}), run_local, ssh)
    pv.collect()
    assert pv.results["my-laptop|anytls|203.0.113.10|443"]["state"] == "unmeasured"


def test_unresolvable_servers_are_down(tmp_path):
    snap = {"proxies": [{"name": "dead", "type": "trojan", "server": "nx.invalid", "port": 443},
                        {"name": "dead-hy2", "type": "hysteria2", "server": "nx.invalid", "port": 8443},
                        SNAPSHOT["proxies"][0]],
            "groups": [{"name": "FB", "type": "fallback", "proxies": ["dead", "dead-hy2", "fra-anytls"]},
                       {"name": "LB", "type": "load-balance", "proxies": ["dead-hy2", "fra-anytls"]}]}
    unresolved = {"state": "down", "error": "cannot resolve"}
    samples = dict(SAMPLES, **{"tcp|nx.invalid|443": unresolved, "icmp|nx.invalid|": unresolved})
    pv = make_view(tmp_path, snap, [["FB", "FB"], ["LB", "LB"]], samples=samples)
    pv.collect()
    for key, method in (("my-laptop|trojan|nx.invalid|443", "tcp"), ("my-laptop|hysteria2|nx.invalid|8443", "icmp")):
        r = pv.results[key]
        assert (r["state"], r["error"], r["method"], r["ms"]) == ("down", "cannot resolve", method, None)
    assert pv.error is None                            # the targets failed, not the measuring machine
    by = entries_of(pv)
    assert [o["ok"] for o in by["FB"]["options"]] == [False, False, True] and by["FB"]["active"] == 2
    assert by["LB"]["active"] == 1


def test_intercepted_hops_have_no_time_but_stay_ok(tmp_path):
    samples = dict(SAMPLES, **{"tcp|hk1.example.com|443": {"state": "intercepted", "addr": "198.18.0.7"}})
    pv = make_view(tmp_path, SNAPSHOT, [["Backup", "Backup"]], samples=samples)
    pv.collect()
    r = pv.results["my-laptop|trojan|hk1.example.com|443"]
    assert r["state"] == "intercepted" and r["ms"] is None and r["loss"] is None and r["measuredFrom"] == "homelab"
    assert "198.18.0.7" in r["error"]
    stale, hk = entries_of(pv)["Backup"]["options"]
    assert hk["ok"] is True and hk["total"] is None and entries_of(pv)["Backup"]["active"] == 1
    assert proxy.outcome({"state": "weird"}) == {"state": "error", "error": "no result from the probe"}
    assert proxy.outcome("Traceback: boom")["state"] == "error"


def test_probe_classifies_unresolvable_and_fake_ip(monkeypatch):
    ns = {"__name__": "probe"}
    exec(proxy.PROBE_SCRIPT, ns)                       # defines the probe's functions; main() only runs as __main__
    answers = {"nx.invalid": socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided"),
               "bad..name": UnicodeError("label empty or too long"),
               "hk1.example.com": "198.18.0.7", "edge.example.com": "198.19.255.254",
               "real.example.com": "192.0.2.10", "v6.example.com": "2001:db8::1"}

    def getaddrinfo(host, port, family=0, kind=0, *rest):
        a = answers[host]
        if isinstance(a, Exception):
            raise a
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, kind, 6, "", (a, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    down = {"state": "down", "error": "cannot resolve"}
    assert ns["tcp"]("nx.invalid", 443) == down and ns["icmp"]("nx.invalid") == down
    assert ns["tcp"]("bad..name", 443) == down
    assert ns["tcp"]("hk1.example.com", 443) == {"state": "intercepted", "addr": "198.18.0.7"}
    assert ns["icmp"]("edge.example.com") == {"state": "intercepted", "addr": "198.19.255.254"}
    assert ns["resolve"]("real.example.com", 443, socket.SOCK_STREAM)[4][0] == "192.0.2.10"   # measured as usual
    assert ns["resolve"]("v6.example.com", 443, socket.SOCK_STREAM)[4][0] == "2001:db8::1"


def test_resolve_hostnames_false_measures_ip_literals_only(tmp_path):
    pv = make_view(tmp_path, SNAPSHOT, [["Backup", "Backup"], ["Everything else", "PROXY"]], resolve_hostnames=False)
    pv.collect()
    probed = {k for _, keys in pv.calls for k in keys}
    assert "tcp|203.0.113.10|443" in probed and not any("example.com" in k for k in probed)
    r = pv.results["my-laptop|trojan|hk1.example.com|443"]
    assert (r["state"], r["error"], r["measuredFrom"]) == ("unmeasured", "hostname (resolve_hostnames = false)", None)
    assert pv.results["my-laptop|anytls|203.0.113.10|443"]["state"] == "ok"
    assert proxy.is_ip("2001:db8::1") and proxy.is_ip("[2001:db8::1]") and not proxy.is_ip("hk1.example.com")


def test_next_proto_udp_is_pinged(tmp_path):
    endpoints = dict(ENDPOINTS)
    endpoints["198.51.100.7:8388"] = dict(ENDPOINTS["198.51.100.7:8388"],
                                          next={"host": "192.0.2.50", "port": 443, "proto": "udp"})
    samples = dict(SAMPLES, **{"icmp|192.0.2.50|": [4.0, 4.0, 4.0]})
    pv = make_view(tmp_path, SNAPSHOT, [["AI", "AI"]], samples=samples, endpoints=endpoints)
    pv.collect()
    r = pv.results["198.51.100.7|udp|192.0.2.50|443"]
    assert (r["method"], r["state"], r["ms"]) == ("icmp", "ok", 4.0)


def test_api_inference_and_shape(view):
    view.collect()
    d = view.api()
    assert set(d) == {"at", "elapsed", "error", "interval", "sources", "edges", "locations"}
    (src,) = d["sources"]
    assert {k: src[k] for k in ("id", "origin", "originLabel", "loc")} == \
        {"id": "laptop", "origin": "my-laptop", "originLabel": "Laptop", "loc": "sfo"}
    everything, ai, backup = src["entries"]

    assert everything["type"] == "select" and everything["active"] == 0 and everything["more"] == 0
    auto, direct = everything["options"]
    assert auto["member"] == "Auto" and auto["label"] == "Auto › fra-anytls"   # url-test: lowest total wins
    assert auto["total"] == 21.0 and auto["ok"] is True
    assert auto["landing"] == {"kind": "internet", "label": "Internet", "loc": "fra"}
    hop = auto["hops"][0]
    assert hop == {"name": "fra-anytls", "proto": "anytls", "host": "203.0.113.10", "port": 443,
                   "label": "Frankfurt relay", "loc": "fra", "node": "vps-fra", "role": "relay",
                   "edge": "my-laptop|anytls|203.0.113.10|443", "synthetic": False}
    assert direct["landing"]["kind"] == "direct" and direct["landing"]["loc"] == "sfo" and direct["hops"] == []

    (exit_,) = ai["options"]
    assert exit_["dialer"] == "Relay › fra-anytls"                           # fallback skips the down hysteria2 hop
    assert [h["name"] for h in exit_["hops"]] == ["fra-anytls", "home-exit", "Bridge → Residential exit"]
    assert exit_["hops"][-1]["synthetic"] is True and exit_["total"] == 33.0
    assert exit_["landing"] == {"kind": "residential", "label": "Residential IP", "loc": "sfo"}

    stale, hk = backup["options"]
    assert stale["ok"] is False and stale["landing"]["kind"] == "stale" and backup["active"] == 1
    assert hk["landing"] == {"kind": "provider", "label": "Provider · Hong Kong", "region": "Hong Kong", "loc": "hkg"}
    assert hk["hops"][0]["role"] == "provider" and hk["hops"][0]["loc"] == "hkg"

    assert {"sfo", "fra", "hkg", "home", "ams"} <= set(d["locations"])
    assert d["locations"]["home"] == {"name": "Home", "code": "HOME", "lat": 37.4, "lon": -122.1}
    assert d["locations"]["fra"]["code"] == "FRA"
    edge = d["edges"]["my-laptop|anytls|203.0.113.10|443"]
    assert {"from", "to", "port", "proto", "method", "measuredFrom", "ref", "ms", "loss", "state"} <= set(edge)
    json.dumps(d)


def test_regions_custom_first_and_builtin_places(tmp_path):
    pv = ProxyView(proxy_cfg([write_snapshot(tmp_path)],
                             regions=[{"match": "^HK", "loc": "xyz", "label": "Somewhere"}],
                             locations={"xyz": {"name": "Xyz", "lat": 1.0, "lon": 2.0}}),
                   lambda: (NODES, "homelab", {}), None, None)
    assert pv.region("HK 01") == ("xyz", "Somewhere")
    assert pv.region("🇹🇼 Taiwan 02") == ("tpe", "Taiwan")
    assert pv.region("Campus relay") == (None, None)                 # "US" only as a separate token
    assert pv.region("US-West 3") == ("lax", "United States")
    assert proxy.resolve_location("tpe")[1]["name"] == "Taipei"
    assert proxy.resolve_location("FRA", {})[0] == "fra"


def test_probe_script_measures_tcp(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    open_port = srv.getsockname()[1]
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    targets = [{"k": "open", "h": "127.0.0.1", "p": open_port, "m": "tcp"},
               {"k": "closed", "h": "127.0.0.1", "p": closed_port, "m": "tcp"}]
    arg = base64.b64encode(json.dumps(targets).encode()).decode()
    try:
        r = subprocess.run([sys.executable, "-", arg], input=proxy.PROBE_SCRIPT.encode(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
    finally:
        srv.close()
    out = json.loads(r.stdout.decode().strip().splitlines()[-1])
    assert len(out["open"]) == 3 and all(isinstance(x, float) for x in out["open"])
    assert out["closed"] == [None, None, None]


# ---------- config ----------

def write_config(tmp_path, px):
    (tmp_path / "proxy").mkdir(exist_ok=True)
    write_snapshot(tmp_path / "proxy")
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"locations": HOME, "proxy": px}))
    return p


def good_source(**over):
    s = {"id": "laptop", "origin": "my-laptop", "files": ["proxy/laptop.json"], "entries": [["All", "PROXY"]]}
    s.update(over)
    return s


def test_config_proxy_ok(tmp_path):
    p = write_config(tmp_path, {"sources": [good_source()], "endpoints": {"203.0.113.10": {"loc": "home"}},
                                "regions": [{"match": "HK|Hong Kong|香港", "loc": "hkg", "label": "Hong Kong"}]})
    cfg, _ = config.load(str(p))
    assert cfg["proxy"]["interval"] == 300
    assert cfg["proxy"]["sources"][0]["files"] == [str(tmp_path / "proxy" / "laptop.json")]   # relative to the file


def test_config_proxy_off_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MESHATLAS_CONFIG", raising=False)
    monkeypatch.setattr(config, "SEARCH_PATHS", ["meshatlas.json"])
    cfg, _ = config.load()
    assert cfg["proxy"]["sources"] == []


@pytest.mark.parametrize("px, msg", [
    ({"interval": 10, "sources": [good_source()]}, "at least 60"),
    ({"bogus": 1}, r"\[proxy\]: unknown key"),
    ({"sources": [good_source(files=["proxy/missing.json"])]}, "snapshot file not found"),
    ({"sources": [good_source(entries=[["All", "NOPE"]])]}, "'NOPE' is not a group or proxy"),
    ({"sources": [good_source(entries=["PROXY"])]}, "each entry must be"),
    ({"sources": [good_source(), good_source()]}, "duplicate id"),
    ({"sources": [good_source(origin="")]}, "needs origin"),
    ({"sources": [{"origin": "x"}]}, "needs an id"),
    ({"endpoints": {"203.0.113.10": {"role": "boss"}}}, "role must be one of"),
    ({"endpoints": {"203.0.113.10": {"loc": "atlantis"}}}, "unknown location 'atlantis'"),
    ({"endpoints": {"203.0.113.10": {"next": {"host": "192.0.2.1"}}}}, "next needs host and a numeric port"),
    ({"regions": [{"match": "(", "loc": "hkg"}]}, "not a valid regular expression"),
    ({"locations": {"xyz": {"lat": "north", "lon": 1}}}, r"\[proxy.locations.xyz\] needs numeric"),
    ({"interval": float("inf"), "sources": [good_source()]}, "at least 60"),
    ({"interval": float("nan"), "sources": [good_source()]}, "at least 60"),
    ({"resolve_hostnames": "no"}, "resolve_hostnames must be true or false"),
    ({"sources": [good_source(note=["x"])]}, "note must be a string"),
    ({"sources": [good_source(label=5)]}, "label must be a string"),
    ({"sources": [good_source(origin_label={"a": 1})]}, "origin_label must be a string"),
    ({"endpoints": {"203.0.113.10": {"node": ["hub"]}}}, "node must be a non-empty string"),
    ({"endpoints": {"203.0.113.10": {"node": ""}}}, "node must be a non-empty string"),
    ({"endpoints": {"203.0.113.10": {"label": 1}}}, "label must be a string"),
    ({"endpoints": {"203.0.113.10": {"next": {"host": ["192.0.2.1"], "port": 1}}}}, "next needs host"),
    ({"endpoints": {"203.0.113.10": {"next": {"host": "192.0.2.1", "port": 1, "proto": "quic"}}}}, "proto must be"),
    ({"endpoints": {"203.0.113.10": {"next": {"host": "192.0.2.1", "port": 1, "label": 2}}}}, "label must be a string"),
    ({"regions": [{"match": "HK", "loc": "hkg", "label": 3}]}, "label must be a string"),
    ({"locations": {"xyz": {"lat": 1, "lon": 1, "name": 2}}}, "name must be a string"),
])
def test_config_proxy_errors(tmp_path, px, msg):
    with pytest.raises(config.ConfigError, match=msg):
        config.load(str(write_config(tmp_path, px)))


@pytest.mark.skipif(config.tomllib is None, reason="needs Python 3.11+ or tomli")
@pytest.mark.parametrize("body, msg", [
    ("interval = inf", "at least 60"),
    ("interval = nan", "at least 60"),
    ('[[proxy.sources]]\nid = "laptop"\norigin = "my-laptop"\nfiles = ["proxy/laptop.json"]\n'
     'entries = [["All", "PROXY"]]\nnote = 2026-11-01', "note must be a string"),      # an unquoted TOML date
])
def test_config_proxy_toml_values(tmp_path, body, msg):
    write_config(tmp_path, {})                          # the snapshot file
    p = tmp_path / "c.toml"
    p.write_text("[proxy]\n" + body + "\n")
    with pytest.raises(config.ConfigError, match=msg):
        config.load(str(p))


def test_config_proxy_accepts_new_values(tmp_path):
    p = write_config(tmp_path, {
        "resolve_hostnames": False,
        "sources": [good_source(origin="hub", entries=[["All", "PROXY"], ["Ads", "REJECT"]])],
        "endpoints": {"198.51.100.7:8388": {"next": {"host": "192.0.2.50", "port": 443, "proto": "UDP"}},
                      "203.0.113.10": {"next": {"host": "192.0.2.51", "port": 443, "proto": "hysteria2"}}}})
    cfg, _ = config.load(str(p))
    assert cfg["proxy"]["resolve_hostnames"] is False and cfg["proxy"]["sources"][0]["origin"] == "hub"
    cfg, _ = config.load(str(write_config(tmp_path, {})))
    assert cfg["proxy"]["resolve_hostnames"] is True


def test_config_rejects_broken_snapshot(tmp_path):
    p = write_config(tmp_path, {"sources": [good_source()]})
    (tmp_path / "proxy" / "laptop.json").write_text("[1, 2")
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.load(str(p))


# ---------- Atlas, API, server ----------

@pytest.fixture
def make_atlas(tmp_path, monkeypatch, status_json):
    monkeypatch.setattr(tailscale, "status", lambda: tailscale.parse_status(status_json))
    monkeypatch.setattr(tailscale, "derp_map", lambda: ({"sfo": ("San Francisco", 37.77, -122.42),
                                                          "fra": ("Frankfurt", 50.11, 8.68)}, {"2": "sfo", "4": "fra"}))
    monkeypatch.setattr(tailscale, "nearest_regions", lambda ids: ["sfo", "fra"])

    def make(px=None):
        extra = {"nodes": {"vps-fra": {"ssh": "root@vps-fra"}}}
        if px:
            extra["proxy"] = px
        a = core.Atlas(config._merge(config.DEFAULTS, extra), tmp_path / "data")
        a.discover()
        return a

    return make


def test_atlas_without_proxy(make_atlas):
    a = make_atlas()
    assert a.proxy is None and a.api_proxy() is None
    assert a.public_config()["proxy"] is False


def test_atlas_proxy_uses_current_nodes(make_atlas, tmp_path, monkeypatch):
    calls = []
    run_local, ssh = fake_runners(calls)
    monkeypatch.setattr(core, "run_local", run_local)
    a = make_atlas(proxy_cfg([write_snapshot(tmp_path)]))
    monkeypatch.setattr(a, "ssh", ssh)
    assert a.public_config()["proxy"] is True
    assert a.api_proxy()["at"] is None                       # nothing measured yet, but the routes are there
    a.proxy.run_round()
    d = a.api_proxy()
    first = d["edges"]["my-laptop|anytls|203.0.113.10|443"]
    assert first["measuredFrom"] == "homelab" and first["ref"] is True     # discovered hub stands in for the laptop
    assert d["edges"]["203.0.113.10|ss|198.51.100.7|8388"]["measuredFrom"] == "vps-fra"
    assert d["sources"][0]["loc"] == "sfo"
    json.dumps(d)


class FakeAtlas:
    col = {"keep_days": 7}

    def __init__(self, payload):
        self.payload = payload

    def api_proxy(self):
        return self.payload


@pytest.mark.parametrize("payload, status", [(None, 404), ({"sources": []}, 200)])
def test_server_proxy_route(payload, status):
    import http.client
    import threading

    from meshatlas.server import make_handler, serve

    httpd = serve("127.0.0.1", 0, make_handler(FakeAtlas(payload), token="s3cret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get(headers):
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            c.request("GET", "/api/proxy", headers=headers)
            r = c.getresponse()
            return r.status, r.read()

        assert get({})[0] == 401                                       # same auth as the other API routes
        st, body = get({"Authorization": "Bearer s3cret"})
        assert st == status
        if payload is None:
            assert json.loads(body) == {"error": "proxy view is not configured"}
    finally:
        httpd.shutdown()


# ---------- snapshots ----------

MIHOMO = {
    "proxies": [
        {"name": "fra-anytls", "type": "anytls", "server": "203.0.113.10", "port": 443, "password": "hunter2-secret",
         "sni": "secret-sni.example.com", "client-fingerprint": "chrome"},
        {"name": "home-exit", "type": "vless", "server": "198.51.100.7", "port": "8443", "uuid": "uuid-secret-0001",
         "dialer-proxy": "Relay", "network": "ws", "ws-opts": {"path": "/secret-path", "headers": {"Host": "h"}},
         "reality-opts": {"public-key": "pubkey-secret"}},
        {"name": "wg", "type": "wireguard", "server": "192.0.2.9", "port": 51820, "private-key": "wg-secret-key"},
        {"type": "ss", "server": "192.0.2.1", "port": 1},                     # no name: dropped
    ],
    "proxy-providers": {
        "sub": {"type": "http", "url": "https://sub.example.com/api?token=url-secret-token", "path": "./providers/sub.yaml",
                "filter": "HK|JP", "exclude-filter": "expire", "override": {"additional-prefix": "[S] ",
                                                                             "dialer-proxy": "fra-anytls"}},
        "gone": {"type": "http", "url": "https://other.example.com/x?key=another-secret", "path": "./providers/gone.yaml"},
        "inline": {"type": "inline", "payload": [{"name": "inl", "type": "socks5", "server": "192.0.2.3", "port": 1080,
                                                  "username": "u", "password": "inline-secret"}]},
    },
    "proxy-groups": [
        {"name": "PROXY", "type": "select", "proxies": ["Auto", "DIRECT"], "url": "https://probe.example.com"},
        {"name": "Auto", "type": "url-test", "proxies": ["fra-anytls"], "use": ["sub"], "interval": 300},
        {"name": "Relay", "type": "fallback", "proxies": ["fra-anytls"]},
        {"name": "All", "type": "select", "include-all": True},
    ],
}
PROVIDER_CACHE = {"proxies": [
    {"name": "HK 01", "type": "trojan", "server": "hk1.example.com", "port": 443, "password": "provider-secret"},
    {"name": "JP 01", "type": "trojan", "server": "jp1.example.com", "port": 443, "password": "provider-secret"},
    {"name": "US 01", "type": "trojan", "server": "us1.example.com", "port": 443, "password": "provider-secret"},
    {"name": "HK expire 2099-01-01", "type": "trojan", "server": "hk9.example.com", "port": 443},
]}
SECRETS = ["hunter2-secret", "secret-sni", "uuid-secret", "secret-path", "pubkey-secret", "wg-secret-key",
           "url-secret-token", "another-secret", "inline-secret", "provider-secret", "probe.example.com", "chrome"]


def json_load(path):
    return json.loads(path.read_text())


def test_snapshot_strips_secrets_and_expands_providers(tmp_path):
    (tmp_path / "providers").mkdir()
    (tmp_path / "providers" / "sub.yaml").write_text(json.dumps(PROVIDER_CACHE))   # JSON is valid YAML
    snap, warnings = proxy_snapshot.snapshot(MIHOMO, tmp_path, load=json_load)
    text = json.dumps(snap)
    for secret in SECRETS:
        assert secret not in text
    for p in snap["proxies"]:
        assert set(p) <= set(proxy.PROXY_KEYS)
    by = {p["name"]: p for p in snap["proxies"]}
    assert by["home-exit"] == {"name": "home-exit", "type": "vless", "server": "198.51.100.7", "port": 8443,
                               "dialer-proxy": "Relay", "network": "ws"}
    assert by["[S] HK 01"] == {"name": "[S] HK 01", "type": "trojan", "server": "hk1.example.com", "port": 443,
                               "dialer-proxy": "fra-anytls", "provider": "sub"}
    assert "[S] US 01" not in by and "[S] HK expire 2099-01-01" not in by      # provider filter / exclude-filter
    assert by["inl"]["provider"] == "inline"
    assert snap["providers"] == [{"name": "sub", "type": "http", "count": 2}, {"name": "gone", "type": "http", "count": 0},
                                 {"name": "inline", "type": "inline", "count": 1}]
    assert any("'gone'" in w and "not found" in w for w in warnings)
    groups = {g["name"]: g for g in snap["groups"]}
    assert groups["Auto"] == {"name": "Auto", "type": "url-test", "proxies": ["fra-anytls"], "use": ["sub"]}
    assert groups["All"]["proxies"] == ["fra-anytls", "home-exit", "wg"]
    assert groups["All"]["use"] == ["sub", "gone", "inline"]

    # and it loads: groups with "use" get the provider's proxies after their own
    path = write_snapshot(tmp_path, snap, "snap.json")
    pv = ProxyView({"sources": [{"id": "s", "origin": "my-laptop", "files": [str(path)], "entries": [["All", "Auto"]]}]},
                   lambda: (NODES, "homelab", {}), None, None)
    assert pv.sources[0]["groups"]["Auto"]["proxies"] == ["fra-anytls", "[S] HK 01", "[S] JP 01"]
    assert "203.0.113.10|trojan|hk1.example.com|443" in pv.plan()                # provider dialer-proxy override


def test_snapshot_http_provider_without_path_uses_mihomo_cache(tmp_path):
    url = "https://sub.example.com/api?token=url-secret-token"
    cache = tmp_path / "proxies" / hashlib.md5(url.encode()).hexdigest()     # Mihomo's default cache file
    cache.parent.mkdir()
    cache.write_text(json.dumps(PROVIDER_CACHE))
    cfg = {"proxy-providers": {"sub": {"type": "http", "url": url}},
           "proxy-groups": [{"name": "Auto", "type": "url-test", "use": ["sub"]}]}
    snap, warnings = proxy_snapshot.snapshot(cfg, tmp_path, load=json_load)
    assert warnings == [] and snap["providers"] == [{"name": "sub", "type": "http", "count": 4}]
    assert {p["name"] for p in snap["proxies"]} == {"HK 01", "JP 01", "US 01", "HK expire 2099-01-01"}
    assert "url-secret-token" not in json.dumps(snap) and "provider-secret" not in json.dumps(snap)
    cache.unlink()
    snap, warnings = proxy_snapshot.snapshot(cfg, tmp_path, load=json_load)
    assert snap["providers"][0]["count"] == 0 and str(cache) in warnings[0]


def test_read_snapshot_drops_unknown_keys(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"proxies": [{"name": "x", "type": "SS", "server": "192.0.2.1", "port": 1,
                                          "password": "pw"}], "groups": None}))
    snap = proxy.read_snapshot(p)
    assert snap["proxies"] == [{"name": "x", "type": "ss", "server": "192.0.2.1", "port": 1}] and snap["groups"] == []


def test_cli_proxy_snapshot_without_yaml(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "yaml", None)        # import yaml -> ImportError
    cfg = tmp_path / "config.yaml"
    cfg.write_text("proxies: []\n")
    assert cli.main(["proxy-snapshot", str(cfg)]) == 2
    err = capsys.readouterr().err
    assert "PyYAML is needed for proxy-snapshot" in err and "#proxy-view-optional" in err
    assert "meshatlas[proxy]" not in err                  # not on PyPI yet


def test_cli_proxy_snapshot_with_yaml(tmp_path, capsys):
    yaml = pytest.importorskip("yaml")
    home = tmp_path / "mihomo"
    (home / "providers").mkdir(parents=True)
    (home / "providers" / "sub.yaml").write_text(yaml.safe_dump(PROVIDER_CACHE, allow_unicode=True))
    cfg = home / "config.yaml"
    cfg.write_text(yaml.safe_dump(MIHOMO, allow_unicode=True))
    out = tmp_path / "out" / "laptop.json"
    assert cli.main(["proxy-snapshot", str(cfg), "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "wrote" in err and "'gone'" in err
    text = out.read_text()
    for secret in SECRETS:
        assert secret not in text
    assert {p["name"] for p in json.loads(text)["proxies"]} >= {"fra-anytls", "[S] HK 01", "inl"}

    bad = home / "bad.yaml"
    bad.write_text("proxies:\n  - name: x\n    password: leak-me\n   bad indent: [\n")
    assert cli.main(["proxy-snapshot", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "not valid YAML" in err and "leak-me" not in err
