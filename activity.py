"""The dashboard's own activity log, for the Debug panel.

Always on, so it is bounded twice: by entry count and by total bytes, oldest dropped first,
and each message is cut short. It lives in memory only; nothing here writes a file, so it
can't fill the disk. (What reaches Docker's own log for the container is capped by the log
driver options in docker-compose.example.yml.)

Callers pass short facts: what happened, to which address, with what status. Never request
or reply bodies, headers, keys or passwords. As a second line of defence, `redact` replaces
any secret registered through `set_secret_source` that still slips into a message or a
traceback.
"""

import logging
import re
import sys
import threading
import time
import traceback
from collections import deque
from urllib.parse import urlsplit

MAX_ENTRIES = 2000
MAX_BYTES = 512 * 1024
MAX_MESSAGE = 500
MAX_TRACEBACK = 4000

LEVELS = ("normal", "verbose")
_NAMES = {"debug": 10, "info": 20, "warn": 30, "error": 40}

_lock = threading.Lock()
_entries = deque()
_bytes = 0
_seq = 0
_level = "normal"
_secret_source = lambda: ()
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# a secret shorter than this would blank out ordinary words
_MIN_SECRET = 4


def set_secret_source(fn):
    """`fn()` returns the secret strings currently stored (keys, passwords)."""
    global _secret_source
    _secret_source = fn


def redact(text):
    for s in _secret_source():
        if isinstance(s, str) and len(s) >= _MIN_SECRET:
            text = text.replace(s, "***")
    return text


def get_level():
    return _level


def set_level(level):
    global _level
    if level not in LEVELS:
        raise ValueError("unknown level")
    _level = level
    log("info", "debug", f"detail level set to {level}")


def clear():
    global _bytes
    with _lock:
        _entries.clear()
        _bytes = 0


def _add(level, source, msg, tb=None):
    global _bytes, _seq
    msg = _CONTROL.sub(" ", redact(str(msg)))[:MAX_MESSAGE]
    if tb:
        msg += "\n" + redact(tb)[-MAX_TRACEBACK:]
    entry = {"ts": time.time(), "level": level, "source": source, "msg": msg, "count": 1}
    size = len(msg) + len(source) + 48
    with _lock:
        last = _entries[-1] if _entries else None
        if last and (last["level"], last["source"], last["msg"]) == (level, source, msg):
            last["count"] += 1  # a poll failing every few seconds is one line, not hundreds
            last["ts"] = entry["ts"]
            _seq += 1
            last["seq"] = _seq
            return
        _seq += 1
        entry["seq"] = entry["id"] = _seq
        entry["size"] = size
        _entries.append(entry)
        _bytes += size
        while _entries and (len(_entries) > MAX_ENTRIES or _bytes > MAX_BYTES):
            _bytes -= _entries.popleft()["size"]


def log(level, source, msg, verbose=False, exc=False):
    """Record one line (and echo it to stderr, which `docker logs` shows). `verbose=True`
    lines (every successful adapter request) are only kept at the verbose level."""
    if verbose and _level != "verbose":
        return
    tb = traceback.format_exc() if exc else None
    _add(level, source, msg, tb)
    print(f"[{source}] {level}: {redact(str(msg))[:MAX_MESSAGE]}", file=sys.stderr, flush=True)
    if tb:
        print(redact(tb)[-MAX_TRACEBACK:], file=sys.stderr, flush=True)


def safe_url(url):
    """scheme://host:port/path without the query string (a query is never needed to
    understand a request, and may carry anything)."""
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc.rpartition('@')[2]}{p.path}"
    except ValueError:
        return "(unparseable address)"


def snapshot(since=0, limit=MAX_ENTRIES):
    with _lock:
        rows = [e for e in _entries if e["seq"] > since][-limit:]
        return [{k: e[k] for k in ("id", "seq", "ts", "level", "source", "msg", "count")} for e in rows], _seq


class _Handler(logging.Handler):
    """Carries Python's own logging (gunicorn, werkzeug, our code's exceptions) into the
    buffer. It does not print: those loggers already write to the console."""

    def emit(self, record):
        try:
            if getattr(record, "_activity_seen", False):
                return  # already taken by a logger further down the chain
            record._activity_seen = True
            level = "error" if record.levelno >= 40 else "warn" if record.levelno >= 30 else "info"
            tb = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else None
            _add(level, record.name.split(".")[0], record.getMessage(), tb)
        except Exception:
            pass


def capture_python_logging():
    """Idempotent. Info and above from gunicorn and werkzeug; warnings from everything else."""
    root = logging.getLogger()
    if not any(isinstance(h, _Handler) for h in root.handlers):
        root.addHandler(_Handler(level=logging.WARNING))
    for name in ("gunicorn.error", "werkzeug"):
        lg = logging.getLogger(name)
        if not any(isinstance(h, _Handler) for h in lg.handlers):
            lg.addHandler(_Handler(level=logging.INFO))
            lg.setLevel(logging.INFO)
