# Contributing

Thanks for helping. Bug reports, small fixes and new app adapters are all welcome.

## Reporting a problem

Open an issue with what you did, what you expected, what happened, your Ratatosk version (shown in
**Settings**), and the **Debug** panel's output around the problem (it never records keys or
passwords, but check it for container names and addresses before pasting). For a security problem,
follow [SECURITY.md](SECURITY.md) instead.

## Running the tests

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
node --test tests/js/*.mjs
for f in static/*.js; do node --check "$f"; done
```

Dependencies are pinned with hashes: edit `requirements.in`, run the `pip-compile` command at its top to regenerate `requirements.txt`, then `pip-audit -r requirements.txt`. The Dockerfile installs with `--require-hashes`.

The Python tests use a fake Docker client, so they need no Docker daemon. `node --test` runs the Node tests for the
panel layout and editor logic (`static/layout.js`, `static/editlogic.js`), and `node --check` only
checks that each page script parses (a single call with several files checks just the first). There is no browser test setup: a change to the page script should also be checked
by hand in a browser.

## Adding support for an app

Most apps need only a YAML file, no code. See [docs/ADAPTERS.md](docs/ADAPTERS.md). Put the file in
`adapters/`, add tests to `tests/test_integrations.py`, and add the app to the supported-apps table in
the README. An adapter that needs code (a login, a special API) goes through a built-in handler in
`handlers.py`; open an issue first so we can agree on the approach.

## Pull requests

- Keep a change to one thing, and add a test with it.
- Match the style of the code around it. Comments explain why, not what.
- Run all the tests above before you open the PR.
- Never commit keys, passwords or personal addresses. Ratatosk must never send a stored key to the
  browser or write one to the logs.
- App names appear in the page and README only as examples; the app itself stays app-agnostic.
