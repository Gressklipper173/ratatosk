# Before the repo goes public

Nothing here blocks a private repo. Work through it before changing the visibility. Delete this file
(or add it to `.gitignore` and remove it from the repo) once it is done.

## Where we stopped (2026-10-11)

Done: history and tree audited, CI green, release workflow written but not yet run, README "Known
limits" added, Dependabot set up. Still to do, in order:

1. Merge Dependabot PR #1 (docker CLI digest; check the Update button first). Close #2 (Python 3.14:
   we stay on 3.12, `dependabot.yml` now ignores Python minor and major bumps). Push the
   `dependabot.yml` change.
2. Screenshots and the one-line pitch. Use a **demo instance**, not the real one: no media-app panels,
   no real container names, IPs, hostnames or titles, and crop out browser chrome. Optional: a
   `docs/demo-compose.yml` with harmless containers (nginx, redis, postgres, whoami, game servers)
   so the screenshots are reproducible. Check the images for metadata before committing.
3. Decide what happens to this file (delete, or ignore and untrack) and the optional items under
   "Decide".
4. Go public, then: description and topics, private vulnerability reporting, Dependabot alerts,
   licence shows as MIT. (Private vulnerability reporting and the `SECURITY.md` / issue-chooser
   links only work on a public repo.)
5. Release: change the `CHANGELOG.md` heading from `0.1.0 (unreleased)` to `0.1.0` and push, tag
   `v0.1.0` (the release workflow checks that tag, `VERSION` and the top changelog entry agree),
   wait for the workflow, make the GHCR package public. Do not tag while the repo is private: the
   package would be private and use up the free storage.
6. After the first release works: switch `docker-compose.example.yml` to
   `image: ghcr.io/gressklipper173/ratatosk:0.1` (keep a `build:` variant) and update the README
   quick start; pull the image once from GHCR to check it turns healthy and shows 0.1.0.

Notes for later:

- Pushing files under `.github/workflows/` needs the `workflow` scope on the `gh` token
  (`gh auth refresh -h github.com -s workflow`); already done once.
- The `docker` base-image digest bumps recur when Docker Hub re-pushes a version tag. Check the
  Update button after merging one: CI does not exercise the compose flags it uses.
- If Docker Hub's anonymous pull limit ever fails a CI run, rerun it or add a Docker Hub login step
  with a free account and an access token secret.
- CI tests on Python 3.12 (the image's version). Moving to a newer minor means changing the version
  in `ci.yml` and `release.yml`, the README line about the image, and the Dependabot `ignore` rule.

## Must do

- [x] **Check the commit identity.** All 10 commits use the no-reply address; no private files
      (`PLAN.md`, `docs-private/`, `.claude/`) were ever committed; no personal strings or keys in any
      commit. Re-check the new commits before going public.
- [x] **Re-run the personal-strings search** on the final tree. Done for the tree and the whole
      history on 2026-10-11. Re-run it once more after the screenshots and demo files are added.
- [x] **Old stack's exposed keys.** Not in this repo's history; rotation judged unnecessary (the
      instance is only reachable on the private network), so dropped from this list.
- [x] **`SECURITY.md`.** How to report a vulnerability privately (GitHub private reporting or an
      address), and what is supported. This tool holds the Docker socket, so it matters more than usual.
- [ ] **README polish.** Screenshots (light and dark, phone and desktop), a one-line pitch next to the
      name. The `git clone` URL is checked. See step 2 above for how to take the screenshots.
- [ ] **Repo settings.** Description, topics (`docker`, `dashboard`, `self-hosted`, `homelab`,
      `container-management`), licence detected as MIT, private vulnerability reporting on.

## Should do

- [x] **`CONTRIBUTING.md`.** How to run the tests, the adapter route for new apps (`docs/ADAPTERS.md`),
      what a good issue looks like.
- [x] **One real build and run of the image.** The CI "image" job builds it, runs it with the Docker
      socket and `no-new-privileges`, and waits for the `HEALTHCHECK`. Passing on `main`.
- [ ] **`CHANGELOG.md`** (written, entry says 0.1.0 unreleased) and a first tagged release (`0.1.0`;
      see step 5 above).
- [x] **`.github/`.** Issue forms (bug, feature, new app adapter), a pull request template, CI
      (`pytest`, `node --test`, `node --check`, Docker build and health check) and Dependabot.
      Dependencies install in two steps because `requirements.txt` is hash-locked and the test tools
      are only version-pinned (`requirements-test.txt`, `requirements-dev.txt`).
- [ ] **Published image.** `.github/workflows/release.yml` builds amd64 and arm64 on a `v*.*.*` tag and
      pushes to GHCR (`0.1.0`, `0.1`, `latest`; no bare `0` tag for 0.x). Not run yet. The compose
      switch comes after the first release works (step 6).
- [x] **Version and health check.** Show the version in Settings and `/api/containers`, add an
      unauthenticated `/healthz`, and a `HEALTHCHECK` in the Dockerfile.
- [ ] **Code of conduct**, if you want one.

## Decide

- [x] **Label prefix.** Done: `ratatosk.name|url|hide|adapter`, with the older `dashboard.*` still accepted (the new one wins).
- [x] **Where to publish images.** GHCR (free, tied to the repo). The Docker Hub name `ratatosk` is taken.
- [ ] **Web presence.** `ratatosk.com` is taken. GitHub Pages is free if you want a docs site.
- [ ] **Funding link** (a "buy me a coffee" line or `.github/FUNDING.yml`), only if it takes off. Payment
      providers usually want identity verification, which is separate from the copyright line.
- [ ] **Stored credentials.** App keys and logins are stored in `/data/integrations.json` unencrypted
      (mode 0600). The README says so. Decide whether to encrypt them before release.

## Known limits to mention or fix

Now listed in the README under "Known limits":

- Single Docker host only; Podman and Kubernetes are not supported or tested.
- A Pelican panel is supported once per Ratatosk; removing one Pelican key removes both.
- Self-detection needs the default container hostname (no custom `hostname:` or host networking).
- Stored keys are not encrypted.
