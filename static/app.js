const csrf = document.body.dataset.csrf;
const UNGROUPED = "Ungrouped";
const LOG_REFRESH_MS = 3000;
// while dragging, the page scrolls when the pointer is within this many px of the
// top or bottom of the viewport, up to this many px per frame at the very edge
const DRAG_SCROLL_EDGE = 90;
const DRAG_SCROLL_MAX = 18;
// on touch screens a drag starts after pressing and holding, so a quick swipe
// still scrolls the page; moving more than the slop first cancels the hold
const LONG_PRESS_MS = 250;
const LONG_PRESS_SLOP = 8;
const MOUSE_DRAG_SLOP = 5;
// the strip of status lights in a group's header stops at this many containers
const MAX_LIGHTS = 24;

const frame = document.getElementById("frame");
const boardEl = document.getElementById("board");
const summaryEl = document.getElementById("summary");
const noticesEl = document.getElementById("notices");
const shelfEl = document.getElementById("shelf");
const editBar = document.getElementById("edit-bar");
const errorEl = document.getElementById("error");
const toggleAllBtn = document.getElementById("toggle-all");
const groupByProject = document.getElementById("group-by-project");
const restoreBtn = document.getElementById("restore-integrations");
const editBtn = document.getElementById("edit-layout");
const addBtn = document.getElementById("add-group");
const noDismissed = document.getElementById("no-dismissed");
const settingsBtn = document.getElementById("settings-btn");
const settingsDlg = document.getElementById("settings");

let containers = [];
let readOnly = false;
// "admin" or "operator"; only admins find, enter or remove app keys
let role = document.body.dataset.role;
const keyLock = () => (readOnly ? "Ratatosk is read-only" : role !== "admin" ? "Only an admin can change app keys" : "");
let loaded = false; // the container list has arrived at least once
let listError = ""; // why the last container list call failed, if it did
// shared across devices: saved on the server
let groupSettings = {
  assignments: {}, custom_groups: [], on_demand_groups: [], critical_groups: [], container_priority: {},
  aliases: {}, group_order: [], hidden_links: [], link_groups: [], link_containers: [], disks: [], group_by_project: false, layout: { columns: {}, hidden: [] },
};
let editing = false;
let dragging = false;
// container name -> {el, update(c), dispose()}; reused across renders so an open
// logs drawer survives the page being re-grouped or refreshed
const items = new Map();
// panel id -> panel; reused across renders so a half-typed key or an open drawer survives
// the panel being moved or the page being refreshed
const panels = new Map();
let shownColumns = []; // the panel ids in each visible column, as last placed
let columnsN = 0;

// How often the page asks the server, in seconds: the container list (with the host vitals) and
// the integration panels are set separately.
const LIST_RATES = [[2, "Every 2 seconds"], [5, "Every 5 seconds"], [15, "Every 15 seconds"], [60, "Every minute"]];
const PANEL_RATES = [[10, "Every 10 seconds"], [30, "Every 30 seconds"], [60, "Every minute"], [180, "Every 3 minutes"]];
const DEFAULT_RATES = { listRefresh: 5, panelRefresh: 30 };
// older versions kept one preset for both
const OLD_PRESETS = { fast: [2, 10], normal: [5, 30], relaxed: [15, 60], slow: [60, 180] };
// per-browser view state. Collapsed panels are kept by panel id; older versions kept
// groups by bare name.
const view = (() => {
  const defaults = { collapsed: {}, maxColumns: 3, ...DEFAULT_RATES };
  try {
    const stored = JSON.parse(localStorage.getItem("view") || "{}");
    const v = { ...defaults, ...stored };
    const collapsed = {};
    for (const [k, on] of Object.entries(v.collapsed || {})) collapsed[/^(host|links|group:|int:)/.test(k) ? k : "group:" + k] = on;
    const maxColumns = Number.isInteger(v.maxColumns) && v.maxColumns >= 1 && v.maxColumns <= 5 ? v.maxColumns : defaults.maxColumns;
    const old = OLD_PRESETS[stored.refresh] || [];
    const rate = (key, rates, i) => {
      const n = rates.some(([s]) => s === stored[key]) ? stored[key] : old[i];
      return rates.some(([s]) => s === n) ? n : defaults[key];
    };
    return { collapsed, maxColumns, listRefresh: rate("listRefresh", LIST_RATES, 0), panelRefresh: rate("panelRefresh", PANEL_RATES, 1) };
  } catch { return defaults; }
})();
function saveView() { try { localStorage.setItem("view", JSON.stringify(view)); } catch {} }

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

const ICONS = {
  chevron: '<path d="m6 9 6 6 6-6"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  grip: '<g fill="currentColor" stroke="none"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></g>',
  cog: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  more: '<g fill="currentColor" stroke="none"><circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="12" cy="19" r="1.7"/></g>',
};
function icon(name, size = 16) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("width", size);
  s.setAttribute("height", size);
  s.setAttribute("aria-hidden", "true");
  s.setAttribute("class", "icon");
  s.innerHTML = ICONS[name]; // fixed strings above, never user data
  return s;
}

function showError(msg) {
  errorEl.textContent = msg || "";
  errorEl.hidden = !msg;
}

// What stays in the banner until it is dealt with, so a poll can't wipe it a moment later:
// a layout save that was refused (until the next one works) and the last failed action or edit.
const sticky = { save: "", action: "" };
function setError(kind, msg) {
  sticky[kind] = msg;
  showErrors();
}
function showErrors() { showError(Object.values(sticky).filter(Boolean).join(" ")); }

function fmtBytes(b) {
  if (b === null || b === undefined) return "–";
  if (b >= 1024 ** 4) return (b / 1024 ** 4).toFixed(2) + " TB";
  if (b >= 1024 ** 3) return (b / 1024 ** 3).toFixed(1) + " GB";
  return Math.round(b / 1024 ** 2) + " MB";
}

// Every state-changing call carries the CSRF token; a string body is sent as JSON.
async function api(url, options = {}) {
  const headers = { ...options.headers };
  if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = csrf;
  if (typeof options.body === "string") headers["Content-Type"] = "application/json";
  const res = await fetch(url, { ...options, headers });
  if (res.redirected || res.status === 401) {
    location.reload();
    throw new Error("session expired");
  }
  return res.json();
}

// ---- groups -------------------------------------------------------------

// Priority says how much it matters when a container is down:
//   critical:  down turns the overall light red
//   wanted:    down turns it amber (the default)
//   on_demand: meant to be stopped sometimes (game servers): left out of the overall count and
//              a stopped one is grey
// A group has a default and a container can override it.
const PRIORITY_TEXT = { critical: "Critical", wanted: "Wanted", on_demand: "On demand" };
function groupPriority(name) {
  return groupSettings.on_demand_groups.includes(name) ? "on_demand" : groupSettings.critical_groups.includes(name) ? "critical" : "wanted";
}
function priorityOf(c) { return groupSettings.container_priority[c.name] || groupPriority(groupOf(c)); }
function setGroupPriority(name, level) {
  const without = (list) => list.filter((g) => g !== name);
  groupSettings.on_demand_groups = level === "on_demand" ? [...without(groupSettings.on_demand_groups), name] : without(groupSettings.on_demand_groups);
  groupSettings.critical_groups = level === "critical" ? [...without(groupSettings.critical_groups), name] : without(groupSettings.critical_groups);
}
function setContainerPriority(name, level) {
  if (level) groupSettings.container_priority[name] = level;
  else delete groupSettings.container_priority[name];
}

// A friendly name set in edit mode, else the container's `ratatosk.name` label.
// Display-only: the container keeps its real name, which is still what every API call
// uses.
// A game server's real name comes from Pelican, if that is set up.
function displayName(c) { return groupSettings.aliases[c.name] || c.label_name || c.game?.name || c.name; }

// Only groups the user turned "Generate links" on for get tiles; the default is none.
function hasLinks(name) { return groupSettings.link_groups.includes(name); }
// A container can override its group: "show" (a tile although the group has none) or "hide".
function linkMode(c) {
  return groupSettings.hidden_links.includes(c.name) ? "hide" : groupSettings.link_containers.includes(c.name) ? "show" : "inherit";
}
function setLinkMode(name, mode) {
  groupSettings.hidden_links = groupSettings.hidden_links.filter((n) => n !== name);
  groupSettings.link_containers = groupSettings.link_containers.filter((n) => n !== name);
  if (mode === "hide") groupSettings.hidden_links.push(name);
  if (mode === "show") groupSettings.link_containers.push(name);
}

// Where a container sits unless the user placed it: its Compose project's group, or Ungrouped
// (which is where everything starts unless the account has "Group by Compose project" switched on).
function defaultGroup(c) { return (groupSettings.group_by_project === true && c.project) || UNGROUPED; }
function groupOf(c) { return groupSettings.assignments[c.name] || defaultGroup(c); }

// The groups that exist, in the order a first layout deals them out: the user's saved
// `group_order` (older versions), then any other group alphabetically, Ungrouped last.
// Ungrouped only exists while something is in it. Where a group sits on the page now
// comes from the panel layout.
function allGroupNames() {
  const names = [];
  const add = (n) => { if (!names.includes(n)) names.push(n); };
  containers.forEach((c) => add(groupOf(c)));
  groupSettings.custom_groups.forEach(add);
  const hasUngrouped = containers.some((c) => groupOf(c) === UNGROUPED);
  const present = names.filter((n) => n !== UNGROUPED || hasUngrouped);
  const ordered = groupSettings.group_order.filter((n) => present.includes(n));
  const rest = present
    .filter((n) => !ordered.includes(n))
    .sort((a, b) => (a === UNGROUPED) - (b === UNGROUPED) || a.localeCompare(b));
  return [...ordered, ...rest];
}

const byShownName = (a, b) => displayName(a).localeCompare(displayName(b));
function groupList() {
  return allGroupNames().map((name) => [name, containers.filter((c) => groupOf(c) === name).sort(byShownName)]);
}

function uniqueGroupName() {
  let name = "New group";
  for (let i = 2; allGroupNames().includes(name); i++) name = "New group " + i;
  return name;
}

// A new group has no place in the layout yet, so it lands in the shortest column.
function addGroup() {
  const name = uniqueGroupName();
  groupSettings.custom_groups.push(name);
  return name;
}

// ---- panel layout -------------------------------------------------------

// [minimum window width in px, columns that fit]; the user's maximum caps the result
const FIT = [[1860, 5], [1480, 4], [1100, 3], [720, 2]].map(([px, n]) => [window.matchMedia(`(min-width: ${px}px)`), n]);
function columnCount() {
  const fits = FIT.find(([mq]) => mq.matches);
  return Math.min(view.maxColumns, fits ? fits[1] : 1);
}
for (const [mq] of FIT) mq.addEventListener("change", () => render(false));

// Panel ids that may stay in the saved layout. Stale ones (a deleted group, an app that
// is gone) are dropped on save so they can't pile up toward the server's limit; nothing
// is dropped before the data they depend on has arrived.
let integrationsLoaded = false;
function isKnownPanel(id) {
  if (id === "host" || id === "links") return true;
  if (id.startsWith("group:")) return !loaded || allGroupNames().includes(id.slice(6));
  if (id === "found") return !integrationsLoaded || integrationData.items.some((i) => i.state !== "configured" && i.state !== "dismissed");
  if (id.startsWith("int:")) return !integrationsLoaded || integrationData.items.some((i) => "int:" + i.container === id);
  return false;
}

// Fix the arrangement shown now as this width's saved one, before a change that would
// otherwise shift the other panels (hiding one, say).
function pinColumns() {
  const columns = { ...groupSettings.layout.columns, [String(columnsN)]: shownColumns.map((c) => [...c]) };
  groupSettings.layout = { ...groupSettings.layout, columns };
}

function panelTitle(id) {
  if (id === "host") return "Host";
  if (id === "links") return "Links";
  if (id === "found") return "Apps found";
  if (id.startsWith("group:")) return id.slice(6);
  const item = integrationData.items.find((i) => "int:" + i.container === id);
  return item ? item.adapter_name : id;
}

// One save at a time, so edits can't land out of order: a save asked for while another runs
// becomes a single trailing one that sends the newest settings. A refused save keeps its message
// in the banner until a later save works.
let savesDone = 0;
const saveGroups = EditLogic.coalesce(async () => {
  let failure = "";
  try {
    const body = await api("/api/groups", { method: "PUT", body: JSON.stringify(groupSettings) });
    if (body.error) failure = "Saving groups: " + body.error;
  } catch (e) {
    failure = "Saving groups: " + e;
  }
  savesDone++;
  setError("save", failure);
});

