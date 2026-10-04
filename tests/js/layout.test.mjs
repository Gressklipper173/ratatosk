import { createRequire } from "node:module";
import test from "node:test";
import assert from "node:assert/strict";

const L = createRequire(import.meta.url)("../../static/layout.js");

const P = (id, kind = "group", rows = 2) => ({ id, kind, rows });
const panels = [P("host", "host"), P("int:sonarr", "int", 1), P("links", "links", 4), P("group:A", "group", 5), P("group:B", "group", 2)];
const empty = { columns: {}, hidden: [] };

test("default deals each panel to the shortest column, in reading order", () => {
  const cols = L.columnsFor(empty, 3, panels);
  assert.equal(cols.length, 3);
  assert.deepEqual(cols.flat().sort(), panels.map((p) => p.id).sort());
  assert.deepEqual(cols[0], ["host", "group:B"]); // host is 7 high, so the second column gets the next
  assert.equal(L.columnsFor(empty, 1, panels)[0].length, panels.length);
});

test("a saved width is used as is, minus panels that no longer exist", () => {
  const layout = { columns: { 2: [["group:A", "gone"], ["host"]] }, hidden: [] };
  const cols = L.columnsFor(layout, 2, panels);
  assert.deepEqual(cols[0].slice(0, 1), ["group:A"]);
  assert.ok(!cols.flat().includes("gone"));
});

test("a panel nobody placed yet lands in the shortest column", () => {
  const layout = { columns: { 2: [["group:A"], []] }, hidden: [] };
  const cols = L.columnsFor(layout, 2, [P("group:A", "group", 5), P("group:New", "group", 1)]);
  assert.deepEqual(cols, [["group:A"], ["group:New"]]);
});

test("a width never arranged is folded from the widest saved one", () => {
  const layout = { columns: { 3: [["a", "d"], ["b", "e"], ["c"]] }, hidden: [] };
  const ps = ["a", "b", "c", "d", "e"].map((id) => P(id));
  assert.deepEqual(L.columnsFor(layout, 1, ps), [["a", "b", "c", "d", "e"]]);
  assert.deepEqual(L.columnsFor(layout, 2, ps), [["a", "c", "e"], ["b", "d"]]);
});

test("4 and 5 columns fold from the widest saved arrangement", () => {
  const layout = { columns: { 3: [["a", "d"], ["b", "e"], ["c"]] }, hidden: [] };
  const ps = ["a", "b", "c", "d", "e"].map((id) => P(id));
  assert.deepEqual(L.columnsFor(layout, 4, ps), [["a", "e"], ["b"], ["c"], ["d"]]);
  assert.deepEqual(L.columnsFor(layout, 5, ps), [["a"], ["b"], ["c"], ["d"], ["e"]]);
});

test("a saved 5-column layout folds down to 3 without losing or unhiding panels", () => {
  const layout = { columns: { 5: [["a"], ["b"], ["c"], ["d"], ["e"]] }, hidden: ["c"] };
  const ps = ["a", "b", "c", "d", "e"].map((id) => P(id));
  const cols = L.columnsFor(layout, 3, ps);
  assert.deepEqual(cols.flat().sort(), ["a", "b", "d", "e"]);
  assert.equal(cols.length, 3);
});

test("default columns with fewer panels than columns leaves some empty", () => {
  const cols = L.defaultColumns([P("a"), P("b")], 5);
  assert.equal(cols.length, 5);
  assert.deepEqual(cols.flat(), ["a", "b"]);
});

test("hidden panels are left out, and not placed again", () => {
  const layout = { columns: { 2: [["host", "links"], ["group:A"]] }, hidden: ["links", "group:B"] };
  const cols = L.columnsFor(layout, 2, panels);
  assert.ok(!cols.flat().includes("links"));
  assert.ok(!cols.flat().includes("group:B"));
  assert.ok(cols.flat().includes("int:sonarr"));
});

