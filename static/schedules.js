// The Schedules section of the Settings dialog (admins), and the form the container row's
// "Schedule…" dialog shares (window.scheduleForm, used by app.js). Plain DOM, text goes in with
// textContent only. The server owns the maths and every check; this only asks and shows.

(function () {
  const box = document.getElementById("schedules-box"); // only in the page for an admin
  const readOnlyMode = () => document.body.dataset.readOnly === "true";
  const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const ACTION_TEXT = { restart: "Restart", update: "Update", auto: "Update/restart" };
  const KINDS = ["auto", "update", "restart"]; // the boxes of a row, left to right
  const FREQ_TEXT = { daily: "Every day", weekly: "Every week", monthly: "Every month" };

  async function call(url, method, body) {
    try {
      return await api(url, { method, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      return { error: String(e) };
    }
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

  function select(options, value, label) {
    const s = el("select");
    for (const [v, text] of options) {
      const o = el("option", "", text);
      o.value = v;
      s.append(o);
    }
    s.value = value;
    s.setAttribute("aria-label", label);
    return s;
  }

  function describe(it) {
    if (it.freq === "weekly") return `every ${WEEKDAYS[it.weekday]} at ${it.time}`;
    if (it.freq === "monthly") return `on day ${it.monthday} of every month at ${it.time}`;
    return `every day at ${it.time}`;
  }

  // Container choices as [name, label, is a Docker Compose container]: what the page already
  // holds, else ask the server. Ratatosk's own container is left out (it can't be scheduled).
  async function containerOptions() {
    let list = typeof containers !== "undefined" ? containers : [];
    if (!list.length) {
      const res = await call("/api/containers", "GET");
      list = res.items || [];
    }
    return list
      .filter((c) => !c.self)
      .map((c) => {
        const shown = typeof displayName === "function" ? displayName(c) : c.name;
        return [c.name, shown === c.name ? c.name : `${shown} (${c.name})`, !!c.project];
      })
      .sort((a, b) => a[1].localeCompare(b[1]));
  }

  // The containers of a new schedule: one row each with three boxes, "Update/restart" (update if a
  // newer image exists, else restart), "Update" (only if one exists) and "Restart". At most one is
  // ticked per row, and a container with none is left out. The ticked ones are listed below in the
  // order they will run (the order they were ticked in), with buttons to move each. get() is the
  // [{name, action}] in run order. Only Restart works for a container that is not a Compose one.
  function containerPicker(names, updateCheck) {
    const labels = new Map(names.map(([value, text]) => [value, text]));
    let chosen = [];
    let locked = false;
    const node = el("div", "sched-picks");
    const fieldset = el("fieldset", "sched-checks");
    fieldset.append(el("legend", "", "Containers"));
    const head = el("div", "sched-pickrow sched-pickhead");
    for (const kind of KINDS) head.append(el("span", "sched-col", ACTION_TEXT[kind]));
    head.append(el("span", ""));
    fieldset.append(head);
    const rows = new Map();
    function tick(name, kind) {
      const boxes = rows.get(name);
      for (const k of KINDS) boxes[k].checked = k === kind && boxes[kind].checked;
      const on = boxes[kind].checked;
      if (!on) chosen = chosen.filter((e) => e.name !== name);
      else if (chosen.some((e) => e.name === name)) chosen = chosen.map((e) => (e.name === name ? { name, action: kind } : e));
      else chosen = [...chosen, { name, action: kind }];
      draw();
    }
    for (const [value, text, compose] of names) {
      const row = el("div", "sched-pickrow");
      const boxes = {};
      for (const kind of KINDS) {
        const box = el("input");
        box.type = "checkbox";
        box.setAttribute("aria-label", `${value}: ${ACTION_TEXT[kind].toLowerCase()}`);
        if (kind !== "restart" && !(compose && updateCheck)) {
          box.dataset.cannot = "1";
          box.title = compose ? "Updates are switched off" : "Only Docker Compose containers can be updated";
        }
        box.addEventListener("change", () => tick(value, kind));
        boxes[kind] = box;
        row.append(box);
      }
      row.append(el("span", "sched-name", text));
      rows.set(value, boxes);
      fieldset.append(row);
    }
    if (!names.length) fieldset.append(el("p", "int-note", "No containers to choose from."));
    const order = el("div", "sched-order");
    const list = el("ol", "sched-chosen");
    order.append(el("p", "int-note", "Runs in this order, one after another:"), list);
    node.append(fieldset, order);

    function draw() {
      if (!chosen.length) {
        // the box is always there at the same size, so ticking never moves the rest of the dialog
        list.replaceChildren(el("li", "sched-placeholder", "Nothing ticked yet."));
      } else list.replaceChildren(...chosen.map((e, i) => {
        const li = el("li", "");
        const what = el("span", "sched-name", labels.get(e.name) || e.name);
        what.append(el("small", "", ACTION_TEXT[e.action]));
        li.append(what);
        for (const [text, word, step] of [["↑", "earlier", -1], ["↓", "later", 1]]) {
          const b = el("button", "mini", text);
          b.type = "button";
          b.setAttribute("aria-label", `Move ${e.name} ${word}`);
          b.disabled = locked || i + step < 0 || i + step >= chosen.length;
          b.addEventListener("click", () => { chosen = window.EditLogic.moveItem(chosen, i, step); draw(); });
          li.append(b);
        }
        return li;
      }));
      for (const boxes of rows.values()) for (const box of Object.values(boxes)) box.disabled = locked || !!box.dataset.cannot;
    }
    draw();
    return { node, get: () => chosen.map((e) => ({ ...e })), lock(flag) { locked = flag; draw(); } };
  }

  // The add form. opts: container (a fixed container name, with a choice of action; else a picker
  // for several containers is shown), compose (whether that container can be updated), names (the
  // picker's [name, label, compose] rows), updateCheck, readOnly, note (a line above the fields),
  // onDone(text) after a schedule was saved.
  function scheduleForm(opts) {
    const form = el("form", "sched-form");
    const msg = status();
    const picker = opts.container ? null : containerPicker(opts.names || [], opts.updateCheck);
    const action = opts.container ? select(KINDS.slice().reverse().map((k) => [k, ACTION_TEXT[k]]), "restart", "Action") : null;
    const freq = select(Object.entries(FREQ_TEXT), "daily", "Repeat");
    const weekday = select(WEEKDAYS.map((d, i) => [String(i), d]), "0", "Weekday");
    const monthday = select(Array.from({ length: 31 }, (_, i) => [String(i + 1), `Day ${i + 1}`]), "1", "Day of the month");
    const time = el("input");
    time.type = "time";
    time.value = "03:00";
    time.required = true;
    time.setAttribute("aria-label", "Time");
    const add = el("button", "btn primary", "Schedule");
    add.type = "submit";
    const monthNote = el("p", "int-note", "The 31st (or any day a month doesn't have) means the last day of that month.");

    if (action) {
      // restart, update, update/restart (in that order in the list)
      for (const o of action.options) {
        if (o.value !== "restart" && !(opts.compose && opts.updateCheck)) {
          o.disabled = true;
          o.title = opts.compose ? "Updates are switched off" : "Only Docker Compose containers can be updated";
        }
      }
    }
    const sync = () => {
      weekday.hidden = freq.value !== "weekly";
      monthday.hidden = monthNote.hidden = freq.value !== "monthly";
    };
    freq.addEventListener("change", sync);
    sync();

    if (opts.note) form.append(el("p", "int-note sched-wide", opts.note));
    if (picker) form.append(picker.node);
    form.append(...[action, freq, weekday, monthday, el("span", "sched-at", "at"), time, add].filter(Boolean), monthNote, msg.node);
    if (opts.readOnly || readOnlyMode()) {
      for (const b of [action, freq, weekday, monthday, time, add].filter(Boolean)) { b.disabled = true; b.title = "Ratatosk is read-only"; }
      picker?.lock(true);
    }
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!time.value) { msg.show("Choose a time.", true); return; }
      const body = { containers: opts.container ? [{ name: opts.container, action: action.value }] : picker.get(), freq: freq.value, time: time.value };
      if (!body.containers.length) { msg.show("Choose at least one container.", true); return; }
      if (freq.value === "weekly") body.weekday = Number(weekday.value);
      if (freq.value === "monthly") body.monthday = Number(monthday.value);
      add.disabled = true;
      const res = await call("/api/schedules", "POST", body);
      add.disabled = false;
      if (res.error) { msg.show(res.error, true); return; }
      const what = body.containers.length === 1
        ? `${ACTION_TEXT[body.containers[0].action].toLowerCase()} ${body.containers[0].name}`
        : body.containers.map((c) => `${c.name} (${ACTION_TEXT[c.action].toLowerCase()})`).join(", ");
      opts.onDone?.(`Scheduled: ${what} ${describe(body)}.`);
    });
    return form;
  }
  window.scheduleForm = scheduleForm;

  function scheduleRow(it, msg, reload) {
    const row = el("li", "sched-row");
    const what = el("span", "sched-what");
    const title = window.EditLogic.scheduleTitle(it.containers, ACTION_TEXT);
    what.append(el("span", "", title));
    if (it.containers.length > 1) {
      // every container's action is named when they differ
      const same = new Set(it.containers.map((c) => c.action)).size === 1;
      what.append(el("small", "", it.containers.map((c) => (same ? c.name : `${c.name} (${ACTION_TEXT[c.action].toLowerCase()})`)).join(" → ")));
    }
    what.append(el("small", "", describe(it)));
    const when = it.enabled ? (it.next_run ? `Next: ${it.next_run}` : "") : "Paused";
    if (when) what.append(el("small", "", when));
    if (it.last_result) what.append(el("small", "", `Last: ${it.last_run ? it.last_run + " – " : ""}${it.last_result}`));
    if (!it.container_exists) {
      const rest = it.containers.length > it.missing.length;
      what.append(el("small", "sched-gone", it.containers.length === 1
        ? "This container no longer exists, so nothing runs."
        : `No longer exists: ${it.missing.join(", ")}.${rest ? " The others still run." : " So nothing runs."}`));
    }

    const on = el("input");
    on.type = "checkbox";
    on.checked = it.enabled;
    on.setAttribute("aria-label", `Enabled: ${title.toLowerCase()}`);
    on.addEventListener("change", async () => {
      const res = await call(`/api/schedules/${encodeURIComponent(it.id)}`, "PUT", { enabled: on.checked });
      msg.show(res.error || (on.checked ? "Schedule on." : "Schedule paused."), !!res.error);
      reload();
    });
    const toggle = el("label", "sched-on");
    toggle.append(on, el("span", "", "On"));
    const remove = el("button", "mini", "Delete");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!confirm(`Delete the schedule "${title.toLowerCase()}, ${describe(it)}"?`)) return;
      const res = await call(`/api/schedules/${encodeURIComponent(it.id)}`, "DELETE");
      msg.show(res.error || "Schedule deleted.", !!res.error);
      reload();
    });
    if (readOnlyMode()) for (const b of [on, remove]) { b.disabled = true; b.title = "Ratatosk is read-only"; }
    row.append(what, toggle, remove);
    return row;
  }

  // The timezone is found by typing ("hel" finds Europe/Helsinki) and picked from the matches, with
  // the arrow keys and Enter or the mouse. The zone names come from the server, so only a real one
  // can be chosen; the list is in the page flow (not a popup) so the scrolling dialog can't clip it.
  const TZ_SHOWN = 50;
  function timezoneField(res, msg, reload) {
    const field = el("div", "set-field sched-tz");
    const text = el("span", "", "Timezone");
    text.append(el("small", "", `Times below are in this zone. It is ${res.now} there now.`));
    const label = el("label", "");
    label.htmlFor = "tz-input";
    label.append(text);

    const input = el("input");
    input.id = "tz-input";
    input.type = "text";
    input.value = res.timezone;
    input.placeholder = "Search, e.g. Helsinki";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", "tz-list");
    const list = el("ul", "sched-tzlist");
    list.id = "tz-list";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Timezones");
    list.hidden = true;
    const box = el("div", "sched-tzbox");
    box.append(input, list);

    let shown = [];
    let active = 0;
    const close = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      input.value = res.timezone; // leaving without choosing puts the current zone back
    };
    const mark = () => {
      [...list.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === active)));
      const li = list.children[active];
      if (li && shown.length) {
        input.setAttribute("aria-activedescendant", li.id);
        li.scrollIntoView?.({ block: "nearest" });
      }
    };
    const open = () => {
      // the box shows the current zone until something is typed; then it is the search
      const q = input.value === res.timezone ? "" : input.value;
      shown = window.EditLogic.tzMatches(res.timezones, q, TZ_SHOWN);
      // nothing typed: the current zone comes first and is the active one, so Enter changes nothing
      if (!q) shown = [res.timezone, ...shown.filter((n) => n !== res.timezone)].slice(0, TZ_SHOWN);
      const rows = shown.map((name, i) => {
        const li = el("li", "", name);
        li.id = `tz-opt-${i}`;
        li.setAttribute("role", "option");
        if (name === res.timezone) li.classList.add("current");
        return li;
      });
      const total = window.EditLogic.tzMatches(res.timezones, q, Infinity).length;
      if (!shown.length) rows.push(el("li", "sched-tzhint", "No timezone matches."));
      else if (total > shown.length) rows.push(el("li", "sched-tzhint", `${total - shown.length} more, keep typing to narrow it down.`));
      list.replaceChildren(...rows);
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = 0;
      mark();
    };
    const choose = async (name) => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      if (name === res.timezone) { input.value = name; return; }
      input.disabled = true;
      const r = await call("/api/schedules/timezone", "PUT", { timezone: name });
      msg.show(r.error || `Timezone is now ${name}.`, !!r.error);
      reload();
    };

    input.addEventListener("focus", () => { input.select(); open(); });
    input.addEventListener("input", open);
    input.addEventListener("blur", close);
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (list.hidden) { open(); return; }
        if (!shown.length) return;
        active = (active + (e.key === "ArrowDown" ? 1 : shown.length - 1)) % shown.length;
        mark();
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (!list.hidden && shown[active]) choose(shown[active]);
      } else if (e.key === "Escape" && !list.hidden) {
        e.preventDefault();
        e.stopPropagation(); // closes the list, not the Settings dialog behind it
        close();
      }
    });
    // mousedown, not click: the input must not lose focus first, or blur would close the list
    list.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const i = [...list.children].indexOf(e.target.closest("li"));
      if (i >= 0 && shown[i]) choose(shown[i]);
    });

    if (res.read_only || readOnlyMode()) { input.disabled = true; input.title = "Ratatosk is read-only"; }
    field.append(label, box);
    return field;
  }

  // "Schedule update/restart": the form in a dialog, one row per container with its three boxes.
  async function scheduleDialog(info, onSaved) {
    const dlg = el("dialog", "cfg narrow");
    const title = el("h2", null, "Schedule update/restart");
    title.id = "upd-sched-title";
    dlg.setAttribute("aria-labelledby", title.id);
    const head = el("div", "cfg-head");
    const close = el("button", "mini", "Close");
    close.type = "button";
    close.addEventListener("click", () => dlg.close());
    head.append(title, close);
    const body = el("div", "cfg-body");
    dlg.append(head, body);
    dlg.addEventListener("close", () => dlg.remove());
    body.append(el("p", "hint", "Loading…"));
    document.body.append(dlg);
    dlg.showModal();
    const names = await containerOptions();
    body.replaceChildren(scheduleForm({
      names,
      updateCheck: info.update_check,
      readOnly: info.read_only,
      note: `Update/restart updates a container if a newer image exists, and restarts it if not. Update does nothing without a newer image. Restart only restarts. Times are in ${info.timezone} (it is ${info.now} there now).`,
      onDone: (text) => { dlg.close(); onSaved(text); },
    }));
  }

  let kept = ""; // the last message survives the reload that follows a change
  async function loadSchedules() {
    const res = await call("/api/schedules", "GET");
    const msg = status();
    if (kept) msg.show(kept.text, kept.bad);
    const wrap = { show(t, b) { kept = t ? { text: t, bad: b } : ""; msg.show(t, b); } };
    if (res.error) { box.replaceChildren(el("p", "int-note bad", res.error)); return; }
    const parts = [timezoneField(res, wrap, loadSchedules)];
    const regular = el("button", "btn primary", "Schedule update/restart");
    regular.type = "button";
    regular.addEventListener("click", () => scheduleDialog(res, (text) => { kept = { text, bad: false }; loadSchedules(); }));
    if (res.read_only || readOnlyMode()) { regular.disabled = true; regular.title = "Ratatosk is read-only"; }
    parts.push(el("div", "sched-actions"));
    parts[1].append(regular);
    if (res.items.length) {
      const list = el("ul", "sched-list");
      for (const it of res.items) list.append(scheduleRow(it, wrap, loadSchedules));
      parts.push(list);
    } else {
      parts.push(el("p", "hint", "Nothing scheduled."));
    }
    parts.push(msg.node);
    box.replaceChildren(...parts);
  }

  if (box) {
    document.getElementById("settings-btn").addEventListener("click", () => {
      kept = "";
      loadSchedules();
    });
  }
})();
