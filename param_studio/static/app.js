/* Param Studio — client logic (vanilla JS, no framework, no build step). */
"use strict";

// ── State ───────────────────────────────────────────────────────────────────
let currentNode = null;
let params = [];                 // [{name,type,value,read_only,description,range,group}]
let groupOrder = [];             // display order of group labels from backend
const rows = new Map();          // name -> {el, p, chip, setValue}
const pending = {};              // name -> {timer, inflight, value, requeue}
let liveTuning = true;           // value of live_tuning_enabled (if present)
let hasLiveTuning = false;
let healthTimer = null;
const COLLAPSE_KEY = "ps_collapsed";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// ── Tiny fetch wrapper ────────────────────────────────────────────────────────
async function api(url, method = "GET", body = null) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  let res, data;
  try {
    res = await fetch(url, opt);
    data = await res.json().catch(() => ({}));
  } catch (e) {
    return { error: "network: " + e.message };
  }
  if (!res.ok) data.error = data.error || ("HTTP " + res.status);
  return data;
}

// ── Formatting helpers ────────────────────────────────────────────────────────
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function decimalsForStep(step, type) {
  if (type === "integer") return 0;
  if (!step || step <= 0) return 3;
  const s = step.toString();
  if (s.includes("e-")) return Math.min(6, parseInt(s.split("e-")[1], 10));
  const dot = s.indexOf(".");
  return dot < 0 ? 0 : Math.min(6, s.length - dot - 1);
}
function fmt(v, type, dec) {
  if (v === null || v === undefined) return "—";
  if (type === "double") return Number(v).toFixed(dec);
  if (type === "integer") return String(Math.round(v));
  return String(v);
}

// ── Toasts ────────────────────────────────────────────────────────────────────
function toast(kind, title, body, ms = 4200) {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = `<div class="t-title">${esc(title)}</div>` + (body ? `<div class="t-body">${body}</div>` : "");
  $("#toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(() => t.remove(), 300); }, ms);
}

// ── Node discovery ──────────────────────────────────────────────────────────
async function loadNodes() {
  const r = await api("/api/nodes");
  const sel = $("#node-select");
  if (r.error || !r.nodes || !r.nodes.length) {
    sel.innerHTML = `<option value="">no param nodes found</option>`;
    setHealth(false, null, "no nodes");
    return;
  }
  const want = currentNode || localStorage.getItem("ps_last_node");
  sel.innerHTML = r.nodes.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  currentNode = r.nodes.includes(want) ? want : r.nodes[0];
  sel.value = currentNode;
  await loadParams();
}

// ── Param load + render ───────────────────────────────────────────────────────
async function loadParams() {
  if (!currentNode) return;
  localStorage.setItem("ps_last_node", currentNode);
  $("#params").innerHTML = `<div class="placeholder"><div class="spinner"></div>Reading parameters from <span class="mono">${esc(currentNode)}</span>…</div>`;
  const r = await api("/api/params?node=" + encodeURIComponent(currentNode));
  if (r.error) {
    $("#params").innerHTML = `<div class="placeholder"><div class="big">Couldn't read ${esc(currentNode)}</div><div>${esc(r.error)}</div></div>`;
    setHealth(false, null, "unreachable");
    return;
  }
  params = r.params;
  groupOrder = r.groups_order || [];
  rows.clear();
  const lt = params.find(p => p.name === "live_tuning_enabled");
  hasLiveTuning = !!lt;
  liveTuning = lt ? !!lt.value : true;
  render();
  updateBanner();
  pingNow();
}

function collapsedSet() {
  // Default (first run, before any toggle): collapse only 'misc' — the
  // not-actively-tuned bucket. Once the user toggles anything we respect their
  // saved set verbatim.
  const raw = localStorage.getItem(COLLAPSE_KEY);
  if (raw === null) return new Set(["misc"]);
  try { return new Set(JSON.parse(raw)); }
  catch { return new Set(); }
}
function saveCollapsed(set) { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...set])); }

