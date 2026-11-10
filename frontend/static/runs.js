import { h, api, shell, pill, fmtTime, duration, pager, runPanel } from "/static/app.js";

const { user, main } = await shell("runs");
const isAdmin = user.role === "admin";
const LIMIT = 25;
let offset = 0, statusFilter = "", autoFilter = "", userFilter = "";

const { automations } = await api("/api/automations");
const names = Object.fromEntries(automations.map(a => [a.slug, a.name]));
const labels = Object.fromEntries(automations.map(a => [a.slug, a.name]));
let people = [];
if (isAdmin) people = (await api("/api/admin/users")).users;

const listBox = h("div");
const dialog = h("dialog", { class: "wide", "aria-label": "Run details" });
let openPanel = null;
dialog.addEventListener("close", () => { if (openPanel) openPanel.stop(); openPanel = null; history.replaceState(null, "", "/runs"); });

function select(id, label, options, onChange) {
  const el = h("select", { id, onchange: () => onChange(el.value) }, ...options.map(([v, t]) => h("option", { value: v }, t)));
  return h("label", { class: "field", for: id }, label, el);
}

function openRun(id, slug) {
  const box = h("div");
  dialog.replaceChildren(box, h("div", { class: "dialog-actions" }, h("button", { class: "secondary", onclick: () => dialog.close() }, "Close")));
  const automation = automations.find(a => a.slug === slug);
  openPanel = runPanel(box, id, { insuranceLabel: (automation && automation.insurance_id_label) || "Insurance ID" });
  history.replaceState(null, "", `/runs?id=${id}`);
  if (!dialog.open) dialog.showModal();
}

async function load() {
  const qs = new URLSearchParams({ limit: LIMIT, offset });
  if (statusFilter) qs.set("status", statusFilter);
  if (autoFilter) qs.set("automation", autoFilter);
  if (userFilter) qs.set("user_id", userFilter);
  const { total, runs } = await api("/api/runs?" + qs);
  const columns = ["Run", ...(isAdmin ? ["User"] : []), "Automation", "Sheet", "Rows", "Created", "Not created", "Status", "Started"];
  listBox.replaceChildren(h("div", { class: "panel" },
    runs.length
      ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ...columns.map(t => h("th", { text: t })))),
        h("tbody", {}, ...runs.map(r => h("tr", { class: "click", tabindex: "0", onclick: () => openRun(r.id, r.automation_slug),
          onkeydown: (e) => { if (e.key === "Enter") openRun(r.id, r.automation_slug); } },
          h("td", { text: "#" + r.id }), isAdmin ? h("td", { class: "wrap", text: r.user_email }) : null,
          h("td", { text: labels[r.automation_slug] || r.automation_slug }), h("td", { class: "wrap", text: r.sheet_name }),
          h("td", { text: r.total }), h("td", { text: r.counts.created }), h("td", { text: r.counts.not_created }),
          h("td", {}, pill(r.status)), h("td", { class: "nowrap", text: fmtTime(r.started_at || r.queued_at) }),
          )))))
      : h("div", { class: "empty" }, h("strong", { text: "No runs yet" })),
    pager(total, LIMIT, offset, (o) => { offset = o; load(); })));
}

const filters = h("div", { class: "filters" },
  select("f-status", "Status", [["", "Any"], ["running", "Running"], ["queued", "Waiting"], ["succeeded", "Finished"], ["failed", "Failed"], ["cancelled", "Stopped"]], v => { statusFilter = v; offset = 0; load(); }),
  select("f-auto", "Automation", [["", "All"], ...Object.entries(names)], v => { autoFilter = v; offset = 0; load(); }),
  isAdmin ? select("f-user", "User", [["", "Everyone"], ...people.map(p => [String(p.id), `${p.name} (${p.email})`])], v => { userFilter = v; offset = 0; load(); }) : null);

main.append(h("div", { class: "page-head" }, h("h1", { text: "History" })),
  filters, listBox, dialog);

await load();
const wanted = new URLSearchParams(location.search).get("id");
if (wanted) {
  try { const run = await api(`/api/runs/${parseInt(wanted, 10)}?rows=0`); openRun(run.id, run.automation_slug); } catch { /* not allowed or gone */ }
}
