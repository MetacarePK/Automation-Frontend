import { h, api, shell, toast, pill, pager, fmtTime, ago, actionLabel, detailsText, clear } from "/static/app.js";

const { user: me, main } = await shell("admin");
const LIMIT = 25;

main.append(h("div", { class: "page-head" }, h("h1", { text: "Admin" })));

const TABS = [["overview", "Overview"], ["users", "Users"], ["logins", "Sign-ins"], ["activity", "Activity"], ["sessions", "Sessions"]];
const tabBar = h("div", { class: "tabs", role: "tablist" });
const body = h("div");
main.append(tabBar, body);

let current = location.hash.replace("#", "") || "overview";
const views = { overview, users, logins, activity, sessions };

function show(name) {
  if (!views[name]) name = "overview";
  current = name;
  history.replaceState(null, "", "#" + name);
  tabBar.replaceChildren(...TABS.map(([key, label]) => h("button", {
    role: "tab", "aria-selected": key === name ? "true" : "false", onclick: () => show(key) }, label)));
  clear(body);
  views[name]().catch(err => body.replaceChildren(h("div", { class: "form-error", text: err.message })));
}

/* ------------------------------------------------------------------ overview */
async function overview() {
  const o = await api("/api/admin/overview");
  const stat = (value, label) => h("div", {}, h("b", { text: value }), h("span", { text: label }));
  const stats = h("div", { class: "stats" },
    stat(o.logins_today.ok, "Sign-ins today"),
    stat(o.logins_today.failed, "Failed today"),
    stat(o.logins_today.unique_users, "People today"),
    stat(o.online_now, "Active now"),
    stat(o.active_sessions, "Open sessions"),
    stat(o.users.pending, "Awaiting approval"));

  const pending = h("section", { class: "panel" }, h("header", {}, h("h2", { text: "Awaiting approval" })),
    o.pending_users.length
      ? h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, ...o.pending_users.map(p => h("tr", {},
        h("td", {}, h("b", { text: p.name }), h("div", { class: "muted", text: p.email })),
        h("td", { class: "nowrap muted", text: ago(p.created_at) }),
        h("td", {}, h("button", { class: "small", onclick: () => approveDialog(p) }, "Review")))))))
      : h("p", { class: "muted", text: "None." }));

  const live = h("section", { class: "panel" }, h("header", {}, h("h2", { text: "Running now" })),
    o.live_runs.length
      ? h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, ...o.live_runs.map(r => h("tr", { class: "click", onclick: () => { location.href = `/runs?id=${r.id}`; } },
        h("td", { text: "#" + r.id }), h("td", { text: r.automation_slug }), h("td", { class: "wrap", text: r.user_email }),
        h("td", { class: "wrap", text: r.sheet_name }), h("td", {}, pill(r.status)))))))
      : h("p", { class: "muted", text: "None." }));

  const failed = h("section", { class: "panel" }, h("header", {}, h("h2", { text: "Failed sign-ins" }), h("button", { class: "quiet", onclick: () => show("logins") }, "See all")),
    o.recent_failed_logins.length
      ? h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {}, ...o.recent_failed_logins.map(f => h("tr", {},
        h("td", { class: "wrap", text: f.email }), h("td", { text: f.reason }), h("td", { class: "mono", text: f.ip || "" }),
        h("td", { class: "nowrap muted", text: ago(f.created_at) }))))))
      : h("p", { class: "muted", text: "None." }));

  body.append(h("div", { class: "stack" }, stats, loginChart(o.login_chart), h("div", { class: "two-col" }, pending, live), failed));
}

