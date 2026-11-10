/* Shared helpers: api(), h(), shell(), run panel. Every value goes through textContent,
   so a patient name in a sheet can never inject markup. */

export const $ = (sel, root = document) => root.querySelector(sel);

/** h("div", {class: "x", onclick: fn}, "text", childNode, ...) */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export async function api(path, { method = "GET", body, form } = {}) {
  const init = { method, headers: {}, credentials: "same-origin" };
  if (form) init.body = form;
  else if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const res = await fetch(path, init);
  if (res.status === 401 && !location.pathname.startsWith("/login") && !location.pathname.startsWith("/signup")) {
    location.href = "/login";
    throw new Error("Please sign in.");
  }
  let data = null;
  if ((res.headers.get("content-type") || "").includes("application/json")) data = await res.json();
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail) ? "Please check the form and try again." : `Request failed (${res.status})`);
  }
  return data;
}

export function toast(message, isError = false) {
  const el = h("div", { class: "toast" + (isError ? " error" : ""), role: "status", text: message });
  document.body.append(el);
  setTimeout(() => el.remove(), isError ? 6000 : 3000);
}

export function fmtTime(iso) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
export function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
}
export function duration(from, to) {
  if (!from) return "-";
  const s = Math.max(0, ((to ? new Date(to) : new Date()) - new Date(from)) / 1000);
  if (s < 60) return Math.round(s) + " s";
  if (s < 3600) return Math.floor(s / 60) + " min " + Math.round(s % 60) + " s";
  return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min";
}

const ACTION_LABELS = {
  signup: "Requested an account", signup_duplicate: "Repeated signup", login: "Signed in", logout: "Signed out",
  login_failed: "Failed sign-in", login_blocked_locked: "Sign-in blocked", account_locked: "Account locked",
  sheet_uploaded: "Uploaded a sheet", run_queued: "Started a run", run_failed: "Run failed",
  run_cancelled: "Run stopped", run_cancel_requested: "Asked to stop a run", download: "Downloaded a sheet",
  user_approved: "Approved an account", user_disabled: "Disabled an account", user_enabled: "Enabled an account",
  access_changed: "Changed access", role_changed: "Changed a role", password_reset: "Reset a password",
  password_changed: "Changed password", user_unlocked: "Unlocked an account", session_revoked: "Ended a session",
  admin_created: "Admin created",
};
export const actionLabel = (a) => ACTION_LABELS[a] || a;
export function detailsText(raw) {
  if (!raw) return "";
  try {
    return Object.entries(JSON.parse(raw)).filter(([, v]) => v !== null && v !== "")
      .map(([k, v]) => `${k.replace(/_/g, " ")}: ${Array.isArray(v) ? v.join(", ") : v}`).join(", ");
  } catch { return raw; }
}

const STATUS_TAG = {
  queued: ["Waiting", "info"], validating: ["Checking", "info"], running: ["Running", "info"],
  completed: ["Completed", "good"], completed_with_errors: ["Completed with errors", "warn"],
  failed: ["Failed", "bad"], interrupted: ["Interrupted", "warn"], cancelled: ["Stopped", "warn"],
  pending: ["Pending", "warn"], active: ["Active", "good"], disabled: ["Disabled", "bad"],
};
export function pill(status) {
  const [label, tone] = STATUS_TAG[status] || [status, ""];
  return h("span", { class: "tag " + tone, text: label });
}

