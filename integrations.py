"""Adapters: YAML descriptions of apps the dashboard can pull a panel from.

An adapter is data, not code: it names an app's API endpoints and the header its key goes
in. Nothing here runs a spec's contents; the only thing a spec can make this module do is
GET paths on the app's own API, with a key the admin approved. See docs/ADAPTERS.md.
"""

import http.client
import io
import json
import os
import re
import tarfile
import urllib.error
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from urllib.parse import urlencode

import yaml

import activity

REQUEST_TIMEOUT = 5  # per socket operation
REQUEST_DEADLINE = 15  # for a whole answer: a server dripping one byte at a time can't hold a thread
FIRST_OK_TIMEOUT = 30  # for every address of one call together
MAX_KEY_FILE = 1024 * 1024
MAX_KEY_SCAN = 64 * 1024  # how much of the key file a regex is run over
MAX_RESPONSE = 4 * 1024 * 1024
MAX_ADAPTER_FILE = 64 * 1024
MAX_ROWS = 50

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TEMPLATE_RE = re.compile(r"\{([A-Za-z0-9_.]+)\}")


class SpecError(ValueError):
    pass


def has_nested_repeat(regex):
    """True if a group that holds a repeat is itself repeated, e.g. (a+)+ or (?:a|b*){2,}: the
    classic way to make a pattern take exponential time. A rough check, not a proof; the text
    a pattern is run over is capped too."""
    stack, i = [False], 0  # per open group: does it hold a repeat?
    while i < len(regex):
        ch = regex[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":  # a character class: nothing inside it repeats
            i += 2 if regex[i + 1:i + 2] == "^" else 1
            i += 1  # a "]" right at the start is a literal
            while i < len(regex) and regex[i] != "]":
                i += 2 if regex[i] == "\\" else 1
        elif ch == "(":
            stack.append(False)
        elif ch == ")" and len(stack) > 1:
            held = stack.pop()
            nxt = regex[i + 1:i + 2]
            if nxt in ("+", "*") or (nxt == "{" and re.match(r"\{\d*,\d*\}", regex[i + 1:])):
                if held:
                    return True
                held = True
            stack[-1] = stack[-1] or held
        elif ch in "+*" or (ch == "{" and re.match(r"\{\d*,\d*\}", regex[i:])):
            stack[-1] = True
        i += 1
    return False


def _need(cond, msg):
    if not cond:
        raise SpecError(msg)


def _text(v, field, limit=200):
    _need(isinstance(v, str) and 0 < len(v) <= limit, f"{field} must be a non-empty string")
    return v


def _path(v, field, url=True):
    """`url` paths are put into a request, so they may not hold spaces either; a key_file
    path is only a file name inside the container."""
    _text(v, field)
    _need(v.startswith("/") and "://" not in v and ".." not in v.split("/"), f"{field} must be a path starting with /")
    _need(not re.search(r"[\x00-\x1f\x7f]", v) and not (url and re.search(r"\s", v)), f"{field} must not contain spaces or control characters")
    return v


def validate_spec(data, trusted=False, handlers=None):
    """A cleaned copy of one adapter spec; raises SpecError saying what is wrong.
    `trusted` is true for the adapters shipped with the dashboard: only those may name a
    Python handler. `handlers` (names that exist) is checked when given."""
    _need(isinstance(data, dict), "the file must contain a mapping")
    out = {}
    out["id"] = _text(data.get("id"), "id", 32)
    _need(ID_RE.match(out["id"]), "id must be lowercase letters, digits, - or _")
    out["name"] = _text(data.get("name"), "name", 64)

    handler = data.get("handler")
    if handler is not None:
        _need(trusted, "handler is only available to adapters shipped with the dashboard")
        _need(isinstance(handler, str) and ID_RE.match(handler), "handler must be a handler name")
        _need(handlers is None or handler in handlers, f"unknown handler {handler}")
        out["handler"] = handler

    match = data.get("match")
    _need(isinstance(match, dict), "match must be a mapping")
    images = match.get("images")
    _need(isinstance(images, list) and images and len(images) <= 20, "match.images must be a list of image names")
    out["match"] = {"images": [_text(i, "match.images entry", 128).lower() for i in images]}
    port = match.get("port")
    _need(isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536, "match.port must be a port number")
    out["match"]["port"] = port

    auth = data.get("auth")
    _need(isinstance(auth, dict), "auth must be a mapping")
    kind = auth.get("kind", "header")
    _need(kind in ("header", "login", "keys"), "auth.kind must be header, login or keys")
    out["auth"] = {"kind": kind}
    if kind == "header":
        header = _text(auth.get("header"), "auth.header", 64)
        _need(HEADER_RE.match(header), "auth.header must be a plain header name")
        out["auth"]["header"] = header
    elif kind == "login":
        _need(handler is not None, "auth.kind login needs a handler that performs the login")
    else:
        _need(handler is not None, "auth.kind keys needs a handler that uses the keys")
        keys = auth.get("keys")
        _need(isinstance(keys, list) and 0 < len(keys) <= 4, "auth.keys must list 1-4 keys")
        out["auth"]["keys"] = []
        for i, k in enumerate(keys):
            _need(isinstance(k, dict), f"auth.keys[{i}] must be a mapping")
            kid = _text(k.get("id"), f"auth.keys[{i}].id", 32)
            _need(ID_RE.match(kid), f"auth.keys[{i}].id must be lowercase letters, digits, - or _")
            suff = k.get("sufficient", False)
            _need(isinstance(suff, bool), f"auth.keys[{i}].sufficient must be true or false")
            out["auth"]["keys"].append({"id": kid, "label": _text(k.get("label"), f"auth.keys[{i}].label", 64), "sufficient": suff})
        _need(len({k["id"] for k in out["auth"]["keys"]}) == len(keys), "auth.keys ids must be unique")

    if "handler" not in out or data.get("test_path") is not None:
        out["test_path"] = _path(data.get("test_path"), "test_path")

    key_file = data.get("key_file")
    if key_file is not None:
        _need(kind == "header", "key_file only applies to header auth; logins and key sets are entered by hand")
        _need(isinstance(key_file, dict), "key_file must be a mapping")
        regex, json_path = key_file.get("regex"), key_file.get("json_path")
        _need((regex is None) != (json_path is None), "key_file needs exactly one of regex or json_path")
        out["key_file"] = {"path": _path(key_file.get("path"), "key_file.path", url=False)}
        if regex is not None:
            _text(regex, "key_file.regex", 200)
            try:
                compiled = re.compile(regex)
            except re.error as e:
                raise SpecError(f"key_file.regex is not a valid pattern: {e}")
            _need(compiled.groups == 1, "key_file.regex needs exactly one (capture group) around the key")
            _need(not has_nested_repeat(regex), "key_file.regex may not repeat a group that is itself repeated, like (a+)+")
            out["key_file"]["regex"] = regex
        else:
            _text(json_path, "key_file.json_path", 100)
            _need(re.match(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$", json_path), "key_file.json_path must look like main.apiKey")
            out["key_file"]["json_path"] = json_path

    panels = data.get("panels")
    if "handler" in out and panels is None:
        panels = []
    else:
        _need(isinstance(panels, list) and 0 < len(panels) <= 10, "panels must be a list of 1-10 panels")
    out["panels"] = []
    for i, p in enumerate(panels):
        where = f"panels[{i}]"
        _need(isinstance(p, dict), f"{where} must be a mapping")
        params = p.get("params", {})
        _need(isinstance(params, dict) and len(params) <= 20, f"{where}.params must be a mapping")
        _need(
            all(isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in params.items()),
            f"{where}.params values must be plain text or numbers",
        )
        panel = {
            "id": _text(p.get("id"), f"{where}.id", 32),
            "title": _text(p.get("title"), f"{where}.title", 64),
            "path": _path(p.get("path"), f"{where}.path"),
            "params": {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items()},
            "rows": _text(p.get("rows"), f"{where}.rows", 100),
            "row_title": _text(p.get("row_title"), f"{where}.row_title"),
        }
        if p.get("row_detail") is not None:
            panel["row_detail"] = _text(p.get("row_detail"), f"{where}.row_detail")
        if p.get("total") is not None:
            panel["total"] = _text(p.get("total"), f"{where}.total", 100)
        out["panels"].append(panel)
    _need(len({p["id"] for p in out["panels"]}) == len(out["panels"]), "panel ids must be unique")
    return out


def load_adapters(*dirs, trusted=(), handlers=None):
    """({id: spec}, [problem strings]). Later directories override earlier ones by id, so a
    user's file in /data/adapters can replace a built-in. A bad file is reported and skipped.
    Only files from the directories in `trusted` may name a Python handler."""
    adapters, problems = {}, []
    for d in dirs:
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith((".yml", ".yaml")))
        except OSError:
            continue
        for n in names:
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    text = f.read(MAX_ADAPTER_FILE + 1)
                if len(text) > MAX_ADAPTER_FILE:
                    raise SpecError(f"the file is over {MAX_ADAPTER_FILE // 1024} KB")
                spec = validate_spec(yaml.safe_load(text), trusted=d in trusted, handlers=handlers)
            # ValueError covers SpecError and a file that is not UTF-8; deep nesting is a RecursionError
            except (OSError, yaml.YAMLError, ValueError, RecursionError) as e:
                problems.append(f"{n}: {e}")
                continue
            adapters[spec["id"]] = spec
    return adapters, problems


# ---- matching and reaching an app ------------------------------------------

def image_name(ref):
    """'lscr.io/linuxserver/sonarr:latest' -> 'sonarr'"""
    ref = ref.split("@")[0]
    last = ref.rsplit("/", 1)[-1]
    return last.split(":")[0].lower()


def image_repo(ref):
    """'ghcr.io/pelican/panel:latest' -> 'ghcr.io/pelican/panel'"""
    ref = ref.split("@")[0]
    head, _, last = ref.rpartition("/")
    return (head + "/" if head else "") + last.split(":")[0].lower()


def label(labels, key):
    """A Ratatosk container label: `ratatosk.<key>`, else the older `dashboard.<key>`, else ""."""
    return str(labels.get("ratatosk." + key) or labels.get("dashboard." + key) or "")


def match_adapter(adapters, labels, image_ref):
    """The adapter for a container, or None. The `ratatosk.adapter` label (or the older
    `dashboard.adapter`) wins: an adapter id picks it, `none` switches the notice off."""
    wanted = label(labels, "adapter").strip().lower()
    if wanted == "none":
        return None
    if wanted:
        return adapters.get(wanted)
    name = image_name(image_ref)
    repo = image_repo(image_ref)
    for spec in adapters.values():
        for entry in spec["match"]["images"]:
            # "panel" is too common a last part: an entry with a slash names the repository
            # ("pelican/panel" matches ghcr.io/pelican/panel)
            if entry == name or ("/" in entry and (repo == entry or repo.endswith("/" + entry))):
                return spec
    return None


def candidate_urls(attrs, container_port):
    """Base URLs to try: the container's address on each network it joined, then the
    published port via the Docker host."""
    urls = []
    net = attrs.get("NetworkSettings", {})
    for n in (net.get("Networks") or {}).values():
        ip = n.get("IPAddress")
        if ip:
            urls.append(f"http://{ip}:{container_port}")
    for b in (net.get("Ports") or {}).get(f"{container_port}/tcp") or []:
        urls.append(f"http://host.docker.internal:{b['HostPort']}")
    return urls


class AppError(Exception):
    """An app call failed; the message is safe to show (it never contains the key)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error, never followed: the request carries a key, and the address came from
    the container, so it must not be sent anywhere else."""

    def redirect_request(self, *args, **kwargs):
        return None


def make_opener(*extra):
    """The one way requests to apps are made: no redirects, and never through a proxy from the
    environment (it would see the key). `extra` are further handlers, e.g. a cookie processor."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), *extra)


def _read_body(resp, deadline):
    """The whole body, read in chunks so the total time and size can be held to a limit.
    Raises AppError past either."""
    read = getattr(resp, "read1", resp.read)  # read1 returns what has arrived instead of waiting for a full chunk
    chunks, size = [], 0
    while True:
        if time.monotonic() > deadline:
            raise AppError("the app took too long to answer")
        chunk = read(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_RESPONSE:
            raise AppError("the app's answer was too large")


def _open(req, opener=None, timeout=REQUEST_TIMEOUT):
    """(status, body) for one request. An HTTP error status is returned like any other, with an
    empty body; anything else that goes wrong (no answer, a malformed one, too slow, too big)
    raises AppError. Every request to an app goes through here."""
    where = f"{req.get_method()} {activity.safe_url(req.full_url)}"
    started = time.monotonic()

    def took():
        return f"{int((time.monotonic() - started) * 1000)} ms"

    try:
        try:
            resp = (opener or make_opener()).open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            e.close()
            activity.log("warn", "adapter", f"{where} -> HTTP {e.code} ({took()})")
            return e.code, b""
        with resp:
            status = resp.status
            body = _read_body(resp, started + REQUEST_DEADLINE)
    except AppError as e:
        activity.log("warn", "adapter", f"{where} -> {e} ({took()})")
        raise
    # a malformed answer (BadStatusLine, IncompleteRead), a bad address (InvalidURL) or a header
    # that can't be encoded must not escape as a 500 for everyone's panels
    except (OSError, http.client.HTTPException, ValueError, RecursionError) as e:
        reason = getattr(e, "reason", e)
        activity.log("info", "adapter", f"{where} -> no answer: {type(reason).__name__} ({took()})", verbose=True)
        raise AppError("could not reach the app")
    activity.log("info", "adapter", f"{where} -> HTTP {status}, {len(body)} bytes ({took()})", verbose=True)
    return status, body


def http_get_json(url, header, key, timeout=REQUEST_TIMEOUT):
    req = urllib.request.Request(url, headers={header: key, "Accept": "application/json"})
    status, body = _open(req, timeout=timeout)
    if status >= 300:  # includes a redirect, which is never followed
        if status in (401, 403):
            raise AppError("the app rejected the key")
        raise AppError(f"the app answered HTTP {status}")
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        activity.log("warn", "adapter", f"GET {activity.safe_url(url)} -> HTTP {status}, {len(body)} bytes that are not JSON")
        raise AppError("the app's answer was not JSON")


REJECTED = ("the app rejected the key", "the app rejected the login")


def first_ok(bases, fn):
    """Runs fn(base) for every candidate address at once; returns (base, result) from the
    first that succeeds. Trying them one by one would cost a full timeout for each address
    on a Docker network the dashboard can't route to. fn signals failure with AppError."""
    error = AppError("could not reach the app")
    if not bases:
        activity.log("warn", "adapter", "no address to try: the container has no network address or published port")
        raise error
    pool = ThreadPoolExecutor(max_workers=len(bases))
    try:
        futures = [pool.submit(lambda b=b: (b, fn(b))) for b in bases]
        try:
            for f in as_completed(futures, timeout=FIRST_OK_TIMEOUT):
                try:
                    return f.result()
                except AppError as e:
                    if str(e) in REJECTED:
                        raise  # reached it; a different address would not change the answer
                    if str(error) == "could not reach the app":
                        error = e
        except FuturesTimeout:
            error = AppError("the app took too long to answer")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    activity.log("warn", "adapter", f"none of {len(bases)} address(es) worked: {error}")
    raise error


def call(bases, spec, key, path, params=None):
    """GET `path` with the key header; returns (base, json) from the first address that answers."""
    query = "?" + urlencode(params) if params else ""
    return first_ok(bases, lambda b: http_get_json(b + path + query, spec["auth"]["header"], key))


def test_key(bases, spec, key):
    """None if the app accepts the key, else a safe error message."""
    try:
        call(bases, spec, key, spec["test_path"])
    except AppError as e:
        return str(e)
    return None


# ---- reading a key out of the container (admin-approved only) ----------------

def key_from_archive(chunks, key_file):
    """The key found in the single file of a `get_archive` tar stream, or None. `key_file`
    is the adapter's key_file mapping: a `regex` with one group, or a `json_path`."""
    parts, size = [], 0
    try:
        for chunk in chunks:
            parts.append(chunk)
            size += len(chunk)
            if size > MAX_KEY_FILE * 2:
                return None
    finally:
        close = getattr(chunks, "close", None)  # don't leave the Docker stream open on an early return
        if close:
            close()
    try:
        with tarfile.open(fileobj=io.BytesIO(b"".join(parts))) as tar:
            files = [m for m in tar.getmembers() if m.isfile()]
            # the one file asked for: a path that is a directory would otherwise give its first file
            if len(files) != 1 or files[0].name != os.path.basename(key_file["path"]) or files[0].size > MAX_KEY_FILE:
                return None
            text = tar.extractfile(files[0]).read().decode("utf-8", errors="replace")
    except tarfile.TarError:
        return None
    if "json_path" in key_file:
        try:
            found = dig(json.loads(text), key_file["json_path"])
        except ValueError:
            return None
        return found.strip() if isinstance(found, str) and found.strip() else None
    m = re.search(key_file["regex"], text[:MAX_KEY_SCAN])
    found = m.group(1) if m else None  # None when the group did not take part, e.g. (x)?abc
    return found.strip() if found and found.strip() else None


# ---- turning an app's answer into panel rows ---------------------------------

def dig(obj, path):
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def fill(template, row):
    def sub(m):
        v = dig(row, m.group(1))
        return "" if v is None else str(v)

    return TEMPLATE_RE.sub(sub, template).strip()


def build_panel(panel, data):
    rows = dig(data, panel["rows"])
    if not isinstance(rows, list):
        raise AppError("the app's answer did not have the expected shape")
    out = []
    for r in rows[:MAX_ROWS]:
        item = {"title": fill(panel["row_title"], r) or "Unknown"}
        if "row_detail" in panel:
            item["detail"] = fill(panel["row_detail"], r)
        out.append(item)
    total = dig(data, panel["total"]) if "total" in panel else None
    return {
        "id": panel["id"],
        "title": panel["title"],
        "rows": out,
        "total": total if isinstance(total, int) and not isinstance(total, bool) else len(rows),
    }
