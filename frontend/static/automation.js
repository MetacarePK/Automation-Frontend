import { h, api, shell, runPanel, toast, clear } from "/static/app.js";

const slug = location.pathname.split("/")[2];
const { main } = await shell("home");

let automation;
try {
  automation = await api(`/api/automations/${encodeURIComponent(slug)}`);
} catch (err) {
  main.append(h("div", { class: "empty" }, h("strong", { text: "Automation not found" }), h("a", { href: "/" }, "Back to automations")));
  throw err;
}
document.title = `${automation.name} - Automation`;

main.append(h("div", { class: "page-head" }, h("div", {},
  h("a", { class: "crumb", href: "/" }, "← Automations"), h("h1", { text: automation.name }))));

const progressSlot = h("div");
const newRunHeading = h("h2", { class: "section-title", text: "New run", hidden: true });
const flow = h("section", { class: "panel" });
main.append(progressSlot, newRunHeading, flow);

let upload = null;
let panel = null;
const selects = {};

/* ---------- 1. choose a sheet ---------- */
const fileInput = h("input", { type: "file", accept: ".xlsx", id: "sheet-file", hidden: true });
const chooseBtn = h("button", { class: "secondary", onclick: () => fileInput.click() }, "Choose sheet");
const uploadError = h("div", { class: "form-error", role: "alert", hidden: true });
const sheetRow = h("div", { class: "drop" }, chooseBtn, fileInput);
const detail = h("div", { class: "flow" });
flow.append(h("div", { class: "panel-body flow" }, sheetRow, uploadError, detail));

fileInput.addEventListener("change", async () => {
  const file = fileInput.files[0];
  if (!file) return;
  uploadError.hidden = true;
  const form = new FormData();
  form.append("file", file);
  chooseBtn.disabled = true;
  chooseBtn.textContent = "Uploading...";
  try {
    upload = await api(`/api/automations/${slug}/upload`, { method: "POST", form });
    renderSheet();
  } catch (err) {
    uploadError.textContent = err.message;
    uploadError.hidden = false;
  } finally {
    chooseBtn.disabled = false;
    chooseBtn.textContent = upload ? "Change sheet" : "Choose sheet";
    fileInput.value = "";
  }
});

/* ---------- 2. columns + options + start ---------- */
function fieldRow(key, label, required, headers, suggested) {
  const select = h("select", { id: "map-" + key },
    h("option", { value: "" }, "Not in sheet"), ...headers.map(header => h("option", { value: header }, header)));
  select.value = suggested[key] && headers.includes(suggested[key]) ? suggested[key] : "";
  selects[key] = select;
  return h("label", { class: "field", for: "map-" + key }, h("span", { class: required ? "req" : "", text: label }), select);
}

const missingRequired = () => Object.entries(automation.required_fields).filter(([k]) => !selects[k] || !selects[k].value).map(([, l]) => l);

