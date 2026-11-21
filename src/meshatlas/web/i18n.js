"use strict";
/* Tiny i18n: English is the source text, other languages map English → translation.
 * t("{n} links", {n: 3}) → "3 links" / "3 条链路". Static HTML uses data-i18n, data-i18n-title, -aria and -ph. */

let LANG = "en";

function detectLang() {
  if (prefs.lang === "en" || prefs.lang === "zh") return prefs.lang;
  const l = ((navigator.languages && navigator.languages[0]) || navigator.language || "en").toLowerCase();
  return l.startsWith("zh") ? "zh" : "en";
}

function t(s, params) {
  let out = LANG === "zh" && Object.prototype.hasOwnProperty.call(ZH, s) ? ZH[s] : s;
  if (params) out = out.replace(/\{(\w+)\}/g, (m, k) => (params[k] != null ? params[k] : m));
  return out;
}

function applyLang() {
  LANG = detectLang();
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  document.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.i18nAria)); });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  updateLangButton();
  equalizeTopBar();
}

/* ---------- switching with a decrypt transition ----------
 * Visible text scrambles into random glyphs, the language flips while everything is scrambled, then the new text
 * resolves character by character (620 ms; same timing and glyph set as the language switch on ljj.world).
 * The flip itself is synchronous: nothing unscrambled is painted between the two halves. */
const SCRAMBLABLE = /[\p{L}\p{N}㐀-鿿]/u;
const GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#@%+";
const LANG_ANIM_MS = 620;
// Not scrambled: aria-live regions (a screen reader would read the glyphs out) and the focused control (its name
// would change every frame).
const LANG_ANIM_SKIP = "[data-no-translate], [aria-live], script, style, noscript, canvas, textarea";
let langAnim = null;   // { raf } while a transition runs

function scrambleText(value, amount, reveal = false) {
  const chars = Array.from(value);
  const idx = [];
  chars.forEach((c, i) => { if (SCRAMBLABLE.test(c)) idx.push(i); });
  const affected = Math.round(idx.length * Math.min(1, Math.max(0, amount)));
  idx.forEach((ci, order) => {
    if (reveal ? order >= affected : order < affected) chars[ci] = GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
  });
  return chars.join("");
}

/** Text nodes inside the viewport; hidden or scrolled-away text just switches. */
function visibleTextNodes() {
  const out = [];
  const vw = window.innerWidth, vh = window.innerHeight;
  const focused = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement && !n.parentElement.closest(LANG_ANIM_SKIP) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
  });
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (!SCRAMBLABLE.test(n.nodeValue)) continue;
    const p = n.parentElement;
    if (focused && focused.contains(p)) continue;
    if (!p.getClientRects().length) continue;
    const r = p.getBoundingClientRect();
    if (r.bottom >= 0 && r.top <= vh && r.right >= 0 && r.left <= vw) out.push(n);
  }
  return out;
}

/** Redraw everything in the current language, synchronously (applyPrefs defers the map to the next frame). */
function rerenderForLang() {
  hideTip();
  tickClock();
  // The drawer and the palette work without data (API offline, first poll pending), so redraw them before the S check.
  if (!$("drawer").hidden) {
    const a = document.activeElement, keep = a && a.closest && a.closest("#drawer") && a.dataset.pref ? [a.dataset.pref, a.dataset.val] : null;
    renderSettings();
    if (keep) [...document.querySelectorAll("#drawer [data-pref]")].find((b) => b.dataset.pref === keep[0] && b.dataset.val === keep[1])?.focus();
  }
  if (!$("palette").hidden) renderPalette();
  relangLog();   // in place, also while the detail page hides the log
  if (!S) return;
  $("node-list").dataset.sig = "";
  M.sig = "";
  if (view === "map") {
    layoutMap();
    renderAll(true);
    renderTimeline();
  } else renderAll(true);
  if (!$("issues").hidden) renderIssues();
}

