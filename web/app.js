"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const FIELD_RE = /\{\{\s*(\w+)\s*\}\}/g;

const state = {
  status: null,
  recipients: { columns: [], rows: [], counts: {} },
  previewIndex: 0,
  claudePreview: null,   // {index, body} when a Claude version is showing
  polling: null,
};

// ---------------------------------------------------------------- draft storage

const DRAFT_KEY = "mailroom-draft";
function saveDraft() {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      campaign: $("#campaign").value, subject: $("#subject").value, body: $("#body").value,
      personalize: $("#personalize").checked, effort: $("#effort").value,
    }));
  } catch { /* storage unavailable: drafts just won't persist */ }
}
function loadDraft() {
  try {
    const d = JSON.parse(localStorage.getItem(DRAFT_KEY) || "{}");
    if (d.campaign) $("#campaign").value = d.campaign;
    if (d.subject) $("#subject").value = d.subject;
    if (d.body) $("#body").value = d.body;
    $("#personalize").checked = !!d.personalize;
    if (d.effort) $("#effort").value = d.effort;
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------- API

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: `Server returned ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `Server returned ${res.status}`);
  return data;
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), isError ? 6000 : 3000);
}

function confirmDialog(title, body, okLabel) {
  const dlg = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-body").textContent = body;
  $("#confirm-ok").textContent = okLabel;
  dlg.showModal();
  return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}

const campaign = () => $("#campaign").value.trim() || "campaign";
const readyRows = () => state.recipients.rows.filter((r) => r.status === "ready");

// ---------------------------------------------------------------- routing

const VIEWS = ["recipients", "message", "send", "history", "unsubscribes", "settings"];
function route() {
  const view = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "recipients";
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== view;
  document.querySelectorAll(".rail a").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (view === "message") renderPreview();
  if (view === "send") renderSendSummary();
  if (view === "history") loadHistory();
  if (view === "unsubscribes") loadSuppression();
  if (view === "settings") loadSettings();
  $("#main").focus?.();
}

// ---------------------------------------------------------------- status

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch (e) {
    $("#conn").innerHTML = `<span>Can't reach the server. Is server.py running?</span>`;
    return;
  }
  const s = state.status;
  $("#conn").innerHTML =
    `<span class="${s.smtp_ready ? "on" : ""}">${s.smtp_ready ? "Email server set" : '<a href="#settings">Set up email server</a>'}</span>` +
    `<span class="${s.claude_ready ? "on" : ""}">${s.claude_ready ? "Claude connected" : "Claude off"}</span>`;
  if (!$("#rate").value) $("#rate").value = s.rate;
}

// ---------------------------------------------------------------- recipients

const SAMPLE = `email,name,company,notes
alice@example.com,Alice,Acme Corp,Asked about pricing at the March webinar
bob@example.com,Bob,Globex,Long-time customer since 2021
not-an-email,Carol,Initech,
alice@example.com,Alice,Acme Corp,duplicate row`;

async function uploadCsv(text) {
  try {
    state.recipients = await api("/api/recipients", { csv: text, campaign: campaign() });
    state.previewIndex = 0;
    state.claudePreview = null;
    renderRecipients();
    toast(`Loaded ${state.recipients.rows.length} rows`);
  } catch (e) {
    toast(e.message, true);
  }
}

async function refreshRecipients() {
  try {
    state.recipients = await api(`/api/recipients?campaign=${encodeURIComponent(campaign())}`);
    renderRecipients();
  } catch { /* keep what we have */ }
}

