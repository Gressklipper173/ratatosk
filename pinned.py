"""Version-pinned images: spotting a newer version tag in the registry, and moving a compose file's
`image:` line to it.

An image pinned to `app:v1.86.0` never shows a normal update, because the digest behind that tag
does not change. These helpers find `app:v1.87.0` instead. The functions that read tags or files
take what they need as arguments, so the tests can drive them without a network or Docker.
"""
import http.client
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urljoin, urlparse

import yaml

import activity
import integrations

TIMEOUT = 15
MAX_BODY = 5 * 1024 * 1024
MAX_PAGES = 10
DOCKER_HUB = "registry-1.docker.io"

# prefix, a dotted number, then a suffix that has to match the pinned tag's own (`-alpine`)
_VERSION_RE = re.compile(r"^(?P<prefix>\D*)(?P<number>\d+(?:\.\d+)*)(?P<suffix>(?:[^\d.].*)?)$")


class RewriteError(Exception):
    """The compose file can't be changed safely; the message is shown to the admin."""


# ---- tags ------------------------------------------------------------------


def split_ref(ref):
    """(registry, repository, tag) for an image reference with a tag, else None (no tag, or
    pinned by digest)."""
    if not ref or "@" in ref:
        return None
    name, _, tag = ref.rpartition(":")
    if not name or "/" in tag or not tag:
        return None
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", name
    if registry in ("docker.io", "index.docker.io"):
        registry = DOCKER_HUB
        if "/" not in repo:
            repo = "library/" + repo
    return registry, repo, tag


def parse_version(tag):
    """(prefix, numbers, suffix) for a tag that is a plain version such as `v1.86.0` or
    `16-alpine`, else None."""
    m = _VERSION_RE.match(tag or "")
    if not m:
        return None
    return m["prefix"], tuple(int(n) for n in m["number"].split(".")), m["suffix"]


def newest_tag(current, tags):
    """The highest tag in `tags` that is newer than `current` and written the same way (same
    prefix, same suffix, same number of dotted parts), or None. Tags in another style
    (`latest`, `v1.2.0-rc1`, `nightly`) never match."""
    cur = parse_version(current)
    if not cur:
        return None
    best, best_key = None, cur[1]
    for tag in tags:
        v = parse_version(tag)
        if v and v[0] == cur[0] and v[2] == cur[2] and len(v[1]) == len(cur[1]) and v[1] > best_key:
            best, best_key = tag, v[1]
    return best


def is_newer(tag, than):
    """True when `tag` is a higher version than `than` in the same style. Tags that don't compare
    are not newer."""
    a, b = parse_version(tag), parse_version(than)
    return bool(a and b and a[0] == b[0] and a[2] == b[2] and len(a[1]) == len(b[1]) and a[1] > b[1])


def with_tag(ref, tag):
    """`ref` with its tag replaced."""
    parts = split_ref(ref)
    return ref[: len(ref) - len(parts[2])] + tag


def _challenge(header):
    """The key/value pairs in a `WWW-Authenticate: Bearer ...` header, or {}."""
    if not header or not header.lower().startswith("bearer "):
        return {}
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


def _get(opener, url, token=None):
    """(status, headers, body) for one GET; an HTTP error status is returned, not raised."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        resp = opener.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, e.headers, b""
    with resp:
        body = resp.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError("the registry's answer was too large")
        return resp.status, resp.headers, body


def list_tags(registry, repo, opener=None):
    """Every tag the registry lists for a repository, read anonymously (the usual token
    handshake of Docker Hub, GHCR and Quay). Raises OSError / ValueError when it can't."""
    opener = opener or integrations.make_opener()
    base = f"https://{registry}"
    url = f"{base}/v2/{quote(repo, safe='/')}/tags/list?n=1000"
    token, tags = None, []
    for _ in range(MAX_PAGES):
        status, headers, body = _get(opener, url, token)
        if status == 401 and token is None:
            challenge = _challenge(headers.get("WWW-Authenticate"))
            realm = urlparse(challenge.get("realm", ""))
            if realm.scheme != "https" or not realm.netloc:
                raise ValueError("the registry wants a login")
            query = {"scope": challenge.get("scope") or f"repository:{repo}:pull"}
            if challenge.get("service"):
                query["service"] = challenge["service"]
            t_status, _, t_body = _get(opener, challenge["realm"] + ("&" if realm.query else "?") + urlencode(query))
            if t_status != 200:
                raise ValueError(f"the registry would not hand out a token (HTTP {t_status})")
            data = json.loads(t_body)
            token = data.get("token") or data.get("access_token")
            if not token:
                raise ValueError("the registry would not hand out a token")
            continue
        if status != 200:
            raise ValueError(f"HTTP {status}")
        found = json.loads(body).get("tags") or []
        tags += [t for t in found if isinstance(t, str)]
        nxt = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', headers.get("Link") or "")
        if not nxt:
            return tags
        url = urljoin(url, nxt.group(1))
        if urlparse(url).netloc != registry:
            raise ValueError("the registry paged to another host")
    return tags


