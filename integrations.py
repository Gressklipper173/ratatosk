"""Adapters: YAML descriptions of apps the dashboard can pull a panel from.

An adapter is data, not code: it names an app's API endpoints and the header its key goes
in. Nothing here runs a spec's contents; the only thing a spec can make this module do is
GET paths on the app's own API, with a key the admin approved. See docs/ADAPTERS.md.
"""

import io
import json
import os
import re
import tarfile
import urllib.error
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import yaml

import activity

REQUEST_TIMEOUT = 5
MAX_KEY_FILE = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_ROWS = 50

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TEMPLATE_RE = re.compile(r"\{([A-Za-z0-9_.]+)\}")


class SpecError(ValueError):
    pass


def _need(cond, msg):
    if not cond:
        raise SpecError(msg)


def _text(v, field, limit=200):
    _need(isinstance(v, str) and 0 < len(v) <= limit, f"{field} must be a non-empty string")
    return v


def _path(v, field):
    _text(v, field)
    _need(v.startswith("/") and "://" not in v and ".." not in v.split("/"), f"{field} must be a path starting with /")
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
        out["key_file"] = {"path": _path(key_file.get("path"), "key_file.path")}
        if regex is not None:
            _text(regex, "key_file.regex", 200)
            try:
                compiled = re.compile(regex)
            except re.error as e:
                raise SpecError(f"key_file.regex is not a valid pattern: {e}")
            _need(compiled.groups == 1, "key_file.regex needs exactly one (capture group) around the key")
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
                with open(os.path.join(d, n)) as f:
                    spec = validate_spec(yaml.safe_load(f), trusted=d in trusted, handlers=handlers)
            except (OSError, yaml.YAMLError, SpecError) as e:
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


def match_adapter(adapters, labels, image_ref):
    """The adapter for a container, or None. The `dashboard.adapter` label wins:
    an adapter id picks it, `none` switches the notice off."""
    wanted = str(labels.get("dashboard.adapter", "")).strip().lower()
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


def http_get_json(url, header, key, timeout=REQUEST_TIMEOUT):
    where = activity.safe_url(url)
    started = time.monotonic()

    def took():
        return f"{int((time.monotonic() - started) * 1000)} ms"

    req = urllib.request.Request(url, headers={header: key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            body = r.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        activity.log("warn", "adapter", f"GET {where} -> HTTP {e.code} ({took()})")
        if e.code in (401, 403):
            raise AppError("the app rejected the key")
        raise AppError(f"the app answered HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        activity.log("info", "adapter", f"GET {where} -> no answer: {type(reason).__name__} ({took()})", verbose=True)
        raise AppError("could not reach the app")
    if len(body) > MAX_RESPONSE:
        activity.log("warn", "adapter", f"GET {where} -> HTTP {status}, answer over {MAX_RESPONSE} bytes ({took()})")
        raise AppError("the app's answer was too large")
    try:
        data = json.loads(body)
    except ValueError:
        activity.log("warn", "adapter", f"GET {where} -> HTTP {status}, {len(body)} bytes that are not JSON ({took()})")
        raise AppError("the app's answer was not JSON")
    activity.log("info", "adapter", f"GET {where} -> HTTP {status}, {len(body)} bytes ({took()})", verbose=True)
    return data


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
        for f in as_completed(futures):
            try:
                return f.result()
            except AppError as e:
                if str(e) in REJECTED:
                    raise  # reached it; a different address would not change the answer
                if str(error) == "could not reach the app":
                    error = e
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
    data = b""
    for chunk in chunks:
        data += chunk
        if len(data) > MAX_KEY_FILE * 2:
            return None
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            member = next((m for m in tar.getmembers() if m.isfile()), None)
            if member is None or member.size > MAX_KEY_FILE:
                return None
            text = tar.extractfile(member).read().decode("utf-8", errors="replace")
    except tarfile.TarError:
        return None
    if "json_path" in key_file:
        try:
            found = dig(json.loads(text), key_file["json_path"])
        except ValueError:
            return None
        return found.strip() if isinstance(found, str) and found.strip() else None
    m = re.search(key_file["regex"], text)
    return m.group(1).strip() if m and m.group(1).strip() else None


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
        "total": total if isinstance(total, int) else len(rows),
    }
