import { h, api, shell, pill, ago, fmtTime, stopRun } from "/static/app.js";

const { main } = await shell("home");

function stateOf(card) {
  const run = card.active_run;
  if (run && run.status === "running") return { on: true, text: run.mine ? "Running" : `Running (${run.user_name})` };
  if (run) return { on: false, text: "Waiting" };
  if (card.running_now > 0) return { on: true, text: "In use" };
  return { on: false, text: "Ready" };
}

function automationCard(card) {
  const state = stateOf(card);
  const run = card.active_run;
  const last = card.last_run ? `Last run ${ago(card.last_run.finished_at || card.last_run.started_at)}` : "No runs yet";

  const actions = h("div", { class: "acts" });
  if (run) {
    actions.append(
      h("button", { class: "stop", onclick: async () => { if (await stopRun(run.id)) setTimeout(load, 1200); } }, "Stop"),
      h("a", { class: "btn secondary", href: `/a/${card.slug}?run=${run.id}` }, "View"));
  } else {
    actions.append(h("a", { class: "btn go", href: `/a/${card.slug}` }, "Start"));
  }

  const el = h("section", { class: "auto" },
    h("div", { class: "auto-top" }, h("h2", { text: card.name }),
      h("span", { class: "state" + (state.on ? " on" : "") }, state.text)),
    h("div", { class: "auto-body" }, h("p", { class: "desc", text: card.description }),
      h("div", { class: "auto-foot" }, h("span", { class: "last", text: last }), actions)));
  el.style.setProperty("--accent", card.accent);
  return el;
}

const cards = h("div", { class: "cards" });
const recent = h("div");
main.append(h("div", { class: "page-head" }, h("h1", { text: "Automations" })), cards, recent);

let names = {};
async function load() {
  const { automations } = await api("/api/automations");
  names = Object.fromEntries(automations.map(a => [a.slug, a.name]));
  cards.replaceChildren(...(automations.length
    ? automations.map(automationCard)
    : [h("div", { class: "empty" }, h("strong", { text: "No automations yet" }), "Ask an administrator for access.")]));

  const { runs } = await api("/api/runs?limit=5");
  recent.replaceChildren(...(runs.length ? [
    h("h2", { class: "section-title", text: "Recent runs" }),
    h("div", { class: "panel" }, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Automation", "Sheet", "Rows", "Status", "Started"].map(t => h("th", { text: t })))),
      h("tbody", {}, ...runs.map(r => h("tr", { class: "click", onclick: () => { location.href = `/runs?id=${r.id}`; } },
        h("td", { text: names[r.automation_slug] || r.automation_slug }), h("td", { class: "wrap", text: r.sheet_name }),
        h("td", { text: r.total }), h("td", {}, pill(r.status)), h("td", { class: "nowrap", text: fmtTime(r.started_at || r.queued_at) }))))))),
  ] : []));
}

await load();
// A run started or stopped elsewhere shows up without a reload.
setInterval(() => { if (!document.hidden) load(); }, 5000);