// bumped on every local layout edit; a poll that started before an edit must not
// overwrite it with the older server copy
let localEdits = 0;

function changed() {
  localEdits++;
  groupSettings.layout = Layout.pruneLayout(groupSettings.layout, isKnownPanel);
  render(true);
  saveGroups();
}

function moveTo(c, target) {
  if (groupOf(c) === target) return;
  // moving a container back to its own default drops the override
  if (target === defaultGroup(c)) delete groupSettings.assignments[c.name];
  else groupSettings.assignments[c.name] = target;
  changed();
}

function renameGroup(oldName, newName) {
  newName = newName.trim();
  if (!newName || newName === oldName) { render(true); return; }
  // a name that is already a group would silently merge the two; the server also caps the length
  const taken = [UNGROUPED, ...allGroupNames(), ...groupSettings.group_order, ...groupSettings.custom_groups, ...containers.map((c) => c.project).filter(Boolean)];
  const problem = EditLogic.groupNameProblem(newName, oldName, taken);
  if (problem) { setError("action", problem); render(true); return; }
  setError("action", "");
  containers.filter((c) => groupOf(c) === oldName).forEach((c) => { groupSettings.assignments[c.name] = newName; });
  const swap = (list) => list.map((g) => (g === oldName ? newName : g));
  groupSettings.custom_groups = swap(groupSettings.custom_groups);
  groupSettings.on_demand_groups = swap(groupSettings.on_demand_groups);
  groupSettings.critical_groups = swap(groupSettings.critical_groups);
  groupSettings.group_order = swap(groupSettings.group_order);
  groupSettings.link_groups = swap(groupSettings.link_groups);
  groupSettings.layout = Layout.renameInLayout(groupSettings.layout, "group:" + oldName, "group:" + newName);
  const was = view.collapsed["group:" + oldName];
  if (was !== undefined) {
    view.collapsed["group:" + newName] = was;
    delete view.collapsed["group:" + oldName];
    saveView();
  }
  changed();
}

function deleteGroup(name, members) {
  members.forEach((c) => { delete groupSettings.assignments[c.name]; });
  const drop = (list) => list.filter((g) => g !== name);
  groupSettings.custom_groups = drop(groupSettings.custom_groups);
  groupSettings.on_demand_groups = drop(groupSettings.on_demand_groups);
  groupSettings.critical_groups = drop(groupSettings.critical_groups);
  groupSettings.group_order = drop(groupSettings.group_order);
  groupSettings.link_groups = drop(groupSettings.link_groups);
  groupSettings.layout = Layout.removeFromLayout(groupSettings.layout, "group:" + name);
  changed();
}

// ---- dragging (pointer events, so it works with mouse and touch) ---------

// Shared by dragging a container and dragging a panel: a ghost under the pointer,
// edge auto-scroll, and one set of pointer listeners. onMove(under, ev) is told which
// element is under the pointer, onDrop() runs on release (not on cancel), and
// onEnd() always runs last, to clear highlights.
function dragSession(e, { label, onMove, onDrop, onEnd }) {
  e.preventDefault();
  dragging = true;
  const ghost = el("div", "ghost", label);
  document.body.append(ghost);
  document.body.classList.add("is-dragging");
  let lastEv = e;
  let raf = 0;

  const place = (ev) => {
    lastEv = ev;
    ghost.style.left = ev.clientX + "px";
    ghost.style.top = ev.clientY + "px";
    onMove(document.elementFromPoint(ev.clientX, ev.clientY), ev);
  };
  // The drag surface has touch-action: none on touch screens, so a drag doesn't also
  // scroll the page; the page has to scroll itself when the pointer nears an edge,
  // or panels off-screen can't be reached.
  const autoscroll = () => {
    const y = lastEv.clientY;
    const h = window.innerHeight;
    let dy = 0;
    if (y < DRAG_SCROLL_EDGE) dy = -DRAG_SCROLL_MAX * (1 - Math.max(y, 0) / DRAG_SCROLL_EDGE);
    else if (y > h - DRAG_SCROLL_EDGE) dy = DRAG_SCROLL_MAX * (1 - Math.max(h - y, 0) / DRAG_SCROLL_EDGE);
    if (dy) {
      window.scrollBy(0, Math.sign(dy) * Math.ceil(Math.abs(dy)));
      place(lastEv); // the page moved under the pointer, so re-check what it's over
    }
    raf = requestAnimationFrame(autoscroll);
  };
  const finish = (ev) => {
    cancelAnimationFrame(raf);
    document.removeEventListener("pointermove", place);
    document.removeEventListener("pointerup", finish);
    document.removeEventListener("pointercancel", finish);
    ghost.remove();
    document.body.classList.remove("is-dragging");
    dragging = false;
    if (ev.type === "pointerup") onDrop();
    onEnd();
  };
  document.addEventListener("pointermove", place);
  document.addEventListener("pointerup", finish);
  document.addEventListener("pointercancel", finish);
  place(e);
  raf = requestAnimationFrame(autoscroll);
}

// Drag a container onto a group (or the "new group" zone).
function startRowDrag(name, wrap, e) {
  const c0 = containers.find((x) => x.name === name);
  let target = null;
  wrap.classList.add("dragging");
  dragSession(e, {
    label: c0 ? displayName(c0) : name,
    onMove(under) {
      const next = under && (under.closest(".group") || under.closest(".new-zone"));
      if (next === target) return;
      if (target) target.classList.remove("drop-target");
      target = next;
      if (target) target.classList.add("drop-target");
    },
    onDrop() {
      const c = containers.find((x) => x.name === name);
      if (!c || !target) return;
      if (target.classList.contains("new-zone")) moveTo(c, addGroup());
      else moveTo(c, target.dataset.group);
    },
    onEnd() {
      wrap.classList.remove("dragging");
      if (target) target.classList.remove("drop-target");
    },
  });
}

// Drag a whole panel to a column and position. Nothing resizes while this runs: the panel
// stays where it is, dimmed, and a bar shows where it will land.
function startPanelDrag(id, box, e) {
  const bar = el("div", "drop-bar");
  let target = null; // {col, index}
  box.classList.add("dragging");
  dragSession(e, {
    label: panelTitle(id),
    onMove(_under, ev) {
      const cols = [...boardEl.querySelectorAll(".col")];
      if (!cols.length) return;
      // the column is picked by x alone, so the gaps and empty columns work as targets too
      let col = cols.findIndex((c) => ev.clientX < c.getBoundingClientRect().right);
      if (col < 0) col = cols.length - 1;
      const kids = [...cols[col].children].filter((k) => k.classList.contains("panel") && k !== box);
      const index = kids.filter((k) => {
        const r = k.getBoundingClientRect();
        return ev.clientY > r.top + r.height / 2;
      }).length;
      if (target && target.col === col && target.index === index && bar.parentNode) return;
      target = { col, index };
      cols[col].insertBefore(bar, kids[index] || null);
    },
    onDrop() {
      if (!target) return;
      groupSettings.layout = Layout.moveInto(groupSettings.layout, columnsN, shownColumns, id, target.col, target.index);
      changed();
    },
    onEnd() {
      bar.remove();
      box.classList.remove("dragging");
    },
  });
}

// Mouse: drag once the pointer has moved a few pixels, so a plain click still works.
// Touch: only after a press-and-hold, so a swipe still scrolls the page.
function armDrag(e, start) {
  const touch = e.pointerType !== "mouse";
  if (!touch && e.button !== 0) return;
  const x0 = e.clientX;
  const y0 = e.clientY;
  let timer = 0;
  const cleanup = () => {
    clearTimeout(timer);
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", cleanup);
    document.removeEventListener("pointercancel", cleanup);
  };
  const move = (ev) => {
    if (Math.hypot(ev.clientX - x0, ev.clientY - y0) <= (touch ? LONG_PRESS_SLOP : MOUSE_DRAG_SLOP)) return;
    cleanup();
    // on a touch screen, moving before the hold finishes means the user is scrolling
    if (!touch) start(ev);
  };
  if (touch) {
    timer = setTimeout(() => {
      cleanup();
      navigator.vibrate?.(10);
      start(e);
    }, LONG_PRESS_MS);
  }
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", cleanup);
  document.addEventListener("pointercancel", cleanup);
}

// Registered once and up front (iOS ignores a touchmove blocker added mid-gesture):
// once a drag is live, finger movement must not also scroll the page.
document.addEventListener(
  "touchmove",
  (e) => { if (dragging && e.cancelable) e.preventDefault(); },
  { passive: false }
);

// Keyboard route for a container row: focus its grip, then up and down move it to the
// neighbouring group, in the order the groups read on the page.
function dragHandle(wrap) {
  const h = el("button", "handle");
  h.append(icon("grip", 18));
  h.type = "button";
  h.title = "Drag onto a group (or use the up and down arrow keys)";
  h.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    const name = wrap.dataset.name;
    const c = containers.find((x) => x.name === name);
    if (!c) return;
    const names = Layout.flatten(shownColumns).filter((id) => id.startsWith("group:")).map((id) => id.slice(6));
    const at = names.indexOf(groupOf(c));
    if (at < 0) return; // its group is not on the page (hidden), so there is no neighbour
    const i = at + (e.key === "ArrowDown" ? 1 : -1);
    if (i < 0 || i >= names.length) return;
    moveTo(c, names[i]);
    // the DOM was rebuilt; return keyboard focus to this container's handle
    items.get(name)?.el.querySelector(".handle")?.focus();
  });
  return h;
}

// ---- container rows -----------------------------------------------------

// A small button for a dialog; `fn` gets the button.
function miniButton(label, cls, fn) {
  const b = el("button", "mini" + (cls ? " " + cls : ""), label);
  b.type = "button";
  b.addEventListener("click", () => fn(b));
  return b;
}

async function act(name, action, btn) {
  if (editing) return;
  btn.disabled = true;
  setError("action", "");
  try {
    const body = await api(`/api/containers/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    if (body.error) setError("action", `${action} ${name}: ${body.error}`);
  } catch (e) {
    setError("action", `${action} ${name}: ${e}`);
  } finally {
    // a restart leaves the container "running" as before, so the row keeps this very button
    btn.disabled = false;
  }
  refresh();
  // a Pelican panel row shows the state too, and a power action takes a few seconds to show
  refreshIntegrations();
  setTimeout(refreshIntegrations, 3000);
  setTimeout(refreshIntegrations, 8000);
}

// Delete a stopped container: pick what goes with it, then confirm what exactly will happen.
// The server checks everything again (admin, stopped, not a Wings server, not Ratatosk itself).
function deleteContainer(c) {
  if (editing) return;
  const name = displayName(c);
  const dlg = el("dialog", "cfg narrow");
  const title = el("h2", null, `Delete ${name}`);
  title.id = "delete-title";
  dlg.setAttribute("aria-labelledby", title.id);
  const body = el("div", "cfg-body");
  const foot = el("div", "cfg-foot");
  const close = miniButton("Cancel", "", () => dlg.close());
  dlg.append(el("div", "cfg-head", undefined), body);
  dlg.firstChild.append(title);
  body.append(foot);
  let volumes = false;

  const choose = () => {
    body.replaceChildren(foot);
    const opts = [
      [false, "Delete the container only", "Like docker compose down. Its anonymous volumes stay on the host as unused storage."],
      [true, "Delete the container and its anonymous volumes", "Like docker compose down -v, for this one container. Anything kept in its unnamed volumes is lost."],
    ];
    const list = opts.map(([value, head, sub]) => {
      const label = el("label", "del-choice");
      const radio = el("input");
      radio.type = "radio";
      radio.name = "del-scope";
      radio.checked = volumes === value;
      radio.addEventListener("change", () => { volumes = value; });
      label.append(radio, el("span", null, head), el("small", null, sub));
      return label;
    });
    body.prepend(...list, el("p", "hint", "Named volumes and mounted folders are never deleted."));
    foot.replaceChildren(miniButton("Continue", "primary", confirmStep), close);
  };

  const confirmStep = () => {
    body.replaceChildren(foot);
    body.prepend(el("p", null, volumes
      ? `This will permanently delete the container ${c.name} and its anonymous volumes. Data stored in those volumes is lost. This can't be undone. Are you sure?`
      : `This will permanently delete the container ${c.name}. Its anonymous volumes are kept. This can't be undone. Are you sure?`));
    const yes = miniButton("Yes, delete", "danger", async () => {
      yes.disabled = true;
      setError("action", "");
      try {
        const res = await api(`/api/containers/${encodeURIComponent(c.name)}`, { method: "DELETE", body: JSON.stringify({ volumes }) });
        if (res.error) setError("action", `delete ${c.name}: ${res.error}`);
      } catch (e) {
        setError("action", `delete ${c.name}: ${e}`);
      }
      dlg.close();
      refresh();
    });
    foot.replaceChildren(yes, miniButton("Back", "", choose), close);
  };

  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  choose();
  dlg.showModal();
}