function renderRecipients() {
  const { columns, rows, counts } = state.recipients;
  $("#recipients-result").hidden = !rows.length;
  const ready = counts.ready || 0;
  const skipped = rows.length - ready;
  $("#nav-recipients").textContent = rows.length ? `${ready} ready${skipped ? `, ${skipped} skipped` : ""}` : "None yet";
  document.querySelector('[data-view="recipients"]').parentElement.classList.toggle("done", ready > 0);

  const order = ["ready", "already sent", "unsubscribed", "duplicate", "invalid"];
  const words = { ready: "ready to send", "already sent": "already sent in this campaign", unsubscribed: "unsubscribed", duplicate: "duplicates", invalid: "invalid addresses" };
  $("#counts").innerHTML = order.filter((k) => counts[k]).map((k) =>
    `<span class="tag ${k.replace(" ", "-")}"><strong>${counts[k]}</strong>${words[k]}</span>`).join("");

  const cols = ["email", ...columns.filter((c) => c !== "email")];
  $("#recipients-table").innerHTML =
    `<thead><tr><th scope="col">Status</th>${cols.map((c) => `<th scope="col">${esc(c)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.map((r) => `<tr class="${r.status === "ready" ? "" : "skip"}"><td><span class="tag ${r.status.replace(" ", "-")}">${esc(r.status)}</span></td>${cols.map((c) => `<td title="${esc(r.row[c])}">${esc(r.row[c])}</td>`).join("")}</tr>`).join("")}</tbody>`;

  $("#field-chips").innerHTML = columns.map((c) => `<button type="button" class="chip" data-field="${esc(c)}">${esc(c)}</button>`).join("");
  $("#fields-hint").textContent = columns.length ? "Insert a field:" : "Add recipients to see the fields you can use.";
  renderPreview();
}

function setupDrop() {
  const drop = $("#drop"), file = $("#file");
  const read = (f) => f && f.text().then(uploadCsv);
  drop.addEventListener("click", () => file.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
  file.addEventListener("change", () => { read(file.files[0]); file.value = ""; });
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); read(e.dataTransfer.files[0]); });
  $("#csv-load").addEventListener("click", () => uploadCsv($("#csv-text").value));
  $("#csv-sample").addEventListener("click", () => { $("#csv-text").value = SAMPLE; });
}

// ---------------------------------------------------------------- message & preview

function fill(template, row, highlight) {
  return template.split(FIELD_RE).map((part, i) => {
    if (i % 2 === 0) return highlight ? esc(part) : part;
    const v = row[part.toLowerCase()] ?? "";
    if (!highlight) return v;
    return v ? esc(v) : `<span class="missing" title="This recipient has no value for ${esc(part)}">${esc(part)}</span>`;
  }).join("");
}

function footer() {
  const u = state.status?.unsubscribe;
  if (!u) return "";
  const target = /^(https?:|mailto:)/.test(u) ? u : `mailto:${u}`;
  return `\n\n--\nTo stop receiving these emails: ${target.replace(/^mailto:/, "")}`;
}

function renderPreview() {
  const rows = state.recipients.rows;
  const i = Math.min(state.previewIndex, Math.max(rows.length - 1, 0));
  const entry = rows[i];
  const row = entry?.row || {};
  const subject = $("#subject").value, body = $("#body").value;

  $("#nav-message").textContent = subject.trim() && body.trim() ? subject.slice(0, 28) + (subject.length > 28 ? "…" : "") : "Not written";
  document.querySelector('[data-view="message"]').parentElement.classList.toggle("done", !!(subject.trim() && body.trim()));

  $("#prev").disabled = i <= 0;
  $("#next").disabled = i >= rows.length - 1;
  $("#preview-who").textContent = rows.length ? `${i + 1} of ${rows.length}: ${row.email || "(no email)"}` : "Add recipients to preview";

  const s = state.status || {};
  $("#pv-from").textContent = s.sender ? (s.sender_name ? `${s.sender_name} <${s.sender}>` : s.sender) : "Not set yet (see Settings)";
  $("#pv-to").textContent = row.email || "";
  $("#pv-subject").innerHTML = fill(subject, row, true) || '<span class="note">No subject</span>';

  const showingClaude = state.claudePreview && state.claudePreview.index === i;
  if (showingClaude) {
    $("#pv-body").textContent = state.claudePreview.body;
    $("#pv-note").textContent = "Claude's version for this person. Each send writes a fresh version, so the wording can differ slightly.";
  } else {
    $("#pv-body").innerHTML = (fill(body, row, true) || '<span class="note">Your message appears here.</span>') + esc(footer());
    $("#pv-note").textContent = $("#personalize").checked
      ? "This is your base message. Claude rewrites it for each person when you send."
      : "Fields fill in from the recipient's row as you type.";
  }
  $("#pv-claude").hidden = !($("#personalize").checked && rows.length);
  $("#pv-claude").textContent = showingClaude ? "Show my original" : "Show Claude's version for this person";
  $("#effort-wrap").hidden = !$("#personalize").checked;
}

async function previewWithClaude() {
  const i = state.previewIndex;
  if (state.claudePreview?.index === i) { state.claudePreview = null; renderPreview(); return; }
  if (!state.status?.claude_ready) { toast("Add your Anthropic API key in Settings first.", true); return; }
  const btn = $("#pv-claude");
  btn.disabled = true; btn.textContent = "Claude is writing…";
  $("#letter").classList.add("loading");
  try {
    const res = await api("/api/preview", { subject: $("#subject").value, body: $("#body").value, index: i, personalize: true, effort: $("#effort").value });
    state.claudePreview = { index: i, body: res.body };
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    $("#letter").classList.remove("loading");
    renderPreview();
  }
}

function insertField(name) {
  // Insert into whichever of subject/body was focused last.
  const el = insertField.target || $("#body");
  const token = `{{${name}}}`;
  const { selectionStart: a = el.value.length, selectionEnd: b = el.value.length } = el;
  el.setRangeText(token, a, b, "end");
  el.focus();
  el.dispatchEvent(new Event("input"));
}

// ---------------------------------------------------------------- send

function renderSendSummary() {
  const ready = readyRows().length;
  const limit = parseInt($("#limit").value, 10) || ready;
  const count = Math.min(limit, ready);
  const rate = parseFloat($("#rate").value) || state.status?.rate || 20;
  const minutes = count / rate;
  const s = state.status || {};
  const problems = [];
  if (!state.recipients.rows.length) problems.push(`No recipients yet. <a href="#recipients">Add them</a>.`);
  if (!$("#subject").value.trim() || !$("#body").value.trim()) problems.push(`The message isn't written yet. <a href="#message">Write it</a>.`);
  if (!s.smtp_ready) problems.push(`Your email server isn't set up, so you can preview but not send. <a href="#settings">Add it in Settings</a>.`);
  if ($("#personalize").checked && !s.claude_ready) problems.push(`Claude personalization is on but there's no API key. <a href="#settings">Add it</a> or turn personalization off.`);
  const testTo = $("#test-to").value.trim();

  $("#send-summary").innerHTML = `
    <p class="big">${count}</p>
    <p class="note count-label">${testTo ? `test emails, all going to ${esc(testTo)}` : count === 1 ? "email ready to send" : "emails ready to send"}</p>
    <dl>
      <dt>Campaign</dt><dd>${esc(campaign())}</dd>
      <dt>From</dt><dd>${s.sender ? esc(s.sender) : '<span class="warn">Not set</span>'}</dd>
      <dt>Subject</dt><dd>${esc($("#subject").value) || '<span class="warn">Empty</span>'}</dd>
      <dt>Personalized</dt><dd>${$("#personalize").checked ? `Yes, by Claude (${count} requests)` : "No, same text with fields filled in"}</dd>
      <dt>Takes about</dt><dd>${count ? (minutes < 1 ? "under a minute" : `${Math.ceil(minutes)} minutes at ${rate} per minute`) : "n/a"}</dd>
      <dt>Unsubscribe</dt><dd>${s.unsubscribe ? esc(s.unsubscribe) : '<span class="warn">No link set. Add one in Settings so people can opt out.</span>'}</dd>
    </dl>
    ${problems.map((p) => `<p class="problem">${p}</p>`).join("")}`;

  const canRun = count > 0 && $("#subject").value.trim() && $("#body").value.trim() && !($("#personalize").checked && !s.claude_ready);
  const busy = state.job?.status === "running";
  $("#dry-run").disabled = !canRun || busy;
  $("#send-btn").disabled = !canRun || !s.smtp_ready || busy;
  $("#send-btn").textContent = testTo ? `Send ${count} test${count === 1 ? "" : "s"} to me` : `Send ${count} email${count === 1 ? "" : "s"}`;
}

