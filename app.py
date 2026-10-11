import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import timedelta
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

import docker
import psutil
from flask.sessions import SecureCookieSessionInterface
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import activity
import editor
import handlers
import integrations
import schedules

DATA_DIR = os.environ.get("DATA_DIR", "/data")
READ_ONLY = os.environ.get("READ_ONLY", "").lower() in ("1", "true", "yes")
# Set when a reverse proxy or tunnel (Cloudflare, Caddy, ...) is the only way in. Then the visitor's
# address comes from the proxy's header instead of the socket, and the page may be served over
# https. Off by default: a header is only trustworthy when nothing else can reach the dashboard.
BEHIND_PROXY = os.environ.get("BEHIND_PROXY", "").lower() in ("1", "true", "yes")
# Which header carries the visitor's address when BEHIND_PROXY is on: "xff" (the last
# X-Forwarded-For entry, which any proxy appends) or "cf" (Cloudflare's CF-Connecting-IP).
# Only one is trusted, because only the proxy's own header is overwritten on the way in; the
# other can be forged by the visitor. Anything else falls back to "xff".
PROXY_IP_HEADER = os.environ.get("PROXY_IP_HEADER", "xff").strip().lower()
_proxy_header_invalid = PROXY_IP_HEADER not in ("xff", "cf")
if _proxy_header_invalid:
    PROXY_IP_HEADER = "xff"
# Storage meters in the host panel. A container can't see the host's disks, so each meter is a
# folder the admin mounted from a disk (only that disk's size and free space are read). Until
# one is added in the page, the dashboard's own volume is shown: /data sits on the same disk as
# Docker's storage.
DEFAULT_DISKS = [{"label": "Storage", "path": "/data"}]
MAX_DISKS = 8
MAX_DISK_LABEL = 32
MAX_DISK_PATH = 200

try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")) as f:
        VERSION = f.read().strip() or "dev"
except OSError:
    VERSION = "dev"

USERS_PATH = os.path.join(DATA_DIR, "users.json")
SECRET_PATH = os.path.join(DATA_DIR, "secret_key")
GROUPS_PATH = os.path.join(DATA_DIR, "groups.json")
GROUPS_DIR = os.path.join(DATA_DIR, "groups")
INTEGRATIONS_PATH = os.path.join(DATA_DIR, "integrations.json")
SCHEDULES_PATH = os.path.join(DATA_DIR, "schedules.json")

# adapters: built-ins ship in ./adapters, an admin's own go in /data/adapters and win by id
BUILTIN_ADAPTERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters")
ADAPTERS, ADAPTER_PROBLEMS = integrations.load_adapters(
    BUILTIN_ADAPTERS,
    os.path.join(DATA_DIR, "adapters"),
    trusted=(BUILTIN_ADAPTERS,),
    handlers=handlers.HANDLERS,
)
activity.capture_python_logging()
for _problem in ADAPTER_PROBLEMS:
    activity.log("warn", "adapters", f"skipped {_problem}")
if _proxy_header_invalid:
    activity.log("warn", "app", 'PROXY_IP_HEADER must be "xff" or "cf"; using "xff"')
PANEL_TTL = 30


def age_arg(default, lo, hi):
    """The `age` query value (seconds the page is happy to see data old), clamped; the page
    sends its own refresh interval so a faster refresh isn't answered from a slower cache."""
    try:
        return min(max(int(request.args.get("age", default)), lo), hi)
    except (TypeError, ValueError):
        return default
KEY_RE = re.compile(r"^[\x21-\x7e]{1,256}$")  # an API key goes into an HTTP header
# a login is sent as form data, so spaces and non-ASCII are fine; control characters are not
LOGIN_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")

ACTIONS = ("start", "stop", "restart")

# stats sampling: each docker stats call takes a second or two, so a background
# thread keeps a cache that /api/containers just reads
SAMPLE_INTERVAL = 5
SAMPLE_WORKERS = 4

DEFAULT_LOG_TAIL = 200
MAX_LOG_TAIL = 1000
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# limits on the group settings a client may save
MAX_GROUP_NAME = 64
MAX_ASSIGNMENTS = 500
MAX_CUSTOM_GROUPS = 100
MAX_GROUP_ORDER = 200
# the movable panels: one id per panel ("host", "links", "int:<container>", "group:<name>")
MAX_PANEL_ID = 300
MAX_PANELS = 200
LAYOUT_WIDTHS = ("1", "2", "3", "4", "5")

# container ports that usually serve a web UI; the lowest published TCP port is the
# fallback when none of these is published
WEB_PORTS = {80, 443, 3000, 5000, 8000, 8080, 8081, 8443, 8888, 9000, 9443}
HTTPS_PORTS = {443, 8443, 9443}

# container updates: on unless UPDATE_CHECK is switched off; the registry is asked
# every UPDATE_INTERVAL seconds (Docker Hub rate-limits anonymous pulls)
UPDATE_CHECK = os.environ.get("UPDATE_CHECK", "true").lower() not in ("0", "false", "no", "off")
UPDATE_INTERVAL = 1800
UPDATE_TIMEOUT = 900

# login throttling: max failures per client IP inside the window
MAX_FAILURES = 5
FAILURE_WINDOW = 300

# a signed-in session lasts this long after its last request; sign-in is needed again after that
SESSION_LIFETIME = timedelta(hours=12)

# One lock around every read-modify-write of users.json and integrations.json, so two admin
# actions at the same moment can't overwrite each other (gunicorn runs several threads).
_data_lock = threading.RLock()


