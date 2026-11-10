import { h, api } from "/static/app.js";

const error = h("div", { class: "form-error", role: "alert", hidden: true });
const email = h("input", { type: "email", id: "email", autocomplete: "username", required: true, autofocus: true });
const password = h("input", { type: "password", id: "password", autocomplete: "current-password", required: true });
const submit = h("button", { type: "submit", class: "big" }, "Sign in");

const form = h("form", {
  onsubmit: async (event) => {
    event.preventDefault();
    error.hidden = true;
    submit.disabled = true;
    try {
      await api("/api/auth/login", { method: "POST", body: { email: email.value, password: password.value } });
      location.href = "/";
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      submit.disabled = false;
    }
  },
}, error,
  h("label", { class: "field", for: "email" }, "Email", email),
  h("label", { class: "field", for: "password" }, "Password", password),
  submit);

document.title = "Sign in - Automation";
document.body.replaceChildren(h("div", { class: "auth" }, h("div", { class: "auth-box" },
  h("div", {}, h("div", { class: "wordmark", text: "Automation" }), h("h1", { text: "Sign in" })),
  form,
  h("p", { class: "switch" }, "No account? ", h("a", { href: "/signup" }, "Request one")))));