def fetch_newest(registry, repo, current):
    """The newest version tag after `current` in a repository, or None. Errors propagate."""
    try:
        return newest_tag(current, list_tags(registry, repo))
    except (OSError, ValueError, http.client.HTTPException) as e:
        activity.log("warn", "updates", f"tag list for {registry}/{repo} failed: {type(e).__name__}: {str(e)[:100]}")
        raise


# ---- compose file ------------------------------------------------------------

_BLANK = re.compile(r"^\s*(#.*)?$")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _service_block(lines, service):
    """(start, end) line indexes of the lines inside `service:` under the top-level `services:`,
    or None."""
    top = next((i for i, l in enumerate(lines) if re.match(r"^services:\s*(#.*)?\r?\n?$", l)), None)
    if top is None:
        return None
    key = re.compile(r"^(\s+)[\"']?" + re.escape(service) + r"[\"']?:\s*(#.*)?\r?\n?$")
    start = None
    for i in range(top + 1, len(lines)):
        if _BLANK.match(lines[i]):
            continue
        if _indent(lines[i]) == 0:  # left the services mapping
            return (start + 1, i) if start is not None else None
        if start is None:
            m = key.match(lines[i])
            if m:
                start, base = i, len(m.group(1))
        elif _indent(lines[i]) <= base:
            return start + 1, i
    return (start + 1, len(lines)) if start is not None else None


def _locate(files, service, ref):
    """(path, lines, index of the image line) for the one `image: <ref>` line of this service in
    the compose files. Raises RewriteError when there is no single plain line to change."""
    owner = None
    for path in files:  # a later file overrides an earlier one
        try:
            with open(path, newline="") as f:
                text = f.read()
            doc = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        svc = ((doc or {}).get("services") or {}).get(service) if isinstance(doc, dict) else None
        if isinstance(svc, dict) and svc.get("image"):
            owner = (path, text, svc["image"])
    if owner is None:
        raise RewriteError("no compose file sets this service's image")
    path, text, image = owner
    if image != ref:
        raise RewriteError("the compose file's image differs from the running one (a variable, or edited since)")
    lines = text.splitlines(keepends=True)
    block = _service_block(lines, service)
    pattern = re.compile(r"^(\s+image:\s*)([\"']?)" + re.escape(ref) + r"\2(\s*(#.*)?)$")
    hits = [i for i in range(*block) if pattern.match(lines[i].rstrip("\r\n"))] if block else []
    if len(hits) != 1:
        raise RewriteError("can't find a plain `image:` line for this service to change (a variable, an anchor or a merge key?)")
    return path, lines, hits[0]


def check_rewrite(files, service, ref):
    """None when the image line can be changed, else the reason it can't."""
    try:
        path, _, _ = _locate(files, service, ref)
    except RewriteError as e:
        return str(e)
    if not (os.access(path, os.W_OK) and os.access(os.path.dirname(path) or ".", os.W_OK)):
        return "the compose folder is mounted read-only for Ratatosk (remove :ro from its mount)"
    return None


def _write(path, text):
    """Replace a file's contents, keeping its mode and owner. A new file and a rename where it
    can, writing in place where the file is itself a mount point."""
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            f.write(text)
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        with open(path, "w", newline="") as f:
            f.write(text)


def rewrite_image(files, service, old_ref, new_ref):
    """Change the service's `image: <old_ref>` line to `<new_ref>`, leaving every other byte of the
    file alone. Returns (path, original text) so the caller can put it back."""
    path, lines, i = _locate(files, service, old_ref)
    original = "".join(lines)
    changed = list(lines)
    changed[i] = lines[i].replace(old_ref, new_ref, 1)
    try:
        _write(path, "".join(changed))
    except OSError as e:
        raise RewriteError(f"could not write {path}: {e.strerror or e}")
    return path, original


def restore(path, original):
    """Put a file back after a failed update. Returns an error text, or None."""
    try:
        _write(path, original)
    except OSError as e:
        return f"could not put {path} back: {e.strerror or e}"
    return None