// Schedule a restart or update of one container. The form is the one Settings uses (schedules.js);
// the server checks everything again (admin, not Ratatosk itself, Compose for an update).
async function scheduleContainer(c) {
  if (editing) return;
  const dlg = el("dialog", "cfg narrow");
  const title = el("h2", null, `Schedule ${displayName(c)}`);
  title.id = "schedule-title";
  dlg.setAttribute("aria-labelledby", title.id);
  const body = el("div", "cfg-body");
  const head = el("div", "cfg-head");
  head.append(title, miniButton("Close", "", () => dlg.close()));
  dlg.append(head, body);
  body.append(el("p", "hint", "Loading…"));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  let info;
  try {
    info = await api("/api/schedules");
  } catch (e) {
    info = { error: String(e) };
  }
  if (info.error || !window.scheduleForm) {
    body.replaceChildren(el("p", "int-note bad", info.error || "The schedule form did not load."));
    return;
  }
  body.replaceChildren(
    window.scheduleForm({
      container: c.name,
      updateCheck: info.update_check,
      readOnly: info.read_only,
      note: `Times are in ${info.timezone} (it is ${info.now} there now). Change the zone and see every schedule under Settings → Schedules.`,
      onDone: (text) => {
        body.replaceChildren(el("p", "hint", text), miniButton("Done", "primary", () => dlg.close()));
      },
    })
  );
}

async function updateContainer(c, btn) {
  if (editing) return;
  const stopNote = c.status === "running" ? "" : "It is stopped now, and updating starts it.\n\n";
  const ok = confirm(
    `Update ${displayName(c)}?\n\n${stopNote}The new image is pulled and the container is recreated. ` +
    "Apps with a database may migrate it on first start, and that usually can't be undone, " +
    "so back up the app's data first."
  );
  if (!ok) return;
  btn.disabled = true;
  setError("action", "");
  try {
    const body = await api(`/api/containers/${encodeURIComponent(c.name)}/update`, { method: "POST" });
    if (body.error) setError("action", `update ${c.name}: ${body.error}`);
  } catch (e) {
    setError("action", `update ${c.name}: ${e}`);
  } finally {
    btn.disabled = false;
  }
  refresh();
}

function logLine(line) {
  const d = el("div");
  const i = line.indexOf(" ");
  const ts = i > 0 && /^\d{4}-\d\d-\d\dT/.test(line) ? line.slice(0, i) : "";
  const msg = ts ? line.slice(i + 1) : line;
  let cls = "";
  if (/\b(error|fatal|panic|critical)\b/i.test(msg)) cls = "err";
  else if (/\bwarn(ing)?\b/i.test(msg)) cls = "warn";
  if (ts) d.append(el("span", "ts", ts + " "));
  d.append(el("span", cls, msg));
  return d;
}

// A small labelled <select> for edit mode: entries are [value, text]. The label element is
// returned; its `select` is the control.
function optionSelect(label, entries, value) {
  const box = el("label", "mini-select");
  box.append(el("span", null, label));
  const sel = el("select");
  for (const [v, t] of entries) {
    const o = el("option", "", t);
    o.value = v;
    sel.append(o);
  }
  sel.value = value;
  box.append(sel);
  box.select = sel;
  return box;
}
const priorityEntries = (inherit) => [...(inherit ? [["", inherit]] : []), ...Object.entries(PRIORITY_TEXT)];

function makeItem(name) {
  const wrap = el("div", "item");
  wrap.dataset.name = name;
  const row = el("div", "row");
  row.append(dragHandle(wrap));
  row.addEventListener("pointerdown", (e) => {
    if (editing && !e.target.closest(".rename-btn, .rename-input")) armDrag(e, (ev) => startRowDrag(name, wrap, ev));
  });
  // a long press would otherwise open the browser's context menu mid-drag
  row.addEventListener("contextmenu", (e) => {
    if (editing && !e.target.closest(".rename-input")) e.preventDefault();
  });

  const dot = el("span", "dot");
  row.append(dot);

  const main = el("button", "row-main");
  main.type = "button";
  const info = el("div", "info");
  const nameEl = el("div", "name");
  const nameText = document.createTextNode("");
  const stateEl = el("small", "state");
  const updTag = el("small", "upd-tag", "update available");
  updTag.hidden = true;
  nameEl.append(nameText, stateEl, updTag);
  const metaEl = el("div", "meta");
  info.append(nameEl, metaEl);
  main.append(info);
  row.append(main);

  // edit mode only: set or clear a friendly name. The row's own buttons are inert
  // while editing, so this is a sibling of `main` (buttons can't nest).
  const renameBtn = el("button", "mini rename-btn", "Rename");
  renameBtn.type = "button";
  renameBtn.title = "Rename (display name only)";
  renameBtn.addEventListener("click", () => {
    const c = containers.find((x) => x.name === name);
    if (!c) return;
    const input = el("input", "rename-input row-rename");
    input.value = displayName(c);
    input.placeholder = c.name;
    input.maxLength = 64;
    input.setAttribute("aria-label", "Display name for " + c.name);
    main.hidden = true;
    renameBtn.hidden = true;
    row.insertBefore(input, main);
    input.focus();
    input.select();
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      input.remove();
      main.hidden = false;
      renameBtn.hidden = false;
      if (!commit) return;
      const value = input.value.trim();
      // empty, or the real name again, clears the friendly name
      if (!value || value === c.name) delete groupSettings.aliases[c.name];
      else groupSettings.aliases[c.name] = value;
      changed();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
  });
  row.append(renameBtn);

  const stats = el("div", "st");
  row.append(stats);

  const kebab = el("button", "kebab");
  kebab.type = "button";
  kebab.append(icon("more", 18));
  kebab.setAttribute("aria-expanded", "false");
  // the "more" button and a click on the row itself both show or hide the actions
  const toggleActs = () => {
    if (editing) return;
    acts.hidden = !acts.hidden;
    kebab.setAttribute("aria-expanded", String(!acts.hidden));
  };
  kebab.addEventListener("click", toggleActs);
  row.append(kebab);

  // the actions sit under the row until the "more" button is pressed
  const acts = el("div", "acts");
  acts.hidden = true;
  // update sits left of Logs; it is rebuilt with the other buttons, so it gets its own slot
  const updateSlot = el("span", "slot");
  acts.append(updateSlot);
  const logsBtn = el("button", "act logs-toggle", "Logs");
  logsBtn.type = "button";
  logsBtn.setAttribute("aria-expanded", "false");
  acts.append(logsBtn);
  const actionBtns = el("span", "slot");
  acts.append(actionBtns);
  // edit mode only: how much it matters when this container is down, and whether it gets a link
  // tile. Either can follow its group. The line sits outside `row`, so it never starts a drag.
  const opts = el("div", "row-opts");
  const prioBox = optionSelect("Priority", priorityEntries("Group"), "");
  const linkBox = optionSelect("Link tile", [["", "Group"], ["show", "Show"], ["hide", "Hide"]], "");
  prioBox.select.addEventListener("change", () => { setContainerPriority(name, prioBox.select.value); changed(); });
  linkBox.select.addEventListener("change", () => { setLinkMode(name, linkBox.select.value || "inherit"); changed(); });
  opts.append(prioBox, linkBox);
  wrap.append(row, opts, acts);

  // logs drawer
  let drawer = null;
  let timer = null;
  let pre = null;
  const loadLogs = async () => {
    try {
      const body = await api(`/api/containers/${encodeURIComponent(name)}/logs?tail=200`);
      if (!pre) return;
      const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      if (body.error) pre.replaceChildren(el("div", "", body.error));
      else pre.replaceChildren(...body.lines.map(logLine));
      if (stick) pre.scrollTop = pre.scrollHeight;
    } catch {}
  };
  const closeDrawer = () => {
    clearInterval(timer);
    timer = null;
    drawer?.remove();
    drawer = null;
    pre = null;
    logsBtn.setAttribute("aria-expanded", "false");
  };
  const openDrawer = () => {
    drawer = el("div", "drawer");
    const bar = el("div", "drawer-bar");
    const auto = el("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.addEventListener("change", () => {
      clearInterval(timer);
      timer = cb.checked ? setInterval(loadLogs, LOG_REFRESH_MS) : null;
    });
    auto.append(cb, "Auto-refresh");
    const refreshBtn = el("button", "", "Refresh");
    refreshBtn.type = "button";
    refreshBtn.addEventListener("click", loadLogs);
    bar.append(el("span", "", "Last 200 lines"), el("span", "spacer"), auto, refreshBtn);
    pre = el("pre", "log");
    drawer.append(bar, pre);
    wrap.append(drawer);
    logsBtn.setAttribute("aria-expanded", "true");
    loadLogs();
  };
  const toggle = () => {
    if (editing) return;
    if (drawer) closeDrawer();
    else openDrawer();
  };
  logsBtn.addEventListener("click", toggle);
  main.addEventListener("click", toggleActs);

  let actionKey = "";
  return {
    el: wrap,
    dispose() {
      closeDrawer();
      acts.hidden = true;
      kebab.setAttribute("aria-expanded", "false");
    },
    update(c) {
      const shownName = displayName(c);
      nameText.nodeValue = shownName + (c.self ? " (this instance)" : "");
      const bits = [c.image];
      if (c.game?.connect) bits.push(c.game.connect);
      else if (c.ports.length) bits.push("ports " + c.ports.join(", "));
      if (shownName !== c.name) bits.unshift(c.name);
      metaEl.textContent = bits.join(" · ");
      stats.textContent = (c.cpu === null || c.cpu === undefined ? "–" : c.cpu.toFixed(1) + "%") + " · " + fmtBytes(c.mem);
      let s = c.health && c.status === "running" ? c.health : c.status;
      // Wings knows more than Docker does: a server that is still saving before it stops is
      // "stopping", not just "running"
      const wings = c.game?.state;
      if (wings) s = { offline: "exited", starting: "restarting", stopping: "restarting" }[wings] || s;
      const upd = c.update;
      dot.className = "dot " + s;
      stateEl.title = "";
      if (upd?.state === "running") {
        dot.className = "dot restarting";
        s = "updating";
      } else if (upd?.state === "error") {
        s = "update failed";
        stateEl.title = upd.error || "";
      }
      stateEl.textContent = wings && s !== "update failed" && s !== "updating" ? wings : s;
      updTag.hidden = !(upd && upd.state !== "running" && !c.self);
      kebab.setAttribute("aria-label", "Actions for " + shownName);
      // "Group (Wanted)" shows what following the group means right now
      prioBox.select.options[0].textContent = `Group (${PRIORITY_TEXT[groupPriority(groupOf(c))]})`;
      linkBox.select.options[0].textContent = `Group (${hasLinks(groupOf(c)) ? "tile" : "no tile"})`;
      const wantPrio = groupSettings.container_priority[c.name] || "";
      const wantLink = linkMode(c) === "inherit" ? "" : linkMode(c);
      if (prioBox.select.value !== wantPrio) prioBox.select.value = wantPrio;
      if (linkBox.select.value !== wantLink) linkBox.select.value = wantLink;

      // only rebuild the buttons when they'd actually differ, so a hover or
      // focus isn't lost to every poll
      const running = wings ? wings !== "offline" : c.status === "running";
      const canDelete = role === "admin" && !c.game && !c.self && ["exited", "created", "dead"].includes(c.status);
      const canUpdate = role === "admin" && upd && upd.state !== "running" && !c.self; // updating is an admin action
      const canSchedule = role === "admin" && !c.self;
      const key = [running, c.self, readOnly, upd?.state, upd?.blocked, canDelete, canUpdate, canSchedule].join("|");
      if (key === actionKey) return;
      actionKey = key;
      actionBtns.replaceChildren();
      updateSlot.replaceChildren();
      if (readOnly) return;
      if (canUpdate) {
        const b = el("button", "act update", "Update");
        b.type = "button";
        if (upd.blocked) {
          b.disabled = true;
          b.title = "Update available, but: " + upd.blocked;
        } else {
          b.title = "A newer image is available";
          b.addEventListener("click", () => updateContainer(c, b));
        }
        updateSlot.append(b);
      }
      for (const a of running ? ["restart", "stop"] : ["start"]) {
        const b = el("button", "act " + a, a[0].toUpperCase() + a.slice(1));
        b.type = "button";
        if (c.self) {
          b.disabled = true;
          b.title = "Ratatosk can't control its own container";
        } else {
          b.addEventListener("click", () => act(c.name, a, b));
        }
        actionBtns.append(b);
      }
      if (canSchedule) {
        const b = el("button", "act schedule", "Schedule…");
        b.type = "button";
        b.title = "Restart or update this container at a set time";
        b.addEventListener("click", () => scheduleContainer(c));
        actionBtns.append(b);
      }
      if (canDelete) {
        const b = el("button", "act delete", "Delete");
        b.type = "button";
        b.title = "Remove this stopped container";
        b.addEventListener("click", () => deleteContainer(c));
        actionBtns.append(b);
      }
    },
  };
}

