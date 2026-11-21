"use strict";
/* Proxy view (optional, [proxy] in the config): the routing chains of Mihomo / Clash instances and the latency of every
 * hop, from api/proxy. Two layouts: chains (one row per route: device → relays → exit → destination) and arcs on the
 * map; two colorings: by latency and by protocol. The map mode only exists when the server has it on
 * (S.config.proxy); api/proxy is fetched only while the mode is on screen, at most once a minute. */

const pxUi = { view: store.get("ma.pxview", "chain"), color: store.get("ma.pxcolor", "latency") };
if (!["chain", "map"].includes(pxUi.view)) pxUi.view = "chain";
if (!["latency", "proto"].includes(pxUi.color)) pxUi.color = "latency";
// data: last good api/proxy; fetched: when it arrived; tried: last attempt (also failed ones, for the 1-minute limit)
const PX = { data: null, fetched: 0, tried: 0, loading: false, missing: false, error: null, sig: "", tipMore: [] };
const PX_REFRESH_MS = 60000;

const PROTO = {
  anytls: ["AnyTLS", "#4ea8ff"], hysteria2: ["Hysteria2", "#c084fc"], hysteria: ["Hysteria", "#a78bfa"],
  tuic: ["TUIC", "#818cf8"], ss: ["Shadowsocks", "#2dd4bf"], trojan: ["Trojan", "#f5a524"], vless: ["VLESS", "#f472b6"],
  vmess: ["VMess", "#fb7185"], wireguard: ["WireGuard", "#7dd3fc"], socks5: ["SOCKS5", "#a1a1aa"], http: ["HTTP", "#e4e4e7"],
  tcp: ["TCP", "#d4d4d8"],
};
const PROTO_OTHER = "#d4d4d8";
const LAT_TIERS = [[80, "#3ecf8e", "< 80 ms"], [160, "#e6c34a", "80–160"], [260, "#f29a3b", "160–260"], [Infinity, "#ff6b6b", "≥ 260"]];
const PX_DOWN = "#ff5a52", PX_NONE = "#6b6b6b";
// Entry types without a label: the row under a DIRECT / REJECT entry already says where its traffic goes.
const GROUP_TYPE = {
  select: "Manual select · first option assumed", fallback: "Fallback · first healthy option",
  "load-balance": "Load balance · first healthy option assumed", "url-test": "URL test · fastest option",
  relay: "Relay chain · members dialed in order", proxy: "Single proxy", direct: "", reject: "",
};

const protoName = (p) => (PROTO[p] || [p || "?"])[0];
const protoColor = (p) => (PROTO[p] || [0, PROTO_OTHER])[1];
const pxCls = (s) => String(s || "").replace(/[^\w-]/g, "");          // backend values used as class names
/** Route names are "group › member". Names from the config are shown as written (like node and location names);
 *  only the built-in DIRECT and REJECT are translated. */
const pxLabel = (s) => String(s ?? "").split(" › ")
  .map((x) => (x === "Direct" || x === "DIRECT" ? t("Direct") : x === "REJECT" ? t("Reject") : x)).join(" › ");
const pxEdge = (key) => (PX.data && PX.data.edges[key]) || null;
const pxOrigin = (s) => s.originLabel || s.origin || "";
const pxPair = (e) => [e.from, e.to].sort().join("|");          // place pair of a map line, either direction
/** A member with no route to draw (REJECT, a name missing from the snapshot, a broken chain): kept in its place. */
const pxPlaceholder = (o) => !o.hops.length && ["reject", "unknown"].includes(o.landing.kind);

function landingText(l) {
  const k = l && l.kind;
  if (k === "direct") return t("Direct (no proxy)");
  if (k === "residential") return t("Residential IP");
  if (k === "provider") return l.region ? t("Provider · {region}", { region: t(l.region) }) : t("Provider");
  if (k === "stale") return t("Old address, probably dead");
  if (k === "reject") return t("Rejected (REJECT)");
  if (k === "unknown") return t("No known route");
  return t("Internet");
}

function pxColor(e, proto) {
  if (pxUi.color === "proto") return protoColor(proto);
  if (!e || e.ms == null) return e && e.state === "down" ? PX_DOWN : PX_NONE;
  return LAT_TIERS.find(([lim]) => e.ms < lim)[1];
}
/** Line style: solid = measured from where the hop starts, ref = measured from the hub instead, dot = not measured,
 *  tun = not measured because a TUN / fake-IP proxy on the measuring machine answers for every address. */
