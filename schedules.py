"""Scheduled actions: which slot an item is due for, and whether a stored item is valid.

Pure functions with no Flask or Docker in them, so the date maths can be tested with a fixed
clock. A schedule item is a dict:

    id, containers (the order they run in, each {"name", "action"}), freq ("daily" | "weekly" |
    "monthly"), time ("HH:MM", in the server timezone), weekday (0 = Monday .. 6, weekly only),
    monthday (1..31, monthly only), enabled, last_slot, last_run, last_result

A container's action is "restart", "update" (only if a newer image exists) or "auto" (update if
there is a newer image, else restart). An older file (or client) has one `action` for the whole
schedule and plain names, or one `container`; both read as the list above.

`last_slot` is the newest slot (epoch seconds) that has been dealt with, whether it ran or was
skipped. The scheduler compares it with `prev_slot()`, so a slot is handled once, and one that
passed while Ratatosk was off is recognised as missed instead of running late.
"""
import calendar
import re
import zoneinfo
from datetime import datetime, timedelta, timezone

ACTIONS = ("restart", "update", "auto")
FREQS = ("daily", "weekly", "monthly")
MAX_ITEMS = 100
MAX_CONTAINERS = 50  # in one schedule
MAX_RESULT = 200
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")  # Docker's own rule for a container name
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DEFAULT_TZ = "UTC"
SEARCH_DAYS = 62  # enough to find a monthly slot, which is never more than 31 days away


# tz data also holds old aliases ("US/Pacific", "Hongkong", "EST5EDT", "Etc/GMT+5") that only add
# noise when searching for a city, so the picker offers Region/City names and UTC
OLD_PREFIXES = ("Etc/", "US/", "Canada/", "Brazil/", "Chile/", "Mexico/", "SystemV/", "posix/", "right/")


def timezones():
    """Names the picker offers. Without tz data on the host only UTC is possible."""
    try:
        names = set(zoneinfo.available_timezones())
    except Exception:
        names = set()
    names = {n for n in names if "/" in n and not n.startswith(OLD_PREFIXES)}
    names.add(DEFAULT_TZ)
    return sorted(names)


def get_zone(name):
    """The ZoneInfo for a stored name, or UTC when it is unknown here (tz data went missing)."""
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        return timezone.utc


def valid_timezone(name):
    return isinstance(name, str) and name in timezones()


def _slot_on(item, day, tz):
    """The slot (epoch seconds) of this item on this calendar day, or None if it has none."""
    freq = item["freq"]
    if freq == "weekly" and day.weekday() != item["weekday"]:
        return None
    if freq == "monthly":
        # the 31st in a 30-day month means the last day of that month
        if day.day != min(item["monthday"], calendar.monthrange(day.year, day.month)[1]):
            return None
    hh, mm = (int(x) for x in item["time"].split(":"))
    # fold=0: a time that happens twice on a clock change runs the first time; one that is
    # skipped runs once, at the shifted time
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).timestamp()


def prev_slot(item, tz, now):
    """The newest slot at or before `now` (epoch seconds), or None."""
    today = datetime.fromtimestamp(now, tz).date()
    for back in range(SEARCH_DAYS + 1):
        slot = _slot_on(item, today - timedelta(days=back), tz)
        if slot is not None and slot <= now:
            return slot
    return None


def next_slot(item, tz, now):
    """The first slot after `now` (epoch seconds), or None."""
    today = datetime.fromtimestamp(now, tz).date()
    for ahead in range(SEARCH_DAYS + 1):
        slot = _slot_on(item, today + timedelta(days=ahead), tz)
        if slot is not None and slot > now:
            return slot
    return None


def fmt(epoch, tz):
    """'2026-10-12 03:00' in the server timezone; empty for no time."""
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d %H:%M")


def _entries(data, base):
    """The submitted containers as [{"name", "action"}], or None when there are none. A bare name
    takes the schedule-wide `action` (what an older file or client sends); an `action` sent alone
    sets that action on every container already stored."""
    data = dict(data)
    if "containers" not in data and "container" in data:
        data["containers"] = [data["container"]]
    stored = (base or {}).get("containers")
    raw = data.get("containers", stored)
    if not isinstance(raw, list):
        return None
    # an action sent with the request wins; else a bare name keeps the action it has in the stored
    # schedule; else the schedule-wide one an older file has
    kept = {e["name"]: e["action"] for e in stored or [] if isinstance(e, dict)}
    sent = data.get("action")
    fallback = (base or {}).get("action")
    entries = []
    for e in raw:
        name, action = (e.get("name"), e.get("action")) if isinstance(e, dict) else (e, None)
        if sent is not None and ("containers" not in data or action is None):
            action = sent
        if action is None:
            action = kept.get(name, fallback)
        entries.append({"name": name, "action": action})
    return entries


def validate(data, base=None):
    """Check a schedule submitted by the page. `base` is the stored item being edited, whose
    values fill in what the page left out. Returns (fields, error); fields hold only what the
    page may set (containers, freq, time, weekday, monthday, enabled)."""
    if not isinstance(data, dict):
        return None, "expected an object"
    entries = _entries(data, base)
    merged = {k: (base or {}).get(k) for k in ("freq", "time", "weekday", "monthday", "enabled")}
    merged.update({k: data[k] for k in merged if k in data})
    if merged["enabled"] is None:
        merged["enabled"] = True
    if not entries or not all(isinstance(e["name"], str) and NAME_RE.match(e["name"]) for e in entries):
        return None, "choose at least one container"
    names = [e["name"] for e in entries]
    if len(set(names)) != len(names):
        return None, "a container is listed twice"
    if len(names) > MAX_CONTAINERS:
        return None, f"at most {MAX_CONTAINERS} containers in one schedule"
    if any(e["action"] not in ACTIONS for e in entries):
        return None, "choose restart, update or update/restart for each container"
    merged["containers"] = entries
    if merged["freq"] not in FREQS:
        return None, "repeat must be daily, weekly or monthly"
    if not isinstance(merged["time"], str) or not TIME_RE.match(merged["time"]):
        return None, "time must be HH:MM"
    if not isinstance(merged["enabled"], bool):
        return None, "enabled must be true or false"
    # a day that does not apply to the frequency is dropped, so a stale one never lingers
    weekday, monthday = merged["weekday"], merged["monthday"]
    if merged["freq"] == "weekly":
        if type(weekday) is not int or not 0 <= weekday <= 6:
            return None, "choose a weekday"
        monthday = None
    elif merged["freq"] == "monthly":
        if type(monthday) is not int or not 1 <= monthday <= 31:
            return None, "day of the month must be 1 to 31"
        weekday = None
    else:
        weekday = monthday = None
    merged["weekday"], merged["monthday"] = weekday, monthday
    return merged, None