function render() {
  const host = $("#params");
  host.innerHTML = "";
  const collapsed = collapsedSet();

  // group → params
  const groups = new Map();
  for (const p of params) {
    if (!groups.has(p.group)) groups.set(p.group, []);
    groups.get(p.group).push(p);
  }
  // Display order from the backend (curated for MPPI, 'misc' last); append any
  // stragglers not in that list.
  const groupNames = [...groupOrder.filter(g => groups.has(g)),
                      ...[...groups.keys()].filter(g => !groupOrder.includes(g)).sort()];

  for (const g of groupNames) {
    const list = groups.get(g);
    const gEl = document.createElement("div");
    gEl.className = "group" + (collapsed.has(g) ? " collapsed" : "");
    gEl.dataset.group = g;
    gEl.innerHTML =
      `<div class="g-head">
         <svg class="caret" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M6 9l6 6 6-6"/></svg>
         <span class="g-name">${esc(g)}</span>
         <span class="g-tag">${list.length}</span>
       </div>
       <div class="rows"></div>`;
    const rowsEl = $(".rows", gEl);
    for (const p of list) rowsEl.appendChild(buildRow(p));
    $(".g-head", gEl).addEventListener("click", () => {
      gEl.classList.toggle("collapsed");
      const c = collapsedSet();
      gEl.classList.contains("collapsed") ? c.add(g) : c.delete(g);
      saveCollapsed(c);
    });
    host.appendChild(gEl);
  }
  applySearch();
}

// ── Row + widget builders ─────────────────────────────────────────────────────
function buildRow(p) {
  const row = document.createElement("div");
  row.className = "row" + (p.read_only ? " ro" : "");
  row.dataset.name = p.name;
  row.dataset.search = (p.name + " " + (p.description || "")).toLowerCase();

  const lock = p.read_only ? `<span class="lock" title="read-only">🔒</span>` : "";
  const name = document.createElement("div");
  name.className = "p-name";
  name.innerHTML = `<span class="nm">${esc(p.name)}${lock}</span>` +
                   (p.description ? `<span class="desc" title="${esc(p.description)}">${esc(p.description)}</span>` : "");

  const ctl = document.createElement("div");
  ctl.className = "ctl";

  const status = document.createElement("div");
  status.className = "status";
  const chip = document.createElement("span");
  chip.className = "chip " + (p.read_only ? "ro" : "idle");
  chip.textContent = p.read_only ? "read-only" : "—";
  status.appendChild(chip);

  let setValue = () => {};
  const dec = p.range ? decimalsForStep(p.range.step, p.type) : decimalsForStep(0, p.type);

  if (p.type === "bool") {
    const sw = document.createElement("label");
    sw.className = "switch";
    sw.innerHTML = `<input type="checkbox"><span class="track"></span><span class="knob"></span>`;
    const cb = $("input", sw);
    cb.checked = !!p.value;
    cb.addEventListener("change", () => scheduleSet(p, cb.checked));
    setValue = v => { cb.checked = !!v; };
    ctl.appendChild(sw);
    const lbl = document.createElement("span");
    lbl.className = "muted mono"; lbl.style.fontSize = "12px";
    lbl.textContent = p.read_only ? String(p.value) : "";
    ctl.appendChild(lbl);

  } else if ((p.type === "double" || p.type === "integer") && p.range) {
    const r = p.range;
    const sliderStep = r.step && r.step > 0 ? r.step : (p.type === "integer" ? 1 : (r.hi - r.lo) / 200 || 0.001);
    const sld = document.createElement("input");
    sld.type = "range"; sld.min = r.lo; sld.max = r.hi; sld.step = sliderStep; sld.value = clampNum(p.value, r);
    const num = document.createElement("input");
    num.className = "num"; num.type = "number"; num.step = sliderStep; num.min = r.lo; num.max = r.hi;
    num.value = fmt(p.value, p.type, dec);
    sld.addEventListener("input", () => { num.value = fmt(parseFloat(sld.value), p.type, dec); });
    sld.addEventListener("change", () => scheduleSet(p, cast(sld.value, p.type)));
    num.addEventListener("change", () => { sld.value = num.value; scheduleSet(p, cast(num.value, p.type)); });
    setValue = v => { sld.value = clampNum(v, r); num.value = fmt(v, p.type, dec); };
    ctl.appendChild(mkLim(fmt(r.lo, p.type, dec)));
    ctl.appendChild(sld);
    ctl.appendChild(mkLim(fmt(r.hi, p.type, dec), true));
    ctl.appendChild(num);

  } else if (p.type === "double" || p.type === "integer") {
    const num = document.createElement("input");
    num.className = "num txt"; num.type = "number"; num.value = fmt(p.value, p.type, dec);
    if (p.type === "integer") num.step = 1;
    num.addEventListener("change", () => scheduleSet(p, cast(num.value, p.type)));
    setValue = v => { num.value = fmt(v, p.type, dec); };
    ctl.appendChild(num);

  } else if (p.type === "string") {
    const txt = document.createElement("input");
    txt.className = "input txt"; txt.type = "text"; txt.value = p.value == null ? "" : p.value;
    txt.addEventListener("change", () => scheduleSet(p, txt.value));
    setValue = v => { txt.value = v == null ? "" : v; };
    ctl.appendChild(txt);

  } else {
    // arrays / not_set — show read-only
    const span = document.createElement("span");
    span.className = "muted mono"; span.style.fontSize = "12px";
    span.textContent = JSON.stringify(p.value);
    ctl.appendChild(span);
    chip.className = "chip ro"; chip.textContent = p.type;
  }

  row.appendChild(name);
  row.appendChild(ctl);
  row.appendChild(status);
  rows.set(p.name, { el: row, p, chip, setValue });
  return row;
}

