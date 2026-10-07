"""Built-in handlers: Python for the apps a YAML adapter can't describe.

A handler is named by `handler:` in an adapter that ships with the dashboard. Adapters an
admin drops into /data/adapters can't name one (see integrations.validate_spec), so they
stay data-only. A handler has two functions:

    check(bases, spec, creds) -> None | safe error message   (optional; default: GET test_path)
    fetch(bases, spec, creds) -> [panel, ...]                (raises integrations.AppError)

`creds` is the stored secret: the key string for header auth, or {"username", "password"}
for `auth.kind: login`. A panel is {"id", "title", "rows", "total"} plus an optional
"summary"; a row is {"title", "detail"} plus an optional "progress" (0-100).
"""

import functools
import hashlib
import http.cookiejar
import json
import re
import threading
import time
import urllib.request
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import activity
import editor
import integrations
from integrations import AppError

Handler = namedtuple("Handler", "check fetch")

MAX_ROWS = integrations.MAX_ROWS


def _shape_error():
    return AppError("the app's answer did not have the expected shape")


# what an answer of the wrong shape below the top level raises: .lower() on a number, ["x"] on a
# list, int() of the Infinity that Python's JSON parser accepts
_SHAPE_ERRORS = (AttributeError, KeyError, TypeError, ValueError, OverflowError)


def _guard(fn):
    """fn, with a wrongly shaped answer turned into the AppError the panel list expects."""

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _SHAPE_ERRORS:
            raise _shape_error()

    return inner


def _guard_check(fn):
    """The same for a `check`, which reports a problem as a message instead of raising."""

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _SHAPE_ERRORS:
            return str(_shape_error())

    return inner


# ---- Seerr ------------------------------------------------------------------

SEERR_LISTS = (("pending", "Awaiting approval"), ("unavailable", "Not yet available"))

# (media type, tmdb id) -> title. A request carries no title, only the media id, so each is
# looked up once per process; a failed lookup is shown as "Unknown" and not remembered.
_titles = {}
_titles_lock = threading.Lock()
MAX_TITLES = 1000
MAX_TITLE = 200


def _seerr_title(bases, spec, key, media_type, tmdb_id):
    # the id goes into a path: only a real number may
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
        return "Unknown"
    kind = "movie" if media_type == "movie" else "tv"
    ident = (kind, tmdb_id)
    with _titles_lock:
        if ident in _titles:
            return _titles[ident]
    try:
        _, data = integrations.call(bases, spec, key, f"/api/v1/{kind}/{tmdb_id}")
    except AppError:
        return "Unknown"
    title = (data.get("title") or data.get("name")) if isinstance(data, dict) else None
    if not isinstance(title, str) or not title:
        return "Unknown"
    title = title[:MAX_TITLE]
    with _titles_lock:
        if len(_titles) >= MAX_TITLES:
            _titles.pop(next(iter(_titles)))  # oldest first
        _titles[ident] = title
    return title


def seerr_fetch(bases, spec, key):
    panels = []
    for flt, title in SEERR_LISTS:
        _, data = integrations.call(
            bases, spec, key, "/api/v1/request", {"filter": flt, "take": 20, "sort": "added"}
        )
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise _shape_error()
        results = results[:MAX_ROWS]
        with ThreadPoolExecutor(max_workers=8) as pool:
            names = list(
                pool.map(
                    lambda r: _seerr_title(bases, spec, key, r.get("type"), (r.get("media") or {}).get("tmdbId")),
                    results,
                )
            )
        rows = []
        for r, name in zip(results, names):
            who = (r.get("requestedBy") or {}).get("displayName") or "unknown"
            rows.append({"title": name, "detail": f"{r.get('type') or '?'} · requested by {who}"})
        total = (data.get("pageInfo") or {}).get("results")
        panels.append({"id": flt, "title": title, "rows": rows, "total": total if isinstance(total, int) and not isinstance(total, bool) else len(rows)})
    return panels


# ---- qBittorrent ------------------------------------------------------------

# (base URL, username, hash of the password) -> opener holding the session cookie. In memory
# only; a session that has expired is replaced on the next 401/403. The password is part of the
# key so a changed password never keeps using the old session.
_sessions = {}

