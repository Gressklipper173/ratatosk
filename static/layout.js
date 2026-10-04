// Panel layout: where each panel sits. No DOM in here, so it can be tested with Node.
//
// A layout is {columns: {"1": [[id...]], "2": [[...], [...]], ... "5": [...]}, hidden: [id...]}.
// There is one arrangement per column count, so arranging on a phone never changes the
// desktop. A width that was never arranged is derived from another one (see `columnsFor`).
// A panel id is "host", "links", "int:<container>" or "group:<name>".
(function (root) {
  "use strict";

  const WIDTHS = [5, 4, 3, 2, 1];

  // a rough height in "lines", to deal panels to the shortest column
  function estimatedHeight(panel) {
    const rows = panel.rows || 0;
    if (panel.kind === "host") return 7;
    if (panel.kind === "links") return 3 + Math.ceil(rows / 2) * 2;
    if (panel.kind === "int") return 4 + rows * 2;
    return 3 + rows * 3; // a group: one row per container
  }

  function shortest(heights) {
    let best = 0;
    for (let i = 1; i < heights.length; i++) if (heights[i] < heights[best]) best = i;
    return best;
  }

  // Deals `panels` (in reading order) to n columns, each to the currently shortest one.
  function defaultColumns(panels, n) {
    const cols = Array.from({ length: n }, () => []);
    const heights = new Array(n).fill(0);
    for (const p of panels) {
      const i = shortest(heights);
      cols[i].push(p.id);
      heights[i] += estimatedHeight(p);
    }
    return cols;
  }

  // Reading order of a set of columns: row by row, left to right.
  function flatten(columns) {
    const out = [];
    const longest = Math.max(0, ...columns.map((c) => c.length));
    for (let r = 0; r < longest; r++) for (const c of columns) if (r < c.length) out.push(c[r]);
    return out;
  }

  // The same panels in `n` columns: reading order kept, dealt round-robin.
  function fold(columns, n) {
    const cols = Array.from({ length: n }, () => []);
    flatten(columns).forEach((id, i) => cols[i % n].push(id));
    return cols;
  }

  // The columns to show at width n: ids of visible panels only. `panels` lists every panel
  // that exists ({id, kind, rows}), in the order a default layout should use.
  function columnsFor(layout, n, panels) {
    const known = new Map(panels.map((p) => [p.id, p]));
    const hidden = new Set(layout.hidden || []);
    const saved = layout.columns || {};
    let base = saved[String(n)];
    if (!base) {
      // the widest saved arrangement is the best source for a width never arranged
      const from = WIDTHS.find((w) => saved[String(w)]);
      base = from ? fold(saved[String(from)], n) : null;
    }
    const cols = base ? base.map((c) => c.filter((id) => known.has(id))) : defaultColumns(panels.filter((p) => !hidden.has(p.id)), n);
    const placed = new Set(cols.flat());
    const heights = cols.map((c) => c.reduce((sum, id) => sum + estimatedHeight(known.get(id)), 0));
    // a panel not placed yet (a new group, a newly found app) goes to the shortest column
    for (const p of panels) {
      if (placed.has(p.id) || hidden.has(p.id)) continue;
      const i = shortest(heights);
      cols[i].push(p.id);
      heights[i] += estimatedHeight(p);
    }
    return cols.map((c) => c.filter((id) => !hidden.has(id)));
  }

  // Where `id` is in `columns`: [column, index], or null.
  function find(columns, id) {
    for (let c = 0; c < columns.length; c++) {
      const i = columns[c].indexOf(id);
      if (i >= 0) return [c, i];
    }
    return null;
  }

  // `current` is what is shown now (columnsFor's result). Returns the layout with `id`
  // moved to column `col` at `index` (counted after the panel has left its old place).
  function moveInto(layout, n, current, id, col, index) {
    const cols = current.map((c) => c.filter((x) => x !== id));
    const c = Math.max(0, Math.min(col, n - 1));
    const i = Math.max(0, Math.min(index, cols[c].length));
    cols[c].splice(i, 0, id);
    return { columns: { ...layout.columns, [String(n)]: cols }, hidden: [...(layout.hidden || [])] };
  }

  // The [column, index] one step from `id` for an arrow key, or null at an edge.
  function neighbor(current, id, dir) {
    const at = find(current, id);
    if (!at) return null;
    const [c, i] = at;
    if (dir === "up") return i > 0 ? [c, i - 1] : null;
    if (dir === "down") return i < current[c].length - 1 ? [c, i + 1] : null;
    const to = dir === "left" ? c - 1 : c + 1;
    if (to < 0 || to >= current.length) return null;
    return [to, Math.min(i, current[to].length)];
  }

  function setHidden(layout, id, hide) {
    const rest = (layout.hidden || []).filter((x) => x !== id);
    return { columns: layout.columns || {}, hidden: hide ? [...rest, id] : rest };
  }

  function mapIds(layout, fn) {
    const columns = {};
    for (const [w, cols] of Object.entries(layout.columns || {})) {
      columns[w] = cols.map((c) => c.map(fn).filter((x) => x !== null));
    }
    return { columns, hidden: (layout.hidden || []).map(fn).filter((x) => x !== null) };
  }

  function renameInLayout(layout, oldId, newId) {
    return mapIds(layout, (x) => (x === oldId ? newId : x));
  }

  function removeFromLayout(layout, id) {
    return mapIds(layout, (x) => (x === id ? null : x));
  }

  // Drops ids `isKnown` rejects, so stale ones don't pile up toward the server's limit.
  function pruneLayout(layout, isKnown) {
    return mapIds(layout, (x) => (isKnown(x) ? x : null));
  }

  const api = { estimatedHeight, defaultColumns, flatten, fold, columnsFor, find, moveInto, neighbor, setHidden, renameInLayout, removeFromLayout, pruneLayout };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Layout = api;
})(typeof window !== "undefined" ? window : globalThis);