function mkLim(text, hi) { const s = document.createElement("span"); s.className = "range-lim" + (hi ? " hi" : ""); s.textContent = text; return s; }
function clampNum(v, r) { v = Number(v); return Math.max(r.lo, Math.min(r.hi, isNaN(v) ? r.lo : v)); }
function cast(v, type) { return type === "integer" ? Math.round(Number(v)) : (type === "double" ? Number(v) : v); }

// ── Status chips ───────────────────────────────────────────────────────────
function setChip(name, cls, text, title = "") {
  const e = rows.get(name);
  if (!e) return;
  e.chip.className = "chip " + cls;
  e.chip.innerHTML = (cls === "idle" || cls === "ro") ? esc(text) : `<span class="c-dot"></span>${esc(text)}`;
  e.chip.title = title;
}

// ── The core: set, then trust the read-back ───────────────────────────────────
function scheduleSet(p, value) {
  setChip(p.name, "pend", "setting…");
  rows.get(p.name)?.el.classList.add("dirty");
  const st = pending[p.name] || (pending[p.name] = {});
  st.value = value;
  clearTimeout(st.timer);
  st.timer = setTimeout(() => flushSet(p), 150);
}

async function flushSet(p) {
  const st = pending[p.name];
  if (!st) return;
  if (st.inflight) { st.requeue = true; return; }
  st.inflight = true;
  const sent = st.value;
  const r = await api("/api/set", "POST", { node: currentNode, name: p.name, type: p.type, value: sent });
  st.inflight = false;
  applyVerdict(p, sent, r);
  if (st.requeue && st.value !== sent) { st.requeue = false; flushSet(p); }
  else st.requeue = false;
}