async function startRun(live) {
  const testTo = $("#test-to").value.trim();
  const count = Math.min(parseInt($("#limit").value, 10) || Infinity, readyRows().length);
  if (live) {
    const title = testTo ? `Send ${count} test emails to ${testTo}?` : `Send ${count} emails now?`;
    const body = testTo
      ? "Every message goes to your test address. Nobody on the list is contacted or marked as sent."
      : `This emails ${count} real people in the “${campaign()}” campaign. Anyone already sent in this campaign is skipped.`;
    if (!(await confirmDialog(title, body, testTo ? "Send tests" : `Send ${count} emails`))) return;
  }
  try {
    state.job = await api("/api/send", {
      send: live, campaign: campaign(), subject: $("#subject").value, body: $("#body").value,
      personalize: $("#personalize").checked, effort: $("#effort").value,
      test_to: testTo, limit: parseInt($("#limit").value, 10) || null,
      rate: parseFloat($("#rate").value) || null,
    });
    renderJob();
    pollJob();
  } catch (e) {
    toast(e.message, true);
  }
}

function pollJob() {
  clearInterval(state.polling);
  state.polling = setInterval(async () => {
    try { state.job = await api("/api/job"); } catch { return; }
    renderJob();
    if (state.job.status !== "running") {
      clearInterval(state.polling);
      refreshRecipients();
      renderSendSummary();
    }
  }, 1000);
}