# (addresses, username, hash of the password) -> (until, message) for a login qBittorrent refused.
# Polling again with the same wrong password would only earn an IP ban (qBittorrent bans after
# 5 failures), so a refusal is remembered for a while.
_login_denied = {}
QBT_DENIED_TTL = 300
QBT_DENIED = ("the app rejected the login", "qBittorrent refused")

QBT_STATES = {
    "pausedDL": "paused",
    "stoppedDL": "stopped",
    "stalledDL": "stalled",
    "queuedDL": "queued",
    "checkingDL": "checking",
    "metaDL": "fetching metadata",
    "forcedMetaDL": "fetching metadata",
    "error": "error",
    "missingFiles": "missing files",
}


def _send(opener, req):
    """(status, body). Kept apart so tests can stand in for the network."""
    return integrations._open(req, opener)


def _cred_id(creds):
    return creds["username"], hashlib.sha256(str(creds["password"]).encode()).hexdigest()


def forget_sessions():
    """Drop every qBittorrent session and remembered refusal: the stored credentials changed."""
    _sessions.clear()
    _login_denied.clear()


def _qbt_login(base, creds):
    jar = http.cookiejar.CookieJar()
    opener = integrations.make_opener(urllib.request.HTTPCookieProcessor(jar))
    # qBittorrent's CSRF check wants a Referer on the app's own address
    req = urllib.request.Request(
        base + "/api/v2/auth/login",
        data=urlencode({"username": creds["username"], "password": creds["password"]}).encode(),
        headers={"Referer": base},
    )
    status, body = _send(opener, req)
    activity.log("info", "qbittorrent", f"login at {activity.safe_url(base)}: HTTP {status}, {len(body)} byte reply")
    if status == 403:
        raise AppError("qBittorrent refused this address (too many failed logins, or Host header validation)")
    # Older versions answer HTTP 200 "Ok." (and 200 "Fails." for a wrong password); newer ones
    # answer 204 with no body on success and 401 on failure.
    if not ((status == 200 and body.strip() == b"Ok.") or status == 204):
        raise AppError("the app rejected the login")
    # don't trust the login reply alone: an authenticated request must now work
    status, _ = _send(opener, urllib.request.Request(base + "/api/v2/app/version", headers={"Referer": base}))
    if status != 200:
        raise AppError("the app rejected the login")
    return opener


def _qbt_session(bases, creds, fresh=False):
    """(base, opener). `fresh` skips the stored session and any remembered refusal, for when the
    admin has just entered the login or the session is known to be dead."""
    ident = _cred_id(creds)
    if not fresh:
        for b in bases:
            if (b, *ident) in _sessions:
                return b, _sessions[(b, *ident)]
        denied = _login_denied.get((tuple(bases), *ident))
        if denied and denied[0] > time.monotonic():
            raise AppError(denied[1])
    try:
        base, opener = integrations.first_ok(bases, lambda b: _qbt_login(b, creds))
    except AppError as e:
        if str(e).startswith(QBT_DENIED):
            _login_denied[(tuple(bases), *ident)] = (time.monotonic() + QBT_DENIED_TTL, str(e))
        raise
    _login_denied.pop((tuple(bases), *ident), None)
    _sessions[(base, *ident)] = opener
    return base, opener


def _qbt_json(bases, creds, path):
    for attempt in (0, 1):
        base, opener = _qbt_session(bases, creds, fresh=attempt == 1)
        status, body = _send(opener, urllib.request.Request(base + path, headers={"Referer": base}))
        if status in (401, 403) and attempt == 0:
            _sessions.pop((base, *_cred_id(creds)), None)
            continue  # the session expired: log in again once
        if status != 200:
            raise AppError(f"the app answered HTTP {status}")
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise AppError("the app's answer was not JSON")


def qbt_check(bases, spec, creds):
    try:
        _qbt_session(bases, creds, fresh=True)
    except AppError as e:
        return str(e)
    return None


def fmt_speed(bps):
    for unit, size in (("GB/s", 1024**3), ("MB/s", 1024**2), ("KB/s", 1024)):
        if bps >= size:
            return f"{bps / size:.1f} {unit}"
    return f"{int(bps)} B/s"


