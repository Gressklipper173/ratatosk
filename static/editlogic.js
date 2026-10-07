// Pure helpers for the page (no DOM, no network), kept apart so Node can test them: putting a file's
// line endings back after a raw edit, checking a group name, and running a task one at a time.
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

  const api = { MAX_GROUP_NAME, lineEnding, restoreEol, groupNameProblem, coalesce };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.EditLogic = api;
})(typeof window !== "undefined" ? window : globalThis);