function renderJob() {
  const j = state.job;
  if (!j || j.status === "idle") return;
  $("#progress").hidden = false;
  const processed = j.done + j.failed;
  const pct = j.total ? Math.round((processed / j.total) * 100) : 100;
  $("#bar").firstElementChild.style.width = pct + "%";
  $("#bar").setAttribute("aria-valuenow", pct);
  const verb = j.live ? "Sent" : "Previewed";
  const titles = {
    running: j.live ? `Sending ${processed} of ${j.total}` : `Preparing ${processed} of ${j.total}`,
    finished: `${verb} ${j.done} of ${j.total}`,
    stopped: `Stopped after ${processed} of ${j.total}`,
    error: "The run stopped with an error",
  };
  $("#progress-title").textContent = titles[j.status] || "";
  $("#progress-sub").textContent =
    j.status === "error" ? j.error :
    j.failed ? `${j.failed} failed. Open a row to see why.` :
    j.status === "stopped" ? "Start again to carry on. People already emailed are skipped." :
    j.live && j.test_to ? `Test run: everything went to ${j.test_to}.` :
    !j.live ? "Nothing was sent. Open a row to read the email." : "";
  $("#stop-btn").hidden = j.status !== "running";
  $("#nav-send").textContent = j.live && !j.test_to ? `${verb} ${j.done}${j.status === "running" ? "…" : ""}` : $("#nav-send").textContent;
  document.querySelector('[data-view="send"]').parentElement.classList.toggle("done", j.live && !j.test_to && j.status === "finished");

  $("#feed").innerHTML = j.items.slice().reverse().map((it) => `
    <li><details>
      <summary><span class="who">${esc(it.email)}${it.to !== it.email ? ` <span class="note">(sent to ${esc(it.to)})</span>` : ""}</span><span class="tag ${it.status}">${esc(it.status)}</span></summary>
      ${it.body ? `<p class="note">${esc(it.subject)}</p><div class="msg">${esc(it.body)}</div>` : ""}
      ${it.detail ? `<p class="err">${esc(it.detail)}</p>` : ""}
    </details></li>`).join("");
}

// ---------------------------------------------------------------- history / suppression / settings

