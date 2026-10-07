// The Account and Users sections of the Settings dialog. Plain DOM, text goes in with
// textContent only; passwords are cleared from the fields as soon as they are sent.

(function () {
  const accountBox = document.getElementById("account-box");
  const usersBox = document.getElementById("users-box"); // only in the page for an admin
  const readOnlyMode = () => document.body.dataset.readOnly === "true";

  async function call(url, method, body) {
    try {
      return await api(url, { method, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      return { error: String(e) };
    }
  }

  function field(type, label, autocomplete) {
    const input = el("input");
    input.type = type;
    input.placeholder = label;
    input.setAttribute("aria-label", label);
    input.autocomplete = autocomplete;
    input.maxLength = 256;
    return input;
  }

  function status() {
    const p = el("p", "int-note");
    p.hidden = true;
    return {
      node: p,
      // a failure is announced at once, anything else politely
      show(text, bad) {
        p.hidden = !text;
        p.textContent = text || "";
        p.classList.toggle("bad", !!bad);
        p.setAttribute("role", bad ? "alert" : "status");
      },
    };
  }

  // ---- your own password ------------------------------------------------

  function buildAccount() {
    const form = el("form", "int-form");
    const current = field("password", "Current password", "current-password");
    const next = field("password", "New password (8 or more characters)", "new-password");
    const again = field("password", "New password again", "new-password");
    const save = el("button", "btn primary", "Change password");
    save.type = "submit";
    const msg = status();
    form.append(current, next, again, save);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (next.value !== again.value) { msg.show("The new passwords do not match.", true); return; }
      const body = { current: current.value, new: next.value };
      current.value = next.value = again.value = "";
      save.disabled = true;
      const res = await call("/api/account/password", "POST", body);
      save.disabled = false;
      msg.show(res.error || "Password changed.", !!res.error);
    });
    accountBox.replaceChildren(form, msg.node);
  }

  // ---- users (admin) ----------------------------------------------------

  const ROLE_TEXT = { admin: "Admin", operator: "Operator" };

  function roleSelect(value) {
    const s = el("select");
    for (const r of ["admin", "operator"]) {
      const o = el("option", "", ROLE_TEXT[r]);
      o.value = r;
      s.append(o);
    }
    s.value = value;
    return s;
  }

  function userRow(u, me, msg, reload) {
    const row = el("li", "user-row");
    row.append(el("span", "user-name", u.name + (u.name === me ? " (you)" : "")));
    const role = roleSelect(u.role);
    role.setAttribute("aria-label", `Role of ${u.name}`);
    role.addEventListener("change", async () => {
      const res = await call(`/api/users/${encodeURIComponent(u.name)}`, "PUT", { role: role.value });
      msg.show(res.error || `${u.name} is now ${ROLE_TEXT[role.value].toLowerCase()}.`, !!res.error);
      reload();
    });
    const reset = el("button", "mini", "Reset password");
    reset.type = "button";
    reset.addEventListener("click", () => {
      row.querySelector("form")?.remove(); // pressing the button again replaces the form
      const pw = field("password", `New password for ${u.name}`, "new-password");
      const ok = el("button", "mini primary", "Save");
      ok.type = "submit";
      const f = el("form", "int-form");
      f.append(pw, ok);
      f.addEventListener("submit", async (e) => {
        e.preventDefault();
        const body = { password: pw.value };
        pw.value = "";
        const res = await call(`/api/users/${encodeURIComponent(u.name)}`, "PUT", body);
        msg.show(res.error || `Password of ${u.name} reset.`, !!res.error);
        if (!res.error) f.remove();
      });
      row.append(f);
      pw.focus();
    });
    const remove = el("button", "mini", "Remove");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!confirm(`Remove ${u.name}? Their layout is deleted too.`)) return;
      const res = await call(`/api/users/${encodeURIComponent(u.name)}`, "DELETE");
      msg.show(res.error || `${u.name} removed.`, !!res.error);
      reload();
    });
    if (readOnlyMode()) for (const b of [role, reset, remove]) { b.disabled = true; b.title = "Ratatosk is read-only"; }
    row.append(role, reset, remove);
    return row;
  }

  function addForm(msg, reload) {
    const form = el("form", "int-form");
    const name = field("text", "Username", "off");
    name.maxLength = 32;
    const pw = field("password", "Password (8 or more characters)", "new-password");
    const role = roleSelect("operator");
    role.setAttribute("aria-label", "Role");
    const add = el("button", "btn primary", "Add user");
    add.type = "submit";
    form.append(name, pw, role, add);
    if (readOnlyMode()) for (const b of [name, pw, role, add]) { b.disabled = true; b.title = "Ratatosk is read-only"; }
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { username: name.value.trim(), password: pw.value, role: role.value };
      pw.value = "";
      const res = await call("/api/users", "POST", body);
      msg.show(res.error || `${body.username} added.`, !!res.error);
      if (!res.error) name.value = "";
      reload();
    });
    return form;
  }

  let kept = ""; // the last message survives the reload that follows a change
  async function loadUsers() {
    const res = await call("/api/users", "GET");
    const msg = status();
    if (kept) msg.show(kept.text, kept.bad);
    const wrap = { show(t, b) { kept = t ? { text: t, bad: b } : ""; msg.show(t, b); } };
    if (res.error) { usersBox.replaceChildren(el("p", "int-note bad", res.error)); return; }
    const list = el("ul", "user-list");
    for (const u of res.items) list.append(userRow(u, res.me, wrap, loadUsers));
    usersBox.replaceChildren(list, addForm(wrap, loadUsers), msg.node);
  }

  buildAccount();
  document.getElementById("settings-btn").addEventListener("click", () => {
    kept = "";
    if (usersBox) loadUsers();
  });
})();