// ---- panels -------------------------------------------------------------

// The box every kind of panel shares: a header (grip and eye while editing; the title is
// a button that folds the panel) and a body. A kind fills the title, a short note, the
// status lights, its own edit controls and the body.
function makePanel(id, kind) {
  const box = el("section", "panel panel-" + kind + (view.collapsed[id] ? " collapsed" : ""));
  box.dataset.panel = id;
  const head = el("div", "ph");
  head.addEventListener("pointerdown", (e) => {
    if (editing && !e.target.closest(".mini, .mini-select, .rename-input, .eye, input")) armDrag(e, (ev) => startPanelDrag(id, box, ev));
  });
  head.addEventListener("contextmenu", (e) => {
    if (editing && !e.target.closest(".rename-input")) e.preventDefault();
  });

  // keyboard route for moving a panel: focus the grip, then the arrow keys
  const grip = el("button", "grip");
  grip.type = "button";
  grip.append(icon("grip", 18));
  grip.title = "Drag to move (or use the arrow keys)";
  grip.addEventListener("keydown", (e) => {
    const dir = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" }[e.key];
    if (!dir) return;
    e.preventDefault();
    const to = Layout.neighbor(shownColumns, id, dir);
    if (!to) return;
    groupSettings.layout = Layout.moveInto(groupSettings.layout, columnsN, shownColumns, id, to[0], to[1]);
    changed();
    // the panel may have moved to another column; put keyboard focus back on its grip
    panels.get(id)?.el.querySelector(".grip")?.focus();
  });

  // the heading holds the button (not the other way round), so headings can be navigated
  const heading = el("h2", "ph-h");
  const main = el("button", "ph-main");
  main.type = "button";
  main.setAttribute("aria-expanded", String(!view.collapsed[id]));
  const title = el("span", "ph-title");
  const meta = el("span", "ph-meta");
  const tail = el("span", "ph-tail");
  main.append(title, meta, tail, icon("chevron"));
  heading.append(main);
  main.addEventListener("click", () => {
    const off = box.classList.toggle("collapsed");
    view.collapsed[id] = off;
    main.setAttribute("aria-expanded", String(!off));
    saveView();
    updateToggleAll();
  });

  const edit = el("span", "ph-edit");
  const eye = el("button", "eye");
  eye.type = "button";
  eye.append(icon("eye", 18));
  eye.addEventListener("click", () => {
    pinColumns();
    groupSettings.layout = Layout.setHidden(groupSettings.layout, id, true);
    changed();
  });

  const body = el("div", "pb");
  head.append(grip, heading, edit, eye);
  box.append(head, body);

  const panel = {
    id, kind, el: box, main: heading, body, edit, key: "", // `main` is what a rename swaps for its input
    setHead(text, note, lights) {
      title.textContent = text;
      meta.textContent = note || "";
      tail.replaceChildren(...(lights || []));
      grip.setAttribute("aria-label", "Move " + text + ". Use the arrow keys.");
      eye.setAttribute("aria-label", "Hide " + text);
      eye.title = "Hide this panel";
    },
    update() {},
  };
  return panel;
}

function led(state) {
  const l = el("span", "led " + state);
  l.setAttribute("role", "img");
  l.setAttribute("aria-label", { ok: "ok", warn: "needs attention", bad: "problem" }[state]);
  return l;
}

// ---- host panel

// The host panel's CPU and memory nodes, kept here (not looked up by id) so the numbers can be
// written while the panel is hidden and so not in the page.
let hostNodes = null;

function buildHost() {
  const p = makePanel("host", "host");
  p.setHead("Host", "", [led("ok")]);
  const meter = (label) => {
    const node = el("div", "meter");
    const line = el("div", "mh");
    const value = el("span", "mv", "–");
    line.append(el("span", "", label), value);
    const bar = el("div", "bar");
    const fill = el("i");
    bar.append(fill);
    const sub = el("div", "ph-note");
    node.append(line, bar, sub);
    return { node, value, fill, sub };
  };
  const cpu = meter("CPU");
  const mem = meter("Memory");
  hostNodes = { cpu, mem };
  const disks = el("div", "meters");
  disksBox = disks;
  p.body.append(cpu.node, mem.node, disks);
  const add = el("button", "mini", "Add disk");
  add.type = "button";
  add.addEventListener("click", () => { if (!addDiskOpen) openAddDisk(); });
  p.edit.append(add);
  p.update = () => {
    if (!editing) addDiskOpen = false;
    renderDisks(lastDisks);
  };
  return p;
}

function renderVitals(v) {
  const { cpu, mem } = hostNodes;
  cpu.value.textContent = Math.round(v.cpu_percent) + "%";
  cpu.fill.style.width = Math.min(v.cpu_percent, 100) + "%";
  cpu.sub.textContent = v.cpu_count + " cores";
  const memPct = (v.mem_used / v.mem_total) * 100;
  mem.value.textContent = fmtBytes(v.mem_used);
  mem.fill.style.width = memPct + "%";
  mem.sub.textContent = `of ${fmtBytes(v.mem_total)} · ${Math.round(memPct)}%`;
  renderDisks(v.disks || []);
}

// ---- storage meters (host panel)
//
// One meter per disk. The list is saved with the layout (`groupSettings.disks`); until a disk
// is added the dashboard's own disk is shown. In edit mode a disk can be renamed or removed, and
// "Add disk" offers the disks this container can see, so nobody types a path.

let lastDisks = [];
let disksBox = null; // the meters' container, set when the host panel is built (it isn't in the page yet on the first render)
let addDiskOpen = false;
let availableDisks = null; // null until fetched; [] when the container sees none

