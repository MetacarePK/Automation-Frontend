import { h, api } from "/static/app.js";

const error = h("div", { class: "form-error", role: "alert", hidden: true });
const name = h("input", { type: "text", id: "name", autocomplete: "name", required: true, maxlength: "100", autofocus: true });
const email = h("input", { type: "email", id: "email", autocomplete: "username", required: true });
const password = h("input", { type: "password", id: "password", autocomplete: "new-password", required: true, minlength: "10" });
const confirmField = h("input", { type: "password", id: "confirm", autocomplete: "new-password", required: true });
const submit = h("button", { type: "submit", class: "big" }, "Request account");
const box = h("div", { class: "auth-box" });

const form = h("form", {
  onsubmit: async (event) => {
    event.preventDefault();
    error.hidden = true;
    if (password.value !== confirmField.value) { error.textContent = "The passwords don't match."; error.hidden = false; return; }
    submit.disabled = true;
    try {
      const res = await api("/api/auth/signup", { method: "POST", body: { name: name.value, email: email.value, password: password.value } });
      box.replaceChildren(
        h("div", {}, h("div", { class: "wordmark", text: "Automation" }), h("h1", { text: "Request sent" })),
        h("p", { class: "form-ok", text: res.message }),
        h("a", { class: "btn secondary", href: "/login" }, "Back to sign in"));
    } catch (err) { error.textContent = err.message; error.hidden = false; submit.disabled = false; }
  },
}, error,
  h("label", { class: "field", for: "name" }, "Name", name),
  h("label", { class: "field", for: "email" }, "Email", email),
  h("label", { class: "field", for: "password" }, "Password", password,
    h("span", { class: "hint", text: "10+ characters, upper and lower case, a number" })),
  h("label", { class: "field", for: "confirm" }, "Repeat password", confirmField),
  submit);

document.title = "Request account - Automation";
box.append(
  h("div", {}, h("div", { class: "wordmark", text: "Automation" }), h("h1", { text: "Request an account" })),
  form,
  h("p", { class: "switch" }, "Have an account? ", h("a", { href: "/login" }, "Sign in")));
document.body.replaceChildren(h("div", { class: "auth" }, box));