async function loadHistory() {
  try {
    const rows = await api("/api/history");
    $("#history-table").innerHTML = rows.length
      ? `<thead><tr><th scope="col">When</th><th scope="col">Campaign</th><th scope="col">Email</th><th scope="col">Result</th><th scope="col">Detail</th></tr></thead><tbody>` +
        rows.map((r) => `<tr><td>${esc(r.timestamp.replace("T", " "))}</td><td>${esc(r.campaign)}</td><td>${esc(r.email)}</td><td><span class="tag ${esc(r.status)}">${esc(r.status)}</span></td><td title="${esc(r.detail)}">${esc(r.detail)}</td></tr>`).join("") + "</tbody>"
      : `<tbody><tr><td class="empty">Nothing sent yet. Real sends show up here.</td></tr></tbody>`;
  } catch (e) { toast(e.message, true); }
}

async function loadSuppression() {
  try { $("#suppression").value = (await api("/api/suppression")).join("\n"); }
  catch (e) { toast(e.message, true); }
}

async function saveSuppression() {
  try {
    const list = await api("/api/suppression", { emails: $("#suppression").value.split(/[\s,;]+/) });
    $("#suppression").value = list.join("\n");
    toast(`Saved ${list.length} address${list.length === 1 ? "" : "es"}`);
    refreshRecipients();
  } catch (e) { toast(e.message, true); }
}

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    const form = $("#settings-form");
    for (const [k, v] of Object.entries(s)) {
      const input = form.elements[k];
      if (!input) continue;
      if (input.type === "password") { input.value = ""; input.placeholder = v ? "Saved. Leave blank to keep it." : "Not set"; }
      else input.value = v;
    }
  } catch (e) { toast(e.message, true); }
}

async function saveSettings(e) {
  e.preventDefault();
  const data = Object.fromEntries(new FormData($("#settings-form")));
  try {
    await api("/api/settings", data);
    await refreshStatus();
    loadSettings();
    toast("Settings saved");
  } catch (err) { toast(err.message, true); }
}

// ---------------------------------------------------------------- wire up

function init() {
  loadDraft();
  setupDrop();

  for (const id of ["#subject", "#body"]) {
    $(id).addEventListener("focus", (e) => { insertField.target = e.target; });
    $(id).addEventListener("input", () => { state.claudePreview = null; renderPreview(); saveDraft(); });
  }
  $("#field-chips").addEventListener("mousedown", (e) => e.preventDefault()); // keep caret in the field
  $("#field-chips").addEventListener("click", (e) => { const b = e.target.closest(".chip"); if (b) insertField(b.dataset.field); });
  $("#personalize").addEventListener("change", () => { state.claudePreview = null; renderPreview(); renderSendSummary(); saveDraft(); });
  $("#effort").addEventListener("change", saveDraft);
  $("#prev").addEventListener("click", () => { state.previewIndex--; renderPreview(); });
  $("#next").addEventListener("click", () => { state.previewIndex++; renderPreview(); });
  $("#pv-claude").addEventListener("click", previewWithClaude);

  let campaignTimer;
  $("#campaign").addEventListener("input", () => {
    saveDraft();
    clearTimeout(campaignTimer);
    campaignTimer = setTimeout(() => { refreshRecipients().then(renderSendSummary); }, 400);
  });
  for (const id of ["#test-to", "#limit", "#rate"]) $(id).addEventListener("input", renderSendSummary);
  $("#dry-run").addEventListener("click", () => startRun(false));
  $("#send-btn").addEventListener("click", () => startRun(true));
  $("#stop-btn").addEventListener("click", () => api("/api/job/stop", {}).catch((e) => toast(e.message, true)));
  $("#save-suppression").addEventListener("click", saveSuppression);
  $("#settings-form").addEventListener("submit", saveSettings);

  window.addEventListener("hashchange", route);
  refreshStatus().then(async () => {
    await refreshRecipients();
    // Pick up a run that was already going (e.g. after a page reload).
    try {
      state.job = await api("/api/job");
      if (state.job.status === "running") pollJob();
      renderJob();
    } catch { /* ignore */ }
    route();
  });
  route();
}

init();