function diskDevice(d) { return d.device.replace(/^\/dev\//, ""); }

async function saveDisks(list) {
  groupSettings.disks = list;
  localEdits++;
  await saveGroups();
  refresh();
  render(true);
}

function renameDisk(index, labelEl) {
  const input = el("input", "rename-input");
  input.value = groupSettings.disks[index].label;
  input.maxLength = 32;
  input.setAttribute("aria-label", "Name for this disk");
  labelEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (commit) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (commit && value && value !== groupSettings.disks[index].label) {
      saveDisks(groupSettings.disks.map((d, i) => (i === index ? { ...d, label: value } : d)));
    } else {
      input.replaceWith(labelEl);
    }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

// Why a disk can be missing from the list, and what to do about it.
const DISK_HELP = "Not listed? Ratatosk only sees disks that something is mounted from. Add a read-only mount of a folder on that disk to Ratatosk's compose file (for example /home:/host/home:ro), recreate it, then reopen this. See the README.";

// The form under the meters: pick one of the disks the container can see, name it, add it.
function addDiskForm() {
  const form = el("form", "disk-form");
  if (availableDisks === null) {
    form.append(el("p", "ph-note", "Looking for disks..."));
    return form;
  }
  const have = new Set(groupSettings.disks.map((d) => d.path));
  const free = availableDisks.filter((d) => !have.has(d.path));
  if (!free.length) {
    form.append(el("p", "ph-note", availableDisks.length
      ? "Every disk Ratatosk can see is already shown."
      : "Ratatosk can't see any disks."));
    form.append(el("p", "ph-note", DISK_HELP));
    const close = el("button", "mini", "Close");
    close.type = "button";
    close.addEventListener("click", () => { addDiskOpen = false; render(true); });
    form.append(close);
    return form;
  }
  const pick = el("select");
  pick.setAttribute("aria-label", "Disk to add");
  for (const d of free) {
    const o = el("option", "", `${diskDevice(d)} · ${fmtBytes(d.total)}, ${Math.round(d.percent)}% used · ${d.path}`);
    o.value = d.path;
    pick.append(o);
  }
  const name = el("input");
  name.placeholder = "Name, e.g. Home";
  name.maxLength = 32;
  name.setAttribute("aria-label", "Name for this disk");
  const suggest = () => { name.value = diskDevice(free.find((d) => d.path === pick.value)); };
  pick.addEventListener("change", suggest);
  suggest();
  const add = el("button", "mini primary", "Add");
  add.type = "submit";
  const cancel = el("button", "mini", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => { addDiskOpen = false; render(true); });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const label = name.value.trim();
    if (!label) { name.focus(); return; }
    addDiskOpen = false;
    saveDisks([...groupSettings.disks, { label, path: pick.value }]);
  });
  form.append(pick, name, add, cancel, el("p", "ph-note", DISK_HELP));
  return form;
}

// Rebuilt only when the list, edit mode or the add form changes; otherwise the numbers
// update in place, so a half-typed rename survives the 5 s poll.
function renderDisks(disks) {
  lastDisks = disks;
  const box = disksBox;
  if (!box) return;
  const configured = groupSettings.disks.length > 0;
  const key = JSON.stringify([disks.map((d) => [d.label, d.path]), editing, configured, addDiskOpen, availableDisks && availableDisks.length]);
  if (box.dataset.key !== key) {
    box.dataset.key = key;
    const nodes = disks.map((d, i) => {
      const m = el("div", "meter");
      const line = el("div", "mh");
      const label = el("span", "", d.label);
      const right = el("span", "mh-right");
      right.append(el("span", "mv", "–"));
      line.append(label, right);
      if (editing && configured) {
        const ctl = el("span", "disk-ctl");
        const ren = el("button", "mini", "Rename");
        ren.type = "button";
        ren.addEventListener("click", () => renameDisk(i, label));
        const rm = el("button", "mini danger", "Remove");
        rm.type = "button";
        rm.addEventListener("click", () => saveDisks(groupSettings.disks.filter((_, j) => j !== i)));
        ctl.append(ren, rm);
        line.append(ctl);
      }
      const bar = el("div", "bar");
      bar.append(el("i"));
      m.append(line, bar, el("div", "ph-note"));
      return m;
    });
    if (editing && !configured) nodes.push(el("p", "ph-note pad", "Showing Ratatosk's own disk. Add a disk to choose which ones to show."));
    if (editing && addDiskOpen) nodes.push(addDiskForm());
    box.replaceChildren(...nodes);
  }
  const meters = box.querySelectorAll(".meter");
  disks.forEach((d, i) => {
    const m = meters[i];
    m.querySelector(".mv").textContent = d.error ? "–" : fmtBytes(d.used);
    m.querySelector(".bar i").style.width = (d.error ? 0 : Math.min(d.percent, 100)) + "%";
    m.querySelector(".ph-note").textContent = d.error
      ? `${d.error}: ${d.path}`
      : `of ${fmtBytes(d.total)} · ${Math.round(d.percent)}% · ${fmtBytes(d.free)} free`;
  });
}

async function openAddDisk() {
  addDiskOpen = true;
  availableDisks = null;
  render(true);
  try {
    const body = await api("/api/disks");
    availableDisks = body.error ? [] : body.items;
  } catch {
    availableDisks = [];
  }
  render(true);
}

// ---- link tiles (the Links panel)

// A container's link: its `ratatosk.url` label, or its best published port on whatever
// host name this page was opened with (so it works over a LAN, Tailscale or a domain).
function linkHref(c) {
  const l = c.link;
  if (l.url) return l.url;
  return `${l.https ? "https" : "http"}://${location.hostname}:${l.port}/`;
}

// Running containers that have a link and either sit in a "Generate links" group or were set
// to Show, minus the user's hidden ones. In edit mode the hidden ones are included (dimmed) so they can be
// shown again.
function tileList() {
  const hidden = groupSettings.hidden_links;
  return containers
    .filter((c) => c.link && c.status === "running" && !c.self && (hasLinks(groupOf(c)) || linkMode(c) === "show") && (editing || !hidden.includes(c.name)))
    .sort(byShownName);
}

function buildLinks() {
  const p = makePanel("links", "links");
  p.update = ({ tiles }, force) => {
    const key = JSON.stringify([editing, tiles.map((c) => [c.name, displayName(c), linkHref(c), groupSettings.hidden_links.includes(c.name)])]);
    if (!force && key === p.key) return;
    p.key = key;
    p.setHead("Links", String(tiles.length));
    if (!tiles.length) {
      p.body.replaceChildren(el("p", "ph-note pad", "No tiles yet. Turn on Generate links for a group."));
      return;
    }
    const grid = el("div", "links");
    for (const c of tiles) {
      const isHidden = groupSettings.hidden_links.includes(c.name);
      const tile = el("div", "tile" + (isHidden ? " is-hidden" : ""));
      const a = el("a", "tile-link");
      a.href = linkHref(c);
      a.target = "_blank";
      a.rel = "noopener";
      a.append(el("span", "tile-name", displayName(c)), el("span", "tile-url", a.host));
      // while editing, a tile is not a link: the button is the only thing to press
      a.addEventListener("click", (e) => { if (editing) e.preventDefault(); });
      tile.append(a);
      const btn = el("button", "mini tile-hide", isHidden ? "Show" : "Hide");
      btn.type = "button";
      btn.addEventListener("click", () => {
        // showing again goes back to what the group says, unless the group has no tiles and this one was forced on
        setLinkMode(c.name, isHidden ? (hasLinks(groupOf(c)) ? "inherit" : "show") : "hide");
        changed();
      });
      tile.append(btn);
      grid.append(tile);
    }
    p.body.replaceChildren(grid);
  };
  return p;
}

// ---- group panels

// ok / warn / bad / off for one container, given how much it matters
function lightState(c, priority) {
  const s = c.health && c.status === "running" ? c.health : c.status;
  if (s === "running" || s === "healthy") return "ok";
  if (s === "restarting" || s === "starting" || s === "paused") return "warn";
  if (s === "exited" || s === "dead" || s === "unhealthy" || s === "created") {
    return priority === "critical" ? "bad" : priority === "wanted" ? "warn" : "off";
  }
  return "off";
}

function buildGroup(id) {
  const p = makePanel(id, "group");
  p.el.classList.add("group");
  p.update = ({ name, members }, force) => {
    const prio = groupPriority(name);
    const linked = hasLinks(name);
    const key = JSON.stringify([editing, prio, linked, members.map((c) => [c.name, c.status, c.health || "", priorityOf(c)])]);
    if (!force && key === p.key) return;
    p.key = key;
    p.el.dataset.group = name;

    const up = members.filter((c) => c.status === "running").length;
    const down = members.length - up;
    let note = members.length ? `${up} up` : "empty";
    const stopped = members.filter((c) => c.status !== "running" && priorityOf(c) === "on_demand").length;
    if (down - stopped) note += ` · ${down - stopped} down`;
    if (stopped) note += ` · ${stopped} stopped`;
    const lights = members.slice(0, MAX_LIGHTS).map((c) => {
      const l = el("i", lightState(c, priorityOf(c)));
      l.title = `${displayName(c)}: ${c.status}`;
      return l;
    });
    const strip = el("span", "strip");
    strip.setAttribute("aria-hidden", "true");
    strip.append(...lights);
    p.setHead(name, note, [...(prio !== "wanted" ? [el("span", "tag", prio === "critical" ? "critical" : "on-demand")] : []), strip]);

    // edit-mode controls for this group
    const od = optionSelect("Priority", priorityEntries(), prio);
    od.title = "The default for this group. Critical: the overall light turns red when one is down. Wanted: amber. On demand: meant to be stopped sometimes, left out of the overall count";
    od.select.addEventListener("change", () => { setGroupPriority(name, od.select.value); changed(); });
    const nl = el("button", "mini", "Generate links");
    nl.type = "button";
    nl.setAttribute("aria-pressed", String(linked));
    nl.title = "Show a link tile for each running container in this group";
    nl.addEventListener("click", () => {
      const list = groupSettings.link_groups;
      groupSettings.link_groups = linked ? list.filter((g) => g !== name) : [...list, name];
      changed();
    });
    const controls = [od, nl];
    if (name !== UNGROUPED) {
      const ren = el("button", "mini", "Rename");
      ren.type = "button";
      ren.addEventListener("click", () => {
        const input = el("input", "rename-input");
        input.value = name;
        input.maxLength = EditLogic.MAX_GROUP_NAME;
        input.setAttribute("aria-label", "Group name");
        p.main.hidden = true;
        p.edit.hidden = true;
        p.main.after(input);
        input.focus();
        input.select();
        let done = false;
        const finish = (commit) => {
          if (done) return;
          done = true;
          input.remove();
          p.main.hidden = false;
          p.edit.hidden = false;
          if (commit) renameGroup(name, input.value);
        };
        input.addEventListener("keydown", (e) => {
          if (e.key === "Enter") finish(true);
          if (e.key === "Escape") finish(false);
        });
        input.addEventListener("blur", () => finish(true));
      });
      controls.unshift(ren);
      // compose-project groups come back on their own, so only user-made ones are deletable
      if (!members.length || members.every((c) => groupSettings.assignments[c.name])) {
        const del = el("button", "mini danger", "Delete");
        del.type = "button";
        del.addEventListener("click", () => deleteGroup(name, members));
        controls.push(del);
      }
    }
    p.edit.replaceChildren(...controls);

    const rows = members.map((c) => {
      const row = items.get(c.name).el;
      row.dataset.prio = priorityOf(c);
      return row;
    });
    p.body.replaceChildren(...(rows.length ? rows : [el("p", "ph-note pad", "No containers here yet. Drag some in while editing.")]));
  };
  return p;
}

// ---- rendering ----------------------------------------------------------

// The browser tab shows the same overall status as the page header: a green, amber or red dot
// and the count in the title. Green = everything that matters is running and healthy; amber =
// a Wanted container is down or something is starting, restarting, paused or unhealthy; red =
// a Critical container is down, or Docker can't be reached.
const FAVICON = { running: "#2a8a57", restarting: "#e2a336", exited: "#e5554b", idle: "#9aa1a8" };
const faviconEl = document.getElementById("favicon");
function setTab(level, text) {
  document.title = text ? `${text} · Ratatosk` : "Ratatosk";
  faviconEl.href = `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="13" fill="${FAVICON[level]}"/></svg>`)}`;
}

function summarize() {
  if (!loaded) { summaryEl.hidden = true; return; }
  // health only counts containers in groups that are expected to stay up
  const monitored = containers.filter((c) => priorityOf(c) !== "on_demand");
  const up = monitored.filter((c) => c.status === "running").length;
  const extra = containers.length - monitored.length;
  const lights = monitored.map((c) => lightState(c, priorityOf(c)));
  const level = lights.includes("bad") ? "exited" : lights.includes("warn") ? "restarting" : "running";
  const key = [up, monitored.length, extra, level].join("|");
  if (summaryEl.dataset.key === key) return;
  summaryEl.dataset.key = key;
  summaryEl.hidden = false;
  const text = el("span");
  text.append(el("b", "", `${up} of ${monitored.length}`), " running");
  summaryEl.replaceChildren(el("span", "dot " + level), text, ...(extra ? [el("span", "", `+ ${extra} on-demand`)] : []));
  setTab(level, `${up}/${monitored.length}`);
}

function renderShelf(descs) {
  const hidden = new Set(groupSettings.layout.hidden);
  const chips = descs.filter((d) => hidden.has(d.id)).map((d) => {
    const chip = el("span", "chip");
    const show = el("button", "mini", "Show");
    show.type = "button";
    show.setAttribute("aria-label", "Show " + panelTitle(d.id));
    show.addEventListener("click", () => {
      groupSettings.layout = Layout.setHidden(groupSettings.layout, d.id, false);
      changed();
    });
    chip.append(el("b", "", panelTitle(d.id)), show);
    return chip;
  });
  shelfEl.replaceChildren(el("span", "ph-meta", "Hidden panels"), ...(chips.length ? chips : [el("span", "ph-meta", "none")]));
}

function updateToggleAll() {
  const boxes = [...boardEl.querySelectorAll(".panel")];
  const allCollapsed = boxes.length > 0 && boxes.every((b) => b.classList.contains("collapsed"));
  toggleAllBtn.textContent = allCollapsed ? "Expand all" : "Collapse all";
}

function placePanels(descs) {
  const n = columnCount();
  let cols = [...boardEl.querySelectorAll(":scope > .col")];
  if (cols.length !== n) {
    cols.forEach((c) => c.remove());
    cols = Array.from({ length: n }, () => boardEl.appendChild(el("div", "col")));
    boardEl.dataset.cols = String(n);
  }
  columnsN = n;
  shownColumns = Layout.columnsFor(groupSettings.layout, n, descs);
  shownColumns.forEach((ids, ci) => {
    const col = cols[ci];
    const want = ids.map((id) => panels.get(id).el);
    // existing nodes are moved, never rebuilt, so their state comes along
    want.forEach((node, i) => { if (col.children[i] !== node) col.insertBefore(node, col.children[i] || null); });
    while (col.children.length > want.length) col.lastElementChild.remove();
  });
}

// force: rebuild every panel even if its data looks unchanged (after a local edit).
// forceInt: also rebuild an integration panel someone is typing in.
function render(force, forceInt) {
  groupByProject.checked = groupSettings.group_by_project === true;
  // drop rows for containers that no longer exist, and make rows for new ones
  const names = new Set(containers.map((c) => c.name));
  for (const [name, item] of items) {
    if (!names.has(name)) { item.dispose(); items.delete(name); }
  }
  for (const c of containers) {
    if (!items.has(c.name)) items.set(c.name, makeItem(c.name));
    items.get(c.name).update(c);
  }
  if (!force && dragging) return;

  const groups = groupList();
  const tiles = tileList();
  const ints = integrationData.items.filter((i) => i.state === "configured");
  const waiting = integrationData.items.filter((i) => i.state !== "configured" && i.state !== "dismissed");
  const descs = [{ id: "host", kind: "host", rows: 0 }];
  const data = { host: null, links: { tiles } };
  for (const i of ints) {
    const id = "int:" + i.container;
    descs.push({ id, kind: "int", rows: i.panels.reduce((s, x) => s + x.rows.length, 0) });
    data[id] = { item: i };
  }
  if (tiles.length || editing) descs.push({ id: "links", kind: "links", rows: tiles.length });
  for (const [name, members] of groups) {
    const id = "group:" + name;
    descs.push({ id, kind: "group", rows: members.length });
    data[id] = { name, members };
  }
  // last, so that a width nobody arranged yet keeps every other panel where it was dealt
  if (waiting.length) {
    descs.push({ id: "found", kind: "found", rows: waiting.length });
    data.found = { items: waiting };
  }

  const wanted = new Set(descs.map((d) => d.id));
  for (const [id, p] of panels) {
    if (id !== "host" && !wanted.has(id)) { p.el.remove(); panels.delete(id); }
  }
  const hidden = new Set(groupSettings.layout.hidden);
  for (const d of descs) {
    let p = panels.get(d.id);
    if (!p) {
      p = { host: buildHost, links: buildLinks, int: buildInt, found: buildFound, group: buildGroup }[d.kind](d.id);
      panels.set(d.id, p);
    }
    if (!hidden.has(d.id)) p.update(data[d.id], force, forceInt);
  }

  placePanels(descs);
  // The first view of a width saves its arrangement, so a later new panel can't reshuffle the rest.
  if (loaded && integrationsLoaded && !editing && !dragging && !groupSettings.layout.columns[String(columnsN)]) {
    pinColumns();
    localEdits++;
    saveGroups();
  }
  summarize();
  renderShelf(descs);
  renderNotices();
  updateToggleAll();
}

// Both polls run one at a time (a slow Docker can't pile requests up and paint older data over
// newer); a call made during a run, such as right after an action, runs once more when it ends.
const refresh = EditLogic.coalesce(loadList);

async function loadList() {
  const editsAtStart = localEdits;
  const savesAtStart = savesDone;
  try {
    const [list, vitals] = await Promise.all([api(`/api/containers?age=${view.listRefresh}`), api("/api/vitals")]);
    if (list.error) { listError = list.error; showError(list.error); setTab("exited", "can't reach Docker"); summaryEl.dataset.key = ""; return; }
    listError = "";
    showErrors(); // a refused save or a failed action stays until it is dealt with
    containers = list.items;
    readOnly = list.read_only;
    role = list.role;
    applyRole();
    loaded = true;
    // the server's copy may predate a save that is running or just finished, so it must not replace newer local edits
    if (!dragging && localEdits === editsAtStart && savesDone === savesAtStart && !saveGroups.busy()) groupSettings = list.groups;
    renderVitals(vitals);
    render(false);
  } catch (e) {
    listError = String(e);
    showError(String(e));
    setTab("exited", "can't reach Ratatosk");
    summaryEl.dataset.key = "";
  }
}

// ---- integrations -------------------------------------------------------

// An app Ratatosk has an adapter for is listed in the "Apps found" panel. Nothing reads or stores
// its API key until the admin presses a button here; the key is never sent to the page.
let integrationData = { items: [], problems: [] };
// per container: the manual-key form is open / a message to show / a request in flight
const manualOpen = new Set();
// forms to focus when they are first built; a rebuild of the panel later must not steal focus
const focusPending = new Set();
function openManual(container) {
  manualOpen.add(container);
  focusPending.add(container);
  renderIntegrations(true);
}
const integrationMsg = new Map();
const integrationBusy = new Set();

function integrationName(name) {
  const c = containers.find((x) => x.name === name);
  return c ? displayName(c) : name;
}

// force: rebuild the panel even if someone is typing in it (after a button press)
function renderIntegrations(force) { render(false, !!force); }

async function integrationCall(name, path, method, body) {
  integrationBusy.add(name);
  integrationMsg.delete(name);
  renderIntegrations(true);
  try {
    const opts = { method };
    if (body) opts.body = JSON.stringify(body);
    const res = await api(`/api/integrations/${encodeURIComponent(name)}/${path}`, opts);
    if (res.error) integrationMsg.set(name, withHint(res.error));
    return !res.error;
  } catch (e) {
    integrationMsg.set(name, String(e));
    return false;
  } finally {
    // show the result now; the list refreshes behind it
    integrationBusy.delete(name);
    renderIntegrations(true);
    refreshIntegrations();
  }
}

// An unreachable app is almost always a Docker network problem, so say how to fix it.
function withHint(msg) {
  if (!msg.includes("could not reach")) return msg;
  return msg + ". Put Ratatosk on the app's Docker network, or add extra_hosts: " +
    '"host.docker.internal:host-gateway" to Ratatosk and publish the app\'s port (see the README).';
}

function findKey(item) {
  const ok = confirm(
    `Find the ${item.adapter_name} key?\n\nRatatosk will read ${item.key_file} inside the ` +
    `${integrationName(item.container)} container, look for the API key, check it against the app and ` +
    "store it on this server. The key is never shown in the browser."
  );
  if (ok) integrationCall(item.container, "find-key", "POST");
}

function removeKey(item) {
  const what = item.auth_kind === "login" ? "login" : item.auth_kind === "keys" ? "keys" : "key";
  const ok = confirm(`Remove the stored ${item.adapter_name} ${what}?\n\nThe panel shows its setup steps again until it is added.`);
  if (ok) integrationCall(item.container, "key", "DELETE");
}

// An app with a set of optional keys (Pelican): one password field per key that isn't stored
// yet. Blank fields are left alone; at least one is needed.
function keysForm(item) {
  const form = el("form", "int-form");
  const inputs = item.auth_keys.filter((k) => !k.set).map((k) => {
    const input = el("input");
    input.type = "password";
    input.id = `key-${k.id}-${item.container}`;
    input.autocomplete = "off";
    input.placeholder = k.label;
    input.setAttribute("aria-label", `${item.adapter_name} ${k.label}`);
    input.maxLength = 256;
    input.dataset.key = k.id;
    return input;
  });
  const save = el("button", "btn primary", "Save");
  save.type = "submit";
  const cancel = el("button", "btn", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => { manualOpen.delete(item.container); renderIntegrations(true); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const keys = {};
    for (const i of inputs) if (i.value.trim()) keys[i.dataset.key] = i.value.trim();
    if (!Object.keys(keys).length) return;
    // clear the fields at once so nothing sensitive stays in the page
    for (const i of inputs) i.value = "";
    if (await integrationCall(item.container, "key", "PUT", { keys })) manualOpen.delete(item.container);
    renderIntegrations(true);
  });
  form.append(...inputs, save, cancel);
  if (focusPending.delete(item.container)) setTimeout(() => inputs[0]?.focus(), 0);
  return form;
}

function manualForm(item) {
  if (item.auth_kind === "keys") return keysForm(item);
  const login = item.auth_kind === "login";
  const form = el("form", "int-form");
  const field = (suffix, type, placeholder, label) => {
    const input = el("input");
    input.type = type;
    input.id = `${suffix}-${item.container}`;
    input.autocomplete = "off";
    input.placeholder = placeholder;
    input.setAttribute("aria-label", label);
    input.maxLength = 256;
    return input;
  };
  const user = login ? field("user", "text", "Username", `${item.adapter_name} username`) : null;
  const secret = login
    ? field("pass", "password", "Password", `${item.adapter_name} password`)
    : field("key", "password", "API key", `${item.adapter_name} API key`);
  const save = el("button", "btn primary", login ? "Save login" : "Save key");
  save.type = "submit";
  const cancel = el("button", "btn", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => { manualOpen.delete(item.container); renderIntegrations(true); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = login ? secret.value : secret.value.trim();
    if (!value || (login && !user.value.trim())) return;
    const body = login ? { username: user.value.trim(), password: value } : { key: value };
    // clear the fields at once so nothing sensitive stays in the page
    secret.value = "";
    if (user) user.value = "";
    if (await integrationCall(item.container, "key", "PUT", body)) manualOpen.delete(item.container);
    renderIntegrations(true);
  });
  form.append(...(login ? [user] : []), secret, save, cancel);
  if (focusPending.delete(item.container)) setTimeout(() => (user || secret).focus(), 0);
  return form;
}

// Start, Stop and Restart for one game-server row. The container is named by the server's UUID,
// so this is the same route as a container row's buttons (which sends it through Pelican).
function serverButtons(r) {
  const box = el("div", "int-row-acts");
  for (const a of r.state === "offline" ? ["start"] : ["restart", "stop"]) {
    const b = el("button", "mini " + a, a[0].toUpperCase() + a.slice(1));
    b.type = "button";
    b.setAttribute("aria-label", `${b.textContent} ${r.title}`);
    b.addEventListener("click", () => act(r.server, a, b));
    box.append(b);
  }
  // editing needs the user key (the panel only has a state with it) and a stopped server
  const offline = r.state === "offline";
  const cfg = el("button", "mini", "Config");
  cfg.type = "button";
  cfg.disabled = !offline;
  cfg.title = offline ? "Edit this server's config files" : "Stop the server to edit its files";
  cfg.setAttribute("aria-label", `Config files of ${r.title}`);
  cfg.addEventListener("click", () => openConfigDialog(r));
  box.append(cfg);
  return box;
}

// ---- config file editor (Pelican game servers) --------------------------------------------
//
// A dialog: pick a file, then edit it as fields or as raw text. The server parses and renders,
// so this only shows what it sends back and returns the values that changed. Switching between
// fields and raw text drops unsaved changes (after asking), since only the server can turn
// fields into text.

function openConfigDialog(r) {
  const name = r.server;
  const base = `/api/containers/${encodeURIComponent(name)}/config`;
  const dlg = el("dialog", "cfg");
  dlg.setAttribute("aria-label", `Config files of ${r.title}`);
  const head = el("div", "cfg-head");
  const closeBtn = el("button", "mini", "Close");
  closeBtn.type = "button";
  head.append(el("h2", null, `${r.title}: config files`), closeBtn);
  const body = el("div", "cfg-body");
  dlg.append(head, body);

  let dirty = false;
  const close = () => {
    if (dirty && !confirm("Close without saving your changes?")) return;
    dlg.close();
  };
  closeBtn.addEventListener("click", close);
  dlg.addEventListener("cancel", (e) => { if (dirty) { e.preventDefault(); close(); } });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();

  const note = (text, bad) => {
    const p = el("p", "int-note" + (bad ? " bad" : ""), text);
    if (bad) p.setAttribute("role", "alert");
    return p;
  };

  // The file list is fetched once and reused when you come back from a file; a save drops it
  // (the file's size changed) and "Try again" asks again.
  let filesRes = null;

  async function showFiles(fresh) {
    dirty = false;
    if (fresh === true) filesRes = null; // a button passes itself as the argument, so only a real true counts
    if (!filesRes) {
      body.replaceChildren(note("Looking for config files…"));
      let res;
      try { res = await api(base); } catch (e) { body.replaceChildren(note(`${e}`, true)); return; }
      if (res.error) { body.replaceChildren(note(res.error, true), miniButton("Try again", "", () => showFiles(true))); return; }
      filesRes = res;
    }
    const res = filesRes;
    const list = el("ul", "cfg-files");
    for (const f of res.files) {
      const li = el("li");
      const b = el("button", "cfg-file");
      b.type = "button";
      b.append(el("span", "mono", f.path), el("span", "int-row-detail", fmtSize(f.size)));
      b.addEventListener("click", () => openFile(f.path));
      li.append(b);
      list.append(li);
    }
    body.replaceChildren(
      res.files.length ? note("Pick a file. Only text settings files (.ini, .cfg, .properties, .txt) are listed.") : note("No config files found in this server's files."),
      list,
      ...(res.truncated ? [note("The server has a lot of files; the list may be incomplete.")] : []),
    );
  }

  async function openFile(path) {
    body.replaceChildren(note("Opening…"));
    let res;
    try { res = await api(`${base}/file?path=${encodeURIComponent(path)}`); } catch (e) { body.replaceChildren(note(`${e}`, true), miniButton("Back", "", showFiles)); return; }
    if (res.error) { body.replaceChildren(note(res.error, true), miniButton("Back to files", "", showFiles)); return; }
    showEditor(res, res.entries.length ? "fields" : "raw", "");
  }

  function showEditor(file, mode, status) {
    dirty = false;
    const edits = {};       // key/value files: line -> new text, or line -> {setting: new text}
    let listValues = file.entries.map((e) => e.value);
    let rawArea = null;
    const err = el("p", "int-note bad");
    err.setAttribute("role", "alert");
    err.hidden = true;
    const touched = () => { dirty = true; saveBtn.disabled = false; err.hidden = true; };

    const bar = el("div", "cfg-bar");
    bar.append(miniButton("‹ Files", "", () => { if (!dirty || confirm("Discard your changes?")) showFiles(); }), el("span", "mono cfg-path", file.path));

    const switchBtn = miniButton(mode === "fields" ? "Raw text" : "Fields", "", () => {
      if (dirty && !confirm("Switching drops your unsaved changes. Continue?")) return;
      showEditor(file, mode === "fields" ? "raw" : "fields", "");
    });
    if (file.entries.length) bar.append(switchBtn);

    const form = el("div", "cfg-form");
    if (mode === "raw") {
      rawArea = el("textarea", "cfg-raw");
      rawArea.value = file.raw;
      rawArea.spellcheck = false;
      rawArea.addEventListener("input", touched);
      form.append(rawArea);
    } else if (file.format === "list") {
      const rows = el("div", "cfg-list");
      const draw = () => {
        rows.replaceChildren();
        listValues.forEach((v, i) => {
          const row = el("div", "cfg-listrow");
          const input = el("input", "mono");
          input.value = v;
          input.addEventListener("input", () => { listValues[i] = input.value; touched(); });
          const rm = miniButton("Remove", "danger", () => { listValues.splice(i, 1); touched(); draw(); });
          row.append(input, rm);
          rows.append(row);
        });
      };
      draw();
      form.append(rows, miniButton("Add entry", "", () => { listValues.push(""); touched(); draw(); }));
    } else {
      let section;
      for (const e of file.entries) {
        if (e.section !== section) {
          section = e.section;
          if (section) form.append(el("h3", "cfg-section", section));
        }
        if (e.fields) {
          const box = el("fieldset", "cfg-struct");
          const legend = el("legend", "mono", e.key);
          box.append(legend);
          if (e.comment) legend.append(infoButton(e.comment, box));
          for (const f of e.fields) {
            box.append(fieldRow(f.key, f.type, f.value, (v) => {
              const cur = (edits[e.line] = edits[e.line] || {});
              if (v === f.value) delete cur[f.key]; else cur[f.key] = v;
              if (!Object.keys(cur).length) delete edits[e.line];
              touched();
            }));
          }
          form.append(box);
        } else {
          form.append(fieldRow(e.key, e.type, e.value, (v) => {
            if (v === e.value) delete edits[e.line]; else edits[e.line] = v;
            touched();
          }, e.comment));
        }
      }
    }

    const saveBtn = miniButton("Save", "primary", async (b) => {
      b.disabled = true;
      err.hidden = true;
      const payload = { path: file.path, hash: file.hash };
      // the textarea's line breaks are all "\n"; the file keeps the ending it had
      if (mode === "raw") payload.raw = EditLogic.restoreEol(file.raw, rawArea.value);
      else payload.edits = file.format === "list" ? { entries: listValues } : edits;
      let res;
      try {
        res = await api(`${base}/file`, { method: "PUT", body: JSON.stringify(payload) });
      } catch (e) { res = { error: `${e}` }; }
      if (res.error) {
        err.textContent = res.error;
        err.hidden = false;
        b.disabled = false;
        return;
      }
      filesRes = null;
      showEditor(res, mode === "fields" && !res.entries.length ? "raw" : mode, "Saved.");
    });
    saveBtn.disabled = true;

    const foot = el("div", "cfg-foot");
    foot.append(saveBtn, miniButton("Reload from server", "", () => { if (!dirty || confirm("Discard your changes?")) openFile(file.path); }), el("span", "int-note", status));
    // the comments at the top of the file often say how it is used (e.g. "changes here are ignored")
    const about = file.header && file.header.length ? [el("div", "cfg-about", file.header.join("\n"))] : [];
    body.replaceChildren(bar, ...about, form, err, foot);
  }

  // one labelled setting; booleans are checkboxes that keep the file's own capitalisation
  function fieldRow(label, type, value, onChange, comment) {
    const wrap = el("div", "cfg-fieldwrap");
    const row = el("label", "cfg-field");
    const name = el("span", "mono", label);
    row.append(name);
    if (comment) name.append(infoButton(comment, wrap));
    let input;
    if (type === "bool") {
      input = el("input");
      input.type = "checkbox";
      input.checked = value.toLowerCase() === "true";
      const upper = value[0] === value[0].toUpperCase();
      input.addEventListener("change", () => {
        const word = input.checked ? "true" : "false";
        onChange(upper ? word[0].toUpperCase() + word.slice(1) : word);
      });
    } else {
      input = el("input", "mono");
      input.value = value;
      if (type === "raw") {
        // a nested list or struct: the server never rewrites it, so showing it editable would lose the edit
        input.readOnly = true;
        input.title = "A nested setting can't be edited here. Use Raw text.";
      } else {
        if (type === "int" || type === "float") input.inputMode = "decimal";
        input.addEventListener("input", () => onChange(input.value));
      }
    }
    row.append(input);
    wrap.prepend(row);
    return wrap;
  }

  // an (i) next to a setting that has a comment above it in the file: hover shows it (title),
  // and a tap or click shows it as a note under the setting, since touch has no hover
  function infoButton(text, host) {
    const b = el("button", "cfg-info", "i");
    b.type = "button";
    b.title = text;
    b.setAttribute("aria-label", "About this setting");
    b.setAttribute("aria-expanded", "false");
    let note = null;
    b.addEventListener("click", (ev) => {
      ev.preventDefault(); // inside a <label>: don't also toggle or focus its input
      if (note) { note.remove(); note = null; } else { note = el("div", "cfg-about", text); host.append(note); }
      b.setAttribute("aria-expanded", String(!!note));
    });
    return b;
  }

  showFiles();
}

function fmtSize(n) { return n >= 1024 ? (n / 1024).toFixed(1) + " KB" : n + " B"; }

// A failed action's message, announced to screen readers when it appears.
function alertNote(text) {
  const p = el("p", "int-note bad", text);
  p.setAttribute("role", "alert");
  return p;
}

// What goes inside an integration's panel: its data once set up, else the setup steps.
function integrationBody(item) {
  const busy = integrationBusy.has(item.container);
  const out = el("div", "int-body");

  if (item.state === "configured") {
    if (item.error) out.append(el("p", "int-note bad", `Can't show ${item.adapter_name}: ${withHint(item.error)}`));
    const missing = item.auth_keys.filter((k) => !k.set);
    if (missing.length && !keyLock()) {
      const add = el("div", "int-actions");
      const btn = el("button", "mini", missing.length === 1 ? `Add ${missing[0].label.toLowerCase()}` : "Add another key");
      btn.type = "button";
      btn.disabled = busy;
      btn.addEventListener("click", () => openManual(item.container));
      add.append(btn);
      if (!manualOpen.has(item.container)) {
        if (!item.auth_keys.some((k) => k.set && k.sufficient)) out.append(el("p", "int-note", `Not set: ${missing.map((k) => k.label).join(", ")}. Each key is optional and switches on its own part of ${item.adapter_name}.`));
        out.append(add);
      }
    }
    if (item.auth_kind === "keys" && manualOpen.has(item.container) && missing.length) out.append(manualForm(item));
    for (const p of item.panels) {
      const section = el("div", "int-section");
      const stitle = el("div", "sh");
      stitle.append(el("span", null, p.title), el("span", "mono", p.summary || (p.total ? String(p.total) : "0")));
      section.append(stitle);
      if (!p.rows.length) section.append(el("p", "int-note", "Nothing to show."));
      const list = el("ul", "int-rows");
      for (const r of p.rows) {
        const li = el("li");
        const line = el("div", "int-row-line");
        line.append(el("span", "int-row-title", r.title));
        if (r.detail) line.append(el("span", "int-row-detail", r.detail));
        li.append(line);
        if (p.power && r.server && r.state && !readOnly) li.append(serverButtons(r));
        if (r.disk) li.append(...diskUse(r.disk));
        if (typeof r.progress === "number") {
          const bar = el("div", "bar");
          const fill = el("i");
          fill.style.width = Math.min(Math.max(r.progress, 0), 100) + "%";
          bar.append(fill);
          li.append(bar);
        }
        list.append(li);
      }
      section.append(list);
      out.append(section);
    }
  } else {
    return setupBody(item);
  }
  const msg = integrationMsg.get(item.container);
  if (msg) out.append(alertNote(msg));
  if (busy) out.append(el("p", "int-note", "Working..."));
  return out;
}

