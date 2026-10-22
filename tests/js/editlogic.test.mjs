import { createRequire } from "node:module";
import test from "node:test";
import assert from "node:assert/strict";

const E = createRequire(import.meta.url)("../../static/editlogic.js");

// ---- restoreEol (a textarea turns every line break into "\n")

test("a CRLF file edited in the textarea is saved with CRLF again", () => {
  const original = "a=1\r\nb=2\r\n";
  assert.equal(E.restoreEol(original, "a=1\nb=3\n"), "a=1\r\nb=3\r\n");
  assert.equal(E.restoreEol(original, "a=1\nb=2\nc=3\n"), "a=1\r\nb=2\r\nc=3\r\n");
});

test("an unedited file comes back exactly, mixed endings included", () => {
  const mixed = "a=1\r\nb=2\nc=3\r";
  assert.equal(E.restoreEol(mixed, "a=1\nb=2\nc=3\n"), mixed);
});

test("an edited file with mixed endings gets the most common one throughout", () => {
  assert.equal(E.restoreEol("a\r\nb\r\nc\n", "a\nb\nc\nd\n"), "a\r\nb\r\nc\r\nd\r\n");
  assert.equal(E.restoreEol("a\nb\nc\r\n", "a\nb\nx\n"), "a\nb\nx\n");
});

test("lone CR files stay lone CR and LF files stay LF", () => {
  assert.equal(E.restoreEol("a=1\rb=2\r", "a=9\nb=2\n"), "a=9\rb=2\r");
  assert.equal(E.restoreEol("a=1\nb=2\n", "a=9\nb=2\n"), "a=9\nb=2\n");
});

test("a file without line breaks takes LF, and a stray CR in the edit is normalised", () => {
  assert.equal(E.restoreEol("a=1", "a=1\nb=2"), "a=1\nb=2");
  assert.equal(E.restoreEol("a=1\n", "a=1\r\nb=2\r"), "a=1\nb=2\n");
});

test("lineEnding breaks ties towards CRLF, then LF", () => {
  assert.equal(E.lineEnding("a\r\nb\n"), "\r\n");
  assert.equal(E.lineEnding("a\nb\rc"), "\n");
  assert.equal(E.lineEnding(""), "\n");
});

// ---- groupNameProblem

test("a group name over the server's limit is refused", () => {
  assert.equal(E.MAX_GROUP_NAME, 64);
  assert.equal(E.groupNameProblem("x".repeat(64), "Old", []), "");
  assert.match(E.groupNameProblem("x".repeat(65), "Old", []), /at most 64/);
  assert.equal(E.groupNameProblem("  " + "x".repeat(64) + "  ", "Old", []), ""); // trimmed first
});

test("a name already in use is refused, but keeping or clearing the name is not a problem", () => {
  const taken = ["Ungrouped", "Media", "Old", "compose-project"];
  assert.match(E.groupNameProblem("Media", "Old", taken), /already a group called "Media"/);
  assert.match(E.groupNameProblem(" Ungrouped ", "Old", taken), /Ungrouped/);
  assert.match(E.groupNameProblem("compose-project", "Old", taken), /compose-project/);
  assert.equal(E.groupNameProblem("Old", "Old", taken), "");
  assert.equal(E.groupNameProblem("   ", "Old", taken), "");
  assert.equal(E.groupNameProblem("Fresh", "Old", taken), "");
});

// ---- coalesce (the save queue)

const tick = () => new Promise((r) => setImmediate(r));

function gated() {
  const calls = [];
  let active = 0;
  let overlap = false;
  const task = () => {
    active++;
    if (active > 1) overlap = true;
    return new Promise((resolve) => {
      calls.push(() => { active--; resolve(); });
    });
  };
  return { calls, task, overlapped: () => overlap };
}

test("calls made while a run is in flight fold into one trailing run", async () => {
  const g = gated();
  const run = E.coalesce(g.task);
  const first = run();
  assert.equal(run.busy(), true);
  run();
  run();
  run();
  assert.equal(g.calls.length, 1);
  g.calls.shift()();
  await tick();
  assert.equal(g.calls.length, 1); // one trailing run, not three
  g.calls.shift()();
  await first;
  await tick();
  assert.equal(g.calls.length, 0);
  assert.equal(run.busy(), false);
  assert.equal(g.overlapped(), false);
});

test("the trailing run sees the state as it is when it starts", async () => {
  let state = 1;
  const sent = [];
  let release;
  const run = E.coalesce(async () => {
    sent.push(state);
    await new Promise((r) => { release = r; });
  });
  run();
  state = 2;
  run();
  state = 3;
  run();
  release();
  await tick();
  release();
  await tick();
  assert.deepEqual(sent, [1, 3]);
});

