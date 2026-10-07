# Before the repo goes public

Nothing here blocks a private repo. Work through it before changing the visibility. Delete this file
(or add it to `.gitignore` and remove it from the repo) once it is done.

## Must do

- [ ] **Check the commit identity.** Every commit must use the GitHub username and the no-reply
      address (`<id>+<username>@users.noreply.github.com`), not a real name or personal email. Check
      the whole history, not just the latest commit. If anything is wrong, fix it before going public;
      a public history can't be taken back.
- [ ] **Re-run the personal-strings search** on the final tree: hostnames, home paths, the tailnet name,
      private IPs, emails, keys, hashes. `PLAN.md` must stay out of the repo.
- [ ] **Rotate the old stack's exposed keys.** The old stack's notes contain a plaintext qBittorrent
      password and a Seerr API key. They must not reach this project, and should be rotated anyway.
- [x] **`SECURITY.md`.** How to report a vulnerability privately (GitHub private reporting or an
      address), and what is supported. This tool holds the Docker socket, so it matters more than usual.
- [ ] **README polish.** Screenshots (light and dark, phone and desktop), a one-line pitch next to the
      name, and a check of the `git clone` URL.
- [ ] **Repo settings.** Description, topics (`docker`, `dashboard`, `self-hosted`, `homelab`,
      `container-management`), licence detected as MIT, private vulnerability reporting on.

## Should do

- [x] **`CONTRIBUTING.md`.** How to run the tests, the adapter route for new apps (`docs/ADAPTERS.md`),
      what a good issue looks like.
- [ ] **One real build and run of the image.** The `HEALTHCHECK`, `COPY VERSION`, the OCI labels and
      the compose example (loopback port, `no-new-privileges`) were written without Docker available.
- [ ] **`CHANGELOG.md`** (written, entry says 0.1.0 unreleased) and a first tagged release (`0.1.0` or `1.0.0`; decide the version scheme).
- [ ] **`.github/`.** Issue and pull request templates, and CI: `pytest`, `node --test`, `node --check`,
      and a Docker build.
- [ ] **Published image.** Build amd64 and arm64 on a tag and push to GHCR, then switch the sample
      compose file to `image: ghcr.io/<owner>/ratatosk:<major>` (keep a `build:` variant for contributors).
      See "Install and update" (roadmap 14) in `PLAN.md`.
- [x] **Version and health check.** Show the version in Settings and `/api/containers`, add an
      unauthenticated `/healthz`, and a `HEALTHCHECK` in the Dockerfile.
- [ ] **Code of conduct**, if you want one.

## Decide

- [x] **Label prefix.** Done: `ratatosk.name|url|hide|adapter`, with the older `dashboard.*` still accepted (the new one wins).
- [ ] **Where to publish images.** The Docker Hub name `ratatosk` is taken (an empty account), so use
      GHCR, or Docker Hub under your own account.
- [ ] **Web presence.** `ratatosk.com` is taken. GitHub Pages is free if you want a docs site.
- [ ] **Funding link** (a "buy me a coffee" line or `.github/FUNDING.yml`), only if it takes off. Payment
      providers usually want identity verification, which is separate from the copyright line.
- [ ] **Stored credentials.** App keys and logins are stored in `/data/integrations.json` unencrypted
      (mode 0600). The README says so. Decide whether to encrypt them before release.

## Known limits to mention or fix

- Single Docker host only; Podman and Kubernetes are not supported or tested.
- A Pelican panel is supported once per Ratatosk; removing one Pelican key removes both.
- Self-detection needs the default container hostname (no custom `hostname:` or host networking).