// The setup steps for an app that was found but is not set up yet: what goes inside its row in "Apps found".
function setupBody(item) {
  const busy = integrationBusy.has(item.container);
  const out = el("div", "int-body");
  const login = item.auth_kind === "login";
  const keys = item.auth_kind === "keys";
  out.append(el("p", "int-note", keys
    ? `Found ${item.adapter_name}. It has ${item.auth_keys.length} kinds of API key, each optional and each switching on its own part (${item.auth_keys.map((k) => k.label).join("; ")}). They are stored hashed by the app, so they can't be looked up: enter the ones you want to use. Nothing is stored until you do.`
    : login
    ? `Found ${item.adapter_name}. To show its data Ratatosk needs a username and password for it. It can't look them up, because the app stores the password hashed. Nothing is stored until you enter them.`
    : `Found ${item.adapter_name}. To show its data Ratatosk needs its API key. Nothing is read until you choose.`));
  const actions = el("div", "int-actions");
  if (item.key_file && !login) {
    const find = el("button", "btn primary", "Find key automatically");
    find.type = "button";
    find.addEventListener("click", () => findKey(item));
    actions.append(find);
  }
  const manual = el("button", "btn", login ? "Enter login" : keys ? "Enter keys" : "Enter key manually");
  manual.type = "button";
  manual.addEventListener("click", () => openManual(item.container));
  const dismiss = el("button", "btn", "Dismiss");
  dismiss.type = "button";
  dismiss.addEventListener("click", () => integrationCall(item.container, "dismiss", "POST"));
  actions.append(manual, dismiss);
  for (const b of actions.querySelectorAll("button")) b.disabled = busy;
  if (keyLock()) {
    for (const b of [...actions.querySelectorAll("button")].filter((b) => b.textContent !== "Dismiss")) {
      b.disabled = true;
      b.title = keyLock();
    }
  }
  out.append(actions);
  if (manualOpen.has(item.container)) out.append(manualForm(item));
  const msg = integrationMsg.get(item.container);
  if (msg) out.append(alertNote(msg));
  if (busy) out.append(el("p", "int-note", "Working..."));
  return out;
}

