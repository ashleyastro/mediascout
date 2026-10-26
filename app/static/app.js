// MediaScout front end. All scraped values are untrusted: always pass them through esc()/safeUrl().
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const state = { currentScan: null, journalists: [], pollTimer: null, demo: false };
const MAX_PREFILL_URL = 7000;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : "";
  } catch { return ""; }
}
function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, ms);
}
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const text = await res.text();
  const body = text ? (() => { try { return JSON.parse(text); } catch { return text; } })() : null;
  if (!res.ok) {
    let detail = body && body.detail !== undefined ? body.detail : body;
    if (Array.isArray(detail)) detail = detail.map(d => d.msg || JSON.stringify(d)).join("; ");
    else if (detail && typeof detail === "object") detail = detail.message || JSON.stringify(detail);
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return body;
}
const splitList = s => s.split(",").map(x => x.trim()).filter(Boolean);
const fmtDate = iso => (iso ? iso.slice(0, 10) : "");

// ------------------------------------------------------------ tabs
function show(view) {
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === view));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "scans") loadScans();
  if (view === "outlets") { loadOutlets(); loadSuppressions(); }
}
$$(".tab").forEach(t => t.addEventListener("click", () => show(t.dataset.view)));

// ------------------------------------------------------------ new scan
async function loadIndustries() {
  const list = await api("/api/industries");
  $("#industry-list").innerHTML = list.map(i => `
    <label class="chip" title="${esc(i.terms.join(", "))}">
      <input type="checkbox" name="industries" value="${esc(i.id)}"><span>${esc(i.label)}</span>
    </label>`).join("");
}

