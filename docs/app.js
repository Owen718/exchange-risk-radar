const DATA_URL = "data/events.json";
const REFRESH_MS = 60_000;

const state = {
  events: [],
  meta: {},
  type: "important",
  exchange: "all",
  query: "",
  showOriginal: false,
};

const $ = (sel) => document.querySelector(sel);

function safeUrl(value) {
  try {
    const u = new URL(value, window.location.href);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

function fmtUtc(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

function relTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  if (Number.isNaN(diff)) return "";
  const min = Math.round(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day} 天前`;
  return `${Math.round(day / 30)} 个月前`;
}

function badge(text, cls) {
  const el = document.createElement("span");
  el.className = `badge ${cls}`;
  el.textContent = text;
  return el;
}

function importanceDots(n) {
  const wrap = document.createElement("span");
  wrap.className = "imp";
  wrap.title = `重要性 ${n}/5`;
  for (let i = 1; i <= 5; i++) {
    const dot = document.createElement("i");
    if (i <= n) dot.className = "on";
    wrap.appendChild(dot);
  }
  return wrap;
}

function renderCard(ev) {
  const card = document.createElement("article");
  card.className = "card";

  const top = document.createElement("div");
  top.className = "card-top";
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = `${fmtUtc(ev.published_at)} · ${relTime(ev.published_at)}`;
  top.appendChild(time);
  top.appendChild(badge(ev.exchange || "Unknown", "exchange"));
  top.appendChild(badge(ev.type_label || ev.event_type || "其他", ev.event_type || "other"));
  if (ev.catalog) top.appendChild(badge(ev.catalog, "other"));
  card.appendChild(top);

  const h2 = document.createElement("h2");
  const url = safeUrl(ev.url);
  const shownTitle = ev.display_title || ev.title || "(无标题)";
  if (url) {
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = shownTitle;
    h2.appendChild(a);
  } else {
    h2.textContent = shownTitle;
  }
  card.appendChild(h2);

  if (ev.summary) {
    const p = document.createElement("p");
    p.className = "summary";
    p.textContent = ev.summary;
    card.appendChild(p);
  }

  if (state.showOriginal && ev.title && ev.title !== shownTitle) {
    const original = document.createElement("p");
    original.className = "original-title";
    original.textContent = `原文：${ev.title}`;
    card.appendChild(original);
  }

  const bottom = document.createElement("div");
  bottom.className = "card-bottom";
  const left = document.createElement("div");
  left.className = "symbols";
  for (const sym of ev.symbols || []) {
    const s = document.createElement("span");
    s.className = "symbol";
    s.textContent = sym;
    left.appendChild(s);
  }
  bottom.appendChild(left);

  const right = document.createElement("div");
  right.className = "meta-right";
  right.appendChild(importanceDots(Number(ev.importance) || 1));
  if (url) {
    const src = document.createElement("a");
    src.className = "source-link";
    src.href = url;
    src.target = "_blank";
    src.rel = "noopener noreferrer";
    src.textContent = "原文 ↗";
    right.appendChild(src);
  }
  bottom.appendChild(right);
  card.appendChild(bottom);
  return card;
}

function filtered() {
  const q = state.query.trim().toLowerCase();
  return state.events.filter((ev) => {
    if (state.type === "important") {
      if ((Number(ev.importance) || 1) < 3) return false;
    } else if (state.type !== "all" && ev.event_type !== state.type) {
      return false;
    }
    if (state.exchange !== "all" && ev.exchange !== state.exchange) return false;
    if (!q) return true;
    const hay = [
      ev.display_title,
      ev.title,
      ev.summary,
      ev.exchange,
      ev.type_label,
      ...(ev.symbols || []),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
}

function render() {
  const feed = $("#feed");
  const empty = $("#empty");
  feed.replaceChildren();
  const rows = filtered();
  for (const ev of rows) feed.appendChild(renderCard(ev));
  empty.hidden = rows.length > 0 || state.events.length === 0 && !state.meta.generated_at;
  $("#result-line").textContent = state.events.length
    ? `显示 ${rows.length} / ${state.events.length} 条事件`
    : "";
}

function renderStats() {
  const events = state.events;
  const total = events.length;
  const now = Date.now();
  const last24 = events.filter((e) => {
    const t = new Date(e.published_at).getTime();
    return Number.isFinite(t) && now - t <= 24 * 3600 * 1000;
  }).length;
  const tags = events.filter((e) => e.event_type === "monitoring_tag").length;
  const sources = state.meta.sources || {};
  const ok = Object.values(sources).filter((s) => s && s.ok).length;
  const all = Object.keys(sources).length;
  $("#stat-total").textContent = total;
  $("#stat-24h").textContent = last24;
  $("#stat-tag").textContent = tags;
  $("#stat-sources").textContent = all ? `${ok}/${all}` : "—";

  const generated = state.meta.generated_at;
  $("#updated").textContent = generated
    ? `更新于 ${fmtUtc(generated)} · ${relTime(generated)}`
    : "等待数据";
  $("#generated").textContent = generated ? `数据生成：${fmtUtc(generated)}` : "";
  const dot = $("#live-dot");
  dot.className = "dot " + (all && ok === all ? "ok" : ok ? "ok" : "err");

  const line = $("#sources-line");
  line.replaceChildren();
  const labels = {
    binance: "Binance",
    bithumb: "Bithumb",
    bybit: "Bybit",
    okx: "OKX",
    bitget: "Bitget",
    kucoin: "KuCoin",
    gate: "Gate",
    mexc: "MEXC",
    crypto_com: "Crypto.com",
    coinbase_status: "Coinbase Status",
    kraken_status: "Kraken Status",
  };
  for (const [name, s] of Object.entries(sources)) {
    const chip = document.createElement("span");
    chip.className = "source-chip" + (s && s.ok ? "" : " err");
    chip.textContent = `${labels[name] || name} ${s && Number.isFinite(s.count) ? s.count : "—"}`;
    if (s && s.error) chip.title = s.error;
    line.appendChild(chip);
  }
}

function populateExchanges() {
  const sel = $("#exchange-filter");
  const current = sel.value || "all";
  const names = [...new Set(state.events.map((e) => e.exchange).filter(Boolean))].sort();
  sel.replaceChildren();
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = "全部交易所";
  sel.appendChild(all);
  for (const name of names) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
  sel.value = names.includes(current) || current === "all" ? current : "all";
  state.exchange = sel.value;
}

async function load() {
  try {
    const r = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    state.meta = data.meta || {};
    state.events = Array.isArray(data.events) ? data.events : [];
    populateExchanges();
    renderStats();
    render();
  } catch (err) {
    $("#updated").textContent = `加载失败：${err.message}`;
    $("#live-dot").className = "dot err";
  }
}

function bind() {
  $("#type-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    state.type = btn.dataset.type || "all";
    render();
  });
  $("#exchange-filter").addEventListener("change", (e) => {
    state.exchange = e.target.value;
    render();
  });
  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value;
    render();
  });
  $("#show-original").addEventListener("change", (e) => {
    state.showOriginal = e.target.checked;
    render();
  });
}

bind();
load();
setInterval(load, REFRESH_MS);