function syncLangRadios() {
  document.querySelectorAll('#drawer [data-pref="lang"]').forEach((b) => {
    const on = JSON.parse(b.dataset.val) === prefs.lang;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
  });
}

/** Set the language preference ("auto" | "en" | "zh") and switch with the transition when the language changes. */
function setLang(value) {
  if (langAnim) return;
  prefs.lang = value;
  store.set("ma.prefs", prefs);
  syncLangRadios();
  const next = detectLang();
  if (next === LANG) return;
  const log = $("log");
  log.setAttribute("aria-busy", "true");   // hold live-region announcements while the log is re-translated
  const swap = () => { applyLang(); try { rerenderForLang(); } catch (e) { console.error(e); } };
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    swap();
    log.removeAttribute("aria-busy");
    return;
  }

  const root = document.documentElement;
  root.dataset.languageTransitioning = "true";
  const half = LANG_ANIM_MS / 2;
  const out = visibleTextNodes().map((node) => ({ node, from: node.nodeValue }));
  const t0 = performance.now();
  langAnim = {};

  const stepIn = (inn, t1) => (now) => {
    const q = Math.min(1, (now - t1) / half);
    for (const j of inn) if (j.node.isConnected) j.node.nodeValue = q >= 1 ? j.to : scrambleText(j.to, q, true);
    if (q < 1) { langAnim.raf = requestAnimationFrame(stepIn(inn, t1)); return; }
    langAnim = null;
    delete root.dataset.languageTransitioning;
    log.removeAttribute("aria-busy");
  };
  const stepOut = (now) => {
    const p = Math.min(1, (now - t0) / half);
    const amount = Math.min(1, p / 0.8);   // scramble over the first 40% of the whole run, fully scrambled until 50%
    for (const j of out) if (j.node.isConnected) j.node.nodeValue = scrambleText(j.from, amount);
    if (p < 1) { langAnim.raf = requestAnimationFrame(stepOut); return; }
    // Everything below runs in one frame, so the browser never paints unscrambled text.
    for (const j of out) if (j.node.isConnected) j.node.nodeValue = j.from;
    swap();
    const inn = visibleTextNodes().map((node) => ({ node, to: node.nodeValue }));
    for (const j of inn) j.node.nodeValue = scrambleText(j.to, 0, true);
    langAnim.raf = requestAnimationFrame(stepIn(inn, performance.now()));
  };
  langAnim.raf = requestAnimationFrame(stepOut);
}

/** The top bar keeps the same width in both languages: "Search" and "搜索" differ by ~22px, which would shift every
 *  button (including the one just clicked) after a switch. The difference goes into a spacer before the search
 *  button (--tb-slack, used by style.css), so the buttons themselves look the same as before. */
function equalizeTopBar() {
  const btn = document.getElementById("btn-search"), label = btn && btn.querySelector("[data-i18n]");
  if (!label) return;
  const cs = getComputedStyle(label), probe = document.createElement("span");
  probe.style.cssText = `position:absolute;visibility:hidden;white-space:nowrap;font:${cs.font};letter-spacing:${cs.letterSpacing}`;
  document.body.appendChild(probe);
  const width = (s) => { probe.textContent = s; return probe.getBoundingClientRect().width; };
  const key = label.dataset.i18n, en = width(key), zh = width(ZH[key] || key);
  probe.remove();
  btn.style.setProperty("--tb-slack", `${Math.max(en, zh) - (LANG === "zh" ? zh : en)}px`);
}

/** An action button, not a toggle: its name says what clicking does, in the target language (lang attribute), and
 *  contains the visible text ("EN" / "中") so voice control can find it. */
function updateLangButton() {
  const b = document.getElementById("btn-lang");
  if (!b) return;
  const toZh = LANG !== "zh";
  b.lang = toZh ? "zh-CN" : "en";
  b.title = toZh ? "切换为中文" : "Switch to English (EN)";
  b.setAttribute("aria-label", b.title);
  const s = b.querySelector("span");
  if (s) s.textContent = toZh ? "中" : "EN";
}