/* Thin paired bars per day, baseline-anchored, 2px gap; hover/focus tooltip; table view for non-visual reading. */
function loginChart(days) {
  const max = Math.max(1, ...days.flatMap(d => [d.ok, d.failed]));
  const tip = h("div", { class: "chart-tip", hidden: true });
  const chart = h("div", { class: "chart", role: "img", "aria-label": "Sign-ins per day for the last 14 days" }, tip);
  for (const d of days) {
    const ok = h("span", { class: "col ok" }), fail = h("span", { class: "col fail" });
    ok.style.height = (d.ok / max * 100) + "%";
    fail.style.height = (d.failed / max * 100) + "%";
    const label = `${d.day}: ${d.ok} successful, ${d.failed} failed`;
    const col = h("div", { class: "day", tabindex: "0", "aria-label": label }, ok, fail);
    const place = () => { tip.textContent = label; tip.hidden = false; tip.style.left = Math.min(col.offsetLeft, chart.clientWidth - 190) + "px"; tip.style.top = "-6px"; };
    col.addEventListener("mouseenter", place); col.addEventListener("focus", place);
    col.addEventListener("mouseleave", () => { tip.hidden = true; }); col.addEventListener("blur", () => { tip.hidden = true; });
    chart.append(col);
  }
  const axis = h("div", { class: "chart-axis" }, ...days.map(d => h("span", { text: d.day.slice(8) })));
  const table = h("div", { class: "table-wrap", hidden: true }, h("table", {},
    h("thead", {}, h("tr", {}, ...["Day", "Successful", "Failed"].map(t => h("th", { text: t })))),
    h("tbody", {}, ...days.map(d => h("tr", {}, h("td", { text: d.day }), h("td", { text: d.ok }), h("td", { text: d.failed }))))));
  const toggle = h("button", { class: "secondary small", onclick: () => { table.hidden = !table.hidden; chart.hidden = axis.hidden = !table.hidden; toggle.textContent = table.hidden ? "View as table" : "View as chart"; } }, "View as table");
  const legend = h("div", { class: "legend" },
    h("span", {}, h("i", { class: "created-swatch" }), "✓ Successful"), h("span", {}, h("i", { class: "nc-swatch" }), "✕ Failed"));
  return h("section", { class: "panel" }, h("header", {}, h("h2", { text: "Sign-ins, 14 days" }), h("div", { class: "row" }, legend, toggle)), chart, axis, table);
}

/* ------------------------------------------------------------------ users */
let usersFilter = { status: "", q: "" };

async function users() {
  const qs = new URLSearchParams();
  if (usersFilter.status) qs.set("status", usersFilter.status);
  if (usersFilter.q) qs.set("q", usersFilter.q);
  const { users: list, all_automations } = await api("/api/admin/users?" + qs);
  const names = Object.fromEntries(all_automations.map(a => [a.slug, a.name]));

  const search = h("input", { type: "search", id: "uq", value: usersFilter.q, placeholder: "Name or email" });
  const status = h("select", { id: "us" }, ...[["", "Any status"], ["pending", "Awaiting approval"], ["active", "Active"], ["disabled", "Disabled"]]
    .map(([v, t]) => h("option", { value: v, selected: v === usersFilter.status }, t)));
  const apply = () => { usersFilter = { status: status.value, q: search.value.trim() }; show("users"); };
  search.addEventListener("keydown", e => { if (e.key === "Enter") apply(); });
  status.addEventListener("change", apply);

  const act = (label, fn, cls = "secondary") => h("button", { class: cls + " small", onclick: async () => { try { await fn(); show("users"); } catch (err) { toast(err.message, true); } } }, label);

  const rows = list.map(u => {
    const locked = u.locked_until && u.locked_until > new Date().toISOString();
    const buttons = [];
    if (u.status === "pending") buttons.push(h("button", { class: "small", onclick: () => approveDialog(u, all_automations) }, "Approve"));
    if (u.status === "active") buttons.push(h("button", { class: "secondary small", onclick: () => accessDialog(u, all_automations) }, "Access"));
    if (u.status === "active" && u.id !== me.id) buttons.push(act("Disable", () => confirmThen(`Disable ${u.email}? They are signed out immediately.`, () => api(`/api/admin/users/${u.id}/disable`, { method: "POST" }))));
    if (u.status === "disabled") buttons.push(act("Enable", () => api(`/api/admin/users/${u.id}/enable`, { method: "POST" })));
    if (locked) buttons.push(act("Unlock", () => api(`/api/admin/users/${u.id}/unlock`, { method: "POST" })));
    buttons.push(h("button", { class: "secondary small", onclick: () => resetDialog(u) }, "Reset password"));
    if (u.id !== me.id) buttons.push(act(u.role === "admin" ? "Make regular user" : "Make admin", () => confirmThen(`${u.role === "admin" ? "Remove admin rights from" : "Give admin rights to"} ${u.email}?`, () => api(`/api/admin/users/${u.id}/role`, { method: "PUT", body: { role: u.role === "admin" ? "user" : "admin" } }))));
    buttons.push(h("button", { class: "secondary small", onclick: () => { activityFilter.user_id = String(u.id); activityOffset = 0; show("activity"); } }, "Activity"));
    return h("tr", {},
      h("td", {}, h("b", { text: u.name }), h("div", { class: "muted wrap", text: u.email })),
      h("td", {}, pill(u.status), u.role === "admin" ? h("span", { class: "pill info", text: "Admin" }) : null, locked ? h("span", { class: "pill bad", text: "Locked" }) : null),
      h("td", { class: "wrap", text: u.automations.map(s => names[s] || s).join(", ") || "-" }),
      h("td", { class: "nowrap", text: u.last_login_at ? ago(u.last_login_at) : "never" }),
      h("td", { text: u.login_count }), h("td", { text: u.run_count }), h("td", { text: u.open_sessions }),
      h("td", {}, h("div", { class: "row" }, ...buttons)));
  });

  body.append(
    h("div", { class: "filters" }, h("label", { class: "field", for: "uq" }, "Search", search), h("label", { class: "field", for: "us" }, "Status", status)),
    h("section", { class: "panel" }, list.length
      ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ...["Person", "Status", "Automations", "Last sign-in", "Sign-ins", "Runs", "Sessions", ""].map(t => h("th", { text: t })))),
        h("tbody", {}, ...rows)))
      : h("div", { class: "empty" }, h("strong", { text: "No people match" }), "Try a different search or status.")));
}

