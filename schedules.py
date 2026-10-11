"""Scheduled actions: which slot an item is due for, and whether a stored item is valid.

Pure functions with no Flask or Docker in them, so the date maths can be tested with a fixed
clock. A schedule item is a dict:

    id, containers (names, in the order they run), action ("restart" | "update"),
    freq ("daily" | "weekly" | "monthly"), time ("HH:MM", in the server timezone),
    weekday (0 = Monday .. 6, weekly only), monthday (1..31, monthly only), enabled,
    last_slot, last_run, last_result

An older file (or client) names one container as `container`; it reads as a list of one.

`last_slot` is the newest slot (epoch seconds) that has been dealt with, whether it ran or was
skipped. The scheduler compares it with `prev_slot()`, so a slot is handled once, and one that
passed while Ratatosk was off is recognised as missed instead of running late.
"""
import calendar
import re
import zoneinfo
from datetime import datetime, timedelta, timezone

ACTIONS = ("restart", "update")
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


def validate(data, base=None):
    """Check a schedule submitted by the page. `base` is the stored item being edited, whose
    values fill in what the page left out. Returns (fields, error); fields hold only what the
    page may set (containers, action, freq, time, weekday, monthday, enabled)."""
    if not isinstance(data, dict):
        return None, "expected an object"
    data = dict(data)
    # the older single-container spelling, from a file written before lists or a client that sends it
    if "containers" not in data and "container" in data:
        data["containers"] = [data["container"]]
    merged = {k: (base or {}).get(k) for k in ("containers", "action", "freq", "time", "weekday", "monthday", "enabled")}
    merged.update({k: data[k] for k in merged if k in data})
    if merged["enabled"] is None:
        merged["enabled"] = True
    names = merged["containers"]
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and NAME_RE.match(n) for n in names):
        return None, "choose at least one container"
    if len(set(names)) != len(names):
        return None, "a container is listed twice"
    if len(names) > MAX_CONTAINERS:
        return None, f"at most {MAX_CONTAINERS} containers in one schedule"
    merged["containers"] = list(names)
    if merged["action"] not in ACTIONS:
        return None, "action must be restart or update"
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