$("#scan-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  const data = new FormData(f);
  const payload = {
    industries: data.getAll("industries"),
    custom_terms: splitList(data.get("custom_terms") || ""),
    executives: splitList(data.get("executives") || ""),
    match_mode: data.get("match_mode"),
    min_articles: Number(data.get("min_articles")),
    lookback_value: data.get("lookback_mode") === "limit" ? Number(data.get("lookback_value")) : null,
    lookback_unit: data.get("lookback_unit"),
    max_outlets: Number(data.get("max_outlets")),
    regions: data.getAll("regions"),
  };
  $("#form-error").textContent = "";
  const btn = f.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    const { id } = await api("/api/scans", { method: "POST", body: JSON.stringify(payload) });
    toast("Scan started. Results fill in as outlets are processed.");
    show("scans");
    openScan(id);
  } catch (err) {
    $("#form-error").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("#lookback-mode").addEventListener("change", e => {
  const limited = e.target.value === "limit";
  $$("[name=lookback_value], [name=lookback_unit]").forEach(el => { el.hidden = !limited; });
});

// ------------------------------------------------------------ scans
function criteria(p) {
  const parts = [];
  if (p.industries?.length) parts.push(p.industries.join(", "));
  if (p.custom_terms?.length) parts.push(p.custom_terms.join(", "));
  if (p.executives?.length) parts.push(p.executives.join(", "));
  const window = p.lookback_value ? `last ${p.lookback_value} ${p.lookback_unit}` : "all time";
  return `${parts.join(p.match_mode === "all" ? " AND " : " OR ")} | ${p.min_articles}+ stories, ${window} | ${p.regions.join("/")}`;
}
function progressCell(s) {
  const pct = s.outlets_total ? Math.round((100 * s.outlets_done) / s.outlets_total) : 0;
  return `${esc(s.stage || "")} ${s.outlets_done}/${s.outlets_total} outlets
    <div class="bar"><div style="width:${pct}%"></div></div>
    <span class="muted small">${s.articles_matched} matching of ${s.articles_checked} checked</span>`;
}
async function loadScans() {
  const scans = await api("/api/scans");
  $("#scans-table tbody").innerHTML = scans.map(s => `
    <tr class="clickable ${state.currentScan === s.id ? "selected" : ""}" data-id="${esc(s.id)}">
      <td>${esc(s.created_at.replace("T", " ").slice(0, 16))}</td>
      <td>${esc(criteria(s.params))}</td>
      <td><span class="status ${esc(s.status)}">${esc(s.status)}</span>${s.error ? `<div class="error small">${esc(s.error)}</div>` : ""}</td>
      <td>${progressCell(s)}</td>
      <td>${s.status === "running" || s.status === "queued" ? `<button class="btn small" data-cancel="${esc(s.id)}">Cancel</button>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">No scans yet.</td></tr>`;
  $$("#scans-table tr[data-id]").forEach(tr => tr.addEventListener("click", ev => {
    if (ev.target.dataset.cancel) return;
    openScan(tr.dataset.id);
  }));
  $$("#scans-table [data-cancel]").forEach(b => b.addEventListener("click", async () => {
    await api(`/api/scans/${b.dataset.cancel}/cancel`, { method: "POST" });
    toast("Cancelling scan");
    setTimeout(loadScans, 800);
  }));
}

async function openScan(id) {
  state.currentScan = id;
  clearTimeout(state.pollTimer);
  $("#results").hidden = false;
  for (const fmt of ["csv", "json", "md"]) $(`#dl-${fmt}`).href = `/api/scans/${id}/export?format=${fmt}`;
  await refreshResults();
}

async function refreshResults() {
  const id = state.currentScan;
  if (!id) return;
  const data = await api(`/api/scans/${id}/journalists`);
  if (id !== state.currentScan) return;
  state.journalists = data.journalists;
  $("#results-title").textContent = `${data.journalists.length} journalists`;
  $("#results-summary").textContent = data.summary + (data.scan.status !== "done" ? ` (scan ${data.scan.status})` : "");
  renderJournalists();
  loadScans();
  if (data.scan.status === "running" || data.scan.status === "queued") {
    state.pollTimer = setTimeout(refreshResults, 4000);
  }
}

function contactCell(c) {
  const links = [];
  if (c.email) {
    links.push(`<a href="mailto:${esc(c.email)}">${esc(c.email)}</a>` +
      (c.email_confidence ? `<span class="badge" title="${esc(c.email_source || "")}">${esc(c.email_confidence)}</span>` : ""));
  }
  for (const [key, label] of [["twitter", "X"], ["linkedin", "LinkedIn"], ["bluesky", "Bluesky"], ["threads", "Threads"],
                              ["mastodon", "Mastodon"], ["muckrack", "Muck Rack"], ["profile_url", "Profile"]]) {
    const u = safeUrl(c[key]);
    if (u) links.push(`<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${label}</a>`);
  }
  return links.join(" ") || `<span class="muted">none found</span>`;
}

function renderJournalists() {
  const q = $("#filter").value.trim().toLowerCase();
  const rows = state.journalists.filter(j =>
    !q || [j.name, ...j.outlets, ...j.topics].join(" ").toLowerCase().includes(q));
  $("#results-empty").hidden = rows.length > 0;
  $("#journalists-table tbody").innerHTML = rows.map(j => `
    <tr>
      <td><b>${esc(j.name)}</b><div class="muted small">${esc(j.topics.join(", "))}</div></td>
      <td>${j.article_count}<div class="muted small">latest ${esc(fmtDate(j.latest_published))}</div></td>
      <td>${esc(j.outlets.join(", "))}</td>
      <td class="contact">${contactCell(j.contact)}</td>
      <td><ul class="stories">${j.articles.slice(0, 3).map(a => {
        const u = safeUrl(a.url);
        return `<li><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a> <span class="muted small">${esc(fmtDate(a.published))}</span></li>`;
      }).join("")}</ul></td>
      <td>${state.demo ? "" : `<button class="btn small" data-optout="${esc(j.contact.email || j.name)}" title="Exclude from all lists">Opt out</button>`}</td>
    </tr>`).join("");
  $$("[data-optout]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm(`Exclude ${b.dataset.optout} from all future results and exports?`)) return;
    await api("/api/suppressions", { method: "POST", body: JSON.stringify({ value: b.dataset.optout }) });
    toast("Added to opt-out list");
    refreshResults();
  }));
}
$("#filter").addEventListener("input", renderJournalists);