test("showing a hidden panel puts it back where it was", () => {
  const layout = { columns: { 2: [["host", "links"], ["group:A"]] }, hidden: ["links"] };
  const shown = L.setHidden(layout, "links", false);
  assert.deepEqual(shown.hidden, []);
  const cols = L.columnsFor(shown, 2, [P("host", "host"), P("links", "links", 3), P("group:A")]);
  assert.deepEqual(cols, [["host", "links"], ["group:A"]]);
});

test("hiding twice records it once", () => {
  const once = L.setHidden(empty, "links", true);
  assert.deepEqual(L.setHidden(once, "links", true).hidden, ["links"]);
});

test("moveInto moves within a column and across columns", () => {
  const current = [["a", "b", "c"], ["d"]];
  const within = L.moveInto(empty, 2, current, "a", 0, 2); // after b and c
  assert.deepEqual(within.columns[2], [["b", "c", "a"], ["d"]]);
  const across = L.moveInto(empty, 2, current, "b", 1, 0);
  assert.deepEqual(across.columns[2], [["a", "c"], ["b", "d"]]);
  const intoEmpty = L.moveInto(empty, 3, [["a"], [], []], "a", 2, 5);
  assert.deepEqual(intoEmpty.columns[3], [[], [], ["a"]]);
});

test("moveInto only changes the width being edited", () => {
  const layout = { columns: { 3: [["a"], ["b"], []], 1: [["a", "b"]] }, hidden: ["x"] };
  const next = L.moveInto(layout, 1, [["a", "b"]], "b", 0, 0);
  assert.deepEqual(next.columns[1], [["b", "a"]]);
  assert.deepEqual(next.columns[3], [["a"], ["b"], []]);
  assert.deepEqual(next.hidden, ["x"]);
  assert.deepEqual(layout.columns[1], [["a", "b"]]); // the input is not changed
});

test("neighbor follows the arrow keys and stops at the edges", () => {
  const current = [["a", "b"], ["c"], []];
  assert.deepEqual(L.neighbor(current, "a", "down"), [0, 1]);
  assert.equal(L.neighbor(current, "a", "up"), null);
  assert.equal(L.neighbor(current, "b", "down"), null);
  assert.deepEqual(L.neighbor(current, "b", "right"), [1, 1]); // clamped to the end
  assert.deepEqual(L.neighbor(current, "c", "right"), [2, 0]);
  assert.equal(L.neighbor(current, "a", "left"), null);
  assert.equal(L.neighbor(current, "zzz", "up"), null);
});

test("rename, remove and prune touch every width and the hidden list", () => {
  const layout = { columns: { 1: [["group:Old", "host"]], 2: [["group:Old"], ["host"]] }, hidden: ["group:Old"] };
  const renamed = L.renameInLayout(layout, "group:Old", "group:New");
  assert.deepEqual(renamed.columns[1], [["group:New", "host"]]);
  assert.deepEqual(renamed.hidden, ["group:New"]);
  const removed = L.removeFromLayout(layout, "group:Old");
  assert.deepEqual(removed.columns[2], [[], ["host"]]);
  assert.deepEqual(removed.hidden, []);
  const pruned = L.pruneLayout(layout, (id) => id === "host");
  assert.deepEqual(pruned.columns[1], [["host"]]);
});

test("the Apps found panel is sized by its row count and gets placed like any other new panel", () => {
  assert.equal(L.estimatedHeight(P("found", "found", 3)), 9);
  const layout = { columns: { 2: [["host"], []] }, hidden: [] };
  const cols = L.columnsFor(layout, 2, [P("host", "host"), P("found", "found", 2)]);
  assert.deepEqual(cols, [["host"], ["found"]]); // the empty column is shorter
});

test("a trailing panel does not move the others on a width nobody arranged", () => {
  const found = P("found", "found", 3);
  for (let n = 1; n <= 5; n++) {
    const without = L.columnsFor(empty, n, panels);
    const withFound = L.columnsFor(empty, n, [...panels, found]).map((c) => c.filter((id) => id !== "found"));
    assert.deepEqual(withFound, without);
  }
});
