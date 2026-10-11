// Pure helpers for the page (no DOM, no network), kept apart so Node can test them: putting a file's
// line endings back after a raw edit, checking a group name, running a task one at a time,
// finding a timezone by what was typed, and ordering a schedule's list of containers.
// Loaded before app.js; also usable from Node (see tests/js/editlogic.test.mjs).

(function (root) {
  // the server's limit on a group name (app.py MAX_GROUP_NAME)
  const MAX_GROUP_NAME = 64;

  // The line ending a file mostly uses: "\r\n", "\n" or a lone "\r" (ties go in that order). A file
  // with no line break counts as "\n".
  function lineEnding(text) {
    const crlf = (text.match(/\r\n/g) || []).length;
    const lf = (text.match(/\n/g) || []).length - crlf;
    const cr = (text.match(/\r/g) || []).length - crlf;
    if (crlf >= lf && crlf >= cr && crlf > 0) return "\r\n";
    return cr > lf ? "\r" : "\n";
  }

  // A <textarea> turns every line break into "\n", so a file with Windows endings would be saved
  // with Unix ones. Put the original's ending back. Text that did not change comes back exactly as
  // it was (even with mixed endings); an edited file gets its most common ending throughout.
  function restoreEol(original, edited) {
    const flat = (s) => s.replace(/\r\n|\r/g, "\n");
    if (flat(original) === flat(edited)) return original;
    const eol = lineEnding(original);
    return eol === "\n" ? flat(edited) : flat(edited).replace(/\n/g, eol);
  }

  // Why `name` can't be a group's new name, or "" if it can. `taken` lists every name in use
  // (the group being renamed may be in it: keeping its own name is not a clash).
  function groupNameProblem(name, oldName, taken) {
    const wanted = name.trim();
    if (!wanted || wanted === oldName) return "";
    if (wanted.length > MAX_GROUP_NAME) return `A group name can have at most ${MAX_GROUP_NAME} characters.`;
    if (new Set(taken).has(wanted)) return `There is already a group called "${wanted}".`;
    return "";
  }

  // Runs `task` one at a time. A call made while it runs does not start a second copy: it asks for
  // one more run once the current one ends, however many calls came in meanwhile. The task should
  // read the current state when it starts, so that last run sends the newest. Returns a function
  // that gives a promise for the run it joined; `.busy()` says whether a run is going or queued.
  function coalesce(task) {
    let inflight = null;
    let again = false;
    function run() {
      if (inflight) {
        again = true;
        return inflight;
      }
      let finished = false; // a task that throws before its first await is over before `inflight` is set
      const p = (async () => {
        try { await task(); } finally { finished = true; inflight = null; }
      })();
      if (!finished) inflight = p;
      const next = () => { if (again) { again = false; run(); } };
      p.then(next, next);
      return p;
    }
    run.busy = () => inflight !== null;
    return run;
  }

  // One line for the result of "Check for updates now": {checked, updates, errors: [{image, error}]}.
  function updateCheckSummary(r) {
    const n = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
    const errors = r.errors || [];
    if (!r.checked && !errors.length) return "No Compose containers with a registry image to check.";
    let text = `Checked ${n(r.checked, "image")}: ` + (r.updates ? `${n(r.updates, "update")} available.` : "no updates.");
    if (errors.length) {
      const names = errors.map((e) => e.image || e.error).join(", ");
      text += ` ${n(errors.length, "image")} could not be checked (${names}); the Debug log has the reason.`;
    }
    return text;
  }

  // The names in `all` (timezones like "Europe/Helsinki") that match what was typed, best first, at
  // most `limit`. Case, spaces and underscores don't matter ("new york" finds America/New_York), and
  // the city counts before the region, so "hel" puts Helsinki ahead of names that merely contain it.
  // Order: the city is exactly what was typed, then the name or city starts with it, then some word
  // starts with it, then it appears anywhere; ties go alphabetically. Nothing typed gives them all.
  function tzMatches(all, query, limit) {
    const flat = (s) => s.toLowerCase().replace(/[_\s]+/g, " ").trim();
    const q = flat(query);
    const out = [];
    for (const name of all) {
      const full = flat(name);
      const city = full.slice(full.lastIndexOf("/") + 1);
      let rank;
      if (!q) rank = 0;
      else if (city === q) rank = 0;
      else if (city.startsWith(q) || full.startsWith(q)) rank = 1;
      else if (full.split(/[/ ]/).some((w) => w.startsWith(q))) rank = 2;
      else if (full.includes(q)) rank = 3;
      else continue;
      out.push({ name, rank });
    }
    out.sort((a, b) => a.rank - b.rank || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    return out.slice(0, limit).map((x) => x.name);
  }

  // A copy of `list` with the entry at `index` moved `step` places (negative is earlier). It stops at
  // the ends instead of wrapping round, and an index that is not in the list changes nothing.
  function moveItem(list, index, step) {
    const out = list.slice();
    const to = index + step;
    if (!Number.isInteger(index) || index < 0 || index >= out.length || to < 0 || to >= out.length) return out;
    const [entry] = out.splice(index, 1);
    out.splice(to, 0, entry);
    return out;
  }

  // What a schedule calls its containers in a heading: the name for one, a count for several.
  function containerTitle(names) {
    return names.length === 1 ? names[0] : `${names.length} containers`;
  }

  const api = { MAX_GROUP_NAME, lineEnding, restoreEol, groupNameProblem, coalesce, updateCheckSummary, tzMatches, moveItem, containerTitle };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.EditLogic = api;
})(typeof window !== "undefined" ? window : globalThis);