test("a call after the queue drained starts a fresh run", async () => {
  let n = 0;
  const run = E.coalesce(async () => { n++; });
  await run();
  await tick();
  await run();
  assert.equal(n, 2);
  assert.equal(run.busy(), false);
});

test("a failing run does not wedge the queue or lose the trailing run", async () => {
  const outcomes = [];
  let fail = true;
  let release;
  const run = E.coalesce(async () => {
    await new Promise((r) => { release = r; });
    if (fail) { fail = false; throw new Error("boom"); }
    outcomes.push("ok");
  });
  const first = run();
  const caught = first.catch((e) => e.message);
  run();
  release();
  assert.equal(await caught, "boom");
  await tick();
  release();
  await tick();
  assert.deepEqual(outcomes, ["ok"]);
  assert.equal(run.busy(), false);
});

test("a task that throws at once still frees the queue", async () => {
  let n = 0;
  const run = E.coalesce(() => { n++; throw new Error("sync"); });
  await assert.rejects(run(), /sync/);
  assert.equal(run.busy(), false);
  await assert.rejects(run(), /sync/);
  assert.equal(n, 2);
});

// ---- updateCheckSummary ("Check for updates now")

test("the update check summary says what was checked, found and missed", () => {
  assert.equal(E.updateCheckSummary({ checked: 0, updates: 0, errors: [] }), "No Compose containers with a registry image to check.");
  assert.equal(E.updateCheckSummary({ checked: 1, updates: 0, errors: [] }), "Checked 1 image: no updates.");
  assert.equal(E.updateCheckSummary({ checked: 14, updates: 2, errors: [] }), "Checked 14 images: 2 updates available.");
  assert.equal(E.updateCheckSummary({ checked: 3, updates: 1, errors: [] }), "Checked 3 images: 1 update available.");
  assert.equal(
    E.updateCheckSummary({ checked: 2, updates: 0, errors: [{ image: "ghcr.io/x/y", error: "APIError: denied" }] }),
    "Checked 2 images: no updates. 1 image could not be checked (ghcr.io/x/y); the Debug log has the reason."
  );
  assert.match(E.updateCheckSummary({ checked: 0, updates: 0, errors: [{ image: "", error: "docker down" }] }), /docker down/);
});

// ---- tzMatches (finding a timezone by what was typed)

const ZONES = [
  "Africa/Cairo", "America/Argentina/Buenos_Aires", "America/New_York", "Asia/Hong_Kong", "Asia/Ho_Chi_Minh",
  "Europe/Helsinki", "Europe/London", "Europe/Oslo", "Pacific/Honolulu", "UTC",
];

test("typing a city finds it without the region", () => {
  assert.deepEqual(E.tzMatches(ZONES, "helsinki", 10), ["Europe/Helsinki"]);
  assert.deepEqual(E.tzMatches(ZONES, "Oslo", 10), ["Europe/Oslo"]);
});

test("one letter lists the cities that start with it, not the regions", () => {
  assert.deepEqual(E.tzMatches(ZONES, "h", 10).slice(0, 4), ["Asia/Ho_Chi_Minh", "Asia/Hong_Kong", "Europe/Helsinki", "Pacific/Honolulu"]);
});

test("case, spaces and underscores don't matter", () => {
  assert.deepEqual(E.tzMatches(ZONES, "NEW york", 10), ["America/New_York"]);
  assert.deepEqual(E.tzMatches(ZONES, "new_york", 10), ["America/New_York"]);
  assert.deepEqual(E.tzMatches(ZONES, "  hong kong ", 10), ["Asia/Hong_Kong"]);
});

test("a typed region or a full name works too", () => {
  assert.deepEqual(E.tzMatches(ZONES, "europe/lon", 10), ["Europe/London"]);
  assert.deepEqual(E.tzMatches(ZONES, "europe", 10), ["Europe/Helsinki", "Europe/London", "Europe/Oslo"]);
});

test("a match in the middle of a name comes after prefix matches", () => {
  assert.deepEqual(E.tzMatches(ZONES, "buenos", 10), ["America/Argentina/Buenos_Aires"]);
  assert.deepEqual(E.tzMatches(ZONES, "ires", 10), ["America/Argentina/Buenos_Aires"]);
  // "ong" is inside Hong_Kong only
  assert.deepEqual(E.tzMatches(ZONES, "ong", 10), ["Asia/Hong_Kong"]);
});

