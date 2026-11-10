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
  sheet_uploaded: "Uploaded a sheet", run_queued: "Started a run", run_succeeded: "Run finished", run_failed: "Run failed",
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
  queued: ["Waiting", "info"], running: ["Running", "info"], succeeded: ["Done", "good"],
  failed: ["Failed", "bad"], cancelled: ["Stopped", "warn"],
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

/* ---------- the run panel: progress, counts, rows, log, download, stop ---------- */
export function runPanel(container, runId, { insuranceLabel = "Insurance ID", onDone } = {}) {
  let showAll = false, showLog = false, timer = null, finishedNotified = false, stopped = false;

  const title = h("h2");
  const statusSlot = h("span");
  const when = h("span", { class: "muted" });
  const stopBtn = h("button", { class: "stop", onclick: async () => { if (await stopRun(runId)) refresh(); } }, "Stop");
  const download = h("a", { class: "btn secondary", href: `/api/runs/${runId}/download`, "aria-disabled": "true" }, "Download sheet");
  const bar = h("div", { class: "bar", role: "img" });
  const counts = h("div", { class: "counts" });
  const message = h("div", { class: "run-msg", hidden: true });
  const tbody = h("tbody");
  const logBox = h("pre", { class: "log", hidden: true, tabindex: "0", "aria-label": "Run log" });
  const moreBtn = h("button", { class: "secondary small", onclick: () => { showAll = !showAll; moreBtn.textContent = showAll ? "Latest 10" : "All rows"; refresh(); } }, "All rows");
  const logBtn = h("button", { class: "secondary small", onclick: () => { showLog = !showLog; logBtn.textContent = showLog ? "Hide log" : "Log"; logBox.hidden = !showLog; if (showLog) refreshLog(); } }, "Log");

  container.replaceChildren(h("div", { class: "panel" },
    h("div", { class: "run-head" }, h("div", { class: "run-title" }, title, statusSlot, when), h("div", { class: "row" }, stopBtn, download)),
    bar, counts, message,
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Row", "Patient", "DOS", "Status", "Visit ID", insuranceLabel, "Charges", "Note"].map(t => h("th", { text: t })))), tbody)),
    h("div", { class: "run-foot" }, moreBtn, logBtn), logBox));

  async function refreshLog() {
    try { const { lines } = await api(`/api/runs/${runId}/log?tail=300`); logBox.textContent = lines.join("\n") || "Nothing logged yet."; logBox.scrollTop = logBox.scrollHeight; }
    catch { /* ignore */ }
  }

  const count = (cls, n, label) => h("div", { class: cls }, h("b", { text: n }), h("span", { text: label }));

  async function refresh() {
    if (stopped) return;
    let run;
    try { run = await api(`/api/runs/${runId}?rows=${showAll ? 5000 : 10}`); }
    catch (err) { message.hidden = false; message.className = "run-msg form-error"; message.textContent = err.message; return; }

    const active = run.status === "queued" || run.status === "running";
    title.textContent = run.sheet_name;
    statusSlot.replaceChildren(pill(run.status));
    when.textContent = run.status === "queued"
      ? `${run.queue_position} ahead`
      : `${fmtTime(run.started_at || run.queued_at)}${run.user_email ? " by " + run.user_email : ""}`;

    const c = run.counts, total = Math.max(run.total, c.seen, 1);
    bar.setAttribute("aria-label", `${c.created} created, ${c.duplicate} duplicate, ${c.not_created} not created of ${run.total}`);
    bar.replaceChildren(...[["s-created", c.created], ["s-duplicate", c.duplicate], ["s-notcreated", c.not_created]]
      .filter(([, n]) => n > 0).map(([cls, n]) => { const s = h("span", { class: cls }); s.style.width = (n / total * 100) + "%"; return s; }));
    counts.replaceChildren(count("c-created", c.created, "Created"), count("c-duplicate", c.duplicate, "Duplicate"),
      count("c-notcreated", c.not_created, "Not created"), count("", run.total, "Rows"));

    if (run.status === "failed" || run.status === "cancelled") { message.hidden = false; message.className = "run-msg form-error"; message.textContent = run.error || "The run did not finish."; }
    else message.hidden = true;

    download.setAttribute("aria-disabled", run.started_at ? "false" : "true");
    stopBtn.hidden = !active;

    tbody.replaceChildren(...run.rows.map(r => {
      const note = r.note || r.error || (r.duplicate_of ? `Already exists (visit ${r.duplicate_of})` : "");
      const done = String(r.status).startsWith("Done");
      return h("tr", {},
        h("td", { text: r.row }), h("td", { class: "wrap", text: r.patient_name }), h("td", { class: "nowrap", text: r.dos }),
        h("td", {}, h("span", { class: "tag " + (done ? (r.duplicate_of ? "warn" : "good") : r.status === "Not Created" ? "bad" : ""), text: r.duplicate_of ? "Duplicate" : r.status })),
        h("td", { class: "mono", text: r.visit_id }), h("td", { class: "mono", text: r.beneficiary_id }),
        h("td", { text: r.line_charges }), h("td", { class: "wrap" + (r.error ? " err" : ""), text: note }));
    }));
    if (!run.rows.length) tbody.replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "muted", text: active ? "Waiting for the first row..." : "No rows recorded." })));

    if (showLog) refreshLog();
    clearTimeout(timer);
    if (active) timer = setTimeout(refresh, 2000);
    else if (!finishedNotified) { finishedNotified = true; if (onDone) onDone(run); }
  }

  refresh();
  return { stop() { stopped = true; clearTimeout(timer); } };
}