// ------------------------------------------------------------ AI hand-off
async function openInAssistant(base) {
  const id = state.currentScan;
  const res = await fetch(`/api/scans/${id}/export?format=prompt`);
  const prompt = await res.text();
  const url = base + encodeURIComponent(prompt);
  if (url.length <= MAX_PREFILL_URL) {
    window.open(url, "_blank", "noopener");
    return;
  }
  // Too long for a URL: copy to the clipboard and open a blank chat.
  try {
    await navigator.clipboard.writeText(prompt);
    toast("Media list copied. Paste it into the new chat.", 6000);
  } catch {
    toast("Clipboard unavailable. Use the Markdown download instead.", 6000);
  }
  window.open(base.replace(/[?&]q=$/, ""), "_blank", "noopener");
}
$("#btn-claude").addEventListener("click", () => openInAssistant("https://claude.ai/new?q="));
$("#btn-chatgpt").addEventListener("click", () => openInAssistant("https://chatgpt.com/?q="));

// ------------------------------------------------------------ email
$("#btn-email").addEventListener("click", () => {
  $("#email-status").textContent = "";
  $("#email-dialog").showModal();
});
$("#email-form").addEventListener("submit", async e => {
  if (e.submitter?.value !== "send") return;
  e.preventDefault();
  const data = new FormData(e.target);
  $("#email-status").textContent = "Sending...";
  try {
    const r = await api(`/api/scans/${state.currentScan}/email`, {
      method: "POST",
      body: JSON.stringify({ to: splitList(data.get("to")), note: data.get("note") || null }),
    });
    $("#email-dialog").close();
    toast(`Sent ${r.journalists} journalists to ${r.to.join(", ")}`);
  } catch (err) {
    $("#email-status").textContent = err.message;
  }
});

// ------------------------------------------------------------ outlets & opt-outs
async function loadOutlets() {
  const { counts, outlets } = await api("/api/outlets");
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  $("#outlet-counts").textContent = `${total} active outlets: ` +
    Object.entries(counts).map(([r, n]) => `${r} ${n}`).join(", ");
  $("#outlets-table tbody").innerHTML = outlets.map(o => `
    <tr><td>${esc(o.name)}</td><td>${esc(o.domain)}</td><td>${esc(o.region)}</td><td>${esc(o.country || "")}</td><td>${esc(o.category || "")}</td></tr>`).join("");
}
$("#import-form").addEventListener("submit", async e => {
  e.preventDefault();
  const res = await fetch("/api/outlets/import", { method: "POST", body: new FormData(e.target) });
  const body = await res.json();
  $("#import-result").textContent = res.ok
    ? `Imported ${body.imported} outlets${body.errors.length ? `, ${body.errors.length} rows skipped` : ""}`
    : (body.detail?.message || "Import failed");
  loadOutlets();
});
async function loadSuppressions() {
  const list = await api("/api/suppressions");
  $("#suppress-list").innerHTML = list.map(v =>
    `<li>${esc(v)} <button class="btn small" data-unsuppress="${esc(v)}">Remove</button></li>`).join("")
    || `<li class="muted">None</li>`;
  $$("[data-unsuppress]").forEach(b => b.addEventListener("click", async () => {
    await api(`/api/suppressions?value=${encodeURIComponent(b.dataset.unsuppress)}`, { method: "DELETE" });
    loadSuppressions();
  }));
}
$("#suppress-form").addEventListener("submit", async e => {
  e.preventDefault();
  await api("/api/suppressions", { method: "POST", body: JSON.stringify({ value: e.target.elements.value.value }) });
  e.target.reset();
  loadSuppressions();
});

// ------------------------------------------------------------ connect
async function loadConfig() {
  const cfg = await api("/api/config");
  $("#mcp-url").value = cfg.mcp_url;
  if (!cfg.email_enabled) $("#btn-email").title = "Configure SMTP_HOST and SMTP_FROM to enable email";
  if (cfg.demo_mode) {
    state.demo = true;
    $("#demo-banner").hidden = false;
    $("#btn-email").hidden = true;
    $("#import-form").hidden = true;
    $("#suppress-form").hidden = true;
    $("[name=executives]").placeholder = `e.g. ${cfg.demo_executives.join(", ")}`;
    $("#demo-sample").addEventListener("click", () => { show("scans"); openScan(cfg.sample_scan_id); });
  }
}
$("#copy-mcp").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#mcp-url").value);
  toast("Connector URL copied");
});

loadIndustries().catch(err => toast(err.message));
loadConfig().catch(() => {});