function applyVerdict(p, sent, r) {
  rows.get(p.name)?.el.classList.remove("dirty");
  if (!r || r.error) {
    setChip(p.name, "bad", "error", r && r.error);
    toast("bad", "Set failed", `<code>${esc(p.name)}</code> — ${esc(r && r.error || "unknown")}`);
    return;
  }
  if (r.timed_out) {
    setChip(p.name, "bad", "no response", "node didn't answer — check network");
    toast("bad", "No response", `<code>${esc(p.name)}</code> — node didn't answer (network?). Value may not be set; retry.`);
    return;
  }
  if (!r.set_ok) {
    setChip(p.name, "bad", "rejected", r.reason);
    toast("bad", "Rejected", `<code>${esc(p.name)}</code> — ${esc(r.reason || "set returned unsuccessful")}`);
    return;
  }
  // accepted — reflect the truth the node reported back
  p.value = r.readback;
  rows.get(p.name)?.setValue(r.readback);
  if (p.name === "live_tuning_enabled") { liveTuning = !!r.readback; updateBanner(); refreshAllChipsForLiveTuning(); }

  if (!r.applied) {
    setChip(p.name, "warn", "differs", `node stored ${r.readback} (clamped/coerced)`);
    toast("warn", "Value adjusted", `<code>${esc(p.name)}</code> → node holds <code>${esc(r.readback)}</code>`);
    return;
  }
  if (hasLiveTuning && !liveTuning && p.name !== "live_tuning_enabled") {
    setChip(p.name, "warn", "stored", `stored on node (${r.rtt_ms}ms); live tuning is OFF so the controller isn't using it yet`);
  } else {
    setChip(p.name, "ok", "✓ " + r.rtt_ms + "ms", "confirmed by read-back");
  }
}

// When live tuning flips, repaint confirmed chips to reflect stored-vs-applied.
function refreshAllChipsForLiveTuning() {
  for (const [name, e] of rows) {
    if (name === "live_tuning_enabled") continue;
    const cls = e.chip.className;
    if (cls.includes("ok") || cls.includes("warn")) {
      if (hasLiveTuning && !liveTuning) setChip(name, "warn", "stored", "live tuning is OFF — controller not using it yet");
      else if (cls.includes("warn") && e.chip.title.includes("live tuning")) setChip(name, "ok", "✓ applied", "confirmed");
    }
  }
}

function updateBanner() {
  $("#banner-live").classList.toggle("show", hasLiveTuning && !liveTuning);
}

// ── Search ────────────────────────────────────────────────────────────────────
function applySearch() {
  const q = $("#search").value.trim().toLowerCase();
  let shown = 0;
  for (const [, e] of rows) {
    const match = !q || e.el.dataset.search.includes(q);
    e.el.classList.toggle("hidden", !match);
    if (match) shown++;
  }
  // hide groups with no visible rows
  for (const g of $$(".group")) {
    const anyVisible = $$(".row", g).some(r => !r.classList.contains("hidden"));
    g.style.display = anyVisible ? "" : "none";
    if (q && anyVisible) g.classList.remove("collapsed");
  }
  $("#search-count").textContent = q ? `${shown}/${rows.size}` : `${rows.size}`;
}

// ── Health badge ────────────────────────────────────────────────────────────
function setHealth(up, rtt, text) {
  $("#health-dot").className = "dot " + (up ? "up" : "down");
  $("#health-text").textContent = text || (up ? "online" : "offline");
  $("#health-rtt").textContent = rtt != null ? rtt + "ms" : "";
}
async function pingNow() {
  if (!currentNode) { setHealth(false, null, "no node"); return; }
  const r = await api("/api/ping?node=" + encodeURIComponent(currentNode));
  if (r.error) { setHealth(false, null, "error"); return; }
  setHealth(r.reachable, r.reachable ? r.rtt_ms : null, r.reachable ? "online" : "no response");
}
function startHealth() { clearInterval(healthTimer); healthTimer = setInterval(pingNow, 2000); }

// ── Snapshot / Restore / Diff ─────────────────────────────────────────────────
async function doSnapshot() {
  if (!currentNode) return;
  const r = await api("/api/snapshot", "POST", { node: currentNode });
  if (r.error) toast("bad", "Snapshot failed", esc(r.error));
  else toast("ok", "Snapshot saved", `<code>${esc(r.file)}</code> — ${r.count} params`);
}

// Read a browsed file → {content, filename} (offline, client-side).
function readPickedFile(input) {
  return new Promise((resolve, reject) => {
    const f = input.files && input.files[0];
    if (!f) { resolve(null); return; }
    const fr = new FileReader();
    fr.onload = () => resolve({ content: fr.result, filename: f.name });
    fr.onerror = () => reject(fr.error);
    fr.readAsText(f);
  });
}