document.getElementById("btn-lang")?.addEventListener("click", () => setLang(LANG === "zh" ? "en" : "zh"));

const ZH = {
  // settings
  "Not supported by this browser.": "这个浏览器不支持。",
  "The browser blocked notifications; allow them in the site settings.": "浏览器已拒绝通知，要在网站设置里放开。",
  "Allowed.": "已允许。",
  "The browser will ask for permission.": "打开时浏览器会请求授权。",
  "Language": "语言", "Auto": "自动",
  "Nodes": "节点", "List order": "列表排序", "Default": "默认顺序", "By location": "按地点", "Problems first": "异常在前",
  "By latency": "按延迟", "Right column of the list": "列表右侧显示", "Latency": "延迟", "Memory": "内存",
  "Containers": "容器", "Availability": "可用率", "Show latency sparklines in the list": "列表里显示延迟趋势线",
  "Visible nodes": "显示哪些节点",
  "Hidden nodes leave the list, the map and the top-bar numbers. Search still finds them.": "隐藏的节点不进列表、地图和顶栏统计，搜索里还能找到。",
  "Map": "地图", "Latency labels on links": "连线上的延迟标签", "All": "全部", "Fastest per location": "每个地点只标最快一条",
  "None": "不标", "Land brightness": "底图亮度", "Dim": "暗", "Normal": "标准", "Bright": "亮",
  "Animated flow on links": "连线流动动画", "Show public ping links": "显示公网 ping 连线",
  "Servers outside the tailnet, watched with ICMP": "不在 tailnet 里、用 ICMP 监控的服务器",
  "Show location code and node count": "地点旁显示代号和台数", "Layout": "布局",
  "Event log at the bottom": "底部事件日志", "Latency timeline at the bottom": "底部延迟时间线",
  "With both off the map takes the space.": "两个都关掉时，地图占满下面的空间。",
  "Top-bar metrics": "顶栏指标", "Up to 5, in the order you tick them.": "最多 5 个，按勾选顺序排列。",
  "Detail page modules": "详情页模块", "When a module is off, its neighbour takes the space.": "关掉的模块，旁边的会自动占满它的位置。",
  "Refresh and time": "刷新和时间", "Auto refresh": "自动刷新", "5 s": "5 秒", "15 s": "15 秒", "1 min": "1 分钟",
  "Paused": "暂停", "Big clock shows": "顶栏时钟大字", "Local time": "本地时间", "Notifications": "通知",
  "Browser notification on new alerts": "新告警时弹浏览器通知",
  "Node offline, container or service trouble, link down. The page must stay open.": "节点离线、容器或服务异常、链路不通。页面要开着。",
  "Reset to defaults": "恢复默认设置", "Settings live in this browser only.": "设置只存在这个浏览器里。",
  "Metric tiles": "指标格", "Resource / latency chart": "资源 / 延迟历史图", "Latency bars": "延迟条形图",
  "Containers · services · Kuma · links": "容器 · 服务 · Kuma · 链路", "Event stream": "事件流",
  "Sidebar: availability": "左栏：可用率", "Sidebar: alerts": "左栏：告警", "Sidebar: notes": "左栏：备注",
  "Node count": "节点数", "Online": "在线", "Hub P50 latency": "中心节点 P50 延迟", "Issues": "异常",
  "Average CPU": "平均 CPU", "Kuma up": "Kuma 正常", "Slowest link": "最慢链路", "Relayed links": "走中继的链路",
  // time
  "{d}d {h}h": "{d}天 {h}小时", "{h}h {m}m": "{h}小时 {m}分", "{m}m": "{m}分",
  "{n}s ago": "{n}秒前", "{n} min ago": "{n} 分钟前", "{n}h ago": "{n} 小时前", "{n}d ago": "{n} 天前",
  // issues
  "{id} is offline (Tailscale)": "{id} 离线（Tailscale）",
  "{id}: public ping unreachable (from {src})": "{id}：公网 ping 不通（从 {src}）",
  "{id}: collection failed {n} times in a row: {err}": "{id}：连续 {n} 次采集失败：{err}",
  "{id}: {n} systemd unit(s) failed": "{id}：{n} 个服务 failed",
  "{id}: {n} unhealthy container(s)": "{id}：{n} 个容器异常",
  "{id}: {n} Kuma monitor(s) DOWN: {names}": "{id}：{n} 个 Kuma 监控 DOWN：{names}",
  "+{n} hidden": "+{n} 隐藏", "{n} nodes": "{n} 台", "Show the list of issues": "点击查看异常列表",
  // list and map
  "Click to select, double-click or Enter for details": "单击选中，双击或回车打开详情", "last seen": "最后在线",
  "{name}: {n} nodes, close card": "{name}：{n} 台，收起说明", "{name}: {n} nodes, open card": "{name}：{n} 台，展开说明",
  "Close card": "收起说明",
  "Mesh · {n} pairs ({x} across locations) · direct {d} · relayed {r}": "全网 · {n} 对节点（跨地点 {x}）· 直连 {d} · 中继 {r}",
  "public ping {n}": "公网 ping {n}", "measured to {id}": "别的节点测到 {id}", "from {id}": "{id} 出发",
  "{n} links": "{n} 条链路", "direct": "直连", "relayed": "中继", "public ping": "公网 ping", "{n} down": "不通 {n}",
  "{id}: no latency data": "{id} 没有延迟数据", "unreachable": "不通", "{n} pairs": "{n} 对",
  "public ICMP ping (outside Tailscale)": "公网 ICMP ping（不走 Tailscale）", "direct WireGuard path": "WireGuard 直连",
  "approx.": "大概位置", "Click to open": "点击展开",
  // detail panel
  "{n} cores · load {l}": "{n} 核 · load {l}", "free": "可用", "No system data from this node yet.": "这台还没采到系统数据。",
  "Collecting…": "正在采集…", "Not collected over SSH: status and latency only.": "这台不做 SSH 采集，只显示在线状态和延迟。",
  "Overview": "概览", "Services": "服务", "LOCAL": "本机", "Open the full detail page (Enter)": "打开完整详情页（回车）",
  "Details": "详情", "up {d}": "已运行 {d}", "collected": "采集", "history": "历史", "{n} points": "{n} 个点",
  "not in the tailnet": "不在 tailnet 里", "online": "在线", "home DERP {r}": "默认 DERP {r}",
  "offline · last seen {ago}": "离线 · 最后在线 {ago}", "To {id}": "到 {id}",
  "this node is the hub (latency is measured from here)": "本机就是中心节点（延迟从这里测）", "Location": "位置",
  "unknown": "未知", "System": "系统", "{r} running / {n} · {b} unhealthy": "{r} 在跑 / {n} 个 · 异常 {b}",
  "no Docker": "没装 Docker", "{r} running · {f} failed": "{r} 个在跑 · failed {f}",
  "{u} up · {d} down · {n} total": "{u} up · {d} down · 共 {n} 项", "Collect": "采集",
  "local, every {s}s": "本机 · 每 {s} 秒", "read-only SSH, every {s}s": "SSH 只读 · 每 {s} 秒",
  "last {n} attempts failed: {err}": "最近 {n} 次失败：{err}", "showing data from {ago}": "页面上是 {ago}的数据",
  "waiting for the first round": "等待第一次采集", "public ping from {src}, every {s}s": "公网 ping（从 {src}）· 每 {s} 秒",
  "Tailscale status and latency only": "只看 Tailscale 在线和延迟", "Availability 24h": "可用率 24H",
  "{n} samples": "{n} 次采样", "recording since {ago}": "从 {ago}开始记录", "collecting": "还在累积",
  "Docker is not installed here.": "这台没装 Docker。", "Containers are not collected for this node.": "这台不采集容器。",
  "(standalone)": "（独立容器）", "Running": "运行", "Stopped": "没在跑", "Problems": "异常",
  "Filter: container / project / image": "过滤：容器名 / 项目 / 镜像",
  "CPU / memory refresh every few rounds on this node": "这台的 CPU / 内存隔几轮才刷新一次",
  "No matching containers.": "没有匹配的容器。", "problem": "异常",
  "container network only, not published": "只在容器网络里，没发布到宿主机", "localhost only": "仅本机",
  "all interfaces": "所有网卡", "host network: uses host ports directly, Docker shows no mapping": "host 网络：直接用宿主机端口，docker 看不到映射",
  "no published ports": "没有发布端口", "Image": "镜像", "Status": "状态", "Project": "项目",
  "(not started by compose)": "（不是 compose 起的）", "Created": "创建", "Networks": "网络", "Ports": "端口", "Logs": "日志",
  "last check": "最后检查", "status page": "状态页", "No systemd here (or nothing collected).": "这台没有 systemd（或没采到）。",
  "Services are not collected for this node.": "这台不采集服务。", "Unit": "单元", "user unit": "用户单元", "State": "状态",
  "Description": "说明", "Inspect": "查看", "System units": "系统服务", "hide": "收起", "show": "展开",
  "No latency data yet (the first round takes about half a minute).": "还没有延迟数据（第一轮探测大约要半分钟）。",
  "Not in the tailnet and no public ping configured, so no latency data.": "不在 tailnet 里，也没配置公网 ping，所以没有延迟数据。",
  "measured by other nodes": "别的节点测到这台", "measured from this node": "从这台出发",
  "History is still building up; check back in a few minutes.": "历史还在累积，过几分钟再看。",
  "Click to open {id}": "点击打开 {id}", "No recent events for {id}.": "最近没有 {id} 的事件。", "No recent alerts.": "最近没有告警。",
  // charts and footer
  "Loading…": "加载中…", "No history yet: one point per latency round.": "还没有历史数据：每轮延迟探测记一个点。",
  "No latency data for this node.": "这台没有延迟数据。", "{d} of data": "有数据 {d}", "{n} lines": "{n} 条",
  "{m}-min averages": "每点 {m} 分钟均值", "7 days": "7 天", "{h} hours": "{h} 小时", "No data.": "没有数据。",
  "spikes up to {ms} are clipped at the top": "尖峰最高 {ms}，超出部分贴顶显示",
  "Auto refresh paused (turn it on in settings)": "已暂停自动刷新（设置里打开）",
  "latency {ago} ({s}s)": "延迟 {ago}（用时 {s}s）", "measuring…": "探测中…", "first latency round…": "第一轮延迟探测中…",
  "waiting for latency data": "等待延迟数据",
  // detail page
  "Links": "链路", "Back to map": "返回地图", "Previous node": "上一台", "hidden": "已隐藏", "Next node": "下一台",
  "Copy link": "复制链接", "History range": "历史范围", "last {n} attempts failed": "最近 {n} 次失败",
  "public ping from {src}": "公网 ping（从 {src}）", "not collected": "不采集", "offline": "离线", "HIDDEN": "已隐藏",
  "24h ago": "24h 前", "12h ago": "12h 前", "now": "现在", "Collecting (one sample per node round)": "还在累积（每轮采集记一次）",
  "No alerts": "没有告警", "Show it in the panel below": "在下方面板中查看", "Open the detail page there": "打开详情页并定位到这里",
  "No notes. Add them with notes = [...] under [nodes.{id}] in meshatlas.toml.": "没有备注。在 meshatlas.toml 的 [nodes.{id}] 下用 notes = [...] 添加。",
  "Last collected": "最后采集", "All detail modules are off. Turn some on in settings (,).": "详情页的模块都关掉了，在设置（,）里打开。",
  "this node is the hub": "本机就是中心节点", "not measured": "没有测量", "Tailscale status": "Tailscale 在线状态",
  "from public ping": "公网 ping 判断", "tailscale ping": "Tailscale 延迟测量", "no systemd": "没有 systemd", "all up": "全部 up",
  "Chart content": "图表内容", "Resources": "资源", "Latency history": "延迟历史", "{m} min per point": "每点 {m} 分钟",
  "CPU and memory history": "CPU 和内存历史", "No latency data yet.": "还没有延迟数据。",
  "over the selected range": "按当前历史范围算", "Open {id}": "打开 {id}", "No alerts for this node.": "这台没有告警事件。",
  "No events recorded for this node yet.": "这台还没有记录到事件。", "{n} events": "{n} 条", "Event filter": "事件筛选",
  "Alerts": "告警", "Load older": "加载更早的", "Newest first · kept for {d} days": "最新在上 · 保留 {d} 天",
  "Online history": "在线记录", "{h}h ago": "{h} 小时前", "no data": "没有数据", "online {p}%": "在线 {p}%",
  "host network": "host 网络", "localhost": "仅本机", "internal": "内部",
  "Filter: container / project / image / port": "搜索容器 / 项目 / 镜像 / 端口", "Filter containers": "过滤容器",
  "Group by compose project": "按 compose 项目分组", "Group by project": "按项目分组", "No latency history.": "没有延迟历史。",
  "Copied ✓": "已复制 ✓",
  // search, issues, matrix, help
  "Close": "关闭", "Collector": "采集器", "All clear, nothing to report.": "全部正常，没有异常。", "Locations": "地点",
  "Commands": "命令", "standalone": "独立", "Map: links from the selected node": "地图：选中节点出发的连线",
  "Map: whole mesh": "地图：全网连线", "Map: latency matrix": "地图：延迟矩阵", "Open details of {id}": "打开 {id} 的详情页",
  "Back to the map": "返回地图", "Display settings": "显示设置", "Keyboard shortcuts": "快捷键",
  "Pause auto refresh": "暂停自动刷新", "Resume auto refresh (5 s)": "恢复自动刷新（5 秒）", "Refresh now": "立即刷新",
  "Open the Uptime Kuma status page": "打开 Uptime Kuma 状态页",
  "No matches. Try a node, container, service, Kuma monitor, location or command.": "没有匹配的结果。可以搜节点、容器、服务、Kuma 监控、地点和命令。",
  "Numbers are round-trip times in ms; brighter is faster.": "数字是往返延迟（ms），底色越亮越快。",
  "Dashed underline": "虚线下划线", "DERP / peer relay": "DERP / peer relay 中继", "dotted underline": "点线下划线",
  "Click a number to select its source.": "点数字选中出发节点。",
  "Latency matrix · {s} sources × {n} nodes · {e} links": "延迟矩阵 · {s} 个出发点 × {n} 台 · {e} 条",
  "Search nodes, containers, services (/ or ⌘K)": "搜索节点、容器、服务（/ 或 ⌘K）", "Search": "搜索",
  "Display settings (,)": "显示设置（,）", "Keyboard shortcuts (?)": "快捷键（?）", "Map mode (m)": "地图模式（m）",
  "Selected": "选中节点", "Links from the selected node": "只画选中节点出发的连线", "Mesh": "全网",
  "Every link between every pair of nodes": "画出所有节点两两之间的连线", "Matrix": "矩阵",
  "Latency table of all pairs": "所有节点两两之间的延迟表", "Map of the tailnet": "tailnet 地图",
  "click an event to open its node": "点事件跳到对应节点", "Time range": "时间范围",
  "Search nodes, containers, services, Kuma monitors, locations or commands": "搜节点、容器、服务、Kuma 监控、地点或命令",
  "select": "选择", "open": "打开", "separate words with spaces to match all of them": "空格分开多个词同时匹配",
  "Search nodes, containers, services, Kuma monitors, locations and commands": "搜索节点、容器、服务、Kuma 监控、地点和命令",
  "Previous / next node (list order)": "切换节点（按列表顺序）",
  "Open the selected node's detail page (or double-click it)": "打开选中节点的详情页（列表里双击也行）",
  "Previous / next node on a detail page": "详情页里切到上一台 / 下一台",
  "Close a dialog → close the location card → back to the map": "关闭弹层 → 收起地点说明 → 从详情页回到地图",
  "Map mode: selected → mesh → matrix (→ proxy, when configured)": "地图模式：选中节点 → 全网 → 矩阵（→ 代理，配置了才有）",
  "This help": "这个说明",
  "On the map: hover a location for a preview, click it for its card, double-click a node in the card for details. Detail pages have their own URL.":
    "地图上：悬停地点看预览，点击展开说明框，双击说明框里的机器打开详情页。详情页的地址可以直接收藏或分享。",
  "Source code on GitHub": "GitHub 上的源代码", "Map: proxy routes": "地图：代理链路",
  // proxy view (proxy.js)
  "Proxy": "代理", "Proxy chains: each machine's routing paths and per-hop latency": "代理链路：各台机器的分流路径和每一跳延迟",
  "Proxy view layout": "代理视角的画法", "Chains": "链路图",
  "One route per row: device → relay → exit → destination": "一行一条路线：设备 → 中转 → 出口 → 落地",
  "Draw each hop as an arc on the map": "把每一跳画成地图上的弧线", "Line color": "线的着色",
  "Color each hop by its latency": "按这一跳的延迟着色", "By protocol": "按协议", "Color each hop by its protocol": "按这一跳用的协议着色",
  "Manual select · first option assumed": "手动选择 · 按第一项推断", "Fallback · first healthy option": "故障转移 · 取第一个能通的",
  "Load balance · first healthy option assumed": "负载均衡 · 按第一个能通的推断", "URL test · fastest option": "测速选优 · 取最快的",
  "Single proxy": "固定节点",
  "Direct (no proxy)": "直连（不走代理）", "Residential IP": "住宅 IP", "Provider · {region}": "机场 · {region}", "Provider": "机场",
  "Old address, probably dead": "旧地址，大概率不通", "Internet": "互联网", "Direct": "直连",
  "Hong Kong": "香港", "Singapore": "新加坡", "Japan": "日本", "United States": "美国", "USA": "美国", "Taiwan": "台湾",
  "United Kingdom": "英国", "Germany": "德国", "South Korea": "韩国", "Korea": "韩国",
  "down": "不通", " · {p}% loss": " · 丢 {p}%", "{from} → {to} ({addr})": "{from} → {to}（{addr}）",
  "forwarded by the previous server": "由上一台服务器转发",
  "Not measured: MeshAtlas can't reach the machine this hop starts from": "测不到：MeshAtlas 连不到这一跳的出发机器",
  "Measurement failed: {err}": "测量失败：{err}", "TCP connect": "TCP 建连", "failed all 3 times": "3 次全失败",
  ", {n} of 3 lost": "，3 次丢 {n} 次", "Measured from {node}": "从 {node} 测",
  " (reference: the real starting point can't be reached)": "（参考：连不到真实起点）",
  "Proxy routes · {s} sources · {n} hops · {when}": "代理链路 · {s} 个来源 · {n} 跳 · {when}",
  "measured {ago}": "{ago}测", "not measured yet": "还没测", " ({s}s)": "（用时 {s}s）",
  "some measurements failed": "部分测量失败", "refresh failed": "刷新失败",
  "Proxy routes · loading…": "代理链路 · 读取中…", "Proxy routes · could not load: {err}": "代理链路 · 读取失败：{err}",
  "Proxy view · not configured on the server": "代理视角 · 服务端没有配置",
  "reference (measured from {hub})": "参考（从 {hub} 测）", "▶ inferred in use · faded = standby": "▶ 推断在用 · 变淡的是备用",
  "Loading proxy routes…": "正在读取代理链路…",
  "The proxy view is not configured. Add a [proxy] section to the config file.": "没有配置代理视角。在配置文件里加上 [proxy] 一节。",
  "Could not load proxy routes ({err}). Trying again in a minute.": "读取代理链路失败（{err}），一分钟后重试。",
  "No proxy sources.": "没有代理来源。", "Origin: {origin}": "从 {origin} 发出",
  "Each hop is measured from the machine it actually leaves from: TCP protocols time the connection handshake; Hysteria, TUIC and WireGuard use ICMP ping. Probes only open the port and never send proxy traffic.":
    "每一跳从它真正出发的机器测：TCP 协议测建连时间，Hysteria、TUIC 和 WireGuard 测 ICMP；只连到端口，不发代理数据。",
  "Topology comes from config snapshots (no passwords). The route in use is inferred with Mihomo's rules; manual select groups are assumed to use their first option.":
    "拓扑来自配置快照（不含密码）；「在用」按 Mihomo 的规则推断，手动选择组按第一项算。",
  "{n} more members not shown": "另有 {n} 个成员没画", "(no usable members)": "（没有可用成员）",
  "Dialer proxy: {d}": "前置：{d}", "A hop is down (or points to a retired address)": "有一跳不通（或是已停用的旧地址）",
  "Sum of hop latencies (shown only when every hop was measured)": "各跳延迟相加（只在每一跳都测到时给出）",
  "Routes using this hop:": "用到它的路线：",
  "{label}: inside the provider's network, from its entry to the exit in this region (not measured)": "{label}：机场内部，从入口到这个地区的出口（测不到）",
  "thick lines are the inferred routes in use": "粗线是推断在用的路线", "{n} same-city hops not drawn": "同城 {n} 跳没画弧",
  "{n} hops with unknown location not drawn": "位置未知的 {n} 跳没画弧",
  "Relay chain · members dialed in order": "链式中转 · 成员按顺序串联", "Reject": "拒绝",
  "Rejected (REJECT)": "拒绝连接（REJECT）", "No known route": "没有已知路线",
  "REJECT: Mihomo refuses these connections": "REJECT：Mihomo 直接拒绝这些连接",
  "Missing from the config snapshot, an empty group, or a broken chain: there is no route to show": "配置快照里没有它、是空组，或者链路断了：没有路线可画",
  "intercepted": "被拦截", "intercepted (local TUN / fake-IP)": "被拦截（本机 TUN / fake-IP）",
  "The machine that measures this hop runs a TUN / fake-IP proxy: every host name resolves into 198.18.0.0/15 there, so the number would be fake.":
    "测这一跳的机器开着 TUN / fake-IP 代理：域名在那里都解析到 198.18.0.0/15，测出来的数是假的。",
  "Not measured: intercepted by a local TUN/fake-IP proxy on {node}": "没测：被 {node} 本机的 TUN / fake-IP 代理拦截",
  "{host} resolves into 198.18.0.0/15 (fake-IP) there, so the number would be fake": "{host} 在那台机器上解析到 198.18.0.0/15（fake-IP），测出来的数是假的",
  "Not measured: {host} is a host name and [proxy] resolve_hostnames = false": "没测：{host} 是域名，而 [proxy] 设了 resolve_hostnames = false",
  "auto refresh paused": "自动刷新已暂停", "Not measured: {err}": "没测：{err}", "Down: cannot resolve {host}": "不通：解析不了 {host}", "Down: {err}": "不通：{err}",
};
document.fonts?.ready.then(equalizeTopBar);   // measured again once web fonts are in