// A game server's disk: "2.5 GB / 10 GB" and a bar, amber from 85 %, red once over the allocation.
// Without an allocation (unlimited) only the size in use is shown; without the user key only the
// allocation.
function diskUse({ used, limit }) {
  const text = used !== null && limit ? `${fmtBytes(used)} / ${fmtBytes(limit)}` : used !== null ? `${fmtBytes(used)} used` : `${fmtBytes(limit)} allocated`;
  const line = el("div", "disk-use mono", `Disk ${text}`);
  if (used === null || !limit) return [line];
  const ratio = used / limit;
  const bar = el("div", "bar disk" + (ratio > 1 ? " over" : ratio >= 0.85 ? " warn" : ""));
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", `Disk ${Math.round(ratio * 100)} percent used`);
  const fill = el("i");
  fill.style.width = Math.min(ratio * 100, 100) + "%";
  bar.append(fill);
  return [line, bar];
}

function buildInt(id) {
  const p = makePanel(id, "int");
  p.update = ({ item }, force, forceInt) => {
    const busy = integrationBusy.has(item.container);
    const key = JSON.stringify([item, readOnly, role, manualOpen.has(item.container), integrationMsg.get(item.container), busy, editing, integrationName(item.container)]);
    if (!force && key === p.key) return;
    // don't rebuild under someone who is typing a key
    if (!forceInt && p.el.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
    p.key = key;
    const state = item.state === "configured" ? (item.error ? "bad" : "ok") : "warn";
    p.setHead(item.adapter_name, integrationName(item.container), [led(state)]);
    if (item.state === "configured") {
      const rm = el("button", "mini", { login: "Remove login", keys: "Remove keys" }[item.auth_kind] || "Remove key");
      rm.type = "button";
      rm.disabled = busy || !!keyLock();
      if (keyLock()) rm.title = keyLock();
      rm.addEventListener("click", () => removeKey(item));
      p.edit.replaceChildren(rm);
    } else {
      p.edit.replaceChildren();
    }
    p.body.replaceChildren(integrationBody(item));
  };
  return p;
}

// ---- "Apps found": every detected app that is not set up yet, one collapsed row each ------------------------

const foundOpen = new Set(); // containers whose row is expanded
const foundIsOpen = (name) => foundOpen.has(name) || manualOpen.has(name) || integrationBusy.has(name) || integrationMsg.has(name);

function buildFound(id) {
  const p = makePanel(id, "int");
  p.update = ({ items }, force, forceInt) => {
    const key = JSON.stringify([items, readOnly, role, items.map((i) => [foundIsOpen(i.container), manualOpen.has(i.container), integrationMsg.get(i.container), integrationBusy.has(i.container), integrationName(i.container)])]);
    if (!force && key === p.key) return;
    // don't rebuild under someone who is typing a key
    if (!forceInt && p.el.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
    p.key = key;
    p.setHead("Apps found", String(items.length), [led("warn")]);
    p.edit.replaceChildren();
    const list = el("ul", "found-list");
    for (const item of items) {
      const open = foundIsOpen(item.container);
      const li = el("li", "found-row" + (open ? " open" : ""));
      const head = el("button", "found-head");
      head.type = "button";
      head.setAttribute("aria-expanded", String(open));
      head.append(el("span", "found-name", item.adapter_name), el("span", "found-sub", integrationName(item.container)), icon("chevron"));
      head.addEventListener("click", () => {
        if (foundIsOpen(item.container)) {
          foundOpen.delete(item.container);
          manualOpen.delete(item.container);
          integrationMsg.delete(item.container);
        } else {
          foundOpen.add(item.container);
        }
        renderIntegrations(true);
      });
      li.append(head);
      if (open) li.append(setupBody(item));
      list.append(li);
    }
    p.body.replaceChildren(list);
  };
  return p;
}

// Dismissed apps (always undoable from right here) and adapter problems, above the board.
function renderNotices() {
  const dismissed = integrationData.items.filter((i) => i.state === "dismissed");
  restoreBtn.hidden = dismissed.length === 0;
  noDismissed.hidden = dismissed.length > 0;
  restoreBtn.textContent = `Restore dismissed integrations (${dismissed.length})`;
  const key = JSON.stringify([dismissed.map((i) => [i.adapter_name, i.container]), integrationData.problems]);
  if (noticesEl.dataset.key === key) return;
  noticesEl.dataset.key = key;
  const nodes = [];
  if (dismissed.length) {
    const names = dismissed.map((i) => i.adapter_name + " (" + integrationName(i.container) + ")");
    const row = el("p", "int-dismissed", `Dismissed: ${names.join(", ")} `);
    const undo = el("button", "mini", "Restore");
    undo.type = "button";
    undo.addEventListener("click", restoreDismissed);
    row.append(undo);
    nodes.push(row);
  }
  if (integrationData.problems.length) {
    nodes.push(el("p", "int-note bad", "Adapter problems: " + integrationData.problems.join("; ")));
  }
  noticesEl.hidden = nodes.length === 0;
  noticesEl.replaceChildren(...nodes);
}

const refreshIntegrations = EditLogic.coalesce(loadIntegrations);

async function loadIntegrations() {
  try {
    const body = await api(`/api/integrations?age=${view.panelRefresh}`);
    if (body.error) return;
    integrationData = body;
    integrationsLoaded = true;
    render(false);
  } catch {}
}

async function restoreDismissed() {
  for (const i of integrationData.items.filter((x) => x.state === "dismissed")) {
    await api(`/api/integrations/${encodeURIComponent(i.container)}/restore`, { method: "POST" });
  }
  refreshIntegrations();
}
restoreBtn.addEventListener("click", restoreDismissed);

// ---- settings and edit mode ---------------------------------------------

settingsBtn.append(icon("cog", 20));
settingsBtn.addEventListener("click", () => settingsDlg.showModal());
document.getElementById("settings-close").addEventListener("click", () => settingsDlg.close());
// a click on the backdrop (the dialog itself, outside its content) closes it
settingsDlg.addEventListener("click", (e) => { if (e.target === settingsDlg) settingsDlg.close(); });
settingsDlg.addEventListener("close", () => settingsBtn.focus());

// admins only, and only when update checks are on: the button is not in the page otherwise
const checkUpdatesBtn = document.getElementById("check-updates");
if (checkUpdatesBtn) {
  const checkStatus = document.getElementById("check-updates-status");
  checkUpdatesBtn.addEventListener("click", async () => {
    checkUpdatesBtn.disabled = true;
    checkStatus.textContent = "Checking…";
    try {
      const r = await api("/api/updates/check", { method: "POST" });
      checkStatus.textContent = r.error || EditLogic.updateCheckSummary(r);
    } catch (e) {
      checkStatus.textContent = String(e);
    } finally {
      checkUpdatesBtn.disabled = false;
    }
    refresh(); // paint the new badges now
  });
}

toggleAllBtn.addEventListener("click", () => {
  const boxes = [...boardEl.querySelectorAll(".panel")];
  const collapse = !boxes.every((b) => b.classList.contains("collapsed"));
  boxes.forEach((b) => {
    b.classList.toggle("collapsed", collapse);
    b.querySelector(".ph-main").setAttribute("aria-expanded", String(!collapse));
    view.collapsed[b.dataset.panel] = collapse;
  });
  saveView();
  updateToggleAll();
});

function setEditing(on) {
  editing = on;
  if (on) items.forEach((item) => item.dispose());
  frame.classList.toggle("editing", on);
  editBar.hidden = !on;
  // the same button, in the same place: Customize while viewing, Done while editing
  editBtn.textContent = on ? "Done" : "Customize";
  editBtn.classList.toggle("primary", on);
  editBtn.setAttribute("aria-pressed", String(on));
  render(true);
}
editBtn.addEventListener("click", () => setEditing(!editing));

addBtn.addEventListener("click", () => {
  addGroup();
  changed();
});

// ---- debug panel --------------------------------------------------------
// Reads /api/debug (the dashboard's own activity log, kept in memory on the server). Text
// goes in with textContent only. Only polls while open.

const debugEl = document.getElementById("debug");
const debugBtn = document.getElementById("debug-btn");
const debugLog = document.getElementById("debug-log");
const debugLevel = document.getElementById("debug-level");
const debugAuto = document.getElementById("debug-auto");
const debugWarn = document.getElementById("debug-warn");
let debugEntries = [];
let debugLast = 0;
let debugTimer = null;

function debugLineEl(e) {
  const d = el("div", e.level === "error" ? "err" : e.level === "warn" ? "warn" : "");
  d.append(el("span", "ts", new Date(e.ts * 1000).toLocaleTimeString([], { hour12: false }) + " "));
  d.append(el("span", "src", e.source + " "));
  d.append(e.msg + (e.count > 1 ? ` (x${e.count})` : ""));
  return d;
}

function debugShown() {
  return debugWarn.checked ? debugEntries.filter((e) => e.level !== "info") : debugEntries;
}

function renderDebug() {
  const stick = debugLog.scrollHeight - debugLog.scrollTop - debugLog.clientHeight < 40;
  const rows = debugShown();
  debugLog.replaceChildren(...(rows.length ? rows.map(debugLineEl) : [el("div", "ts", "Nothing recorded yet.")]));
  if (stick) debugLog.scrollTop = debugLog.scrollHeight;
}

async function loadDebug(full) {
  try {
    const body = await api("/api/debug" + (full ? "" : `?since=${debugLast}`));
    if (body.error) return;
    if (full) debugEntries = body.entries;
    else {
      // a repeated line is updated on the server (same id, new count), so merge by id
      for (const e of body.entries) {
        const at = debugEntries.findIndex((x) => x.id === e.id);
        if (at >= 0) debugEntries[at] = e;
        else debugEntries.push(e);
      }
      debugEntries = debugEntries.slice(-2000);
    }
    debugLast = body.last;
    debugLevel.value = body.level;
    renderDebug();
  } catch {}
}

function setDebugOpen(open) {
  debugEl.hidden = !open;
  debugBtn.setAttribute("aria-expanded", String(open));
  clearInterval(debugTimer);
  debugTimer = null;
  if (!open) return;
  loadDebug(true).then(() => { debugLog.scrollTop = debugLog.scrollHeight; });
  if (debugAuto.checked) debugTimer = setInterval(() => loadDebug(false), LOG_REFRESH_MS);
}
// The debug log is admin-only on the server; the role can change while the page is open.
function applyRole() {
  debugBtn.hidden = role !== "admin";
  if (debugBtn.hidden && !debugEl.hidden) setDebugOpen(false);
}
applyRole();
debugBtn.addEventListener("click", () => setDebugOpen(debugEl.hidden));
document.getElementById("debug-close").addEventListener("click", () => { setDebugOpen(false); debugBtn.focus(); });
debugAuto.addEventListener("change", () => setDebugOpen(true));
debugWarn.addEventListener("change", renderDebug);
debugLevel.addEventListener("change", async () => {
  try {
    await api("/api/debug/level", { method: "POST", body: JSON.stringify({ level: debugLevel.value }) });
  } catch {}
  loadDebug(true);
});
// navigator.clipboard exists only on HTTPS or localhost, and the dashboard is often opened
// over plain http on a LAN or tailnet, so fall back to a hidden textarea and execCommand
async function copyText(text) {
  if (window.isSecureContext && navigator.clipboard) {
    try { await navigator.clipboard.writeText(text); return true; } catch {}
  }
  const ta = el("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;";
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch {}
  ta.remove();
  return ok;
}
document.getElementById("debug-copy").addEventListener("click", async (ev) => {
  const text = debugShown().map((e) => `${new Date(e.ts * 1000).toISOString()} ${e.level} ${e.source}: ${e.msg}`).join("\n");
  ev.target.textContent = (await copyText(text)) ? "Copied" : "Copy failed";
  setTimeout(() => { ev.target.textContent = "Copy"; }, 1500);
});

panels.set("host", buildHost());
render(false);
refresh();
refreshIntegrations();

// Right after the admin account is made the page opens on a "searching" screen. It goes away as soon as
// the container list has arrived, with or without other containers (Ratatosk itself is always in it).
// If the list still hasn't arrived after 15 s it asks whether to carry on, for a host where only the
// host stats are wanted.
const welcomeEl = document.getElementById("welcome");
// The screen is a modal: while it shows, the rest of the page can't be reached by keyboard or screen reader.
function setPageInert(on) {
  for (const child of frame.children) if (child !== welcomeEl) child.inert = on;
}
if (location.search.includes("welcome")) history.replaceState(null, "", location.pathname);
function startWelcome() {
  setPageInert(true);
  welcomeEl.tabIndex = -1;
  welcomeEl.focus();
  const SEARCH_MS = 15000, STEP_MS = 1500;
  const title = document.getElementById("welcome-title");
  const text = document.getElementById("welcome-text");
  const spinner = document.getElementById("welcome-spinner");
  const actions = document.getElementById("welcome-actions");
  const proceed = document.getElementById("welcome-proceed");
  const finish = () => {
    welcomeEl.hidden = true;
    setPageInert(false);
  };
  let run = 0; // a newer search cancels an older one
  async function search() {
    const mine = ++run;
    spinner.hidden = false; actions.hidden = true;
    title.textContent = "Searching for containers…";
    text.textContent = "Looking at what is running on this host.";
    const end = Date.now() + SEARCH_MS;
    while (Date.now() < end) {
      await refresh();
      if (mine !== run) return;
      if (loaded) { finish(); return; }
      await new Promise((r) => setTimeout(r, STEP_MS));
      if (mine !== run) return;
    }
    spinner.hidden = true; actions.hidden = false;
    title.textContent = "Could not find any containers";
    text.textContent = "Ratatosk could not get the container list from Docker. Proceed to the dashboard?" +
      (listError ? ` (${listError})` : "");
    proceed.focus();
  }
  proceed.addEventListener("click", () => { run++; finish(); });
  document.getElementById("welcome-retry").addEventListener("click", search);
  search();
}
if (!welcomeEl.hidden) startWelcome();
// Timers are re-armed when the setting changes. A tab that was hidden catches up as soon as it is
// shown again instead of waiting for the next tick (browsers slow timers in background tabs).
let listTimer = null;
let panelTimer = null;
// A tick is skipped while the tab is hidden or the last request is still running.
function startListPolling() {
  clearInterval(listTimer);
  listTimer = setInterval(() => { if (!document.hidden && !refresh.busy()) refresh(); }, view.listRefresh * 1000);
}
function startPanelPolling() {
  clearInterval(panelTimer);
  panelTimer = setInterval(() => { if (!document.hidden && !refreshIntegrations.busy()) refreshIntegrations(); }, view.panelRefresh * 1000);
}
// Saved with the account (not this browser): kept in the group settings like the layout.
groupByProject.addEventListener("change", () => {
  groupSettings.group_by_project = groupByProject.checked;
  changed();
});
const maxColumns = document.getElementById("max-columns");
maxColumns.value = String(view.maxColumns);
maxColumns.addEventListener("change", () => {
  const n = Number(maxColumns.value);
  view.maxColumns = Number.isInteger(n) && n >= 1 && n <= 5 ? n : 3;
  saveView();
  render(false);
});
// one rate select: fill it, show the saved value, and on a pick save, re-arm its own timer and refresh its own data
function rateSelect(id, rates, key, restart, now) {
  const sel = document.getElementById(id);
  for (const [secs, label] of rates) sel.append(new Option(label, String(secs)));
  sel.value = String(view[key]);
  sel.addEventListener("change", () => {
    const n = Number(sel.value);
    view[key] = rates.some(([s]) => s === n) ? n : DEFAULT_RATES[key];
    saveView();
    restart();
    now();
  });
}
rateSelect("list-refresh", LIST_RATES, "listRefresh", startListPolling, refresh);
rateSelect("panel-refresh", PANEL_RATES, "panelRefresh", startPanelPolling, refreshIntegrations);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refresh(); refreshIntegrations(); }
});
startListPolling();
startPanelPolling();