async function fileList(targetId, dirId, onPick) {
  const r = await api("/api/snapshots");
  if (dirId) $(dirId).textContent = r.dir || "";
  const host = $(targetId);
  if (r.error || !r.files || !r.files.length) {
    host.innerHTML = `<div class="muted">No profiles in this folder yet — use Browse… to open one from elsewhere.</div>`;
    return;
  }
  // Files not prefixed 'snapshot_' are the curated params_*.yaml race configs.
  host.innerHTML = r.files.map(f => {
    const cfg = !f.startsWith("snapshot_");
    return `<div class="file-item${cfg ? " cfg" : ""}" data-f="${esc(f)}"><span class="fn">${esc(f)}</span></div>`;
  }).join("");
  $$(".file-item", host).forEach(it => it.addEventListener("click", () => {
    $$(".file-item", host).forEach(x => x.classList.remove("sel"));
    it.classList.add("sel");
    onPick(it.dataset.f);
  }));
}

let restoreSource = null;   // {file} | {content, filename}
function openRestore() {
  restoreSource = null;
  $("#btn-do-restore").disabled = true;
  $("#restore-result").innerHTML = "";
  $("#restore-file-input").value = "";
  fileList("#restore-files", "#restore-dir", f => {
    restoreSource = { file: f };
    $("#btn-do-restore").disabled = false;
  });
  $("#modal-restore").classList.add("show");
}
async function restoreBrowse() {
  const picked = await readPickedFile($("#restore-file-input"));
  if (!picked) return;
  restoreSource = picked;
  $$("#restore-files .file-item").forEach(x => x.classList.remove("sel"));
  $("#btn-do-restore").disabled = false;
  $("#restore-result").innerHTML = `<div class="muted">selected <span class="mono">${esc(picked.filename)}</span></div>`;
}
async function doRestore() {
  if (!restoreSource) return;
  $("#btn-do-restore").disabled = true;
  $("#restore-result").innerHTML = `<div class="muted"><span class="spinner" style="width:16px;height:16px;display:inline-block;vertical-align:middle"></span> restoring & confirming each param…</div>`;
  const r = await api("/api/restore", "POST", { node: currentNode, ...restoreSource });
  if (r.error) { $("#restore-result").innerHTML = `<div class="muted">${esc(r.error)}</div>`; return; }
  const fails = r.results.filter(x => x.status === "failed");
  $("#restore-result").innerHTML =
    `<div><span class="muted">from</span> <span class="mono">${esc(r.profile || "")}</span></div>` +
    `<div style="margin-top:4px"><b style="color:var(--ok)">${r.applied} applied</b> · ` +
    `<b style="color:var(--bad)">${r.failed} failed</b> · ` +
    `<span class="muted">${r.skipped} skipped</span></div>` +
    (fails.length ? `<div style="margin-top:8px;max-height:200px;overflow:auto">` +
      fails.map(x => `<div class="mono" style="font-size:12px;color:var(--bad)">${esc(x.name)}: ${esc(x.reason || "failed")}</div>`).join("") + `</div>` : "");
  toast(r.failed ? "warn" : "ok", "Restore complete", `${r.applied} applied, ${r.failed} failed, ${r.skipped} skipped`);
  await loadParams();
}