def load_secret_key():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(SECRET_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
        return key


class DataFileError(RuntimeError):
    """A data file that exists but can't be trusted. Never answered with an empty default for
    users.json: an empty user list would hand /setup to whoever reaches the page next."""


ROLES = ("admin", "operator")
PRIORITIES = ("critical", "wanted", "on_demand")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
MIN_PASSWORD = 8


def load_users():
    """{username: {"hash", "role"}}. A file saved before roles existed holds {username: hash};
    those accounts were all admins, so they stay admins. A file that is unreadable, not JSON or
    not an object raises DataFileError instead of reading as "no users" (see above)."""
    try:
        with open(USERS_PATH) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise DataFileError(f"{USERS_PATH} can't be read ({type(e).__name__}: {e}). Fix or restore it; Ratatosk will not treat it as empty, which would re-open /setup") from e
    if not isinstance(raw, dict):
        raise DataFileError(f"{USERS_PATH} is not a JSON object. Fix or restore it; Ratatosk will not treat it as empty, which would re-open /setup")
    users = {}
    for name, rec in raw.items():
        if isinstance(rec, str):
            rec = {"hash": rec, "role": "admin"}
        if isinstance(rec, dict) and isinstance(rec.get("hash"), str):
            role = rec.get("role")
            users[name] = {"hash": rec["hash"], "role": role if role in ROLES else "operator"}
    if raw and not users:
        raise DataFileError(f"{USERS_PATH} has no usable account in it. Fix or restore it; Ratatosk will not treat it as empty, which would re-open /setup")
    return users


def write_json(path, data):
    """Atomic replace through a temp file of its own (mkstemp: unique name, mode 0600), so two
    writers never share one."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_users(users):
    write_json(USERS_PATH, users)


def empty_layout():
    return {"columns": {}, "hidden": []}


def empty_groups():
    return {
        "assignments": {},
        "custom_groups": [],
        "on_demand_groups": [],
        "critical_groups": [],
        "container_priority": {},
        "link_containers": [],
        "aliases": {},
        "group_order": [],
        "hidden_links": [],
        "link_groups": [],
        # a brand-new account starts with everything in "Ungrouped"
        "group_by_project": False,
        "disks": [],
        "layout": empty_layout(),
    }


def groups_path(user):
    """Each user arranges the page for themself. The file name is a hash of the username, so
    no name can reach outside the folder."""
    return os.path.join(GROUPS_DIR, hashlib.sha256(user.encode()).hexdigest()[:24] + ".json")


_groups_warned = set()


def load_groups(user=None):
    """The user's own settings. Until they save for the first time they see the shared
    groups.json that existed before users had their own, so nobody starts from an empty page.
    A file that is unreadable or not an object is skipped with one warning (a display
    preference is not worth a crash), and the defaults apply."""
    data = None
    for path in ([groups_path(user)] if user else []) + [GROUPS_PATH]:
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            data = None
            problem = f"{type(e).__name__}: {e}"
        else:
            if isinstance(data, dict):
                break
            data, problem = None, "not a JSON object"
        if path not in _groups_warned:
            _groups_warned.add(path)
            activity.log("warn", "app", f"{path} was ignored ({problem}); using default group settings")
    if data is None:
        return empty_groups()
    return {
        "assignments": data.get("assignments", {}),
        "custom_groups": data.get("custom_groups", []),
        # files saved before this setting existed simply don't have it
        "on_demand_groups": data.get("on_demand_groups", []),
        # priority: a group is Critical (listed here), On demand (above) or Wanted (neither);
        # a container can override its group
        "critical_groups": data.get("critical_groups", []),
        "container_priority": data.get("container_priority", {}),
        # containers whose link tile is on although their group's "Generate links" is off
        "link_containers": data.get("link_containers", []),
        "aliases": data.get("aliases", {}),
        "group_order": data.get("group_order", []),
        "hidden_links": data.get("hidden_links", []),
        "link_groups": data.get("link_groups", []),
        # on = a container starts in its Compose project's group; off = in "Ungrouped". Files saved
        # before this setting existed keep the old behaviour (on); only a brand-new account starts off.
        "group_by_project": data["group_by_project"] if isinstance(data.get("group_by_project"), bool) else True,
        # the host panel's storage meters; files saved before they existed don't have it
        "disks": data.get("disks", []),
        # files saved before the panel layout existed don't have it; the client then
        # computes a default arrangement
        "layout": data.get("layout", empty_layout()),
    }


def valid_label(name):
    return isinstance(name, str) and 0 < len(name.strip()) <= MAX_GROUP_NAME


def valid_panel_id(pid):
    return isinstance(pid, str) and 0 < len(pid) <= MAX_PANEL_ID


def validate_layout(layout):
    """A cleaned copy of the panel layout, or None if malformed. `columns` maps a column
    count ("1" to "5") to that many lists of panel ids: one arrangement per width, so
    arranging on a phone never changes the desktop. `hidden` is shared by every width."""
    if not isinstance(layout, dict):
        return None
    columns = layout.get("columns", {})
    hidden = layout.get("hidden", [])
    if not isinstance(columns, dict) or not isinstance(hidden, list):
        return None
    if len(hidden) > MAX_PANELS or not all(valid_panel_id(p) for p in hidden):
        return None
    out = {}
    for width, cols in columns.items():
        if width not in LAYOUT_WIDTHS or not isinstance(cols, list) or len(cols) != int(width):
            return None
        if not all(isinstance(c, list) for c in cols):
            return None
        ids = [p for c in cols for p in c]
        if len(ids) > MAX_PANELS or not all(valid_panel_id(p) for p in ids):
            return None
        seen = set()
        clean = []
        for c in cols:
            # a panel is in one column only; a repeat is dropped
            clean.append([p for p in c if not (p in seen or seen.add(p))])
        out[width] = clean
    return {"columns": out, "hidden": list(dict.fromkeys(hidden))}


def validate_disks(disks):
    """A cleaned [{"label", "path"}], or None if malformed. A path is only ever measured
    (size and free space), never read."""
    if not isinstance(disks, list) or len(disks) > MAX_DISKS:
        return None
    out = []
    for d in disks:
        if not isinstance(d, dict):
            return None
        label, path = d.get("label"), d.get("path")
        if not isinstance(label, str) or not isinstance(path, str):
            return None
        label, path = label.strip(), path.strip()
        if not 0 < len(label) <= MAX_DISK_LABEL or not path.startswith("/") or len(path) > MAX_DISK_PATH or "\x00" in path:
            return None
        out.append({"label": label, "path": path})
    if len({d["path"] for d in out}) != len(out):
        return None
    return out


def validate_groups(data):
    """Returns a cleaned copy of the client's group settings, or None if malformed."""
    if not isinstance(data, dict):
        return None
    assignments = data.get("assignments")
    custom = data.get("custom_groups")
    on_demand = data.get("on_demand_groups", [])
    aliases = data.get("aliases", {})
    order = data.get("group_order", [])
    hidden = data.get("hidden_links", [])
    link_groups = data.get("link_groups", [])
    critical = data.get("critical_groups", [])
    priority = data.get("container_priority", {})
    link_containers = data.get("link_containers", [])
    by_project = data.get("group_by_project", True)
    if not isinstance(by_project, bool):
        return None
    layout = validate_layout(data.get("layout", empty_layout()))
    if layout is None:
        return None
    disks = validate_disks(data.get("disks", []))
    if disks is None:
        return None
    if not isinstance(link_groups, list) or len(link_groups) > MAX_CUSTOM_GROUPS:
        return None
    if not isinstance(critical, list) or len(critical) > MAX_CUSTOM_GROUPS or not all(valid_label(g) for g in critical):
        return None
    if not isinstance(priority, dict) or len(priority) > MAX_ASSIGNMENTS:
        return None
    if not all(isinstance(k, str) and 0 < len(k) <= 256 and v in PRIORITIES for k, v in priority.items()):
        return None
    if not isinstance(link_containers, list) or len(link_containers) > MAX_ASSIGNMENTS:
        return None
    if not all(isinstance(n, str) and 0 < len(n) <= 256 for n in link_containers):
        return None
    if not all(valid_label(g) for g in link_groups):
        return None
    if not isinstance(hidden, list) or len(hidden) > MAX_ASSIGNMENTS:
        return None
    if not all(isinstance(n, str) and 0 < len(n) <= 256 for n in hidden):
        return None
    if not isinstance(order, list) or len(order) > MAX_GROUP_ORDER:
        return None
    if not all(valid_label(g) for g in order):
        return None
    if not isinstance(assignments, dict) or not isinstance(custom, list) or not isinstance(on_demand, list):
        return None
    if not isinstance(aliases, dict):
        return None
    if len(assignments) > MAX_ASSIGNMENTS or len(custom) > MAX_CUSTOM_GROUPS or len(on_demand) > MAX_CUSTOM_GROUPS:
        return None
    if len(aliases) > MAX_ASSIGNMENTS:
        return None
    # group names and container display names share the same length/emptiness rules
    if not all(isinstance(k, str) and len(k) <= 256 and valid_label(v) for k, v in assignments.items()):
        return None
    if not all(isinstance(k, str) and len(k) <= 256 and valid_label(v) for k, v in aliases.items()):
        return None
    if not all(valid_label(g) for g in custom) or not all(valid_label(g) for g in on_demand):
        return None
    return {
        "assignments": {k: v.strip() for k, v in assignments.items()},
        "custom_groups": list(dict.fromkeys(g.strip() for g in custom)),
        # groups whose containers are expected to be stopped sometimes (game servers
        # started on demand); the client leaves them out of the overall health count
        "on_demand_groups": list(dict.fromkeys(g.strip() for g in on_demand)),
        # a group is in at most one of the two lists; On demand wins
        "critical_groups": [g for g in dict.fromkeys(g.strip() for g in critical) if g not in {x.strip() for x in on_demand}],
        "container_priority": dict(priority),
        # a container is either forced on or hidden, not both; hidden wins
        "link_containers": [n for n in dict.fromkeys(link_containers) if n not in set(hidden)],
        # container name -> friendly name shown in the dashboard; display-only, the
        # container itself is never renamed
        "aliases": {k: v.strip() for k, v in aliases.items()},
        # the user's top-to-bottom order of groups; groups not listed here (new ones)
        # are placed after the listed ones by the client
        "group_order": list(dict.fromkeys(g.strip() for g in order)),
        # containers whose link tile the user hid
        "hidden_links": list(dict.fromkeys(hidden)),
        # groups whose containers get link tiles; off unless the user turns it on
        "link_groups": list(dict.fromkeys(g.strip() for g in link_groups)),
        # whether containers start in their Compose project's group (default) or in "Ungrouped"
        "group_by_project": by_project,
        # the host panel's storage meters: [{"label", "path"}]
        "disks": disks,
        # where each panel sits, per column count, and which panels are hidden
        "layout": layout,
    }


app = Flask(__name__)
app.secret_key = load_secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME)

# Every script and stylesheet is a file of ours, and the page is never framed. Styles set from
# script go through el.style.x (allowed); a style="" attribute or inline <style> would be blocked.
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; frame-ancestors 'none'"


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    # the API answers hold container details and config files, and the pages hold the CSRF token
    if request.path.startswith("/api/") or resp.mimetype == "text/html":
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def client_ip():
    """Who is asking. Behind a trusted proxy that is the address the proxy reports in the one
    header PROXY_IP_HEADER names (the last X-Forwarded-For entry, which the proxy itself
    appended, or Cloudflare's CF-Connecting-IP); the other header is ignored, since a visitor
    can forge it. Otherwise the socket's peer. Without this every visitor behind a tunnel
    shares one address, so the login throttle would lock everyone out together."""
    if BEHIND_PROXY:
        if PROXY_IP_HEADER == "cf":
            ip = request.headers.get("CF-Connecting-IP", "").strip()
        else:
            ip = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        if ip:
            return ip[:64]
    return request.remote_addr


def is_https():
    if request.is_secure:
        return True
    return BEHIND_PROXY and request.headers.get("X-Forwarded-Proto", "").split(",")[-1].strip().lower() == "https"


class _Sessions(SecureCookieSessionInterface):
    """The session cookie is marked Secure whenever the page itself came over https, so it is
    never sent in the clear. Plain http (a LAN or Tailscale address) keeps working, because a
    Secure cookie would not be stored there."""

    def get_cookie_secure(self, app):
        return is_https()


app.session_interface = _Sessions()

docker_client = docker.from_env()
activity.log("info", "app", f"started{' (read-only)' if READ_ONLY else ''}")


@app.errorhandler(Exception)
def log_unhandled(e):
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, DataFileError):
        activity.log("error", "app", f"{request.method} {request.path} refused: {e}")
        return jsonify({"error": "a data file is damaged, see the Debug panel or the container logs"}), 503
    activity.log("error", "app", f"{request.method} {request.path} failed: {type(e).__name__}: {e}", exc=True)
    return jsonify({"error": "internal error, see the Debug panel"}), 500


# Until the first admin exists, /setup is gated by a token that only appears
# in the container logs, so a stranger who reaches a fresh install can't claim it.
setup_token = None
if not load_users():
    setup_token = secrets.token_urlsafe(16)
    print(f"[setup] No users yet. Open /setup and use token: {setup_token}", flush=True)

_failures = {}
# checked when the user doesn't exist, so an unknown name costs the same time as a wrong password
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def throttled(ip):
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < FAILURE_WINDOW]
    if recent:
        _failures[ip] = recent
    else:
        _failures.pop(ip, None)
    return len(recent) >= MAX_FAILURES


def record_failure(ip):
    _failures.setdefault(ip, []).append(time.time())


def _password_stamp(rec):
    """A short fingerprint of the stored password hash. A session carries it, so changing or
    resetting the password signs out every session that was opened with the old one."""
    return hashlib.sha256(rec["hash"].encode()).hexdigest()[:16]