async function confirmThen(message, fn) { if (!confirm(message)) throw new Error("Cancelled"); return fn(); }

function dialogShell(title, content, actions) {
  const dlg = h("dialog", { "aria-label": title }, h("form", { method: "dialog" }, h("h2", { text: title }), content, h("div", { class: "dialog-actions" }, ...actions(() => dlg.close()))));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

function accessChecklist(user, all, preselect) {
  const boxes = all.map(a => h("input", { type: "checkbox", id: "ac-" + a.slug, value: a.slug, checked: preselect.includes(a.slug) }));
  const list = h("div", { class: "access-list" }, ...all.map((a, i) => h("label", { class: "check", for: "ac-" + a.slug }, boxes[i], a.name)));
  return { list, chosen: () => boxes.filter(b => b.checked).map(b => b.value) };
}

async function approveDialog(person, all) {
  all = all || (await api("/api/admin/users")).all_automations;
  const { list, chosen } = accessChecklist(person, all, all.map(a => a.slug));
  dialogShell(`Approve ${person.name}`, h("div", { class: "stack" }, h("p", { class: "muted", text: `${person.email} can sign in once approved. Choose which automations they may use.` }), list), (close) => [
    h("button", { type: "button", class: "secondary", onclick: close }, "Cancel"),
    h("button", { type: "button", onclick: async () => { try { await api(`/api/admin/users/${person.id}/approve`, { method: "POST", body: { automations: chosen() } }); toast("Account approved"); close(); show(current); } catch (err) { toast(err.message, true); } } }, "Approve account")]);
}

function accessDialog(person, all) {
  const { list, chosen } = accessChecklist(person, all, person.automations);
  dialogShell(`Automation access for ${person.name}`, list, (close) => [
    h("button", { type: "button", class: "secondary", onclick: close }, "Cancel"),
    h("button", { type: "button", onclick: async () => { try { await api(`/api/admin/users/${person.id}/access`, { method: "PUT", body: { automations: chosen() } }); toast("Access saved"); close(); show("users"); } catch (err) { toast(err.message, true); } } }, "Save access")]);
}

function resetDialog(person) {
  const pw = h("input", { type: "text", id: "rp", autocomplete: "off", placeholder: "New temporary password" });
  dialogShell(`Reset password for ${person.name}`, h("div", { class: "stack" },
    h("p", { class: "muted", text: "They are signed out everywhere. Send them the new password in person; they can change it under My account." }),
    h("label", { class: "field", for: "rp" }, "New password", pw)), (close) => [
    h("button", { type: "button", class: "secondary", onclick: close }, "Cancel"),
    h("button", { type: "button", onclick: async () => { try { await api(`/api/admin/users/${person.id}/reset-password`, { method: "POST", body: { password: pw.value } }); toast("Password reset"); close(); } catch (err) { toast(err.message, true); } } }, "Reset password")]);
}

/* ------------------------------------------------------------------ sign-ins */
const loginFilter = { user_id: "", success: "", since_days: "7" };
let loginOffset = 0;

async function userOptions() {
  const { users: list } = await api("/api/admin/users");
  return [["", "Everyone"], ...list.map(p => [String(p.id), `${p.name} (${p.email})`])];
}
function selectField(id, label, options, value, onChange) {
  const el = h("select", { id, onchange: () => onChange(el.value) }, ...options.map(([v, t]) => h("option", { value: v, selected: v === value }, t)));
  return h("label", { class: "field", for: id }, label, el);
}

async function logins() {
  const people = await userOptions();
  const qs = new URLSearchParams({ limit: LIMIT, offset: loginOffset });
  for (const [k, v] of Object.entries(loginFilter)) if (v) qs.set(k, v);
  const { total, events } = await api("/api/admin/logins?" + qs);
  const refilter = (key) => (v) => { loginFilter[key] = v; loginOffset = 0; show("logins"); };
  body.append(
    h("div", { class: "filters" },
      selectField("l-user", "Person", people, loginFilter.user_id, refilter("user_id")),
      selectField("l-ok", "Result", [["", "All"], ["true", "Successful"], ["false", "Failed"]], loginFilter.success, refilter("success")),
      selectField("l-days", "Period", [["1", "Last 24 hours"], ["7", "Last 7 days"], ["30", "Last 30 days"], ["", "All time"]], loginFilter.since_days, refilter("since_days"))),
    h("section", { class: "panel" }, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["When", "Email", "Result", "Reason", "IP address", "Browser"].map(t => h("th", { text: t })))),
      h("tbody", {}, ...events.map(e => h("tr", {},
        h("td", { class: "nowrap", text: fmtTime(e.created_at) }), h("td", { class: "wrap", text: e.email }),
        h("td", {}, h("span", { class: "pill " + (e.success ? "good" : "bad"), text: e.success ? "✓ Signed in" : "✕ Failed" })),
        h("td", { text: e.reason || "" }), h("td", { class: "mono", text: e.ip || "" }), h("td", { class: "wrap muted", text: (e.user_agent || "").slice(0, 70) })))))),
      pager(total, LIMIT, loginOffset, (o) => { loginOffset = o; show("logins"); })));
}