function renderSheet() {
  for (const k of Object.keys(selects)) delete selects[k];
  const { headers, suggested_map, row_count, filename } = upload;

  const grid = h("div", {},
    h("div", { class: "map-grid" }, ...automation.input_fields.map(([k, l, r]) => fieldRow(k, l, r, headers, suggested_map))),
    h("div", { class: "map-sub", text: "Filled in by the automation" }),
    h("div", { class: "map-grid" }, ...automation.output_fields.map(([k, l, r]) => fieldRow(k, l, r, headers, suggested_map))));

  const matched = missingRequired().length === 0;
  const cols = h("details", { class: "cols", open: !matched },
    h("summary", { text: matched ? "Columns matched" : "Match the columns" }), grid);

  const saveToggle = h("input", { type: "checkbox", id: "save", checked: true });
  const workers = h("input", { type: "number", id: "workers", min: "1", max: String(automation.max_workers), value: String(Math.min(4, automation.max_workers)) });
  const limit = h("input", { type: "number", id: "limit", min: "1", placeholder: "All" });
  const only = h("input", { type: "text", id: "only" });
  const allowDup = h("input", { type: "checkbox", id: "allow-dup" });
  const noResume = h("input", { type: "checkbox", id: "no-resume" });
  const error = h("div", { class: "form-error", role: "alert", hidden: true });
  const startBtn = h("button", { class: "go big", onclick: start }, "Start");

  async function start() {
    error.hidden = true;
    const column_map = {};
    for (const [key, select] of Object.entries(selects)) column_map[key] = select.value;
    const missing = missingRequired();
    if (missing.length) { error.textContent = `Pick a column for ${missing.join(" and ")}.`; error.hidden = false; cols.open = true; return; }
    if (saveToggle.checked && !confirm(`Create visits in Office Ally from ${filename}?`)) return;
    startBtn.disabled = true;
    try {
      const res = await api(`/api/automations/${slug}/run`, { method: "POST", body: {
        filename, column_map, save: saveToggle.checked, workers: parseInt(workers.value, 10) || 1,
        limit: limit.value ? parseInt(limit.value, 10) : null, only: only.value || null,
        allow_duplicate: allowDup.checked, no_resume: noResume.checked } });
      toast(res.queue_position ? "Queued" : "Started");
      showRun(res.run_id);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally { startBtn.disabled = false; }
  }

  detail.replaceChildren(
    h("div", { class: "file-chip" }, h("div", { text: filename }), h("span", { text: `${row_count} row${row_count === 1 ? "" : "s"}` })),
    cols,
    h("div", { class: "launch" },
      startBtn,
      h("label", { class: "check", for: "save" }, saveToggle, "Save to Office Ally"),
      h("label", { class: "check", for: "workers" }, "Windows", workers)),
    h("details", { class: "adv" }, h("summary", { text: "More options" }),
      h("div", { class: "adv-grid" },
        h("label", { class: "field", for: "limit" }, "First N rows only", limit),
        h("label", { class: "field", for: "only" }, "Names containing", only),
        h("label", { class: "check", for: "allow-dup" }, allowDup, "Allow duplicates"),
        h("label", { class: "check", for: "no-resume" }, noResume, "Start over, ignore earlier progress"))),
    error);
}

function showRun(runId) {
  if (panel) panel.stop();
  clear(progressSlot);
  const box = h("div");
  progressSlot.append(box);
  newRunHeading.hidden = false;
  panel = runPanel(box, runId, { insuranceLabel: automation.insurance_id_label, onOpenRun: showRun });
  progressSlot.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* A run that is still going (or opened from a card) shows first. */
const wanted = new URLSearchParams(location.search).get("run");
if (wanted) showRun(parseInt(wanted, 10));
else {
  const { runs } = await api(`/api/runs?automation=${encodeURIComponent(slug)}&limit=1`);
  if (runs.length && (runs[0].status === "running" || runs[0].status === "queued")) showRun(runs[0].id);
}

/* ---------- Office Ally login: for admins and people an admin allowed ---------- */
if (automation.can_manage_credentials) {
  const body = h("div", { class: "flow" });
  const box = h("details", { class: "panel login-box" }, h("summary", { class: "panel-body", text: "Office Ally login" }), h("div", { class: "panel-body" }, body));
  let loaded = false;

  async function loadLogin() {
    const status = await api(`/api/automations/${slug}/credentials`);
    const inputs = {};
    const grid = h("div", { class: "cred-grid" }, ...status.fields.map(f => {
      const input = h("input", { type: f.secret ? "password" : "text", id: "cred-" + f.key, autocomplete: "off", spellcheck: "false",
        placeholder: f.secret ? (f.is_set ? "Saved. Leave blank to keep it" : "Not set") : "" });
      if (!f.secret) input.value = f.value || "";
      inputs[f.key] = input;
      return h("label", { class: "field", for: "cred-" + f.key }, f.label, input);
    }));
    const msg = h("div", { hidden: true });
    const say = (ok, text, result) => {
      msg.hidden = false; msg.className = ok ? "form-ok" : "form-error"; msg.textContent = text;
      // when the sign-in failed, show what the Office Ally page actually displayed
      for (const page of (result && result.pages) || []) {
        msg.append(h("div", { class: "diag" },
          h("div", {}, h("b", { text: page.title || "(no title)" })), h("div", { class: "muted mono", text: page.url || "" }),
          page.text ? h("div", { text: page.text }) : null,
          page.window ? h("div", { class: "muted", text: `Browser window ${page.window[0]} x ${page.window[1]}` }) : null));
      }
      if (result && result.error) msg.append(h("div", { class: "muted mono", text: result.error }));
    };
    const saveBtn = h("button", { onclick: async () => {
      msg.hidden = true;
      const values = {};
      for (const f of status.fields) {
        const v = inputs[f.key].value;
        if (f.secret ? v !== "" : v !== (f.value || "")) values[f.key] = v;
      }
      if (!Object.keys(values).length) { say(false, "Nothing to update."); return; }
      saveBtn.disabled = true;
      try { await api(`/api/automations/${slug}/credentials`, { method: "PUT", body: { values } }); toast("Login saved"); await loadLogin(); }
      catch (err) { say(false, err.message); }
      finally { saveBtn.disabled = false; }
    } }, "Save");
    const testBtn = h("button", { class: "secondary", onclick: async () => {
      if (!confirm("Sign in to Office Ally with the saved login to check it? Nothing is changed there.")) return;
      msg.hidden = true; testBtn.disabled = true; testBtn.textContent = "Testing...";
      try { const r = await api(`/api/automations/${slug}/credentials/test`, { method: "POST" }); say(r.ok, r.message, r); }
      catch (err) { say(false, err.message); }
      finally { testBtn.disabled = false; testBtn.textContent = "Test login"; }
    } }, "Test login");
    if (status.busy) { saveBtn.disabled = testBtn.disabled = true; Object.values(inputs).forEach(i => { i.disabled = true; }); }
    body.replaceChildren(grid, h("div", { class: "cred-actions" }, saveBtn, testBtn,
      status.busy ? h("span", { class: "muted", text: "A run is in progress. Update the login after it finishes." }) : null), msg);
  }
  box.addEventListener("toggle", () => { if (box.open && !loaded) { loaded = true; loadLogin().catch(err => body.replaceChildren(h("div", { class: "form-error", text: err.message }))); } });
  main.append(h("div", { class: "section-gap" }), box);
}