/* ---------- page shell: top bar + content ---------- */
export async function shell(active) {
  const user = await api("/api/auth/me");
  const links = [["/", "Automations", "home"], ["/runs", "History", "runs"], ["/account", "Account", "account"]];
  if (user.role === "admin") links.push(["/admin", "Admin", "admin"]);

  const nav = h("nav", { class: "nav", "aria-label": "Main" });
  const adminBadge = h("span", { class: "badge-count", hidden: true });
  for (const [href, label, key] of links) {
    nav.append(h("a", { href, "aria-current": key === active ? "page" : null }, label, key === "admin" ? adminBadge : null));
  }
  const bar = h("header", { class: "topbar" }, h("div", { class: "topbar-in" },
    h("a", { class: "wordmark", href: "/" }, "Automation"), nav,
    h("div", { class: "topbar-user" }, h("span", { text: user.name }),
      h("button", { class: "text", onclick: async () => { await api("/api/auth/logout", { method: "POST" }); location.href = "/login"; } }, "Sign out"))));
  const main = h("main", { class: "page", id: "main" });
  document.body.replaceChildren(bar, main);

  if (user.role === "admin") {
    api("/api/admin/overview").then(o => {
      if (o.users.pending > 0) { adminBadge.textContent = o.users.pending; adminBadge.hidden = false; adminBadge.title = "Accounts waiting for approval"; }
    }).catch(() => {});
  }
  return { user, main };
}

export function pager(total, limit, offset, onChange) {
  const page = Math.floor(offset / limit) + 1, pages = Math.max(1, Math.ceil(total / limit));
  return h("div", { class: "pager" },
    h("span", { text: `${page} / ${pages}` }),
    h("button", { class: "secondary small", disabled: offset <= 0, onclick: () => onChange(Math.max(0, offset - limit)) }, "Previous"),
    h("button", { class: "secondary small", disabled: offset + limit >= total, onclick: () => onChange(offset + limit) }, "Next"));
}

/** Stop a run after a short confirmation. Returns true when it was asked to stop. */
export async function stopRun(runId) {
  if (!confirm("Stop this run? Visits already saved stay saved.")) return false;
  try { await api(`/api/runs/${runId}/cancel`, { method: "POST" }); toast("Stopping..."); return true; }
  catch (err) { toast(err.message, true); return false; }
}

/* ---------- the run panel: state, counts, rows, files, log, history, stop, retry ---------- */
const OPEN = ["queued", "validating", "running"];
const PHASE = { validating: "Checking the sheet and the automation", running: "Working through the sheet" };