def current_user():
    """(username, role) of the signed-in user, or (None, None). The role is read from the file
    on every call, so removing or demoting someone takes effect at once, and a session opened
    before the last password change is no longer signed in."""
    name = session.get("user")
    rec = load_users().get(name) if name else None
    if rec and session.get("pw") == _password_stamp(rec):
        return name, rec["role"]
    return None, None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user()[0] is None:
            session.clear()
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    """Signed in as an admin. Used for users and for credential steps."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        name, role = current_user()
        if name is None:
            session.clear()
            return redirect(url_for("login"))
        if role != "admin":
            activity.log("warn", "auth", f"{name} ({role}) was refused {request.method} {request.path}: admins only")
            return jsonify({"error": "only an admin can do this"}), 403
        return view(*args, **kwargs)

    return wrapped


def start_session(username):
    session.clear()
    session.permanent = True  # PERMANENT_SESSION_LIFETIME applies
    session["user"] = username
    session["pw"] = _password_stamp(load_users()[username])
    session["csrf"] = secrets.token_hex(16)


def _safe_eq(a, b):
    """Constant-time equality for two strings. compare_digest raises on non-ASCII str, so
    compare the encoded bytes: odd input is simply unequal."""
    return secrets.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))


def csrf_ok(form=False):
    """The page sends its token in the X-CSRF-Token header. A plain HTML form (the log-out
    button) can't set headers, so with form=True a `csrf_token` form field counts too."""
    token = request.headers.get("X-CSRF-Token", "") or (request.form.get("csrf_token", "") if form else "")
    return bool(token) and _safe_eq(token, session.get("csrf", ""))


def _pre_csrf():
    """The token the login and setup forms carry. There is no session yet, so it lives in the
    cookie until the sign-in replaces the session; it stops another site logging a visitor in
    to an account of its own."""
    if not session.get("csrf_pre"):
        session["csrf_pre"] = secrets.token_hex(16)
    return session["csrf_pre"]


def _pre_csrf_ok():
    token = request.form.get("csrf_token", "")
    return bool(token) and _safe_eq(token, session.get("csrf_pre", ""))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    global setup_token
    if setup_token is None:
        return redirect(url_for("login"))
    if request.method == "GET":
        return render_template("setup.html", error=None, csrf_token=_pre_csrf())
    if not _pre_csrf_ok():
        return render_template("setup.html", error="This page expired, try again", csrf_token=_pre_csrf()), 403

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    token = request.form.get("token", "")
    error = None
    if not _safe_eq(token, setup_token):
        error = "Invalid setup token (see the container logs)"
    elif not USERNAME_RE.match(username):
        error = "Username: 1 to 32 letters, digits, dots, dashes or underscores"
    elif not _valid_password(password):
        error = f"Password must be at least {MIN_PASSWORD} characters (256 at most)"
    if error:
        return render_template("setup.html", error=error, csrf_token=_pre_csrf())

    save_users({username: {"hash": generate_password_hash(password), "role": "admin"}})
    setup_token = None
    activity.log("info", "auth", f"first admin created: {username}")
    start_session(username)
    # the page shows a "searching for containers" screen once, right after the account is made
    return redirect(url_for("index", welcome=1))


@app.route("/login", methods=["GET", "POST"])
def login():
    if setup_token is not None:
        return redirect(url_for("setup"))
    if request.method == "GET":
        return render_template("login.html", error=None, csrf_token=_pre_csrf())
    if not _pre_csrf_ok():
        return render_template("login.html", error="This page expired, try again", csrf_token=_pre_csrf()), 403

    ip = client_ip()
    if throttled(ip):
        activity.log("warn", "auth", f"login from {ip} refused: too many failures")
        return render_template("login.html", error="Too many attempts, try again later", csrf_token=_pre_csrf()), 429
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    stored = load_users().get(username)
    # always hash-check, so an unknown name takes as long as a wrong password
    password_ok = check_password_hash(stored["hash"] if stored else _DUMMY_HASH, password)
    if stored and password_ok:
        start_session(username)
        activity.log("info", "auth", f"{username} signed in from {ip}")
        return redirect(url_for("index"))
    record_failure(ip)
    activity.log("warn", "auth", f"failed sign-in from {ip}")
    return render_template("login.html", error="Invalid username or password", csrf_token=_pre_csrf()), 401


@app.route("/logout", methods=["POST"])
def logout():
    # POST with the CSRF token (header, or the csrf_token field of a plain form), so another
    # site can't sign a visitor out with an image tag. Someone already signed out just lands on
    # the login page.
    if session.get("csrf") and not csrf_ok(form=True):
        return jsonify({"error": "bad csrf token"}), 403
    session.clear()
    return redirect(url_for("login"))


def _user_error(msg, code=400):
    return jsonify({"error": msg}), code


def _admin_count(users):
    return sum(1 for u in users.values() if u["role"] == "admin")


def _valid_password(pw):
    return isinstance(pw, str) and MIN_PASSWORD <= len(pw) <= 256


# User management. Admin only, and switched off by READ_ONLY like the other credential steps;
# changing your own password is always allowed. Every change is recorded without the password.
@app.route("/api/users", methods=["GET", "POST"])
@admin_required
def api_users():
    if request.method == "GET":
        users = load_users()
        return jsonify({"error": None, "me": session["user"], "items": [{"name": n, "role": u["role"]} for n, u in sorted(users.items())]})
    if READ_ONLY:
        return _user_error("Ratatosk is in read-only mode", 403)
    if not csrf_ok():
        return _user_error("bad csrf token", 403)
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    name, role, password = body.get("username"), body.get("role"), body.get("password")
    if not isinstance(name, str) or not USERNAME_RE.match(name):
        return _user_error("username: 1 to 32 letters, digits, dots, dashes or underscores")
    if role not in ROLES:
        return _user_error("pick a role")
    if not _valid_password(password):
        return _user_error(f"password must be at least {MIN_PASSWORD} characters")
    hashed = generate_password_hash(password)
    with _data_lock:
        users = load_users()
        if name in users:
            return _user_error("that username is taken", 409)
        users[name] = {"hash": hashed, "role": role}
        save_users(users)
    activity.log("info", "auth", f"{session['user']} created {role} {name}")
    return jsonify({"error": None})


@app.route("/api/users/<name>", methods=["PUT", "DELETE"])
@admin_required
def api_user(name):
    if READ_ONLY:
        return _user_error("Ratatosk is in read-only mode", 403)
    if not csrf_ok():
        return _user_error("bad csrf token", 403)
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    with _data_lock:
        users = load_users()
        if name not in users:
            return _user_error("no such user", 404)
        if request.method == "DELETE":
            if users[name]["role"] == "admin" and _admin_count(users) == 1:
                return _user_error("this is the only admin; make another admin first", 409)
            del users[name]
            save_users(users)
            try:
                os.remove(groups_path(name))
            except FileNotFoundError:
                pass
            activity.log("info", "auth", f"{session['user']} removed {name}")
            return jsonify({"error": None})
        role, password = body.get("role"), body.get("password")
        if role is None and password is None:
            return _user_error("nothing to change")
        if role is not None:
            if role not in ROLES:
                return _user_error("pick a role")
            if users[name]["role"] == "admin" and role != "admin" and _admin_count(users) == 1:
                return _user_error("this is the only admin; make another admin first", 409)
        if password is not None and not _valid_password(password):
            return _user_error(f"password must be at least {MIN_PASSWORD} characters")
        if role is not None:
            users[name]["role"] = role
            activity.log("info", "auth", f"{session['user']} set {name} to {role}")
        if password is not None:
            users[name]["hash"] = generate_password_hash(password)
            activity.log("info", "auth", f"{session['user']} reset the password of {name}")
        save_users(users)
    return jsonify({"error": None})


@app.route("/api/account/password", methods=["POST"])
@login_required
def api_account_password():
    if not csrf_ok():
        return _user_error("bad csrf token", 403)
    ip = client_ip()
    if throttled(ip):
        return _user_error("too many attempts, try again later", 429)
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    current, new = body.get("current"), body.get("new")
    with _data_lock:
        users = load_users()
        me = users[session["user"]]
        if not isinstance(current, str) or not check_password_hash(me["hash"], current):
            record_failure(ip)
            activity.log("warn", "auth", f"{session['user']}: wrong current password when changing it")
            return _user_error("the current password is wrong", 403)
        if not _valid_password(new):
            return _user_error(f"password must be at least {MIN_PASSWORD} characters")
        me["hash"] = generate_password_hash(new)
        save_users(users)
    session["pw"] = _password_stamp(me)  # this device stays signed in; every other session is signed out
    activity.log("info", "auth", f"{session['user']} changed their password")
    return jsonify({"error": None})


@app.route("/healthz")
def healthz():
    """For Docker's HEALTHCHECK and uptime monitors. No sign-in, and nothing in it but "ok".
    Once the first account exists it also asks Docker, so a missing or broken socket mount
    shows as unhealthy (503). Before that, the setup page is the thing to reach, so it is ok."""
    if setup_token is None:
        try:
            alive = docker_client.ping()
        except Exception:
            alive = False
        if not alive:
            return jsonify({"ok": False}), 503
    return jsonify({"ok": True})


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html", version=VERSION, user=session["user"], role=current_user()[1], csrf_token=session["csrf"], read_only=READ_ONLY, update_check=UPDATE_CHECK,
        welcome=request.args.get("welcome") == "1",
    )


def own_container_id():
    # inside a container, HOSTNAME defaults to the short container id
    return os.environ.get("HOSTNAME", "")


def is_self(c):
    return bool(own_container_id()) and c.short_id.startswith(own_container_id()[:12])