let diffSource = null;      // {file} | {content, filename}
function openDiff() {
  diffSource = null;
  $("#diff-file-input").value = "";
  $("#diff-result").innerHTML = `<div class="muted">Pick a profile or Browse… to compare against the live node.</div>`;
  fileList("#diff-files", "#diff-dir", f => runDiff({ file: f }));
  $("#modal-diff").classList.add("show");
}
async function diffBrowse() {
  const picked = await readPickedFile($("#diff-file-input"));
  if (!picked) return;
  $$("#diff-files .file-item").forEach(x => x.classList.remove("sel"));
  runDiff(picked);
}
async function runDiff(source) {
  diffSource = source;
  $("#diff-result").innerHTML = `<div class="muted"><span class="spinner" style="width:16px;height:16px;display:inline-block;vertical-align:middle"></span> comparing…</div>`;
  const r = await api("/api/diff", "POST", { node: currentNode, ...source });
  if (r.error) { $("#diff-result").innerHTML = `<div class="muted">${esc(r.error)}</div>`; return; }
  const diffs = r.rows.filter(x => x.differ);
  let html = `<div style="margin-bottom:8px"><span class="muted">vs</span> <span class="mono">${esc(r.profile || "")}</span> — <b style="color:var(--accent)">${diffs.length}</b> of ${r.rows.length} differ</div>`;
  html += `<table class="diff"><thead><tr><th>param</th><th>saved</th><th>current</th><th></th></tr></thead><tbody>`;
  for (const x of r.rows) {
    const canRevert = x.differ && !x.read_only && !x.missing;
    html += `<tr class="${x.differ ? "differ" : "same"}">
      <td>${esc(x.name)}</td>
      <td><span class="v-saved">${esc(JSON.stringify(x.saved))}</span></td>
      <td><span class="v-cur">${x.missing ? "—" : esc(JSON.stringify(x.current))}</span></td>
      <td>${canRevert ? `<button class="revert" data-n="${esc(x.name)}" data-t="${esc(x.type)}" data-v='${esc(JSON.stringify(x.saved))}'>← saved</button>` : ""}</td>
    </tr>`;
  }
  html += `</tbody></table>`;
  $("#diff-result").innerHTML = html;
  $$("#diff-result .revert").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "…";
    const v = JSON.parse(b.dataset.v);
    const res = await api("/api/set", "POST", { node: currentNode, name: b.dataset.n, type: b.dataset.t, value: v });
    if (res.error || res.timed_out || !res.set_ok) toast("bad", "Revert failed", `<code>${esc(b.dataset.n)}</code>`);
    else { toast("ok", "Reverted", `<code>${esc(b.dataset.n)}</code> → <code>${esc(JSON.stringify(v))}</code>`); runDiff(diffSource); loadParams(); }
  }));
}

// ── Wiring ────────────────────────────────────────────────────────────────────
function closeModals() { $$(".modal-bg").forEach(m => m.classList.remove("show")); }

function init() {
  $("#node-select").addEventListener("change", e => { currentNode = e.target.value; loadParams(); });
  $("#btn-reload").addEventListener("click", loadParams);
  $("#search").addEventListener("input", applySearch);
  $("#btn-expand").addEventListener("click", () => {
    const groups = $$(".group");
    const anyOpen = groups.some(g => !g.classList.contains("collapsed"));
    const c = collapsedSet();
    groups.forEach(g => {
      g.classList.toggle("collapsed", anyOpen);
      anyOpen ? c.add(g.dataset.group) : c.delete(g.dataset.group);
    });
    saveCollapsed(c);
    $("#btn-expand").textContent = anyOpen ? "Expand all" : "Collapse all";
  });
  $("#btn-snapshot").addEventListener("click", doSnapshot);
  $("#btn-restore").addEventListener("click", openRestore);
  $("#btn-diff").addEventListener("click", openDiff);
  $("#btn-do-restore").addEventListener("click", doRestore);
  $("#restore-browse").addEventListener("click", () => $("#restore-file-input").click());
  $("#restore-file-input").addEventListener("change", restoreBrowse);
  $("#diff-browse").addEventListener("click", () => $("#diff-file-input").click());
  $("#diff-file-input").addEventListener("change", diffBrowse);
  $$("[data-close]").forEach(b => b.addEventListener("click", closeModals));
  $$(".modal-bg").forEach(m => m.addEventListener("click", e => { if (e.target === m) closeModals(); }));

  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { closeModals(); }
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); $("#search").focus(); }
    if (e.key === "r" && !typing && !e.ctrlKey && !e.metaKey) loadParams();
  });

  loadNodes().then(startHealth);
}
document.addEventListener("DOMContentLoaded", init);