def fmt_eta(seconds):
    if seconds >= 86400:
        return f"{seconds // 86400} d {seconds % 86400 // 3600} h"
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def _qbt_row(t):
    progress = t.get("progress")
    progress = progress if isinstance(progress, (int, float)) else 0
    parts = [f"{int(progress * 100)}%"]
    label = QBT_STATES.get(t.get("state"))
    speed = t.get("dlspeed") or 0
    eta = t.get("eta")
    if label:
        parts.append(label)
    else:
        if speed:
            parts.append(fmt_speed(speed))
        if isinstance(eta, int) and 0 < eta < 8640000:  # qBittorrent reports 8640000 for "never"
            parts.append("ETA " + fmt_eta(eta))
    return {"title": t.get("name") or "Unknown", "detail": " · ".join(parts), "progress": round(progress * 100, 1)}


def qbt_fetch(bases, spec, creds):
    torrents = _qbt_json(bases, creds, "/api/v2/torrents/info")
    transfer = _qbt_json(bases, creds, "/api/v2/transfer/info")
    if not isinstance(torrents, list) or not isinstance(transfer, dict):
        raise _shape_error()
    active = sorted(
        (t for t in torrents if isinstance(t.get("progress"), (int, float)) and t["progress"] < 1),
        key=lambda t: t["progress"],
    )
    return [
        {
            "id": "downloading",
            "title": "Downloading",
            "rows": [_qbt_row(t) for t in active[:MAX_ROWS]],
            "total": len(active),
            "summary": f"↓ {fmt_speed(transfer.get('dl_info_speed') or 0)}   ↑ {fmt_speed(transfer.get('up_info_speed') or 0)}",
        }
    ]


# ---- Pelican ------------------------------------------------------------------
#
# Two optional keys, each unlocking its own part:
#   application (admin): every server, with name and ports        /api/application/servers
#   client:              live state and power actions             /api/client/...
# Pelican wants "Authorization: Bearer <key>", which the YAML format can't send, hence a handler.
# Power goes through the panel because a stop done behind Wings' back (raw Docker) looks like a
# crash to it, and it restarts the server.

_BEARER = {"auth": {"header": "Authorization"}}
PELICAN_LABELS = {"application": "admin key", "client": "user key"}
PELICAN_PAGE = 100
PELICAN_MAX_PAGES = 10
PELICAN_MAX_STATE_CALLS = 100
PELICAN_SIGNALS = ("start", "stop", "restart")
PELICAN_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")  # \Z: "$" would let a trailing newline through
PELICAN_FILES_DEADLINE = 20  # seconds for the whole walk of a server's files

# The newest answer, shared by the Pelican panel and the container rows (which only read it):
# {"at": time, "data": {"servers": {uuid: {"name", "connect", "state"}}, "state_error": str|None},
#  "bases": the addresses it came from, "gen": bumped by pelican_forget}
_pelican = {"at": 0.0, "data": None, "bases": None, "gen": 0}
_pelican_lock = threading.Lock()


def pelican_cached(max_age=120, bases=None):
    """(age in seconds, servers dict) of the last good answer, or (None, {}) if there is none or
    it is too old to show. With `bases`, only an answer that came from those addresses counts."""
    with _pelican_lock:
        at, data, came_from = _pelican["at"], _pelican["data"], _pelican["bases"]
    age = time.time() - at
    if not data or age > max_age or (bases is not None and tuple(bases) != came_from):
        return None, {}
    return age, data["servers"]


def pelican_forget():
    """Drop the cached answer, and any refresh still running (it would put it back)."""
    with _pelican_lock:
        _pelican.update(at=0.0, data=None, bases=None, gen=_pelican["gen"] + 1)


def _pelican_pages(bases, key, path, params=None):
    """The `attributes` of every row of a paged list. Pages are asked for by number: the
    panel's own "next" links carry its public address, which the dashboard may not reach."""
    rows = []
    for page in range(1, PELICAN_MAX_PAGES + 1):
        query = {**(params or {}), "per_page": PELICAN_PAGE, "page": page}
        _, body = integrations.call(bases, _BEARER, f"Bearer {key}", path, query)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise _shape_error()
        rows += [r["attributes"] for r in data if isinstance(r, dict) and isinstance(r.get("attributes"), dict)]
        total = ((body.get("meta") or {}).get("pagination") or {}).get("total_pages")
        if not isinstance(total, int) or page >= total:
            break
    return rows


