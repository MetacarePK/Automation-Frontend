import { h, api, shell, pager, fmtTime, actionLabel, detailsText } from "/static/app.js";

const { user, main } = await shell("account");
const LIMIT = 25;
let offset = 0;

const current = h("input", { type: "password", id: "cur", autocomplete: "current-password", required: true });
const next = h("input", { type: "password", id: "new", autocomplete: "new-password", required: true, minlength: "10" });
const msg = h("div", { hidden: true });
const passwordForm = h("form", {
  class: "flow",
  onsubmit: async (event) => {
    event.preventDefault();
    msg.hidden = true;
    try {
      await api("/api/account/password", { method: "POST", body: { current_password: current.value, new_password: next.value } });
      msg.className = "form-ok"; msg.textContent = "Password changed."; msg.hidden = false;
      current.value = next.value = "";
    } catch (err) { msg.className = "form-error"; msg.textContent = err.message; msg.hidden = false; }
  },
}, h("div", { class: "map-grid" },
  h("label", { class: "field", for: "cur" }, "Current password", current),
  h("label", { class: "field", for: "new" }, "New password", next)),
  msg, h("div", {}, h("button", { type: "submit" }, "Change password")));

const logBox = h("div");
async function loadLog() {
  const { total, events } = await api(`/api/account/activity?limit=${LIMIT}&offset=${offset}`);
  logBox.replaceChildren(
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["When", "What", "Details", "IP"].map(t => h("th", { text: t })))),
      h("tbody", {}, ...events.map(e => h("tr", {},
        h("td", { class: "nowrap", text: fmtTime(e.created_at) }), h("td", { text: actionLabel(e.action) }),
        h("td", { class: "wrap muted", text: detailsText(e.details) }), h("td", { class: "mono", text: e.ip || "" })))))),
    pager(total, LIMIT, offset, (o) => { offset = o; loadLog(); }));
}

main.append(
  h("div", { class: "page-head" }, h("div", {}, h("h1", { text: user.name }), h("p", { text: user.email }))),
  h("section", { class: "panel" }, h("header", {}, h("h2", { text: "Password" })), h("div", { class: "panel-body" }, passwordForm)),
  h("h2", { class: "section-title", text: "My activity" }),
  h("section", { class: "panel" }, logBox));
await loadLog();