function pxStyle(e) {
  if (!e || e.state === "unmeasured" || e.state === "error") return "dot";
  if (e.state === "intercepted") return "tun";
  if (e.state === "down") return "down";
  return e.ref ? "ref" : "solid";
}
function pxMs(e, unit = false) {
  if (!e) return "—";
  if (e.ms == null) return e.state === "down" ? t("down") : e.state === "intercepted" ? t("intercepted") : t("not measured");
  const ms = (e.ms < 10 ? e.ms.toFixed(1) : String(Math.round(e.ms))) + (unit ? "ms" : "");
  return ms + (e.loss ? t(" · {p}% loss", { p: Math.round(e.loss * 100) }) : "");
}
const PX_NO_RESOLVE = /^cannot resolve\b/i, PX_NO_LOOKUP = /^hostname \(resolve_hostnames/i;
function pxTip(e, fromLabel, h) {
  const lines = [t("{from} → {to} ({addr})", { from: fromLabel, to: h.label || h.host || "", addr: `${h.host}:${h.port}` }),
    protoName(h.proto) + (h.synthetic ? " · " + t("forwarded by the previous server") : "")];
  const err = (e && e.error) || "";
  const from = () => { if (e.measuredFrom) lines.push(t("Measured from {node}", { node: e.measuredFrom }) + (e.ref ? t(" (reference: the real starting point can't be reached)") : "")); };
  if (!e || e.state === "unmeasured") {
    lines.push(PX_NO_LOOKUP.test(err) ? t("Not measured: {host} is a host name and [proxy] resolve_hostnames = false", { host: h.host })
      : err ? t("Not measured: {err}", { err }) : t("Not measured: MeshAtlas can't reach the machine this hop starts from"));
  } else if (e.state === "intercepted") {
    lines.push(t("Not measured: intercepted by a local TUN/fake-IP proxy on {node}", { node: e.measuredFrom || e.from || "?" }),
      t("{host} resolves into 198.18.0.0/15 (fake-IP) there, so the number would be fake", { host: h.host }));
  } else if (e.state === "error") lines.push(t("Measurement failed: {err}", { err }));
  else if (e.state === "down" && err) {      // no time at all: the probe could not even try (a name that does not resolve)
    lines.push(PX_NO_RESOLVE.test(err) ? t("Down: cannot resolve {host}", { host: h.host }) : t("Down: {err}", { err }));
    from();
  } else {
    const loss = e.loss && e.ms != null ? t(", {n} of 3 lost", { n: Math.round(e.loss * 3) }) : "";
    lines.push(`${e.method === "icmp" ? "ICMP ping" : t("TCP connect")} ${e.ms == null ? t("failed all 3 times") : `${e.ms} ms`}${loss}`);
    from();
  }
  return lines.join("\n");
}

/* ---------- data ---------- */
/** Fill in missing arrays so a partial payload never breaks rendering. */
function pxNormalize(d) {
  d.edges = d.edges || {};
  d.locations = d.locations || {};
  d.sources = (d.sources || []).map((s) => ({ ...s, entries: (s.entries || []).map((en) => ({ ...en,
    options: (en.options || []).map((o) => ({ ...o, hops: o.hops || [], landing: o.landing || { kind: "internet" } })) })) }));
  return d;
}

async function loadProxy() {
  if (PX.loading) return;
  PX.loading = true;
  PX.tried = Date.now();
  try {
    const r = await fetch("api/proxy", { cache: "no-store" });
    if (r.status === 404) { PX.missing = true; PX.data = null; PX.error = null; }
    else {
      const d = r.ok ? await r.json() : null;
      if (!d || !Array.isArray(d.sources)) throw new Error((d && d.error) || `HTTP ${r.status}`);
      PX.data = pxNormalize(d);
      PX.fetched = Date.now();
      PX.missing = false;
      PX.error = null;
    }
  } catch (e) {
    PX.error = String((e && e.message) || e);      // keep the last good data, try again in a minute
  }
  PX.loading = false;
  PX.sig = "";
  if (view === "map" && mapMode === "proxy") { M.sig = ""; renderMap(); }
}

const pxLocations = () => ({ ...((S && S.config.locations) || {}), ...((PX.data && PX.data.locations) || {}) });
function pxUsedLocs() {
  const out = new Set();
  for (const s of (PX.data && PX.data.sources) || []) {
    if (s.loc) out.add(s.loc);
    for (const en of s.entries) for (const o of en.options) {
      for (const h of o.hops) if (h.loc) out.add(h.loc);
      if (o.landing.loc) out.add(o.landing.loc);
    }
  }
  return out;
}
/** Places the proxy map shows; layoutMap() fits the projection to them. Empty until data is loaded. */
function proxyFitLocs() {
  const locs = pxLocations();
  return [...pxUsedLocs()].map((k) => locs[k]).filter((l) => l && Number.isFinite(l.lat) && Number.isFinite(l.lon));
}
/** [lonA, lonB] of every place pair the proxy map draws a line between: fitView cuts the map where the fewest cross. */
function proxyFitPairs() {
  const locs = pxLocations();
  const { edges, legs } = pxLines(PX.data);
  const out = new Map();
  for (const e of [...edges, ...legs]) {
    const a = locs[e.from], b = locs[e.to];
    if (a && b && Number.isFinite(a.lon) && Number.isFinite(b.lon)) out.set(pxPair(e), [a.lon, b.lon]);
  }
  return [...out.values()];
}
function proxyFitSig() {
  const ls = proxyFitLocs();
  return ls.length ? JSON.stringify([ls.map((l) => [l.lat, l.lon]), proxyFitPairs()]) : "";
}
/** What the proxy map draws. edges: one per measured hop between two places (once even when several routes share it);
 *  legs: provider entry → exit region (inside the provider, not measurable); sameCity: hops that stay in one place;
 *  unplaced: hops not drawn because one of their ends has no known location. A hop after one with an unknown location
 *  starts from that unknown place, so it is not drawn from an earlier place either. */
function pxLines(d) {
  const edges = new Map(), legs = new Map(), sameCity = new Set(), unplaced = new Set();
  for (const s of (d && d.sources) || []) for (const en of s.entries) en.options.forEach((o, i) => {
    let from = s.loc || null, fromLabel = pxOrigin(s);
    for (const h of o.hops) {
      if (!from || !h.loc) unplaced.add(h.edge);
      else if (from === h.loc) sameCity.add(h.edge);
      else {
        const cur = edges.get(h.edge) || { from, to: h.loc, proto: h.proto, key: h.edge, hop: h, fromLabel, active: false, users: [] };
        cur.active = cur.active || i === en.active;
        cur.users.push(`${s.label || s.id || ""} · ${en.label || ""} · ${o.member || ""}`);
        edges.set(h.edge, cur);
      }
      from = h.loc || null;
      fromLabel = h.label || h.host || "";
    }
    const last = o.hops[o.hops.length - 1];
    if (!last || !o.landing.loc || !last.loc || o.landing.loc === last.loc) return;
    const k = `${last.loc}>${o.landing.loc}`;
    const cur = legs.get(k) || { from: last.loc, to: o.landing.loc, proto: last.proto, active: false, label: landingText(o.landing) };
    cur.active = cur.active || i === en.active;
    legs.set(k, cur);
  });
  for (const k of unplaced) if (edges.has(k) || sameCity.has(k)) unplaced.delete(k);    // drawn (or same-city) elsewhere
  for (const k of sameCity) if (edges.has(k)) sameCity.delete(k);
  return { edges: [...edges.values()], legs: [...legs.values()], sameCity, unplaced };
}
/** Hops the views show (every option of every entry; a hop shared by several routes once), with their results:
 *  the counts in the status cover these, not members of nested groups that were measured but never shown. */
function pxShownHops(d) {
  const keys = new Set();
  for (const s of (d && d.sources) || []) for (const en of s.entries) for (const o of en.options) for (const h of o.hops) keys.add(h.edge);
  return [...keys].map((k) => d.edges[k] || null);
}
/** Protocols in use, in table order: the legend of the protocol coloring lists only these. */
function pxProtos() {
  const seen = new Set();
  for (const s of (PX.data && PX.data.sources) || []) for (const en of s.entries) for (const o of en.options) for (const h of o.hops) seen.add(h.proto || "?");
  const order = Object.keys(PROTO);
  return [...seen].sort((a, b) => ((order.indexOf(a) + 1) || 99) - ((order.indexOf(b) + 1) || 99) || a.localeCompare(b));
}

function pxEmptyText() {
  if (PX.missing) return t("The proxy view is not configured. Add a [proxy] section to the config file.");
  if (PX.error) return t("Could not load proxy routes ({err}). Trying again in a minute.", { err: PX.error });
  return t("Loading proxy routes…");
}

/* ---------- render ---------- */
function renderProxy() {
  if (!PX.loading && Date.now() - PX.tried >= PX_REFRESH_MS) loadProxy();
  document.querySelectorAll("[data-pxview]").forEach((b) => b.classList.toggle("on", b.dataset.pxview === pxUi.view));
  document.querySelectorAll("[data-pxcolor]").forEach((b) => b.classList.toggle("on", b.dataset.pxcolor === pxUi.color));
  renderProxyLegend();
  const d = PX.data, sub = $("map-sub"), st = pxStatus();
  if (d) {
    const MARK = "\u0001", [a, b = ""] = t("Proxy routes · {s} sources · {n} hops · {when}",
      { s: d.sources.length, n: pxShownHops(d).length, when: MARK }).split(MARK);
    sub.innerHTML = esc(a) + st.html + esc(b);
    PX.tipMore = [d.error, PX.error, prefs.refresh === 0 && t("Auto refresh paused (turn it on in settings)")].filter(Boolean);
  } else {
    sub.textContent = (PX.missing ? t("Proxy view · not configured on the server")
      : PX.error ? t("Proxy routes · could not load: {err}", { err: PX.error }) : t("Proxy routes · loading…"))
      + (prefs.refresh === 0 ? " · " + t("auto refresh paused") : "");
    PX.tipMore = [];
  }
  sub.title = [sub.textContent, ...PX.tipMore].join("\n");
  // the header squeezes the subtitle to nothing at common widths: the legend repeats the status where it always shows
  const ps = $("px-status");
  ps.innerHTML = st.html;
  ps.title = sub.title;
  return pxUi.view === "map" ? renderProxyMap() : renderProxyChain();
}

/** Measurement status: age of the round (an .ago span: tickClock keeps it current even with auto refresh paused), hops
 *  down, failed probes, a failed refresh (the views keep the last good data), paused auto refresh. {html, text}. */
function pxStatus() {
  const d = PX.data, parts = [];
  const add = (text, cls = "", html = esc(text)) => parts.push({ text, html: cls ? `<span class="${cls}">${html}</span>` : html });
  if (d) {
    const secs = d.elapsed ? t(" ({s}s)", { s: d.elapsed }) : "";
    if (d.at) {
      const MARK = "\u0001", ago = fmtAgo(d.at), [a, b = ""] = t("measured {ago}", { ago: MARK }).split(MARK);
      add(a + ago + b + secs, "", `${esc(a)}<span class="ago" data-ts="${+d.at}">${esc(ago)}</span>${esc(b + secs)}`);
    } else add(t("not measured yet"));
    const down = pxShownHops(d).filter((e) => e && e.state === "down").length;
    if (down) add(t("{n} down", { n: down }), "px-st-bad");
    if (d.error) add(t("some measurements failed"), "px-st-warn");
  }
  if (PX.error) add(t("refresh failed"), "px-st-bad");
  if (prefs.refresh === 0) add(t("auto refresh paused"), "px-st-warn");
  return { html: parts.map((p) => p.html).join(" · "), text: parts.map((p) => p.text).join(" · ") };
}

function renderProxyLegend() {
  const el = $("px-legend");
  const protos = pxUi.color === "proto" ? pxProtos() : [];
  const tun = !!PX.data && pxShownHops(PX.data).some((e) => e && e.state === "intercepted");     // only when it occurs
  const sig = JSON.stringify([pxUi.color, protos, tun, LANG, hub()]);      // LANG: a language switch redraws it
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  const item = (c, cls, label, tip = "") => `<span${tip ? ` title="${esc(tip)}"` : ""}><i class="lg-px ${cls}"${c ? ` style="--c:${c}"` : ""}></i>${esc(label)}</span>`;
  const colors = pxUi.color === "proto" ? protos.map((p) => item(protoColor(p), "", protoName(p)))
    : LAT_TIERS.map(([, c, l]) => item(c, "", l));
  el.innerHTML = colors.join("")
    + item("", "s-ref", t("reference (measured from {hub})", { hub: hub() }))
    + item("", "s-dot", t("not measured"))
    + (tun ? item("", "s-tun", t("intercepted (local TUN / fake-IP)"),
      t("The machine that measures this hop runs a TUN / fake-IP proxy: every host name resolves into 198.18.0.0/15 there, so the number would be fake.")) : "")
    + item(PX_DOWN, "s-down", t("down"))
    + `<span class="px-inuse">${esc(t("▶ inferred in use · faded = standby"))}</span>`
    + `<span class="legend-r" id="px-status"></span>`;
}

function renderProxyChain() {
  const box = $("proxy");
  const d = PX.data;
  const sig = JSON.stringify([PX.fetched, PX.missing, d ? "" : PX.error, pxUi.color, LANG, hub()]);
  if (sig === PX.sig && box.innerHTML) return;
  PX.sig = sig;
  if (!d) { box.innerHTML = `<div class="empty">${esc(pxEmptyText())}</div>`; return; }
  const top = box.scrollTop;
  const srcs = d.sources.map((s) => `
    <section class="px-src">
      <div class="px-src-h"><i class="px-dev"></i><b>${esc(s.label || s.id || "")}</b>${s.originLabel && s.originLabel !== s.origin ? `<span>${esc(t("Origin: {origin}", { origin: s.originLabel }))}</span>` : ""}${s.note ? `<span>${esc(s.note)}</span>` : ""}</div>
      ${s.entries.map((en) => pxEntry(s, en)).join("")}
    </section>`).join("");
  box.innerHTML = `<div class="px-wrap">${srcs || `<div class="empty">${esc(t("No proxy sources."))}</div>`}
    <div class="px-foot">${esc(t("Each hop is measured from the machine it actually leaves from: TCP protocols time the connection handshake; Hysteria, TUIC and WireGuard use ICMP ping. Probes only open the port and never send proxy traffic."))}
    ${esc(t("Topology comes from config snapshots (no passwords). The route in use is inferred with Mihomo's rules; manual select groups are assumed to use their first option."))}</div></div>`;
  box.scrollTop = top;
}

function pxEntry(s, en) {
  // a name missing from the snapshot comes as type "proxy": with nothing known about it, it gets no type label either
  const unknown = en.options.length === 1 && en.options[0].landing.kind === "unknown" && !en.options[0].hops.length;
  const kind = unknown && en.type === "proxy" ? ""
    : Object.prototype.hasOwnProperty.call(GROUP_TYPE, en.type) ? GROUP_TYPE[en.type] && t(GROUP_TYPE[en.type]) : en.type || "";
  const more = en.more ? " · " + t("{n} more members not shown", { n: en.more }) : "";
  const rows = en.options.length ? en.options.map((o, i) => pxRow(s, o, i === en.active)).join("")
    : en.type === "reject" ? pxRow(s, { member: en.group, label: en.group, ok: false, total: null, hops: [], landing: { kind: "reject" } }, true)
    : `<div class="px-row on"><span class="px-mem">${esc(t("(no usable members)"))}</span></div>`;
  return `<div class="px-entry">
    <div class="px-entry-h"><span class="px-tag">${esc(en.label || "")}</span><span class="px-grp">${esc([en.group, kind].filter(Boolean).join(" · ") + more)}</span></div>
    ${rows}
  </div>`;
}

/** One route: member | chain | total. The chain wraps between hops (a hop and the server it reaches stay together, so do
 *  the arrow and the landing), so where the traffic leaves is always on screen; a piece wider than the whole column
 *  shrinks with an ellipsis (full text on hover), and anything still too wide scrolls, with a thin scrollbar. */
function pxRow(s, o, active) {
  const dev = `<span class="px-dev" title="${esc(pxOrigin(s))}"></span>`, land = landingText(o.landing);
  let cells = "", from = pxOrigin(s);
  o.hops.forEach((h, i) => {
    const e = pxEdge(h.edge);
    cells += `<span class="px-hop">${i ? "" : dev}<span class="px-link ${pxStyle(e)}" style="--c:${pxColor(e, h.proto)}" title="${esc(pxTip(e, from, h))}">`
      + `<i>${esc(protoName(h.proto))}</i><b>${esc(pxMs(e))}</b></span>`
      + `<span class="px-node ${pxCls(h.role)}" title="${esc([h.label || h.host, h.name].filter(Boolean).join("\n"))}">${esc(h.label || h.host || h.name || "?")}</span></span>`;
    from = h.label || h.host || "";
  });
  cells += `<span class="px-end">${o.hops.length ? "" : dev}<span class="px-arrow" aria-hidden="true">⇢</span>`
    + `<span class="px-land ${pxCls(o.landing.kind)}" title="${esc(land)}">${esc(land)}</span></span>`;
  const ph = pxPlaceholder(o);
  const memTip = (o.member || "") + (o.dialer ? "\n" + t("Dialer proxy: {d}", { d: pxLabel(o.dialer) }) : "")
    + (!ph && o.ok === false ? "\n" + t("A hop is down (or points to a retired address)") : "")
    + (o.landing.kind === "reject" ? "\n" + t("REJECT: Mihomo refuses these connections") : "")
    + (o.landing.kind === "unknown" ? "\n" + t("Missing from the config snapshot, an empty group, or a broken chain: there is no route to show") : "");
  return `<div class="px-row ${active ? "on" : ""} ${ph ? "stub" : o.ok === false ? "bad" : ""}">
    <span class="px-mem" title="${esc(memTip)}">${active ? "▶ " : ""}${esc(o.label ? pxLabel(o.label) : pxLabel(o.member))}</span>
    <span class="px-chain">${cells}</span>
    <span class="px-total" title="${esc(t("Sum of hop latencies (shown only when every hop was measured)"))}">${o.total != null ? Math.round(o.total) + " ms" : ""}</span>
  </div>`;
}

/* Labels on the proxy map are placed greedily: each takes the first of its candidate boxes that stays on the map and
 * clear of the markers and of every label placed before it. */
const PX_LOC_FS = 11, PX_LOC_H = 13, PX_HOP_FS = 10.5, PX_HOP_LH = 13;
const PX_TS = Array.from({ length: 19 }, (_, i) => (i + 1) / 20);      // points along an arc where its label may sit
let pxCtx = null;
/** Width of `text` in the map font (SVG text has no size until it is laid out, so it is measured on a canvas). */
function pxTextW(text, size) {
  pxCtx ||= document.createElement("canvas").getContext("2d");
  pxCtx.font = `${size}px ${getComputedStyle(document.documentElement).getPropertyValue("--mono").trim() || "monospace"}`;
  return pxCtx.measureText(text).width;
}
/** Boxes are {x, y, w, h}; a box may have a leader line `lead` [x1, y1, x2, y2] from its marker, which must not cross
 *  anything either and is kept as a thin obstacle. place(cands) keeps and returns the first free box; with `force`, when
 *  none is free, the one on the map that covers the least (null without `force`). add / drop keep or release a box.
 *  [x0, x1] is the part of the panel the world covers. */
function pxPlacer(x0, x1, H, taken) {
  const onMap = (r) => r.x >= x0 + 2 && r.y >= 2 && r.x + r.w <= x1 - 2 && r.y + r.h <= H - 2;
  const area = (a, b) => Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x))
    * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
  // sampled from 6px out, past the marker it starts from
  const crosses = ([ax, ay, bx, by], o) => {
    const t0 = Math.min(1, 6 / (Math.hypot(bx - ax, by - ay) || 1));
    for (let i = 0; i <= 8; i++) {
      const tt = t0 + ((1 - t0) * i) / 8, x = ax + (bx - ax) * tt, y = ay + (by - ay) * tt;
      if (x > o.x && x < o.x + o.w && y > o.y && y < o.y + o.h) return true;
    }
    return false;
  };
  const cover = (r) => taken.reduce((s, o) => s + area(r, o) + (r.lead && crosses(r.lead, o) ? 30 : 0), 0);
  const free = (c) => onMap(c) && !taken.some((o) => overlap(c, o, 2) || (c.lead && crosses(c.lead, o)));
  const add = (r) => {
    taken.push(r);
    if (r.lead) {
      const [ax, ay, bx, by] = r.lead;
      taken.push({ x: Math.min(ax, bx) - 1, y: Math.min(ay, by), w: Math.abs(bx - ax) + 2, h: Math.abs(by - ay), of: r });
    }
  };
  return {
    place(cands, force = false) {
      let r = cands.find(free) || null;
      if (!r && force) {
        const on = cands.filter(onMap);
        r = (on.length ? on : cands).reduce((a, b) => (cover(b) < cover(a) ? b : a));
      }
      if (r) add(r);
      return r;
    },
    add,
    drop: (r) => { for (let i = taken.length - 1; i >= 0; i--) if (taken[i] === r || taken[i].of === r) taken.splice(i, 1); },
  };
}
/** Where a place label may go: beside, above or below the marker, then stacked further out with a leader line. */
function pxLocCands(p, w, h) {
  const [x, y] = p, out = [];
  for (const k of [0, 1, 2]) {
    const s = k * (h + 2);
    if (!k) out.push({ x: x + 8, y: y - h / 2 }, { x: x - 8 - w, y: y - h / 2 });
    out.push({ x: x - w / 2, y: y - 7 - h - s, k }, { x: x - w / 2, y: y + 7 + s, k },
      { x: x + 5, y: y - 5 - h - s, k }, { x: x + 5, y: y + 5 + s, k },
      { x: x - 5 - w, y: y - 5 - h - s, k }, { x: x - 5 - w, y: y + 5 + s, k });
  }
  return out.map(({ k, ...c }) => {
    if (!k) return { ...c, w, h };
    const cx = Math.max(c.x, Math.min(x, c.x + w)), cy = Math.max(c.y, Math.min(y, c.y + h));
    return { ...c, w, h, lead: [x, y, cx, cy] };
  });
}