/* ------------------------------------------------------------------ activity */
const activityFilter = { user_id: "", action: "", q: "" };
let activityOffset = 0;

async function activity() {
  const people = await userOptions();
  const qs = new URLSearchParams({ limit: LIMIT, offset: activityOffset });
  for (const [k, v] of Object.entries(activityFilter)) if (v) qs.set(k, v);
  const { total, events, actions } = await api("/api/admin/activity?" + qs);
  const refilter = (key) => (v) => { activityFilter[key] = v; activityOffset = 0; show("activity"); };
  const search = h("input", { type: "search", id: "a-q", value: activityFilter.q, placeholder: "Email, run number, file..." });
  search.addEventListener("keydown", e => { if (e.key === "Enter") refilter("q")(search.value.trim()); });
  body.append(
    h("div", { class: "filters" },
      selectField("a-user", "Person", people, activityFilter.user_id, refilter("user_id")),
      selectField("a-action", "Action", [["", "Any action"], ...actions.map(a => [a, actionLabel(a)])], activityFilter.action, refilter("action")),
      h("label", { class: "field", for: "a-q" }, "Search", search)),
    h("section", { class: "panel" }, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["When", "Who", "What", "Details", "From"].map(t => h("th", { text: t })))),
      h("tbody", {}, ...events.map(e => h("tr", {},
        h("td", { class: "nowrap", text: fmtTime(e.created_at) }), h("td", { class: "wrap", text: e.actor_email || "-" }),
        h("td", { text: actionLabel(e.action) }), h("td", { class: "wrap muted", text: [e.target_type ? `${e.target_type} ${e.target_id ?? ""}` : "", detailsText(e.details)].filter(Boolean).join(" | ") }),
        h("td", { class: "mono", text: e.ip || "" })))))),
      pager(total, LIMIT, activityOffset, (o) => { activityOffset = o; show("activity"); })));
}

/* ------------------------------------------------------------------ sessions */
async function sessions() {
  const { sessions: list } = await api("/api/admin/sessions");
  body.append(h("section", { class: "panel" }, list.length
    ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Person", "Signed in", "Last seen", "IP address", "Browser", ""].map(t => h("th", { text: t })))),
      h("tbody", {}, ...list.map(s => h("tr", {},
        h("td", {}, h("b", { text: s.name }), h("div", { class: "muted wrap", text: s.email })),
        h("td", { class: "nowrap", text: fmtTime(s.created_at) }), h("td", { class: "nowrap", text: ago(s.last_seen_at) }),
        h("td", { class: "mono", text: s.ip || "" }), h("td", { class: "wrap muted", text: (s.user_agent || "").slice(0, 70) }),
        h("td", {}, h("button", { class: "secondary small", onclick: async () => { if (!confirm(`End this session for ${s.email}?`)) return; try { await api(`/api/admin/sessions/${s.id}/revoke`, { method: "POST" }); toast("Session ended"); show("sessions"); } catch (err) { toast(err.message, true); } } }, "End session")))))))
    : h("div", { class: "empty" }, h("strong", { text: "No open sessions" }))));
}

show(current);