export function runPanel(container, runId, { insuranceLabel = "Insurance ID", onDone, onOpenRun } = {}) {
  let showAll = false, showLog = false, showHistory = false, view = "all", timer = null, finishedNotified = false, stopped = false;
  let filesLoaded = false;

  const title = h("h2");
  const statusSlot = h("span");
  const when = h("span", { class: "muted" });
  const retryBtn = h("button", { class: "secondary", hidden: true, onclick: retry }, "Retry");
  const stopBtn = h("button", { class: "stop", hidden: true, onclick: async () => { if (await stopRun(runId)) refresh(); } }, "Stop");
  const note = h("div", { class: "run-msg", hidden: true });
  const chainLine = h("div", { class: "run-chain muted", hidden: true });
  const bar = h("div", { class: "bar", role: "img" });
  const counts = h("div", { class: "counts" });
  const carriedLine = h("div", { class: "run-chain muted", hidden: true });
  const views = h("div", { class: "seg", role: "group", "aria-label": "Show rows" });
  const tbody = h("tbody");
  const files = h("div", { class: "row" });
  const logBox = h("pre", { class: "log", hidden: true, tabindex: "0", "aria-label": "Run log" });
  const historyBox = h("ol", { class: "events", hidden: true, "aria-label": "Run history" });
  const moreBtn = h("button", { class: "secondary small", onclick: () => { showAll = !showAll; moreBtn.textContent = showAll ? "Latest 10" : "All rows"; refresh(); } }, "All rows");
  const logBtn = h("button", { class: "secondary small", onclick: () => { showLog = !showLog; logBtn.textContent = showLog ? "Hide log" : "Log"; logBox.hidden = !showLog; if (showLog) refreshLog(); } }, "Log");
  const configBox = h("dl", { class: "config", hidden: true });
  const configBtn = h("button", { class: "secondary small", onclick: () => { configBox.hidden = !configBox.hidden; configBtn.textContent = configBox.hidden ? "Settings" : "Hide settings"; } }, "Settings");
  const historyBtn = h("button", { class: "secondary small", onclick: () => { showHistory = !showHistory; historyBtn.textContent = showHistory ? "Hide history" : "History"; historyBox.hidden = !showHistory; if (showHistory) refreshEvents(); } }, "History");

  container.replaceChildren(h("div", { class: "panel" },
    h("div", { class: "run-head" }, h("div", { class: "run-title" }, title, statusSlot, when), h("div", { class: "row" }, retryBtn, stopBtn)),
    note, chainLine, bar, counts, carriedLine,
    h("div", { class: "run-views" }, views),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Row", "Patient", "DOS", "Status", "Visit ID", insuranceLabel, "Charges", "Note"].map(t => h("th", { text: t })))), tbody)),
    h("div", { class: "run-foot" }, moreBtn, logBtn, historyBtn, configBtn, files), configBox, logBox, historyBox));

  async function retry() {
    if (!confirm("Run this sheet again? Rows that are already done are skipped.")) return;
    try {
      const res = await api(`/api/runs/${runId}/retry`, { method: "POST" });
      toast("Retry queued");
      if (onOpenRun) onOpenRun(res.run_id); else location.href = `/runs?id=${res.run_id}`;
    } catch (err) { toast(err.message, true); }
  }

  async function refreshLog() {
    try { const { lines, purged } = await api(`/api/runs/${runId}/log?tail=300`); logBox.textContent = lines.join("\n") || (purged ? "The log was removed by the retention policy." : "Nothing logged yet."); logBox.scrollTop = logBox.scrollHeight; }
    catch { /* ignore */ }
  }
  async function refreshEvents() {
    try {
      const { events } = await api(`/api/runs/${runId}/events`);
      historyBox.replaceChildren(...events.map(e => h("li", { class: "ev " + e.level }, h("span", { class: "nowrap", text: fmtTime(e.ts) }), h("span", { text: e.message }))));
    } catch { /* ignore */ }
  }
  async function refreshFiles() {
    try {
      const { files: list, purged } = await api(`/api/runs/${runId}/files`);
      files.replaceChildren(...list.filter(f => f.kind !== "log").map(f =>
        h("a", { class: "btn secondary small", href: `/api/runs/${runId}/download?file=${f.kind}` }, f.label)),
        ...(purged ? [h("span", { class: "muted", text: "Files removed by the retention policy" })] : []));
      filesLoaded = true;
    } catch { /* ignore */ }
  }

  const count = (cls, n, label) => h("div", { class: cls }, h("b", { text: n }), h("span", { text: label }));
  const VIEWS = [["all", "All"], ["failed", "Failed"], ["skipped", "Skipped"], ["created", "Successful"]];
  function renderViews() {
    views.replaceChildren(...VIEWS.map(([key, label]) => h("button", {
      class: "secondary small", "aria-pressed": key === view ? "true" : "false",
      onclick: () => { view = key; renderViews(); refresh(); } }, label)));
  }
  renderViews();

  async function refresh() {
    if (stopped) return;
    let run;
    try { run = await api(`/api/runs/${runId}?rows=${showAll ? 5000 : 10}&view=${view}`); }
    catch (err) { note.hidden = false; note.className = "run-msg form-error"; note.textContent = err.message; return; }

    const active = OPEN.includes(run.status);
    title.textContent = run.sheet_name;
    statusSlot.replaceChildren(pill(run.status));
    when.textContent = run.status === "queued"
      ? (run.queue_position ? `${run.queue_position} ahead` : "starting soon")
      : `${fmtTime(run.started_at || run.queued_at)}${run.user_email ? " by " + run.user_email : ""}${run.attempt > 1 ? ` (attempt ${run.attempt})` : ""}${run.finished_at && run.started_at ? `, took ${duration(run.started_at, run.finished_at)}` : ""}`;

    if (run.chain && run.chain.length > 1) {
      chainLine.hidden = false;
      chainLine.replaceChildren("Attempts: ", ...run.chain.flatMap((r, i) => [i ? " then " : "",
        r.id === run.id ? h("b", { text: `#${r.id}` }) : h("a", { href: `/runs?id=${r.id}`, text: `#${r.id}` }), ` (${(STATUS_TAG[r.status] || [r.status])[0].toLowerCase()})`]));
    } else chainLine.hidden = true;

    const c = run.counts, total = Math.max(run.total, c.seen + c.carried, 1);
    bar.setAttribute("aria-label", `${c.created} successful, ${c.duplicate} skipped, ${c.not_created} failed, ${c.pending} pending`);
    bar.replaceChildren(...[["s-created", c.created + c.carried], ["s-duplicate", c.duplicate], ["s-notcreated", c.not_created]]
      .filter(([, n]) => n > 0).map(([cls, n]) => { const s = h("span", { class: cls }); s.style.width = (n / total * 100) + "%"; return s; }));
    counts.replaceChildren(count("c-created", c.created, "Successful"), count("c-duplicate", c.duplicate, "Skipped"),
      count("c-notcreated", c.not_created, "Failed"), count("c-pending", c.pending, "Pending"),
      ...(c.carried ? [count("c-carried", c.carried, "Done earlier")] : []), count("", run.total, "Rows"));
    carriedLine.hidden = true;

    if (run.status === "failed" || run.status === "interrupted" || run.status === "cancelled") {
      note.hidden = false; note.className = "run-msg " + (run.status === "failed" ? "form-error" : "form-warn"); note.textContent = run.error || "The run did not finish.";
    } else if (run.status === "completed_with_errors") {
      const n = run.counts.not_created;
      note.hidden = false; note.className = "run-msg form-warn";
      note.textContent = n ? `${n} row${n === 1 ? "" : "s"} failed. Review ${n === 1 ? "it" : "them"} below, then retry.` : (run.summary || "Some rows need attention.");
    } else if (active) {
      note.hidden = false; note.className = "run-msg form-info"; note.textContent = run.status === "queued" ? "Waiting for its turn. It starts by itself." : PHASE[run.status] || "";
    } else note.hidden = true;

    const cfg = run.config || {};
    const yes = (v) => v ? "Yes" : "No";
    const rowsOf = [["Save to Office Ally", yes(cfg.save)], ["Browser windows", cfg.workers], ["First N rows only", cfg.limit || "All"],
      ["Names containing", cfg.only || "Anyone"], ["Allow duplicates", yes(cfg.allow_duplicate)], ["Ignore earlier progress", yes(cfg.no_resume)],
      ["Columns", Object.entries(cfg.column_map || {}).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(", ")]];
    configBox.replaceChildren(...rowsOf.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
    stopBtn.hidden = !run.can_cancel;
    retryBtn.hidden = !run.can_retry;

    tbody.replaceChildren(...run.rows.map(r => {
      const note2 = r.note || r.error || (r.duplicate_of ? `Already exists (visit ${r.duplicate_of})` : "");
      const done = String(r.status).startsWith("Done");
      return h("tr", {},
        h("td", { text: r.row }), h("td", { class: "wrap", text: r.patient_name }), h("td", { class: "nowrap", text: r.dos }),
        h("td", {}, h("span", { class: "tag " + (done ? (r.duplicate_of ? "warn" : "good") : r.status === "Not Created" ? "bad" : ""), text: r.duplicate_of ? "Skipped" : r.status === "Not Created" ? "Failed" : r.status })),
        h("td", { class: "mono", text: r.visit_id }), h("td", { class: "mono", text: r.beneficiary_id }),
        h("td", { text: r.line_charges }), h("td", { class: "wrap" + (r.error ? " err" : ""), text: note2 }));
    }));
    if (!run.rows.length) tbody.replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "muted",
      text: run.rows_purged_at ? "Row details were removed by the retention policy." : active ? "Waiting for the first row..." : view === "all" ? "No rows recorded." : "No rows in this view." })));

    if (showLog) refreshLog();
    if (showHistory) refreshEvents();
    if (!active && !filesLoaded) refreshFiles();
    if (active) filesLoaded = false;
    clearTimeout(timer);
    if (active) timer = setTimeout(refresh, 2000);
    else if (!finishedNotified) { finishedNotified = true; if (onDone) onDone(run); }
  }

  refresh();
  return { stop() { stopped = true; clearTimeout(timer); } };
}
