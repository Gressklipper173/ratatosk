# Changelog

All notable changes are listed here. Versions follow [semantic versioning](https://semver.org/).

## 0.1.0 (unreleased)

First public version.

- Lists every container with state, health, ports, CPU and memory; start, stop, restart and delete
  stopped containers, update a Compose container's image, and read its logs.
- Groups with priorities, drag-and-drop panels per screen width, link tiles and a host panel with
  storage meters.
- App panels through adapters (Sonarr, Radarr, Seerr, qBittorrent, Pelican) and your own in `/data/adapters`.
- Users with admin and operator roles; changing or resetting a password signs out that account's other sessions.
- Debug panel, `READ_ONLY` mode, `BEHIND_PROXY` mode, `/healthz` and a Docker `HEALTHCHECK`.
- Container labels `ratatosk.name`, `ratatosk.url`, `ratatosk.hide` and `ratatosk.adapter` (the older
  `dashboard.*` names still work).
- Security hardening from a code review:
  - `PROXY_IP_HEADER` (`xff` by default, or `cf` for Cloudflare) picks the one header trusted for the
    visitor's address behind `BEHIND_PROXY`, so a forged header no longer gets past the login throttle.
  - Requests to apps never follow redirects or use a proxy from the environment, so a stored key is
    not re-sent elsewhere, and a slow, malformed or oversized answer is an error for that one panel.
  - Content-Security-Policy, `X-Frame-Options`, `X-Content-Type-Options` and `Cache-Control: no-store`
    on pages and API answers.
  - Sessions last 12 hours; signing out is a POST with a CSRF token; the sign-in and setup forms carry
    one too; unknown user names take as long to check as wrong passwords.
  - The Debug log and container updates are admin-only. Operators can still edit game-server config files.
  - A damaged `users.json` stops start-up (503 while running) instead of re-opening `/setup`.
  - Writes to the users and keys files are serialised and use a unique temporary file.
  - Dependencies are locked with hashes (`requirements.in` → `requirements.txt`, Flask 3.1.3 and
    gunicorn 23.0.0) and the base images are pinned by digest.
- Schedules (admins): restart or update containers at a time of day, daily, weekly or monthly, from
  **Schedule…** in a container's menu or **Settings → Schedules → Schedule update/restart**. A schedule
  holds up to 50 containers, each with its own action: **Update/restart** (update if a newer image
  exists, else restart), **Update** (only if one exists) or **Restart**. They run one after another in
  an order you set, each update waiting for the one before it, and a failure does not stop the rest.
  One timezone, picked in Settings; a run
  missed while Ratatosk was off is skipped and logged; `READ_ONLY` skips them. Adds the `tzdata`
  package to the locked requirements and `schedules.py` to the image.
- **Settings → Updates → Check for updates now** (admins) checks every Compose container's registry on
  demand, stopped containers included. The automatic check also covers stopped containers, repeats
  once a minute after a start if a registry did not answer, and runs when a container with a new image appears.
- Pinned version notice: a Compose container pinned to a version tag (`v1.86.0`, `1.2`, `16-alpine`) gets a
  yellow **Update** button (admins) when the registry has a newer version of the same style (same prefix,
  suffix and number of parts; no `-rc`, `latest` or `nightly`). The dialog offers four choices: edit the
  compose file's `image:` line to the newer version, edit it to `latest`, change it yourself, or ignore.
  The first two pull and recreate the container, need the project folder mounted without `:ro`, change
  only that one line and restore the file if the update fails. Tags are listed with an anonymous request
  to the registry in the same 30-minute pass (off with `UPDATE_CHECK=false`); schedules never change a tag.
  Adds `pinned.py` to the image.
- **Settings → Updates → Ignored version notices** (admins) lists the notices you ignored, with **Un-ignore**.
  An ignored notice stays hidden until a newer version appears. Stored in the new `ignored_updates.json`
  in `/data`; ignoring and un-ignoring still work in `READ_ONLY`.
- `/healthz` also asks Docker and answers 503 when the socket is missing or unusable.
- The example compose file publishes `127.0.0.1:8090` only and sets `no-new-privileges`.
- qBittorrent: a rejected login is not retried for about five minutes, to stay clear of its IP ban.
- Adapters: a file that is not UTF-8 or is over 64 KB is skipped and reported instead of stopping the
  app; `key_file` is stricter (one named file, 64 KB searched, no nested repeats).
- Fixes: hiding the Host panel no longer breaks container refreshes; action buttons re-enable after a
  failed action or a restart; the config editor keeps line endings, rejects values that would corrupt
  a file, and raw mode keeps CRLF files as they were.
- Docs: `READ_ONLY`, the `Secure` cookie, adapter trust, the socket-proxy advice and the `DISABLE_SAMPLER`
  and `HOSTNAME` variables are described as they behave.