def _connect(alloc):
    """'port 25565', or 'ip:port' when the allocation is bound to a specific address."""
    port = alloc.get("port")
    if not isinstance(port, int):
        return None
    ip = alloc.get("ip_alias") or alloc.get("ip")
    return f"port {port}" if ip in (None, "", "0.0.0.0", "::") else f"{ip}:{port}"


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _disk_limit(attrs):
    """The server's allocated disk in bytes, or None when it has none. Pelican gives MiB, and
    0 means unlimited."""
    mib = _number((attrs.get("limits") or {}).get("disk"))
    return int(mib * 1024 * 1024) if mib else None


def _allocations(attrs):
    data = ((attrs.get("relationships") or {}).get("allocations") or {}).get("data")
    return [a["attributes"] for a in data or [] if isinstance(a, dict) and isinstance(a.get("attributes"), dict)]


def _pelican_list(bases, creds):
    """{uuid: {"name", "connect"}}: all servers with the admin key, else the client key's own."""
    out = {}
    if creds.get("application"):
        rows = _pelican_pages(bases, creds["application"], "/api/application/servers", {"include": "allocations"})
        primary = lambda a, allocs: next((x for x in allocs if x.get("id") == a.get("allocation")), None)
    else:
        rows = _pelican_pages(bases, creds["client"], "/api/client")
        primary = lambda a, allocs: next((x for x in allocs if x.get("is_default")), None)
    for a in rows:
        uuid = a.get("uuid")
        if not isinstance(uuid, str) or not PELICAN_UUID.match(uuid):
            continue
        allocs = _allocations(a)
        alloc = primary(a, allocs)
        name = a.get("name")
        out[uuid] = {"name": name if isinstance(name, str) and name else uuid, "connect": _connect(alloc) if alloc else None, "disk_limit": _disk_limit(a)}
    return out


@_guard
def pelican_servers(bases, creds):
    """{"servers": {uuid: {"name", "connect", "state"}}, "state_error": str|None}. `state` is
    Wings' own (offline, starting, running, stopping) and needs the client key; a server that
    key can't see just has no state. Raises AppError when the list itself can't be read."""
    if not (creds.get("application") or creds.get("client")):
        raise AppError("no Pelican key is stored")
    with _pelican_lock:
        gen = _pelican["gen"]
    servers = _pelican_list(bases, creds)
    state_error = None
    if creds.get("client"):
        ids = sorted(servers)[:PELICAN_MAX_STATE_CALLS]

        def state(uuid):
            try:
                _, body = integrations.call(bases, _BEARER, f"Bearer {creds['client']}", f"/api/client/servers/{uuid}/resources")
                attrs = (body.get("attributes") or {}) if isinstance(body, dict) else {}
                value = attrs.get("current_state")
                # bytes on disk, known whether or not the server is running
                used = _number((attrs.get("resources") or {}).get("disk_bytes"))
            except AppError as e:
                return None, None, str(e)
            except _SHAPE_ERRORS:
                return None, None, str(_shape_error())  # one odd answer is that server's problem, not the panel's
            return (value if isinstance(value, str) else None), (int(used) if used is not None else None), None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(state, ids))
        for uuid, (value, used, _) in zip(ids, results):
            servers[uuid]["state"] = value
            servers[uuid]["disk_used"] = used
        errors = [e for _, _, e in results if e]
        # one server the key can't see is normal; every call failing means the key is the problem
        if ids and len(errors) == len(ids):
            state_error = errors[0]
    for info in servers.values():
        info.setdefault("state", None)
        info.setdefault("disk_used", None)
    result = {"servers": servers, "state_error": state_error}
    with _pelican_lock:
        if _pelican["gen"] == gen:  # not if the key was removed while this was running
            _pelican.update(at=time.time(), data=result, bases=tuple(bases))
    return result