def compute_stats(raw):
    """CPU percent (of one core, like `docker stats`) and memory in bytes from one
    docker stats sample.

    Memory is what the container's processes actually hold: anonymous memory plus
    shared memory (cgroup v2 `anon` + `shmem`, or v1 `rss`). File page cache is
    left out. `docker stats` only subtracts the *inactive* part of it, so a
    container doing heavy file I/O (a torrent client) shows many GB of mostly
    cache, which disagrees with the host figure (total - available) that treats
    all cache as reclaimable."""
    cpu = raw.get("cpu_stats", {})
    pre = raw.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    ncpu = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    pct = cpu_delta / sys_delta * ncpu * 100 if sys_delta > 0 and cpu_delta >= 0 else 0.0

    detail = raw.get("memory_stats", {}).get("stats", {})
    if "anon" in detail:
        mem = detail["anon"] + detail.get("shmem", 0)
    elif "rss" in detail:
        mem = detail["rss"]
    else:
        mem = None
    return {"cpu": round(pct, 1), "mem": mem}


# container name -> {"cpu": percent, "mem": bytes}; replaced wholesale each cycle so
# stopped containers drop out
_stats = {}


def _sample_one(c):
    try:
        return c.name, compute_stats(c.stats(stream=False))
    except docker.errors.DockerException:
        return None


def sample_stats(pool):
    global _stats
    try:
        running = docker_client.containers.list()
    except docker.errors.DockerException:
        return
    _stats = dict(r for r in pool.map(_sample_one, running) if r)


def _stats_sampler():
    with ThreadPoolExecutor(max_workers=SAMPLE_WORKERS) as pool:
        while True:
            sample_stats(pool)
            time.sleep(SAMPLE_INTERVAL)


# primes psutil so the first /api/vitals call already has a baseline to diff against
psutil.cpu_percent(interval=None)


def pick_link(labels, ports):
    """The link tile for a container, or None. the `ratatosk.url` label wins when it is an
    http(s) URL; otherwise the best published TCP port that is reachable from other
    machines (the client supplies the host name, from the address bar)."""
    if integrations.label(labels, "hide").lower() in ("1", "true", "yes"):
        return None
    url = integrations.label(labels, "url").strip()
    if url:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return {"url": url, "port": None, "https": False}
    found = []
    for key, bindings in ports.items():
        port, _, proto = key.partition("/")
        if proto != "tcp" or not port.isdigit():
            continue
        for b in bindings or []:
            if b.get("HostIp") not in ("127.0.0.1", "::1"):
                found.append((int(port), b["HostPort"]))
    if not found:
        return None
    found.sort(key=lambda f: (f[0] not in WEB_PORTS, f[0]))
    container_port, host_port = found[0]
    return {"url": None, "port": host_port, "https": container_port in HTTPS_PORTS}


# ---- container updates ----------------------------------------------------
#
# An update is `docker compose pull` + `up -d` for the one service, run with the
# project, config files and working dir that Compose stamped on the container as
# labels. That only works when those files are visible at the same path inside this
# container (see README), so Compose containers get a badge either way and the
# button says why when it can't run.

# image reference -> manifest digest the registry reports; replaced per answer, so
# a registry error keeps the last good one
_registry = {}
# container name -> {"state": "running" | "error", "error": str | None}
_jobs = {}
_jobs_lock = threading.Lock()


def compose_info(labels):
    """(project, service, config files, working dir) for a Compose container, else None."""
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    files = [f for f in labels.get("com.docker.compose.project.config_files", "").split(",") if f]
    if not (project and service and files):
        return None
    return project, service, files, labels.get("com.docker.compose.project.working_dir")


def local_digests(c):
    try:
        repo_digests = c.image.attrs.get("RepoDigests") or []
    except docker.errors.DockerException:
        return set()
    return {d.partition("@")[2] for d in repo_digests if "@" in d}


def image_ref(c):
    ref = c.attrs.get("Config", {}).get("Image", "")
    return "" if ref.startswith("sha256:") else ref


_check_lock = threading.Lock()
# images the registry has been asked about since start, answered or not, so a container whose image
# is not in here yet (created or started after the last pass) can ask for a check now
_attempted = set()
_check_wake = threading.Event()
CHECK_WORKERS = 4
CHECK_RETRY = 60  # seconds before the first pass is repeated if some registry did not answer
CHECK_COOLDOWN = 10  # seconds between "Check for updates now" clicks
_last_manual = 0.0


def is_pending(c):
    """True when the registry's newest image for this container differs from the one it runs."""
    latest = _registry.get(image_ref(c))
    local = local_digests(c)
    return bool(latest and local and latest not in local)


def check_registry():
    """One pass over every Compose container's image, running or not; errors leave the old
    answer. Returns {"checked", "updates", "errors": [{"image", "error"}]}, or None when another
    pass is already running."""
    if not _check_lock.acquire(blocking=False):
        return None
    try:
        found, refs = [], set()
        try:
            for c in docker_client.containers.list(all=True):
                labels = c.attrs.get("Config", {}).get("Labels") or {}
                ref = image_ref(c)
                if not (ref and compose_info(labels)) or is_self(c):
                    continue
                _attempted.add(ref)  # even a local build, so it does not ask again
                if local_digests(c):
                    found.append(c)
                    refs.add(ref)
        except docker.errors.DockerException as e:
            activity.log("warn", "updates", f"update check could not list containers: {e}")
            return {"checked": 0, "updates": 0, "errors": [{"image": "", "error": str(e)[:200]}]}

        def ask(ref):
            try:
                _registry[ref] = docker_client.images.get_registry_data(ref).id
            except Exception as e:
                activity.log("warn", "updates", f"registry check for {ref} failed: {type(e).__name__}: {e}")
                return {"image": ref, "error": f"{type(e).__name__}: {e}"[:200]}
            return None

        with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
            errors = [r for r in pool.map(ask, sorted(refs)) if r]
        return {"checked": len(refs), "updates": sum(1 for c in found if is_pending(c)), "errors": errors}
    finally:
        _check_lock.release()


def wake_if_unchecked(containers):
    """Ask the checker for a pass now if a Compose container runs an image nobody has asked the
    registry about yet, instead of leaving it to the next timer."""
    for c in containers:
        ref = image_ref(c)
        if ref and ref not in _attempted and compose_info(c.attrs.get("Config", {}).get("Labels") or {}) and not is_self(c):
            _check_wake.set()
            return


def _update_checker():
    first = True
    while True:
        result = check_registry()
        # right after a boot the network or the registry may not answer yet: try once more soon
        delay = CHECK_RETRY if first and result and result["errors"] else UPDATE_INTERVAL
        first = False
        _check_wake.wait(delay)
        _check_wake.clear()


@app.route("/api/updates/check", methods=["POST"])
@admin_required
def updates_check():
    # asking registries changes nothing here, so this stays available in READ_ONLY mode
    global _last_manual
    if not csrf_ok():
        return jsonify({"error": "bad csrf token"}), 403
    if not UPDATE_CHECK:
        return jsonify({"error": "update checks are switched off"}), 400
    if time.monotonic() - _last_manual < CHECK_COOLDOWN:
        return jsonify({"error": "checked a moment ago, try again in a few seconds"}), 429
    result = check_registry()
    if result is None:
        return jsonify({"error": "a check is already running"}), 409
    _last_manual = time.monotonic()
    return jsonify({"error": None, **result})


def update_blockers(files):
    if shutil.which("docker") is None:
        return "this image has no docker CLI"
    missing = [f for f in files if not os.path.exists(f)]
    if missing:
        return f"compose file not visible to Ratatosk: {missing[0]} (mount its folder at the same path)"
    return None


def update_info(c, labels):
    """What the row shows about updates, or None to show nothing."""
    if not UPDATE_CHECK:
        return None
    info = compose_info(labels)
    if info is None:
        return None
    with _jobs_lock:
        job = _jobs.get(c.name)
    if job and job["state"] == "running":
        return {"available": True, "state": job["state"], "error": job["error"], "blocked": update_blockers(info[2])}
    pending = is_pending(c)
    if job:
        if pending:
            return {"available": True, "state": job["state"], "error": job["error"], "blocked": update_blockers(info[2])}
        # a failed update whose container has since been updated by hand: nothing left to show
        with _jobs_lock:
            if _jobs.get(c.name) is job:
                del _jobs[c.name]
        return None
    if not pending:
        return None
    return {"available": True, "state": None, "error": None, "blocked": update_blockers(info[2])}