/** Map layout: one arc per measured hop between two places (drawn once even when several routes share it). Every line
 *  goes the short way round the globe; one that crosses the map's cut (lon0 ± 180°) is drawn twice, a world width apart,
 *  so it leaves one edge and comes back in from the other (fitView picks the cut that the fewest lines cross). */
function renderProxyMap() {
  const d = PX.data;
  const sig = JSON.stringify(["px", M.W, M.H, M.view, PX.fetched, PX.missing, d ? "" : PX.error, pxUi.color, LANG, hub()]);
  if (sig === M.sig) return;
  M.sig = sig;
  hideTip();
  const { W, H, proj } = M;
  const svg = d3.select("#map-svg").attr("viewBox", `0 0 ${W} ${H}`).on("click", null);
  svg.selectAll("*").remove();
  if (!d) {
    svg.append("text").attr("x", W / 2).attr("y", H / 2).attr("text-anchor", "middle").attr("font-size", 12)
      .attr("fill", "#7a7a7a").text(pxEmptyText());
    return;
  }
  const locs = pxLocations();
  const L = (k) => (locs[k] && Number.isFinite(locs[k].lat) && Number.isFinite(locs[k].lon) ? locs[k] : null);
  const P = (k) => (L(k) ? proj([L(k).lon, L(k).lat]) : null);
  const worldW = 2 * Math.PI * proj.scale();                  // 360° of longitude, in pixels
  const x0 = Math.max(0, W / 2 - worldW / 2), x1 = Math.min(W, W / 2 + worldW / 2);   // the part of the panel the world covers
  /** Ends of the line a → b the short way round (|Δlon| ≤ 180°) and the x offsets to draw it at: [0], or [0, ±worldW]
   *  when it crosses the cut. */
  const span = (a, b) => {
    const p0 = P(a), p2 = P(b);
    if (!p0 || !p2) return null;
    const dLon = ((((L(b).lon - L(a).lon + 180) % 360) + 360) % 360) - 180;
    const shift = Math.round((p0[0] + (dLon / 360) * worldW - p2[0]) / worldW) * worldW;
    return { p0, p2: [p2[0] + shift, p2[1]], offs: shift ? [0, -shift] : [0] };
  };
  const shiftBy = (o) => (o ? `translate(${o.toFixed(1)},0)` : null);
  const onMapX = (x) => x >= x0 && x <= x1;

  const { edges, legs, sameCity, unplaced } = pxLines(d);
  const pairs = {};
  for (const e of edges) (pairs[pxPair(e)] ||= []).push(e);

  svg.append("defs").append("clipPath").attr("id", "px-map-clip")
    .append("rect").attr("x", x0).attr("y", 0).attr("width", x1 - x0).attr("height", H);
  const g = svg.append("g").attr("clip-path", "url(#px-map-clip)");
  const marks = svg.append("g"), leaders = svg.append("g"), labels = svg.append("g");
  const hopLabels = new Map();      // place pair → labels of its active hops: they stack into one block
  const list = Object.values(pairs).flatMap((arr) => arr.sort((a, b) => a.active - b.active).map((e, i) => ({ ...e, i })));
  for (const e of list.sort((a, b) => a.active - b.active)) {
    const sp = span(e.from, e.to);
    if (!sp) continue;
    const { p0, p2, offs } = sp;
    const dx = p2[0] - p0[0], dy = p2[1] - p0[1], len = Math.hypot(dx, dy) || 1;
    const sign = e.from < e.to ? 1 : -1;
    const bow = Math.min(len * (0.05 + 0.035 * e.i), 26 + 16 * e.i) * sign;      // long arcs must not bulge off the map
    const c = [(p0[0] + p2[0]) / 2 - (dy / len) * bow, (p0[1] + p2[1]) / 2 + (dx / len) * bow];
    const curve = (t) => [0, 1].map((j) => (1 - t) ** 2 * p0[j] + 2 * (1 - t) * t * c[j] + t * t * p2[j]);
    const r = pxEdge(e.key);
    const st = pxStyle(r);
    const d0 = `M${p0[0].toFixed(1)},${p0[1].toFixed(1)} Q${c[0].toFixed(1)},${c[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    const tip = `${pxTip(r, e.fromLabel, e.hop)}\n${t("Routes using this hop:")}\n${[...new Set(e.users)].slice(0, 8).join("\n")}`;
    for (const o of offs) {
      g.append("path").attr("d", d0).attr("transform", shiftBy(o)).attr("fill", "none").attr("stroke", pxColor(r, e.proto))
        .attr("stroke-width", e.active ? 2.2 : 1).attr("opacity", e.active ? 0.95 : 0.3)
        .attr("stroke-dasharray", st === "ref" ? "6 4" : st === "dot" ? "1.5 4" : st === "tun" ? "6 3 1.5 3" : st === "down" ? "4 3" : null);
      g.append("path").attr("d", d0).attr("transform", shiftBy(o)).attr("fill", "none").attr("stroke", "transparent")
        .attr("stroke-width", 12).style("cursor", "help").on("mousemove", (ev) => showTip(ev, tip)).on("mouseleave", hideTip);
    }
    if (e.active) {
      // the label goes on the copy whose middle is on the map, else on the one with the longest piece on the map
      const vis = (o) => PX_TS.filter((tt) => onMapX(curve(tt)[0] + o));
      const o = offs.find((v) => onMapX(curve(0.5)[0] + v)) ?? offs.reduce((a, b) => (vis(b).length > vis(a).length ? b : a));
      const ts = vis(o), mid = ts.length ? (ts[0] + ts[ts.length - 1]) / 2 : 0.5;
      const pk = pxPair(e);
      if (!hopLabels.has(pk)) hopLabels.set(pk, []);
      hopLabels.get(pk).push({ text: `${protoName(e.proto)} ${pxMs(r, true)}`, color: pxColor(r, e.proto), len,
        ts: ts.length ? ts.sort((a, b) => Math.abs(a - mid) - Math.abs(b - mid)) : [0.5], at: (tt) => { const q = curve(tt); return [q[0] + o, q[1]]; } });
    }
  }
  for (const e of legs) {
    const sp = span(e.from, e.to);
    if (!sp) continue;
    const { p0, p2, offs } = sp;
    const tip = t("{label}: inside the provider's network, from its entry to the exit in this region (not measured)", { label: e.label });
    for (const o of offs) {
      g.append("line").attr("x1", p0[0]).attr("y1", p0[1]).attr("x2", p2[0]).attr("y2", p2[1]).attr("transform", shiftBy(o))
        .attr("stroke", pxUi.color === "proto" ? protoColor(e.proto) : "#8a8a8a")
        .attr("stroke-width", e.active ? 1.4 : 0.8).attr("stroke-dasharray", "1.5 4").attr("opacity", e.active ? 0.85 : 0.35);
      g.append("line").attr("x1", p0[0]).attr("y1", p0[1]).attr("x2", p2[0]).attr("y2", p2[1]).attr("transform", shiftBy(o))
        .attr("stroke", "transparent").attr("stroke-width", 10).style("cursor", "help")
        .on("mousemove", (ev) => showTip(ev, tip)).on("mouseleave", hideTip);
    }
  }

  // labels, greedily; the corner title and every marker are obstacles. Short codes beside their marker go first (the
  // most crowded places choose first), then the hop labels (shortest arcs first: they have the least room; one with no
  // free spot takes the one that covers the least), then each code grows into the place's full name where that finds
  // room: beside the marker, else stacked further out with a leader line; otherwise the short code stays.
  // the corner title, then one line per kind of hop that is not drawn
  const title = [`PROXY ROUTES · ${t("thick lines are the inferred routes in use")}`,
    sameCity.size && t("{n} same-city hops not drawn", { n: sameCity.size }),
    unplaced.size && t("{n} hops with unknown location not drawn", { n: unplaced.size })].filter(Boolean);
  title.forEach((line, i) => svg.append("text").attr("x", 12).attr("y", 20 + i * 14).attr("fill", "#5e5e5e").attr("font-size", 10.5)
    .attr("letter-spacing", ".06em").text(line));
  const places = [...pxUsedLocs()].map((k) => {
    const l = L(k), code = (l && l.code) || k;
    return { k, p: P(k), code, full: `${code} ${(l && l.name) || ""}`.trim() };
  }).filter((u) => u.p);
  const placer = pxPlacer(x0, x1, H, [{ x: 0, y: 0, w: 12 + Math.max(...title.map((l) => pxTextW(l, 10.5) + l.length * 0.63)) + 4, h: 26 + 14 * (title.length - 1) },
    ...places.map((u) => ({ x: u.p[0] - 4, y: u.p[1] - 4, w: 8, h: 8 }))]);
  const crowd = (u) => places.filter((v) => Math.hypot(v.p[0] - u.p[0], v.p[1] - u.p[1]) < 60).length;
  places.sort((a, b) => crowd(b) - crowd(a) || a.p[0] - b.p[0]);
  const locCands = (u, text) => pxLocCands(u.p, pxTextW(text, PX_LOC_FS) + 2, PX_LOC_H);
  for (const u of places) {
    marks.append("rect").attr("x", u.p[0] - 4).attr("y", u.p[1] - 4).attr("width", 8).attr("height", 8).attr("fill", "#f0f0f0");
    marks.append("circle").attr("cx", u.p[0]).attr("cy", u.p[1]).attr("r", 7).attr("fill", "transparent")   // full name on hover
      .on("mousemove", (ev) => showTip(ev, u.full)).on("mouseleave", hideTip);
    u.box = placer.place(locCands(u, u.code).filter((c) => !c.lead));
    u.text = u.code;
  }
  // hop labels of one place pair stack into one block (identical lines once)
  const blocks = [...hopLabels.values()].map((ls) => {
    ls = ls.filter((l, i) => ls.findIndex((m) => m.text === l.text && m.color === l.color) === i);
    const w = Math.max(...ls.map((l) => pxTextW(l.text, PX_HOP_FS))) + 4, h = ls.length * PX_HOP_LH + 2;
    const cands = [];      // above / below a point of the arc, centred or to one side, then further away
    for (const lift of [0, 1, 2, 3]) for (const tt of ls[0].ts) for (const dx of [-w / 2, -w + 6, -6]) {
      const [x, y] = ls[0].at(tt), bx = Math.max(x0 + 2, Math.min(x1 - 2 - w, x + dx));
      cands.push({ x: bx, y: y - 3 - h - lift * PX_HOP_LH, w, h }, { x: bx, y: y + 3 + lift * PX_HOP_LH, w, h });
    }
    return { ls, w, cands, len: Math.min(...ls.map((l) => l.len)) };
  }).sort((a, b) => a.len - b.len);
  for (const b of blocks) b.box = placer.place(b.cands);
  for (const b of blocks) b.box ||= placer.place(b.cands, true);
  for (const u of places) {
    if (u.full !== u.code) {
      if (u.box) placer.drop(u.box);
      const box = placer.place(locCands(u, u.full));
      if (box) Object.assign(u, { box, text: u.full });
      else if (u.box) placer.add(u.box);
    }
    if (!u.box) { const cs = locCands(u, u.code); u.box = placer.place(cs) || placer.place(cs, true); }
    const { box } = u;
    if (box.lead) leaders.append("line").attr("x1", box.lead[0]).attr("y1", box.lead[1]).attr("x2", box.lead[2]).attr("y2", box.lead[3])
      .attr("stroke", "#5a5a5a").attr("stroke-width", 1);
    labels.append("text").attr("x", box.x + 1).attr("y", box.y + 10).attr("font-size", PX_LOC_FS).attr("fill", "#b4b4b4")
      .attr("paint-order", "stroke").attr("stroke", "#050505").attr("stroke-width", 3).attr("pointer-events", "none").text(u.text);
  }
  for (const { ls, w, box } of blocks) {
    ls.forEach((l, i) => labels.append("text").attr("x", box.x + w / 2).attr("y", box.y + 10.5 + i * PX_HOP_LH)
      .attr("text-anchor", "middle").attr("font-size", PX_HOP_FS).attr("fill", l.color)
      .attr("paint-order", "stroke").attr("stroke", "#050505").attr("stroke-width", 3).attr("pointer-events", "none").text(l.text));
  }
}

document.addEventListener("click", (ev) => {
  const b = ev.target.closest && ev.target.closest("[data-pxview],[data-pxcolor]");
  if (!b) return;
  if (b.dataset.pxview) { pxUi.view = b.dataset.pxview; store.set("ma.pxview", pxUi.view); }
  if (b.dataset.pxcolor) { pxUi.color = b.dataset.pxcolor; store.set("ma.pxcolor", pxUi.color); }
  PX.sig = "";
  M.sig = "";
  renderMap();      // switching chains ↔ map also changes the projection: renderMap lays the map out again
});
// The tooltips of the subtitle and the status repeat the subtitle, whose age tickClock moves on even when nothing
// redraws the view (auto refresh paused): they take its current text on hover.
document.addEventListener("mouseover", (ev) => {
  const el = mapMode === "proxy" && ev.target.closest && ev.target.closest("#map-sub, #px-status");
  if (el && el.title) el.title = [$("map-sub").textContent, ...PX.tipMore].join("\n");
});