def pelican_check(bases, spec, creds):
    """None if every key given is accepted, else a safe message naming the key."""
    for field, path in (("application", "/api/application/servers"), ("client", "/api/client")):
        if creds.get(field):
            try:
                integrations.call(bases, _BEARER, f"Bearer {creds[field]}", path, {"per_page": 1})
            except AppError as e:
                return f"{PELICAN_LABELS[field]}: {e}"
    return None


def pelican_power(bases, creds, uuid, signal):
    """Send start/stop/restart to the panel for one server. Raises AppError with a safe message."""
    key = creds.get("client")
    if not key:
        raise AppError("no Pelican user key is stored")
    if signal not in PELICAN_SIGNALS or not PELICAN_UUID.match(uuid):
        raise AppError("not a power action for a Pelican server")
    # a POST must go to one address only (asking all at once would send the signal twice), so
    # find one that answers with a harmless GET first
    base, _ = integrations.call(bases, _BEARER, f"Bearer {key}", "/api/client", {"per_page": 1})
    req = urllib.request.Request(
        f"{base}/api/client/servers/{uuid}/power",
        data=json.dumps({"signal": signal}).encode(),
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    status, _ = _send(integrations.make_opener(), req)
    if status in (401, 403):
        raise AppError("Pelican refused: the user key can't control this server")
    if status == 404:
        raise AppError("Pelican doesn't know this server for that user key")
    if status not in (200, 202, 204):
        raise AppError(f"Pelican answered HTTP {status}")
    with _pelican_lock:
        _pelican["at"] = 0.0  # the state is about to change: refresh on the next poll


# ---- config files (the editor) -------------------------------------------------------------
# Shapes checked read-only against the real panel on 2026-10-04: `files/list` answers
# {"data": [{"attributes": {name, size, is_file, is_symlink, ...}}]} (not paged), `files/contents`
# answers the raw text, and a missing file is an HTTP 500 with an empty body. The write call
# (POST files/write?file=, raw body) has not been tried on the real panel.

def _file_url(base, uuid, action, **query):
    return f"{base}/api/client/servers/{uuid}/files/{action}?{urlencode(query)}"


def _bearer_request(url, key, method="GET", data=None):
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "text/plain"
    return urllib.request.Request(url, data=data, headers=headers, method=method)


@_guard
def pelican_state(bases, creds, uuid):
    """Wings' own state for one server ("offline", "running", ...), or None if it isn't reported."""
    if not PELICAN_UUID.match(uuid):
        raise AppError("not a Pelican server")
    _, body = integrations.call(bases, _BEARER, f"Bearer {creds['client']}", f"/api/client/servers/{uuid}/resources")
    value = (body.get("attributes") or {}).get("current_state") if isinstance(body, dict) else None
    return value if isinstance(value, str) else None


def _list_dir(bases, creds, uuid, directory):
    _, body = integrations.call(bases, _BEARER, f"Bearer {creds['client']}", f"/api/client/servers/{uuid}/files/list", {"directory": directory})
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise _shape_error()
    return [r["attributes"] for r in data if isinstance(r, dict) and isinstance(r.get("attributes"), dict) and isinstance(r["attributes"].get("name"), str)]


@_guard
def pelican_files(bases, creds, uuid):
    """(files, truncated): config-looking files in a server's tree, each {"path", "size"}.
    Breadth first, bounded by depth and by the number of entries looked at."""
    if not PELICAN_UUID.match(uuid):
        raise AppError("not a Pelican server")
    found, seen, queue = [], 0, [("/", 0)]
    deadline = time.monotonic() + PELICAN_FILES_DEADLINE
    while queue:
        if time.monotonic() > deadline:
            return found, True  # out of time: what was found so far, marked as cut short
        directory, depth = queue.pop(0)
        try:
            entries = _list_dir(bases, creds, uuid, directory)
        except AppError:
            if directory == "/":
                raise  # nothing listed at all: the key or the server is the problem
            continue
        for e in entries:
            seen += 1
            if seen > editor.MAX_ENTRIES:
                return found, True
            path = f"{directory.rstrip('/')}/{e['name']}"
            if e.get("is_file"):
                if editor.is_candidate(e["name"], e.get("size")):
                    found.append({"path": path, "size": e["size"]})
            elif not e.get("is_symlink") and depth < editor.MAX_DEPTH and not editor.skip_dir(e["name"]):
                queue.append((path, depth + 1))
    return found, False


def _listed(bases, creds, uuid, path):
    """The size the panel reports for `path`, or None: only files it lists can be read or written."""
    parent, _, name = path.rpartition("/")
    for e in _list_dir(bases, creds, uuid, parent or "/"):
        if e["name"] == name and e.get("is_file") and not e.get("is_symlink"):
            return e.get("size")
    return None


def pelican_file_read(bases, creds, uuid, path):
    """The text of one listed file. Raises AppError with a safe message."""
    if not PELICAN_UUID.match(uuid) or not editor.safe_path(path):
        raise AppError("not a file the editor can open")
    size = _listed(bases, creds, uuid, path)
    if size is None or not editor.is_candidate(path.rsplit("/", 1)[-1], size):
        raise AppError("the panel doesn't list that file")

    def get(base):
        status, body = _send(integrations.make_opener(), _bearer_request(_file_url(base, uuid, "contents", file=path), creds["client"]))
        if status in (401, 403):
            raise AppError("Pelican refused: the user key can't read this server's files")
        if status != 200:
            raise AppError(f"Pelican couldn't read that file (HTTP {status})")
        return body

    _, body = integrations.first_ok(bases, get)
    if len(body) > editor.MAX_SIZE:
        raise AppError("that file is too large to edit here")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise AppError("that file isn't UTF-8 text")


def pelican_file_write(bases, creds, uuid, path, text):
    """Replace one listed file. One POST to one address (found with a harmless GET first)."""
    if not PELICAN_UUID.match(uuid) or not editor.safe_path(path):
        raise AppError("not a file the editor can write")
    data = text.encode("utf-8")
    if len(data) > editor.MAX_SIZE:
        raise AppError("that file would be too large")
    if _listed(bases, creds, uuid, path) is None:
        raise AppError("the panel doesn't list that file")
    base, _ = integrations.call(bases, _BEARER, f"Bearer {creds['client']}", "/api/client", {"per_page": 1})
    status, _ = _send(integrations.make_opener(), _bearer_request(_file_url(base, uuid, "write", file=path), creds["client"], "POST", data))
    if status in (401, 403):
        raise AppError("Pelican refused: the user key can't write this server's files")
    if status not in (200, 204):
        raise AppError(f"Pelican couldn't save the file (HTTP {status})")


def pelican_fetch(bases, spec, creds):
    result = pelican_servers(bases, creds)
    servers = sorted(({"uuid": u, **s} for u, s in result["servers"].items()), key=lambda s: s["name"].lower())
    # `server` and `state` let the page put Start/Stop/Restart on a row; the power buttons only
    # exist with the user key, which is what sends them
    rows = [
        {"title": s["name"], "detail": " · ".join(x for x in (s["connect"], s["state"]) if x), "server": s["uuid"], "state": s["state"]}
        for s in servers[:MAX_ROWS]
    ]
    # disk use needs the user key, the allocation comes with either key; a row shows what is known
    for r, s in zip(rows, servers):
        if s["disk_used"] is not None or s["disk_limit"] is not None:
            r["disk"] = {"used": s["disk_used"], "limit": s["disk_limit"]}
    panel = {"id": "servers", "title": "Game servers", "rows": rows, "total": len(servers), "power": bool(creds.get("client"))}
    if creds.get("client"):
        running = sum(1 for s in servers if s["state"] == "running")
        if result["state_error"]:
            panel["summary"] = "state unavailable"
        elif len(servers) > PELICAN_MAX_STATE_CALLS:
            panel["summary"] = f"{running} running (of the first {PELICAN_MAX_STATE_CALLS})"  # only those were asked about
        else:
            panel["summary"] = f"{running} running"
    return [panel]


HANDLERS = {
    "seerr": Handler(check=None, fetch=_guard(seerr_fetch)),
    "qbittorrent": Handler(check=_guard_check(qbt_check), fetch=_guard(qbt_fetch)),
    "pelican": Handler(check=_guard_check(pelican_check), fetch=_guard(pelican_fetch)),
}