def _run_update(name, project, service, files, workdir):
    cmd = ["docker", "compose", "-p", project]
    if workdir:
        cmd += ["--project-directory", workdir]
    for f in files:
        cmd += ["-f", f]
    error = None
    for step in (["pull", service], ["up", "-d", "--no-deps", service]):
        try:
            r = subprocess.run(cmd + step, capture_output=True, text=True, timeout=UPDATE_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            error = str(e)
            break
        if r.returncode != 0:
            error = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["compose failed"]
            error = error[0]
            break
    activity.log("error" if error else "info", "updates", f"update of {name} " + (f"failed: {error}" if error else "finished"))
    with _jobs_lock:
        if error:
            _jobs[name] = {"state": "error", "error": error}
        else:
            del _jobs[name]
    if not error:
        check_registry()


def is_game_server(labels):
    """Wings starts every game server's container with this label; the container is named by
    the server's UUID."""
    return labels.get("ContainerType") == "server_process"


def game_info(c, labels):
    """None for an ordinary container. For a game server: what Pelican says about it, if
    its adapter is set up ({"name", "connect", "state"}, any of them None)."""
    if not is_game_server(labels):
        return None
    _, servers = handlers.pelican_cached()
    return {"name": None, "connect": None, "state": None, **servers.get(c.name, {})}


def describe(c):
    state = c.attrs.get("State", {})
    labels = c.attrs.get("Config", {}).get("Labels") or {}
    stats = _stats.get(c.name) or {}
    ports = c.attrs.get("NetworkSettings", {}).get("Ports") or {}
    published = sorted(
        {b["HostPort"] for bindings in ports.values() for b in (bindings or [])},
        key=int,
    )
    image_tags = c.image.tags
    return {
        "id": c.short_id,
        "name": c.name,
        "image": image_tags[0] if image_tags else c.image.short_id,
        "status": c.status,
        "health": (state.get("Health") or {}).get("Status"),
        "ports": published,
        "label_name": integrations.label(labels, "name") or None,
        "link": pick_link(labels, ports),
        "project": labels.get("com.docker.compose.project"),
        "service": labels.get("com.docker.compose.service"),
        "update": update_info(c, labels),
        "game": game_info(c, labels),
        "cpu": stats.get("cpu"),
        "mem": stats.get("mem"),
        "self": is_self(c),
    }


@app.route("/api/containers")
@login_required
def api_containers():
    try:
        containers = docker_client.containers.list(all=True)
        # twice the page's refresh interval, so Pelican is asked about every other refresh at most
        refresh_pelican(containers, age_arg(PELICAN_STALE // 2, 1, 30) * 2)
        if UPDATE_CHECK:
            wake_if_unchecked(containers)
        items = [describe(c) for c in containers]
    except docker.errors.DockerException as e:
        return jsonify({"error": str(e), "items": []}), 502
    items.sort(key=lambda i: i["name"])
    user, role = current_user()
    return jsonify({"error": None, "items": items, "read_only": READ_ONLY, "version": VERSION, "user": user, "role": role, "groups": load_groups(session["user"])})


@app.route("/api/groups", methods=["PUT"])
@login_required
def save_groups():
    # group layout is a display preference, not a container action, so it stays
    # editable in READ_ONLY mode
    if not csrf_ok():
        return jsonify({"error": "bad csrf token"}), 403
    cleaned = validate_groups(request.get_json(silent=True))
    if cleaned is None:
        return jsonify({"error": "invalid group settings"}), 400
    os.makedirs(GROUPS_DIR, exist_ok=True)
    write_json(groups_path(session["user"]), cleaned)
    return jsonify({"error": None, "groups": cleaned})


@app.route("/api/vitals")
@login_required
def api_vitals():
    mem = psutil.virtual_memory()
    disks = []
    for d in load_groups(session["user"]).get("disks") or DEFAULT_DISKS:
        try:
            u = psutil.disk_usage(d["path"])
            disks.append({"label": d["label"], "path": d["path"], "used": u.used, "total": u.total, "free": u.free, "percent": u.percent})
        except OSError:
            # the folder is gone (a mount was removed): that meter says so, the rest still work
            disks.append({"label": d["label"], "path": d["path"], "error": "not available"})
    return jsonify(
        {
            "disks": disks,
            "cpu_percent": psutil.cpu_percent(interval=None),
            "cpu_count": psutil.cpu_count(),
            "mem_used": mem.total - mem.available,
            "mem_total": mem.total,
        }
    )


def available_disks():
    """The disks this container can see, one per device, for the "Add disk" picker. Each is
    reached through its shortest mount point, and only real directories on real devices count
    (not /proc, tmpfs, or single files such as /etc/hosts)."""
    best = {}
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint
        if mp.startswith(("/proc", "/sys", "/dev")) or not os.path.isdir(mp):
            continue
        if part.device in best and len(mp) >= len(best[part.device]["path"]):
            continue
        try:
            u = psutil.disk_usage(mp)
        except OSError:
            continue
        best[part.device] = {
            "path": mp, "device": part.device, "fstype": part.fstype,
            "used": u.used, "total": u.total, "free": u.free, "percent": u.percent,
        }
    return sorted(best.values(), key=lambda d: d["path"])


@app.route("/api/disks")
@login_required
def api_disks():
    return jsonify({"error": None, "items": available_disks()})


@app.route("/api/containers/<name>/logs")
@login_required
def container_logs(name):
    tail = min(max(request.args.get("tail", DEFAULT_LOG_TAIL, type=int), 1), MAX_LOG_TAIL)
    try:
        raw = docker_client.containers.get(name).logs(tail=tail, timestamps=True)
    except docker.errors.NotFound:
        return jsonify({"error": "no such container"}), 404
    except docker.errors.DockerException as e:
        return jsonify({"error": str(e)}), 500
    text = ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))
    return jsonify({"error": None, "lines": text.splitlines()})


@app.route("/api/containers/<name>/update", methods=["POST"])
@admin_required
def container_update(name):
    if READ_ONLY:
        return jsonify({"error": "Ratatosk is in read-only mode"}), 403
    if not csrf_ok():
        return jsonify({"error": "bad csrf token"}), 403
    error, code = start_update(name)
    return jsonify({"error": error}), code


def start_update(name):
    """Begin updating one Compose container in the background. Returns (error, http status):
    (None, 202) when it started. Shared by the Update button and the scheduler, which do their
    own permission and read-only checks."""
    if not UPDATE_CHECK:
        return "updates are switched off", 400
    try:
        c = docker_client.containers.get(name)
    except docker.errors.NotFound:
        return "no such container", 404
    except docker.errors.DockerException as e:
        return str(e), 500
    if is_self(c):
        return "Ratatosk can't update its own container", 400
    d = describe(c)
    if not d["update"]:
        return "no update available", 400
    if d["update"]["blocked"]:
        return d["update"]["blocked"], 400
    labels = c.attrs.get("Config", {}).get("Labels") or {}
    project, service, files, workdir = compose_info(labels)
    with _jobs_lock:
        if _jobs.get(name, {}).get("state") == "running":
            return "already updating", 409
        _jobs[name] = {"state": "running", "error": None}
    activity.log("info", "updates", f"update of {name} started")
    threading.Thread(target=_run_update, args=(name, project, service, files, workdir), daemon=True).start()
    return None, 202


PELICAN_STALE = 10  # default seconds before the container list asks Pelican again
_pelican_busy = False
_pelican_busy_lock = threading.Lock()


def pelican_context(containers):
    """(container, spec, creds) for the set-up Pelican panel, or None."""
    store = load_integrations()
    for c in containers:
        spec = adapter_for(c)
        if spec and spec["id"] == "pelican" and c.status == "running" and not is_self(c):
            creds = stored_secret(store, c, spec)
            if creds:
                return c, spec, creds
    return None


def _pelican_refresh(bases, creds):
    global _pelican_busy
    try:
        handlers.pelican_servers(bases, creds)
    except integrations.AppError:
        pass  # already in the activity log; the rows keep the last good answer until it is too old
    finally:
        with _pelican_busy_lock:
            _pelican_busy = False


def refresh_pelican(containers, stale=PELICAN_STALE):
    """Keep Pelican's names and states fresh for the container rows without ever making the
    list wait for the panel: the work runs in the background and the rows read what it left."""
    global _pelican_busy
    if not any(is_game_server(c.attrs.get("Config", {}).get("Labels") or {}) for c in containers):
        return
    age, _ = handlers.pelican_cached(max_age=stale)
    if age is not None:
        return
    ctx = pelican_context(containers)
    if not ctx:
        return
    with _pelican_busy_lock:
        if _pelican_busy:
            return
        _pelican_busy = True
    c, spec, creds = ctx
    bases = integrations.candidate_urls(c.attrs, spec["match"]["port"])
    if os.environ.get("DISABLE_SAMPLER") == "1":
        _pelican_refresh(bases, creds)  # tests: no threads, so the result is there at once
    else:
        threading.Thread(target=_pelican_refresh, args=(bases, creds), daemon=True).start()


@app.route("/api/containers/<name>/<action>", methods=["POST"])
@login_required
def container_action(name, action):
    if READ_ONLY:
        return jsonify({"error": "Ratatosk is in read-only mode"}), 403
    if not csrf_ok():
        return jsonify({"error": "bad csrf token"}), 403
    if action not in ACTIONS:
        return jsonify({"error": "unknown action"}), 400
    error, code = run_container_action(name, action)
    return jsonify({"error": error}), code


def run_container_action(name, action):
    """Start, stop or restart one container. Returns (error, http status): (None, 200) on
    success. Shared by the buttons and the scheduler, which do their own permission and
    read-only checks."""
    try:
        c = docker_client.containers.get(name)
        if is_self(c):
            return "Ratatosk can't control its own container", 400
        if is_game_server(c.attrs.get("Config", {}).get("Labels") or {}):
            # Wings owns a game server's lifecycle: a stop sent straight to Docker looks like a
            # crash to it and it starts the server again, so go through the panel when we can
            ctx = pelican_context(docker_client.containers.list(all=True))
            if ctx and ctx[2].get("client") and handlers.PELICAN_UUID.match(c.name):
                try:
                    handlers.pelican_power(integrations.candidate_urls(ctx[0].attrs, ctx[1]["match"]["port"]), ctx[2], c.name, action)
                except integrations.AppError as e:
                    activity.log("error", "containers", f"{action} {name} through Pelican failed: {e}")
                    return str(e), 502
                activity.log("info", "containers", f"{action} {name} through Pelican")
                _panel_cache.pop(ctx[0].name, None)  # the Pelican panel's list shows the old state otherwise
                return None, 200
            activity.log("warn", "containers", f"{action} {name}: no Pelican user key, using Docker directly (Wings may treat a stop as a crash and restart it)")
        if action == "start":
            c.start()
        else:
            getattr(c, action)(timeout=10)
        activity.log("info", "containers", f"{action} {name}")
        return None, 200
    except docker.errors.NotFound:
        return "no such container", 404
    except docker.errors.DockerException as e:
        activity.log("error", "containers", f"{action} {name} failed: {e}")
        return str(e), 500


@app.route("/api/containers/<name>", methods=["DELETE"])
@admin_required
def container_delete(name):
    """Remove one stopped container (`docker rm`). `volumes: true` also removes its anonymous
    volumes (`docker rm -v`); named volumes and bind mounts are never touched. A game server is
    Wings' to remove, so it is refused here whatever the page showed."""
    if READ_ONLY:
        return jsonify({"error": "Ratatosk is in read-only mode"}), 403
    if not csrf_ok():
        return jsonify({"error": "bad csrf token"}), 403
    body = request.get_json(silent=True)
    volumes = body.get("volumes") if isinstance(body, dict) else None
    if not isinstance(volumes, bool):
        return jsonify({"error": "volumes must be true or false"}), 400
    try:
        c = docker_client.containers.get(name)
        if is_self(c):
            return jsonify({"error": "Ratatosk can't delete its own container"}), 400
        if is_game_server(c.attrs.get("Config", {}).get("Labels") or {}):
            return jsonify({"error": "This container belongs to Wings. Delete the server in Pelican instead"}), 400
        if c.status not in ("exited", "created", "dead"):
            return jsonify({"error": "Only a stopped container can be deleted"}), 409
        c.remove(v=volumes)
        activity.log("info", "containers", f"deleted {name}" + (" with its anonymous volumes" if volumes else ""))
        return jsonify({"error": None})
    except docker.errors.NotFound:
        return jsonify({"error": "no such container"}), 404
    except docker.errors.DockerException as e:
        activity.log("error", "containers", f"delete {name} failed: {e}")
        return jsonify({"error": str(e)}), 500


# ---- game-server config editor ----------------------------------------------------------------
#
# Goes through Pelican's Client API with the stored user key, for Pelican game servers only. The
# server has to be offline (a running game rewrites its files on shutdown), checked here on every
# call and not just by the page. READ_ONLY turns the whole thing off, reading included, since
# config files can hold passwords. File contents are never written to the activity log.

def _editor_target(name):
    """((bases, creds, uuid), None) or (None, error response)."""
    if READ_ONLY:
        return None, (jsonify({"error": "Ratatosk is in read-only mode"}), 403)
    if not handlers.PELICAN_UUID.match(name):
        return None, (jsonify({"error": "not a Pelican game server"}), 404)
    try:
        c = docker_client.containers.get(name)
    except docker.errors.NotFound:
        return None, (jsonify({"error": "no such container"}), 404)
    except docker.errors.DockerException:
        return None, (jsonify({"error": "could not reach Docker"}), 500)
    if not is_game_server(c.attrs.get("Config", {}).get("Labels") or {}):
        return None, (jsonify({"error": "not a Pelican game server"}), 404)
    ctx = pelican_context(docker_client.containers.list(all=True))
    if not ctx or not ctx[2].get("client"):
        return None, (jsonify({"error": "editing files needs the Pelican user key"}), 409)
    bases = integrations.candidate_urls(ctx[0].attrs, ctx[1]["match"]["port"])
    try:
        state = handlers.pelican_state(bases, ctx[2], name)
    except integrations.AppError as e:
        return None, (jsonify({"error": str(e)}), 502)
    if state != "offline":
        return None, (jsonify({"error": "Stop the server before editing its files."}), 409)
    return (bases, ctx[2], name), None


def _file_view(path, text):
    parsed = editor.parse(text)
    return {"path": path, "raw": text, "hash": editor.digest(text), "format": parsed["format"], "entries": parsed["entries"], "header": parsed["header"]}


@app.route("/api/containers/<name>/config", methods=["GET"])
@login_required
def config_files(name):
    target, err = _editor_target(name)
    if err:
        return err
    try:
        files, truncated = handlers.pelican_files(*target)
    except integrations.AppError as e:
        activity.log("error", "editor", f"listing files of {name} failed: {e}")
        return jsonify({"error": str(e)}), 502
    activity.log("info", "editor", f"listed {len(files)} config file(s) of {name}", verbose=True)
    return jsonify({"error": None, "files": files, "truncated": truncated})


@app.route("/api/containers/<name>/config/file", methods=["GET", "PUT"])
@login_required
def config_file(name):
    put = request.method == "PUT"
    if put and not csrf_ok():
        return jsonify({"error": "bad csrf token"}), 403
    target, err = _editor_target(name)
    if err:
        return err
    body = request.get_json(silent=True) if put else None
    if put and not isinstance(body, dict):
        return jsonify({"error": "bad request"}), 400
    path = body.get("path") if put else request.args.get("path", "")
    if not editor.safe_path(path):
        return jsonify({"error": "not a file the editor can open"}), 400
    try:
        current = handlers.pelican_file_read(*target, path)
        if not put:
            activity.log("info", "editor", f"opened a config file of {name} ({len(current)} chars)")
            return jsonify({"error": None, **_file_view(path, current)})
        if body.get("hash") != editor.digest(current):
            return jsonify({"error": "The file changed on disk. Reload it and try again."}), 409
        if isinstance(body.get("raw"), str):
            text = body["raw"]
        else:
            edits = body.get("edits")
            if not isinstance(edits, dict):
                return jsonify({"error": "nothing to save"}), 400
            text = editor.render(current, editor.parse(current)["format"], edits)
        if len(text.encode("utf-8")) > editor.MAX_SIZE:
            return jsonify({"error": "that file would be too large"}), 400
        if text != current:
            handlers.pelican_file_write(*target, path, text)
            activity.log("info", "editor", f"saved a config file of {name} ({len(text)} chars)")
        return jsonify({"error": None, **_file_view(path, text)})
    except editor.EditError as e:
        return jsonify({"error": str(e)}), 400
    except integrations.AppError as e:
        activity.log("error", "editor", f"{'saving' if put else 'opening'} a file of {name} failed: {e}")
        return jsonify({"error": str(e)}), 502


# ---- debug panel ----------------------------------------------------------
#
# Reads the in-memory activity log (activity.py). Admins only: it holds container names, internal
# addresses and error text. Also open in READ_ONLY; the level switch only changes what gets recorded.

_secret_cache = (None, ())


def _stored_secrets():
    """Everything the log must never show: stored keys and passwords, the setup token and the
    session key. Re-read only when integrations.json changes."""
    global _secret_cache
    try:
        stamp = os.stat(INTEGRATIONS_PATH).st_mtime_ns
    except OSError:
        stamp = None
    if _secret_cache[0] != stamp or stamp is None:
        found = []
        for v in load_integrations()["keys"].values():
            found += list(v.values()) if isinstance(v, dict) else [v]
        _secret_cache = (stamp, tuple(x for x in found if isinstance(x, str)))
    extra = tuple(x for x in (setup_token, app.secret_key) if x)
    return _secret_cache[1] + extra


activity.set_secret_source(_stored_secrets)


@app.route("/api/debug")
@admin_required
def api_debug():
    since = request.args.get("since", 0, type=int)
    entries, last = activity.snapshot(since)
    return jsonify({"error": None, "entries": entries, "last": last, "level": activity.get_level(), "levels": list(activity.LEVELS)})


@app.route("/api/debug/level", methods=["POST"])
@admin_required
def api_debug_level():
    if not csrf_ok():
        return jsonify({"error": "bad csrf token"}), 403
    body = request.get_json(silent=True)
    level = body.get("level") if isinstance(body, dict) else None
    if level not in activity.LEVELS:
        return jsonify({"error": "unknown level"}), 400
    activity.set_level(level)
    return jsonify({"error": None, "level": level})


# ---- integrations ---------------------------------------------------------
#
# A container that matches an adapter shows up as "found". Nothing reads or stores a key
# until an admin asks: either "find it" (read the one file the adapter names) or type it
# in. Either way the key is tested against the app first, stored in integrations.json,
# and never sent back to the browser.

# container name -> (time fetched, {"panels": [...], "error": str | None})
_panel_cache = {}


def load_integrations():
    try:
        with open(INTEGRATIONS_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    keys = data.get("keys", {})
    dismissed = data.get("dismissed", [])
    return {
        "keys": keys if isinstance(keys, dict) else {},
        "dismissed": dismissed if isinstance(dismissed, list) else [],
    }


def save_integrations(data):
    write_json(INTEGRATIONS_PATH, data)
    _panel_cache.clear()
    handlers.pelican_forget()
    handlers.forget_sessions()


def adapter_for(c):
    labels = c.attrs.get("Config", {}).get("Labels") or {}
    return integrations.match_adapter(ADAPTERS, labels, image_ref(c))


def fetch_panels(c, spec, key, ttl=PANEL_TTL):
    cached = _panel_cache.get(c.name)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    if c.status != "running":
        return {"panels": [], "error": "the container is not running"}
    bases = integrations.candidate_urls(c.attrs, spec["match"]["port"])
    try:
        result = {"panels": [], "error": None}
        if spec.get("handler"):
            result["panels"] = handlers.HANDLERS[spec["handler"]].fetch(bases, spec, key)
        else:
            for p in spec["panels"]:
                _, data = integrations.call(bases, spec, key, p["path"], p["params"])
                result["panels"].append(integrations.build_panel(p, data))
    except integrations.AppError as e:
        result = {"panels": [], "error": str(e)}
        activity.log("warn", "integrations", f"{spec['id']} panel for {c.name} failed: {e}")
    _panel_cache[c.name] = (time.time(), result)
    return result


def stored_secret(store, c, spec):
    """What is stored for this container if it fits the adapter's auth kind, else None.
    (An adapter that changed from key to login makes the old entry unusable, not an error.)"""
    secret = store["keys"].get(c.name)
    kind = spec["auth"]["kind"]
    if kind == "keys":
        # any one of the adapter's keys is enough; each is optional
        if not isinstance(secret, dict):
            return None
        have = {k["id"]: secret[k["id"]] for k in spec["auth"]["keys"] if isinstance(secret.get(k["id"]), str) and secret[k["id"]]}
        return have or None
    if kind == "login":
        ok = isinstance(secret, dict) and all(isinstance(secret.get(k), str) for k in ("username", "password"))
    else:
        ok = isinstance(secret, str) and bool(secret)
    return secret if ok else None


@app.route("/api/integrations")
@login_required
def api_integrations():
    ttl = age_arg(PANEL_TTL, 5, 300)
    try:
        containers = docker_client.containers.list(all=True)
    except docker.errors.DockerException as e:
        return jsonify({"error": str(e), "items": [], "problems": ADAPTER_PROBLEMS}), 502
    store = load_integrations()
    items = []
    for c in sorted(containers, key=lambda c: c.name):
        spec = adapter_for(c)
        if spec is None or is_self(c):
            continue
        item = {
            "container": c.name,
            "adapter": spec["id"],
            "adapter_name": spec["name"],
            "key_file": (spec.get("key_file") or {}).get("path"),
            "auth_kind": spec["auth"]["kind"],
            "auth_keys": [],
            "state": "found",
            "panels": [],
            "error": None,
        }
        key = stored_secret(store, c, spec)
        if spec["auth"]["kind"] == "keys":
            item["auth_keys"] = [{**k, "set": bool(key and k["id"] in key)} for k in spec["auth"]["keys"]]
        if key:
            item["state"] = "configured"
            item.update(fetch_panels(c, spec, key, ttl))
        elif c.name in store["dismissed"]:
            item["state"] = "dismissed"
        items.append(item)
    return jsonify({"error": None, "items": items, "problems": ADAPTER_PROBLEMS})


def _integration_target(name, write):
    """(container, adapter, None) or (None, None, error response). `write` is for the
    calls that read a key out of a container or store one: refused in READ_ONLY."""
    if write and READ_ONLY:
        return None, None, (jsonify({"error": "Ratatosk is in read-only mode"}), 403)
    if not csrf_ok():
        return None, None, (jsonify({"error": "bad csrf token"}), 403)
    try:
        c = docker_client.containers.get(name)
    except docker.errors.NotFound:
        return None, None, (jsonify({"error": "no such container"}), 404)
    except docker.errors.DockerException as e:
        return None, None, (jsonify({"error": str(e)}), 500)
    spec = adapter_for(c)
    if spec is None or is_self(c):
        return None, None, (jsonify({"error": "no integration for this container"}), 400)
    return c, spec, None


def _store_key(c, spec, secret):
    """Test the key (or login) against the app, then save it. Returns a response.
    `secret` is a string for header auth, {"username", "password"} for a login."""
    existing = None
    if spec["auth"]["kind"] == "keys":
        # a set of optional keys: only what was typed is checked, and it is merged into what is stored
        allowed = {k["id"] for k in spec["auth"]["keys"]}
        secret = {k: v.strip() for k, v in secret.items() if k in allowed and isinstance(v, str) and v.strip()}
        valid, what = bool(secret) and all(KEY_RE.match(v) for v in secret.values()), "an API key"
        existing = stored_secret(load_integrations(), c, spec) or {}
    elif isinstance(secret, str):
        valid, what = bool(KEY_RE.match(secret)), "an API key"
    else:
        parts = [secret.get("username"), secret.get("password")]
        valid, what = all(isinstance(p, str) and LOGIN_RE.match(p) for p in parts), "a username and password"
    if not valid:
        return jsonify({"error": f"that does not look like {what}"}), 400
    if c.status != "running":
        return jsonify({"error": "start the container first so it can be checked"}), 400
    bases = integrations.candidate_urls(c.attrs, spec["match"]["port"])
    handler = handlers.HANDLERS.get(spec.get("handler"))
    if handler and handler.check:
        problem = handler.check(bases, spec, secret)
    else:
        problem = integrations.test_key(bases, spec, secret)
    if problem:
        activity.log("warn", "integrations", f"{spec['id']} credentials for {c.name} not stored: {problem}")
        return jsonify({"error": problem}), 400
    with _data_lock:
        store = load_integrations()
        if existing is not None:
            existing = stored_secret(store, c, spec) or {}  # as stored now, not as it was before the check
        store["keys"][c.name] = {**existing, **secret} if existing is not None else secret
        store["dismissed"] = [n for n in store["dismissed"] if n != c.name]
        save_integrations(store)
    kept = f" ({', '.join(sorted(secret))})" if existing is not None else ""
    activity.log("info", "integrations", f"{spec['id']} credentials for {c.name} tested and stored{kept}")
    return jsonify({"error": None})


@app.route("/api/integrations/<name>/find-key", methods=["POST"])
@admin_required
def integration_find_key(name):
    c, spec, err = _integration_target(name, write=True)
    if err:
        return err
    key_file = spec.get("key_file")
    if not key_file:
        return jsonify({"error": "this app has no key file to read; enter it manually"}), 400
    activity.log("info", "integrations", f"admin asked to read {key_file['path']} in {c.name}")
    try:
        stream, _ = c.get_archive(key_file["path"])
        key = integrations.key_from_archive(stream, key_file)
    except docker.errors.NotFound:
        activity.log("warn", "integrations", f"{key_file['path']} not found in {c.name}")
        return jsonify({"error": f"{key_file['path']} was not found in the container"}), 404
    except docker.errors.DockerException as e:
        activity.log("error", "integrations", f"reading {key_file['path']} in {c.name} failed: {e}")
        return jsonify({"error": str(e)}), 500
    if not key:
        activity.log("warn", "integrations", f"no key found in {key_file['path']} in {c.name}")
        return jsonify({"error": f"no key found in {key_file['path']}; enter it manually"}), 404
    return _store_key(c, spec, key)


@app.route("/api/integrations/<name>/key", methods=["PUT", "DELETE"])
@admin_required
def integration_key(name):
    c, spec, err = _integration_target(name, write=True)
    if err:
        return err
    if request.method == "DELETE":
        with _data_lock:
            store = load_integrations()
            store["keys"].pop(c.name, None)
            save_integrations(store)
        activity.log("info", "integrations", f"credentials for {c.name} removed")
        return jsonify({"error": None})
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    if spec["auth"]["kind"] == "keys":
        given = body.get("keys")
        if not isinstance(given, dict) or not any(isinstance(v, str) and v.strip() for v in given.values()):
            return jsonify({"error": "enter at least one key"}), 400
        return _store_key(c, spec, given)
    if spec["auth"]["kind"] == "login":
        user, password = body.get("username"), body.get("password")
        if not isinstance(user, str) or not isinstance(password, str):
            return jsonify({"error": "a username and password are required"}), 400
        return _store_key(c, spec, {"username": user.strip(), "password": password})
    key = body.get("key")
    if not isinstance(key, str):
        return jsonify({"error": "a key is required"}), 400
    return _store_key(c, spec, key.strip())


@app.route("/api/integrations/<name>/<action>", methods=["POST"])
@login_required
def integration_dismiss(name, action):
    # hiding a notice is a display preference, like groups, so READ_ONLY allows it
    if action not in ("dismiss", "restore"):
        return jsonify({"error": "unknown action"}), 400
    c, spec, err = _integration_target(name, write=False)
    if err:
        return err
    with _data_lock:
        store = load_integrations()
        if action == "dismiss":
            if c.name in store["keys"]:
                return jsonify({"error": "remove the key first"}), 400
            if c.name not in store["dismissed"]:
                store["dismissed"].append(c.name)
        else:
            store["dismissed"] = [n for n in store["dismissed"] if n != c.name]
        save_integrations(store)
    return jsonify({"error": None})


SCHEDULE_TICK = 30  # seconds between looks at the clock
SCHEDULE_GRACE = 300  # a slot noticed later than this is skipped and logged, not run late
_schedule_stop = threading.Event()  # the scheduler waits on it between ticks; tests set it to end the loop


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _clean_schedule(raw):
    """A stored item as the code expects it, or None when it is damaged."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    fields, err = schedules.validate(raw)
    if err:
        return None
    result = raw.get("last_result")
    return {
        "id": raw["id"], **fields,
        "last_slot": _number(raw.get("last_slot")) or 0,
        "last_run": _number(raw.get("last_run")),
        "last_result": result[:schedules.MAX_RESULT] if isinstance(result, str) else "",
    }


def load_schedules():
    """{"timezone", "items"}; a missing or damaged file is an empty list (as for integrations)."""
    try:
        with open(SCHEDULES_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw = data.get("items")
    items = [i for i in (_clean_schedule(r) for r in (raw if isinstance(raw, list) else [])) if i]
    tz = data.get("timezone")
    return {"timezone": tz if schedules.valid_timezone(tz) else schedules.DEFAULT_TZ, "items": items[:schedules.MAX_ITEMS]}


def save_schedules(data):
    write_json(SCHEDULES_PATH, data)


def _rebase(item, tz, now):
    """Count the newest slot already past as dealt with, so an edit, a new timezone or switching
    an item on never runs for a time that has gone by."""
    item["last_slot"] = schedules.prev_slot(item, tz, now) or 0


def _schedule_outcome(label, err, code, done):
    """Log and summarise one scheduled action: done, skipped (nothing to do or not possible
    right now) or failed."""
    if err is None:
        activity.log("info", "schedules", f"{label}: {done}")
        return done
    if code in (400, 409):
        activity.log("warn", "schedules", f"{label} skipped: {err}")
        return f"skipped: {err}"[:schedules.MAX_RESULT]
    activity.log("error", "schedules", f"{label} failed: {err}")
    return f"failed: {err}"[:schedules.MAX_RESULT]


UPDATE_WAIT = 2 * UPDATE_TIMEOUT + 60  # an update is a pull and an up, each with its own timeout


def _wait_for_update(name):
    """Block until this container's update job is over. Returns its error, or None when it
    finished (a finished job removes itself; a failed one stays with its error)."""
    deadline = time.monotonic() + UPDATE_WAIT
    while True:
        with _jobs_lock:
            job = _jobs.get(name)
        if job is None:
            return None
        if job["state"] != "running":
            return job["error"]
        if time.monotonic() > deadline:
            return "timed out waiting for the update"
        _schedule_stop.wait(2)


def _summary(results):
    """The one line the list shows for [(name, outcome)]: the outcome itself for a single
    container, else the counts and what did not go through."""
    if len(results) == 1:
        return results[0][1]
    bad = [(n, t) for n, t in results if t.startswith(("skipped", "failed"))]
    parts = [f"{len(results) - len(bad)} done"]
    for kind in ("skipped", "failed"):
        count = sum(1 for _, t in bad if t.startswith(kind))
        if count:
            parts.append(f"{count} {kind}")
    text = ", ".join(parts)
    if bad:
        text += " (" + "; ".join(f"{n}: {t.split(': ', 1)[-1]}" for n, t in bad) + ")"
    return text[:schedules.MAX_RESULT]


def _run_schedule(item):
    """Do one scheduled action on each of its containers, one after another in the stored order,
    and return a short result for the list. One that can't be done is noted and the rest go on."""
    names = item["containers"]
    if READ_ONLY:
        activity.log("warn", "schedules", f"scheduled {item['action']} of {', '.join(names)} skipped: Ratatosk is in read-only mode")
        return "skipped: read-only mode"
    if item["action"] == "update":
        # an update needs the registry's newest answer, which may be up to half an hour old
        check_registry()
    results = []
    for i, name in enumerate(names):
        label = f"scheduled {item['action']} of {name}"
        if item["action"] == "restart":
            err, code = run_container_action(name, "restart")
            text = _schedule_outcome(label, err, code, "restarted")
        else:
            err, code = start_update(name)
            if err is None and i < len(names) - 1:
                # the next one waits, so a database others rely on is back before they are touched
                text = _schedule_outcome(label, _wait_for_update(name), 500, "updated")
            else:
                text = _schedule_outcome(label, err, code, "update started")
        results.append((name, text))
    return _summary(results)


def _finish_due(item, slot, now, tz):
    """Run one due schedule (or note that it was missed) and store how it went."""
    if now - slot > SCHEDULE_GRACE:
        result = f"missed: not running at {schedules.fmt(slot, tz)}"
        activity.log("warn", "schedules", f"scheduled {item['action']} of {', '.join(item['containers'])} {result}")
        ran = False
    else:
        result, ran = _run_schedule(item), True
    with _data_lock:
        data = load_schedules()
        for stored in data["items"]:
            if stored["id"] == item["id"]:
                stored["last_result"] = result
                if ran:
                    stored["last_run"] = now
                save_schedules(data)
                break


def run_due_schedules(now):
    """Run every schedule whose slot has come since the last look. A slot more than
    SCHEDULE_GRACE late (Ratatosk was off or the host asleep) is logged as missed, not run."""
    with _data_lock:
        data = load_schedules()
        tz = schedules.get_zone(data["timezone"])
        due = []
        for item in data["items"]:
            if not item["enabled"]:
                continue
            slot = schedules.prev_slot(item, tz, now)
            if slot is None or slot <= item["last_slot"]:
                continue
            item["last_slot"] = slot
            due.append((dict(item), slot))
        if due:
            save_schedules(data)
    for item, slot in due:
        if len(item["containers"]) > 1:
            # a list can spend many minutes waiting for updates; on its own thread it does not make
            # the schedules after it late (and so "missed")
            threading.Thread(target=_finish_due, args=(item, slot, now, tz), daemon=True).start()
        else:
            _finish_due(item, slot, now, tz)


def _scheduler():
    while True:
        try:
            run_due_schedules(time.time())
        except Exception as e:
            activity.log("error", "schedules", f"scheduler error: {type(e).__name__}: {e}")
        if _schedule_stop.wait(SCHEDULE_TICK):
            return


def _schedule_view(item, tz, now, names):
    nxt = schedules.next_slot(item, tz, now) if item["enabled"] else None
    shown = {k: item[k] for k in ("id", "containers", "action", "freq", "time", "weekday", "monthday", "enabled", "last_result")}
    missing = [] if names is None else [n for n in item["containers"] if n not in names]
    return {
        **shown,
        "next_run": schedules.fmt(nxt, tz),
        "last_run": schedules.fmt(item["last_run"], tz),
        "container_exists": not missing,
        "missing": missing,
    }


def _container_error(name, action):
    """Why this container can't take this scheduled action, or None."""
    try:
        c = docker_client.containers.get(name)
    except docker.errors.NotFound:
        return "no such container"
    except docker.errors.DockerException as e:
        return str(e)
    if is_self(c):
        return "Ratatosk can't control its own container"
    if action == "update":
        if not UPDATE_CHECK:
            return "updates are switched off"
        if compose_info(c.attrs.get("Config", {}).get("Labels") or {}) is None:
            return "only Docker Compose containers can be updated"
    return None


def _schedule_target_error(fields, names=None):
    """Why a container of this schedule can't take its action, or None. `names` narrows the
    check to some of the containers (the ones an edit added)."""
    for name in fields["containers"] if names is None else names:
        err = _container_error(name, fields["action"])
        if err:
            return f"{name}: {err}" if len(fields["containers"]) > 1 else err
    return None


def _schedule_write_refusal():
    if READ_ONLY:
        return jsonify({"error": "Ratatosk is in read-only mode"}), 403
    if not csrf_ok():
        return jsonify({"error": "bad csrf token"}), 403
    return None


@app.route("/api/schedules", methods=["GET", "POST"])
@admin_required
def api_schedules():
    now = time.time()
    if request.method == "POST":
        refused = _schedule_write_refusal()
        if refused:
            return refused
        fields, err = schedules.validate(request.get_json(silent=True))
        err = err or (fields and _schedule_target_error(fields))
        if err:
            return jsonify({"error": err}), 400
        with _data_lock:
            data = load_schedules()
            if len(data["items"]) >= schedules.MAX_ITEMS:
                return jsonify({"error": f"at most {schedules.MAX_ITEMS} schedules"}), 400
            item = {"id": secrets.token_hex(4), **fields, "last_slot": 0, "last_run": None, "last_result": ""}
            _rebase(item, schedules.get_zone(data["timezone"]), now)
            data["items"].append(item)
            save_schedules(data)
        return jsonify({"error": None, "id": item["id"]}), 201
    try:
        names = {c.name for c in docker_client.containers.list(all=True)}
    except docker.errors.DockerException:
        names = None  # can't tell, so don't flag anything as gone
    data = load_schedules()
    tz = schedules.get_zone(data["timezone"])
    return jsonify({
        "error": None,
        "timezone": data["timezone"],
        "timezones": schedules.timezones(),
        "now": schedules.fmt(now, tz),
        "read_only": READ_ONLY,
        "update_check": UPDATE_CHECK,
        "items": [_schedule_view(i, tz, now, names) for i in data["items"]],
    })


@app.route("/api/schedules/<sid>", methods=["PUT", "DELETE"])
@admin_required
def api_schedule(sid):
    refused = _schedule_write_refusal()
    if refused:
        return refused
    body = request.get_json(silent=True)
    with _data_lock:
        data = load_schedules()
        item = next((i for i in data["items"] if i["id"] == sid), None)
        if item is None:
            return jsonify({"error": "no such schedule"}), 404
        if request.method == "DELETE":
            data["items"].remove(item)
            save_schedules(data)
            return jsonify({"error": None})
        fields, err = schedules.validate(body, base=item)
        if not err:
            # a new action means every container is checked again; otherwise only the ones added
            added = None if fields["action"] != item["action"] else [n for n in fields["containers"] if n not in item["containers"]]
            if added is None or added:
                err = _schedule_target_error(fields, added)
        if err:
            return jsonify({"error": err}), 400
        item.update(fields)
        _rebase(item, schedules.get_zone(data["timezone"]), time.time())
        save_schedules(data)
    return jsonify({"error": None})


@app.route("/api/schedules/timezone", methods=["PUT"])
@admin_required
def api_schedules_timezone():
    refused = _schedule_write_refusal()
    if refused:
        return refused
    body = request.get_json(silent=True)
    name = body.get("timezone") if isinstance(body, dict) else None
    if not schedules.valid_timezone(name):
        return jsonify({"error": "unknown timezone"}), 400
    with _data_lock:
        data = load_schedules()
        data["timezone"] = name
        tz, now = schedules.get_zone(name), time.time()
        for item in data["items"]:
            _rebase(item, tz, now)
        save_schedules(data)
    return jsonify({"error": None})


# one gunicorn worker (see Dockerfile), so a single sampler thread; tests turn it off
if os.environ.get("DISABLE_SAMPLER") != "1":
    threading.Thread(target=_scheduler, daemon=True).start()
    threading.Thread(target=_stats_sampler, daemon=True).start()
    if UPDATE_CHECK:
        threading.Thread(target=_update_checker, daemon=True).start()