test("an exact city beats names that only start with it", () => {
  assert.deepEqual(E.tzMatches(["Europe/Oslo_Extra", "Europe/Oslo", "Asia/Oslo"], "oslo", 10), ["Asia/Oslo", "Europe/Oslo", "Europe/Oslo_Extra"]);
});

test("nothing typed gives everything up to the limit; nothing matching gives nothing", () => {
  assert.equal(E.tzMatches(ZONES, "", 3).length, 3);
  assert.equal(E.tzMatches(ZONES, "   ", 100).length, ZONES.length);
  assert.deepEqual(E.tzMatches(ZONES, "zzz", 10), []);
  assert.deepEqual(E.tzMatches(ZONES, "utc", 10), ["UTC"]);
});

// ---- moveItem and containerTitle (a schedule's list of containers)

test("moveItem moves one entry and leaves the original list alone", () => {
  const list = ["db", "app", "web"];
  assert.deepEqual(E.moveItem(list, 2, -1), ["db", "web", "app"]);
  assert.deepEqual(E.moveItem(list, 0, 1), ["app", "db", "web"]);
  assert.deepEqual(E.moveItem(list, 0, 2), ["app", "web", "db"]);
  assert.deepEqual(list, ["db", "app", "web"]);
});

test("moveItem stops at the ends and ignores a bad index", () => {
  const list = ["db", "app", "web"];
  assert.deepEqual(E.moveItem(list, 0, -1), list);
  assert.deepEqual(E.moveItem(list, 2, 1), list);
  assert.deepEqual(E.moveItem(list, 5, -1), list);
  assert.deepEqual(E.moveItem(list, -1, 1), list);
  assert.deepEqual(E.moveItem(list, 0.5, 1), list);
  assert.deepEqual(E.moveItem([], 0, 1), []);
  assert.notEqual(E.moveItem(list, 0, -1), list); // always a copy
});

const WORDS = { restart: "Restart", update: "Update", auto: "Update/restart" };
const entry = (name, action) => ({ name, action });

test("scheduleTitle names one container, counts several, and leads with a shared action", () => {
  assert.equal(E.scheduleTitle([entry("db", "restart")], WORDS), "Restart db");
  assert.equal(E.scheduleTitle([entry("db", "auto")], WORDS), "Update/restart db");
  assert.equal(E.scheduleTitle([entry("db", "update"), entry("app", "update")], WORDS), "Update 2 containers");
  assert.equal(E.scheduleTitle([entry("a", "restart"), entry("b", "restart"), entry("c", "restart")], WORDS), "Restart 3 containers");
});

test("scheduleTitle drops the action when the containers differ", () => {
  assert.equal(E.scheduleTitle([entry("db", "update"), entry("app", "restart")], WORDS), "2 containers");
  assert.equal(E.scheduleTitle([entry("a", "auto"), entry("b", "update"), entry("c", "restart")], WORDS), "3 containers");
});

// ---- retagImage and pinnedDialogText (the newer-version dialog for a pinned image)

test("retagImage swaps the tag and keeps the registry and path", () => {
  assert.equal(E.retagImage("ghcr.io/o/app:v1.86.0", "v1.87.0"), "ghcr.io/o/app:v1.87.0");
  assert.equal(E.retagImage("nginx:1.25", "1.26"), "nginx:1.26");
});

test("retagImage does not take a registry port for a tag, and drops a digest", () => {
  assert.equal(E.retagImage("host:5000/app", "v2"), "host:5000/app:v2");
  assert.equal(E.retagImage("host:5000/app:v1", "v2"), "host:5000/app:v2");
  assert.equal(E.retagImage("o/app:v1@sha256:abc", "v2"), "o/app:v2");
});

test("pinnedDialogText names the versions and the line to use", () => {
  const t = E.pinnedDialogText("web", { current: "v1.86.0", newer: "v1.87.0", latest: true, reason: null }, "o/app:v1.86.0");
  assert.match(t.intro, /^web is pinned to v1\.86\.0, .*v1\.87\.0 is available\.$/);
  assert.equal(t.newLine, "image: o/app:v1.87.0");
  assert.match(t.ignoreHint, /newer than v1\.87\.0 appears/);
  assert.equal(t.canRetag, true);
  assert.equal(t.latest, true);
});

test("pinnedDialogText turns the compose rewrite off when there is a reason", () => {
  const t = E.pinnedDialogText("web", { current: "v1", newer: "v2", latest: false, reason: "mounted read-only" }, "o/app:v1");
  assert.equal(t.canRetag, false);
  assert.equal(t.latest, false);
});
