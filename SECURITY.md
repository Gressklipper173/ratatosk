# Security policy

Ratatosk talks to the Docker socket, so a flaw in it can mean control of the host. Please report
problems privately, not in a public issue.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting**: open the repository's **Security** tab and choose
**Report a vulnerability**. Please include:

- what you found and what an attacker gains,
- the steps or a small proof of concept to reproduce it,
- the Ratatosk version (shown in **Settings**) and how you run it (reverse proxy, `READ_ONLY`, ...).

You will get an answer as soon as a person can read it. This is a small project maintained in spare
time, so there is no guaranteed response time. A fix is released before the details are made public,
and you are credited if you want to be.

## Supported versions

Nothing is tagged yet. Fixes go into the current `main` (the latest version), not into older copies.
Once releases are tagged, only the latest one will be supported.

## What is and isn't a vulnerability here

Ratatosk is meant for a private network, behind a VPN or an access layer. The README's
[Security](README.md#security) section lists what it does and doesn't protect against. In scope, for
example:

- getting past the sign-in, the CSRF check or the role limits (operators can't change keys or users),
- reading a stored app key or password through the page or the API (they are never sent to the browser),
- reaching a container action, file or Docker feature the signed-in role shouldn't have,
- a secret appearing in the Debug log,
- the pinned-version update writing anything other than the single `image:` line of one service, or
  writing to a file that is not a compose file you mounted.

Out of scope:

- anything that needs the Docker socket to be exposed to the internet against the README's advice,
- the fact that anyone signed in can start and stop containers, and that stored app keys are kept
  unencrypted in `/data` (mode 0600, which does not hold back root, and Ratatosk runs as root); both
  are documented behaviour,
- that an admin can make Ratatosk edit the `image:` line of a compose file you mounted without `:ro`
  (documented behaviour; mount it `:ro` to prevent it),
- running Ratatosk without `BEHIND_PROXY` behind a proxy, or behind a proxy that passes the
  visitor's own `X-Forwarded-For` or `CF-Connecting-IP` through (see `PROXY_IP_HEADER` in the README),
- adapters you install in `/data/adapters` yourself: that folder is admin-trust, and an adapter's
  `key_file` can name any file in the matching container (see [docs/ADAPTERS.md](docs/ADAPTERS.md)).
