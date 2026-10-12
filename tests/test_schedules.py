import json
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import docker
import pytest
from conftest import FakeContainer, post_login

import schedules

UTC = timezone.utc
OSLO = ZoneInfo("Europe/Oslo")
COMPOSE = {
    "com.docker.compose.project": "media",
    "com.docker.compose.service": "web",
    "com.docker.compose.project.config_files": "/srv/media/compose.yml",
    "com.docker.compose.project.working_dir": "/srv/media",
}


def at(y, m, d, hh=0, mm=0, tz=UTC):
    return datetime(y, m, d, hh, mm, tzinfo=tz).timestamp()


def item(freq="daily", time="03:00", weekday=None, monthday=None):
    return {"freq": freq, "time": time, "weekday": weekday, "monthday": monthday}


# ---- slot maths ------------------------------------------------------------


def test_daily_slots():
    it = item()
    now = at(2026, 10, 12, 12, 0)
    assert schedules.prev_slot(it, UTC, now) == at(2026, 10, 12, 3, 0)
    assert schedules.next_slot(it, UTC, now) == at(2026, 10, 13, 3, 0)
    # before today's time, the newest slot is yesterday's
    assert schedules.prev_slot(it, UTC, at(2026, 10, 12, 2, 59)) == at(2026, 10, 11, 3, 0)
    # a slot exactly now counts as past, not next
    assert schedules.prev_slot(it, UTC, at(2026, 10, 12, 3, 0)) == at(2026, 10, 12, 3, 0)
    assert schedules.next_slot(it, UTC, at(2026, 10, 12, 3, 0)) == at(2026, 10, 13, 3, 0)


def test_weekly_slots():
    it = item("weekly", "04:30", weekday=6)  # Sunday; 2026-10-11 is a Sunday
    assert schedules.prev_slot(it, UTC, at(2026, 10, 14, 9)) == at(2026, 10, 11, 4, 30)
    assert schedules.next_slot(it, UTC, at(2026, 10, 14, 9)) == at(2026, 10, 18, 4, 30)


def test_monthly_slots_and_short_months():
    it = item("monthly", "01:00", monthday=31)
    # 31st exists in October, so it is the 31st; in November it is the 30th; in February the 28th
    assert schedules.next_slot(it, UTC, at(2026, 10, 15)) == at(2026, 10, 31, 1, 0)
    assert schedules.next_slot(it, UTC, at(2026, 11, 1)) == at(2026, 11, 30, 1, 0)
    assert schedules.next_slot(it, UTC, at(2027, 2, 1)) == at(2027, 2, 28, 1, 0)
    assert schedules.prev_slot(it, UTC, at(2026, 12, 5)) == at(2026, 11, 30, 1, 0)
    assert schedules.next_slot(item("monthly", "01:00", monthday=1), UTC, at(2026, 12, 31, 23)) == at(2027, 1, 1, 1, 0)


def test_slots_follow_the_timezone():
    it = item()
    # 03:00 in Oslo is 01:00 UTC in summer (CEST) and 02:00 UTC in winter (CET)
    assert schedules.next_slot(it, OSLO, at(2026, 7, 1)) == at(2026, 7, 1, 1, 0)
    assert schedules.next_slot(it, OSLO, at(2026, 12, 1)) == at(2026, 12, 1, 2, 0)
    assert schedules.fmt(at(2026, 7, 1, 1, 0), OSLO) == "2026-07-01 03:00"


def test_a_time_skipped_by_a_clock_change_runs_once():
    it = item(time="02:30")  # 2026-03-29 in Oslo: 02:00 jumps to 03:00
    slots = []
    now = at(2026, 3, 28, 12)
    for _ in range(3):
        now = schedules.next_slot(it, OSLO, now)
        slots.append(now)
    assert [schedules.fmt(s, OSLO)[:10] for s in slots] == ["2026-03-29", "2026-03-30", "2026-03-31"]


def test_a_time_repeated_by_a_clock_change_runs_once():
    it = item(time="02:30")  # 2026-10-25 in Oslo: 03:00 falls back to 02:00, so 02:30 happens twice
    now = at(2026, 10, 24, 12)
    first = schedules.next_slot(it, OSLO, now)
    second = schedules.next_slot(it, OSLO, first)
    assert schedules.fmt(first, OSLO)[:10] == "2026-10-25"
    assert first == at(2026, 10, 25, 0, 30)  # the first 02:30, still summer time
    assert schedules.fmt(second, OSLO)[:10] == "2026-10-26"


def test_timezone_list_has_utc_and_real_zones():
    names = schedules.timezones()
    assert "UTC" in names and "Europe/Oslo" in names
    assert not any(n.startswith("posix/") for n in names)
    # old aliases are left out; everything else is Region/City, plus UTC
    assert not {"US/Pacific", "Hongkong", "EST5EDT", "Etc/GMT+5", "localtime"} & set(names)
    assert all("/" in n for n in names if n != "UTC")
    assert schedules.valid_timezone("Europe/Oslo") and not schedules.valid_timezone("Mars/Base")
    assert schedules.get_zone("Mars/Base") is not None  # unknown falls back instead of crashing


# ---- validation ------------------------------------------------------------


def base(**kw):
    return {"containers": ["web"], "action": "restart", "freq": "daily", "time": "03:00", **kw}


def test_valid_schedules_keep_only_the_day_that_applies():
    f, err = schedules.validate(base(weekday=2, monthday=5))
    assert err is None and f["weekday"] is None and f["monthday"] is None and f["enabled"] is True
    f, err = schedules.validate(base(freq="weekly", weekday=0, monthday=9))
    assert err is None and f["weekday"] == 0 and f["monthday"] is None
    f, err = schedules.validate(base(freq="monthly", monthday=31, weekday=3))
    assert err is None and f["monthday"] == 31 and f["weekday"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"containers": [""]},
        {"containers": ["../etc"]},
        {"containers": [5]},
        {"containers": "web"},
        {"containers": []},
        {"containers": ["web", "web"]},
        {"action": "stop"},
        {"freq": "hourly"},
        {"time": "3:00"},
        {"time": "24:00"},
        {"time": "03:60"},
        {"time": None},
        {"enabled": "yes"},
        {"freq": "weekly"},  # no weekday
        {"freq": "weekly", "weekday": 7},
        {"freq": "weekly", "weekday": True},
        {"freq": "monthly"},
        {"freq": "monthly", "monthday": 0},
        {"freq": "monthly", "monthday": 32},
        {"freq": "monthly", "monthday": "5"},
    ],
)
def test_invalid_schedules_are_refused(bad):
    f, err = schedules.validate(base(**bad))
    assert f is None and err


def test_validate_rejects_non_objects():
    assert schedules.validate([1])[1] and schedules.validate(None)[1]


def test_validate_fills_gaps_from_the_stored_item():
    stored = {"containers": ["web"], "action": "restart", "freq": "weekly", "time": "03:00", "weekday": 1, "monthday": None, "enabled": True}
    f, err = schedules.validate({"enabled": False}, base=stored)
    assert err is None and f["enabled"] is False and f["weekday"] == 1


# ---- API -------------------------------------------------------------------


def hdr(csrf):
    return {"X-CSRF-Token": csrf}


def create(c, csrf, **kw):
    return c.post("/api/schedules", json=base(**kw), headers=hdr(csrf))


def test_list_starts_empty_in_utc(env, logged_in):
    c, _ = logged_in
    body = c.get("/api/schedules").get_json()
    assert body["items"] == [] and body["timezone"] == "UTC" and "Europe/Oslo" in body["timezones"]


def test_create_list_edit_delete(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("web"))
    r = create(c, csrf, freq="weekly", weekday=2)
    assert r.status_code == 201
    sid = r.get_json()["id"]
    [it] = c.get("/api/schedules").get_json()["items"]
    assert it["id"] == sid and it["containers"] == [{"name": "web", "action": "restart"}] and it["freq"] == "weekly"
    assert it["next_run"] and it["last_run"] == "" and it["container_exists"] is True
    # an edit may send only what changed
    assert c.put(f"/api/schedules/{sid}", json={"time": "05:15"}, headers=hdr(csrf)).status_code == 200
    [it] = c.get("/api/schedules").get_json()["items"]
    assert it["time"] == "05:15" and it["weekday"] == 2
    assert c.put(f"/api/schedules/{sid}", json={"enabled": False}, headers=hdr(csrf)).status_code == 200
    [it] = c.get("/api/schedules").get_json()["items"]
    assert it["enabled"] is False and it["next_run"] == ""
    assert c.delete(f"/api/schedules/{sid}", headers=hdr(csrf)).status_code == 200
    assert c.get("/api/schedules").get_json()["items"] == []
    assert c.delete(f"/api/schedules/{sid}", headers=hdr(csrf)).status_code == 404
    assert c.put(f"/api/schedules/{sid}", json={"time": "05:15"}, headers=hdr(csrf)).status_code == 404


def test_create_refusals(env, logged_in, monkeypatch):
    c, csrf = logged_in
    env.client.add(FakeContainer("web"))
    env.client.add(FakeContainer("dashboard", short_id="dashboardself"))
    assert c.post("/api/schedules", json=base()).status_code == 403  # no csrf
    assert create(c, csrf, time="nope").status_code == 400
    assert create(c, csrf, containers=["ghost"]).status_code == 400
    assert "Ratatosk" in create(c, csrf, containers=["dashboard"]).get_json()["error"]
    assert "Compose" in create(c, csrf, action="update").get_json()["error"]  # web is not a Compose container
    monkeypatch.setattr(env.app, "UPDATE_CHECK", False)
    env.client.add(FakeContainer("comp", labels=COMPOSE))
    assert "switched off" in create(c, csrf, containers=["comp"], action="update").get_json()["error"]
    monkeypatch.setattr(env.app, "UPDATE_CHECK", True)
    assert create(c, csrf, containers=["comp"], action="update").status_code == 201
    assert c.post("/api/schedules", data="not json", headers=hdr(csrf)).status_code == 400
    monkeypatch.setattr(env.app.schedules, "MAX_ITEMS", 1)
    assert create(c, csrf).status_code == 400


def test_read_only_blocks_every_write(env, logged_in, monkeypatch):
    c, csrf = logged_in
    env.client.add(FakeContainer("web"))
    sid = create(c, csrf).get_json()["id"]
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert create(c, csrf).status_code == 403
    assert c.put(f"/api/schedules/{sid}", json={"enabled": False}, headers=hdr(csrf)).status_code == 403
    assert c.delete(f"/api/schedules/{sid}", headers=hdr(csrf)).status_code == 403
    assert c.put("/api/schedules/timezone", json={"timezone": "UTC"}, headers=hdr(csrf)).status_code == 403
    assert c.get("/api/schedules").get_json()["read_only"] is True  # reading stays possible


def test_operators_cannot_touch_schedules(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("web"))
    sid = create(c, csrf).get_json()["id"]
    assert c.post("/api/users", json={"username": "op", "role": "operator", "password": "operator-pass"}, headers=hdr(csrf)).status_code == 200
    o = env.app.app.test_client()
    assert post_login(o, "op", "operator-pass").status_code == 302
    with o.session_transaction() as s:
        ocsrf = s["csrf"]
    assert o.get("/api/schedules").status_code == 403
    assert o.post("/api/schedules", json=base(), headers=hdr(ocsrf)).status_code == 403
    assert o.put(f"/api/schedules/{sid}", json={"enabled": False}, headers=hdr(ocsrf)).status_code == 403
    assert o.delete(f"/api/schedules/{sid}", headers=hdr(ocsrf)).status_code == 403
    assert o.put("/api/schedules/timezone", json={"timezone": "UTC"}, headers=hdr(ocsrf)).status_code == 403


def test_signed_out_gets_nothing(env):
    c = env.app.app.test_client()
    for method, path in (("get", "/api/schedules"), ("post", "/api/schedules"), ("put", "/api/schedules/x"), ("delete", "/api/schedules/x"), ("put", "/api/schedules/timezone")):
        assert getattr(c, method)(path).status_code == 302, path


def test_timezone_is_chosen_from_the_list(env, logged_in):
    c, csrf = logged_in
    assert c.put("/api/schedules/timezone", json={"timezone": "Mars/Base"}, headers=hdr(csrf)).status_code == 400
    assert c.put("/api/schedules/timezone", json={"timezone": 5}, headers=hdr(csrf)).status_code == 400
    assert c.put("/api/schedules/timezone", json={"timezone": "Europe/Oslo"}).status_code == 403
    assert c.put("/api/schedules/timezone", json={"timezone": "Europe/Oslo"}, headers=hdr(csrf)).status_code == 200
    assert c.get("/api/schedules").get_json()["timezone"] == "Europe/Oslo"


def test_a_damaged_file_reads_as_empty_and_bad_items_are_dropped(env):
    with open(env.app.SCHEDULES_PATH, "w") as f:
        f.write("{not json")
    assert env.app.load_schedules() == {"timezone": "UTC", "items": []}
    good = {"id": "a1", **base(), "last_slot": 5, "last_run": None, "last_result": ""}
    with open(env.app.SCHEDULES_PATH, "w") as f:
        json.dump({"timezone": "Mars/Base", "items": [good, {"id": "b2", **base(time="99:99")}, "junk", {"container": "x"}]}, f)
    data = env.app.load_schedules()
    assert data["timezone"] == "UTC" and [i["id"] for i in data["items"]] == ["a1"]
    with open(env.app.SCHEDULES_PATH, "w") as f:
        json.dump({"items": None}, f)
    assert env.app.load_schedules()["items"] == []


def test_a_container_that_is_gone_is_flagged(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("web"))
    create(c, csrf)
    del env.client.by_name["web"]
    [it] = c.get("/api/schedules").get_json()["items"]
    assert it["container_exists"] is False and it["missing"] == ["web"]


# ---- the scheduler ---------------------------------------------------------

SLOT = at(2026, 10, 12, 3, 0)


def seed(env, last_slot=at(2026, 10, 11, 3, 0), **kw):
    """One stored daily 03:00 UTC schedule, last dealt with yesterday's slot."""
    it = {"id": "s1", **base(**kw), "last_slot": last_slot, "last_run": None, "last_result": ""}
    env.app.save_schedules({"timezone": "UTC", "items": [it]})


def stored(env):
    [it] = env.app.load_schedules()["items"]
    return it


def test_a_due_restart_runs_once(env):
    web = env.client.add(FakeContainer("web"))
    seed(env)
    env.app.run_due_schedules(SLOT + 10)
    assert web.calls == ["restart"]
    it = stored(env)
    assert it["last_result"] == "restarted" and it["last_run"] == SLOT + 10 and it["last_slot"] == SLOT
    env.app.run_due_schedules(SLOT + 40)
    env.app.run_due_schedules(SLOT + 3600)
    assert web.calls == ["restart"]
    assert any("scheduled restart of web: restarted" in e["msg"] for e in env.app.activity.snapshot()[0])


def test_nothing_runs_before_the_time(env):
    web = env.client.add(FakeContainer("web"))
    seed(env)
    env.app.run_due_schedules(SLOT - 1)
    assert web.calls == [] and stored(env)["last_result"] == ""


def test_a_slot_that_passed_while_off_is_missed_not_run_late(env):
    web = env.client.add(FakeContainer("web"))
    seed(env)
    env.app.run_due_schedules(SLOT + env.app.SCHEDULE_GRACE + 1)
    assert web.calls == []
    it = stored(env)
    assert it["last_result"].startswith("missed") and it["last_run"] is None and it["last_slot"] == SLOT
    env.app.run_due_schedules(SLOT + env.app.SCHEDULE_GRACE + 60)
    assert web.calls == []
    # tomorrow's slot runs as normal
    env.app.run_due_schedules(SLOT + 86400 + 5)
    assert web.calls == ["restart"]


def test_a_disabled_schedule_does_not_run(env):
    web = env.client.add(FakeContainer("web"))
    seed(env, enabled=False)
    env.app.run_due_schedules(SLOT + 5)
    assert web.calls == [] and stored(env)["last_slot"] == at(2026, 10, 11, 3, 0)


def test_enabling_does_not_run_for_a_time_already_past(env, logged_in):
    c, csrf = logged_in
    web = env.client.add(FakeContainer("web"))
    sid = create(c, csrf, enabled=False).get_json()["id"]
    assert c.put(f"/api/schedules/{sid}", json={"enabled": True}, headers=hdr(csrf)).status_code == 200
    env.app.run_due_schedules(env.app.time.time())
    assert web.calls == []


def test_changing_the_timezone_does_not_run_anything(env, logged_in):
    c, csrf = logged_in
    web = env.client.add(FakeContainer("web"))
    seed(env, last_slot=0)
    # between UTC's 03:00 and Oslo's 03:00 (which is earlier in UTC): without a rebase this is due
    assert c.put("/api/schedules/timezone", json={"timezone": "Asia/Tokyo"}, headers=hdr(csrf)).status_code == 200
    assert stored(env)["last_slot"] > 0
    env.app.run_due_schedules(env.app.time.time())
    assert web.calls == []


def test_read_only_skips_and_says_so(env, monkeypatch):
    web = env.client.add(FakeContainer("web"))
    seed(env)
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    env.app.run_due_schedules(SLOT + 5)
    assert web.calls == [] and "read-only" in stored(env)["last_result"]


def test_a_failed_restart_is_recorded(env):
    web = env.client.add(FakeContainer("web"))

    def boom(timeout=None):
        raise docker.errors.APIError("daemon unhappy")

    web.restart = boom
    seed(env)
    env.app.run_due_schedules(SLOT + 5)
    assert stored(env)["last_result"].startswith("failed: ") and "daemon unhappy" in stored(env)["last_result"]


def test_a_missing_container_is_recorded_as_failed(env):
    seed(env)
    env.app.run_due_schedules(SLOT + 5)
    assert stored(env)["last_result"] == "failed: no such container"


def test_ratatosk_itself_is_skipped(env):
    me = env.client.add(FakeContainer("dashboard", short_id="dashboardself"))
    seed(env, containers=["dashboard"])
    env.app.run_due_schedules(SLOT + 5)
    assert me.calls == [] and stored(env)["last_result"].startswith("skipped")


def add_web(env, local="sha256:old", latest="sha256:new"):
    env.client.add(FakeContainer("web", labels=COMPOSE, image_ref="app:1", repo_digests=[f"app@{local}"]))
    env.client.registry["app:1"] = latest
    env.app._registry.clear()  # nobody has asked the registry yet: the schedule has to


def run_threads_inline(env, monkeypatch):
    thread = lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args))
    monkeypatch.setattr(env.app, "threading", SimpleNamespace(Thread=thread))


def test_a_due_update_checks_the_registry_then_updates(env, monkeypatch):
    add_web(env)
    seed(env, action="update")
    monkeypatch.setattr(env.app.os.path, "exists", lambda p: True)
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    calls = []
    monkeypatch.setattr(env.app.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    run_threads_inline(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert [c[-2:] for c in calls] == [["pull", "web"], ["--no-deps", "web"]]
    assert stored(env)["last_result"] == "update started"


def test_a_due_update_with_nothing_new_is_skipped(env, monkeypatch):
    add_web(env, latest="sha256:old")
    seed(env, action="update")
    calls = []
    monkeypatch.setattr(env.app.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    env.app.run_due_schedules(SLOT + 5)
    assert calls == [] and stored(env)["last_result"] == "skipped: no update available"


def test_a_due_update_blocked_by_a_missing_compose_file_is_skipped(env, monkeypatch):
    add_web(env)
    seed(env, action="update")
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    env.app.run_due_schedules(SLOT + 5)
    assert "compose file not visible" in stored(env)["last_result"] and stored(env)["last_result"].startswith("skipped")


def test_a_due_update_is_skipped_while_one_runs(env, monkeypatch):
    add_web(env)
    seed(env, action="update")
    monkeypatch.setattr(env.app.os.path, "exists", lambda p: True)
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    env.app._jobs["web"] = {"state": "running", "error": None}
    env.app.run_due_schedules(SLOT + 5)
    assert stored(env)["last_result"] == "skipped: already updating"


def test_updates_switched_off_skips_a_due_update(env, monkeypatch):
    add_web(env)
    seed(env, action="update")
    monkeypatch.setattr(env.app, "UPDATE_CHECK", False)
    env.app.run_due_schedules(SLOT + 5)
    assert stored(env)["last_result"] == "skipped: updates are switched off"


def test_the_loop_runs_each_tick_and_survives_an_error(env, monkeypatch):
    seen = []

    def fake(now):
        seen.append(now)
        if len(seen) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(env.app, "run_due_schedules", fake)
    waits = []

    def wait(t):
        waits.append(t)
        return len(waits) >= 2  # the second wait ends the loop

    monkeypatch.setattr(env.app._schedule_stop, "wait", wait)
    env.app._scheduler()
    assert len(seen) == 2 and waits == [env.app.SCHEDULE_TICK] * 2
    assert any("scheduler error: RuntimeError: boom" in e["msg"] for e in env.app.activity.snapshot()[0])


# ---- several containers in one schedule --------------------------------------


def test_a_list_keeps_its_order_and_the_older_spelling_reads_as_a_list():
    f, err = schedules.validate(base(containers=["db", "app", "web"]))
    assert err is None and [e["name"] for e in f["containers"]] == ["db", "app", "web"]
    legacy = {"container": "web", "action": "restart", "freq": "daily", "time": "03:00"}
    f, err = schedules.validate(legacy)
    assert err is None and f["containers"] == [{"name": "web", "action": "restart"}] and "container" not in f and "action" not in f
    # a client that still sends one name changes the list to that one
    f, err = schedules.validate({"container": "db", "action": "update"}, base=base(containers=["web", "app"]))
    assert err is None and f["containers"] == [{"name": "db", "action": "update"}]


def test_too_many_containers_are_refused(monkeypatch):
    monkeypatch.setattr(schedules, "MAX_CONTAINERS", 2)
    assert schedules.validate(base(containers=["a", "b"]))[1] is None
    assert "at most 2" in schedules.validate(base(containers=["a", "b", "c"]))[1]


def test_a_file_from_before_lists_still_loads(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("web"))
    old = {"id": "s1", "container": "web", "action": "restart", "freq": "daily", "time": "03:00", "enabled": True,
           "last_slot": 5, "last_run": None, "last_result": ""}
    with open(env.app.SCHEDULES_PATH, "w") as f:
        json.dump({"timezone": "UTC", "items": [old]}, f)
    [it] = c.get("/api/schedules").get_json()["items"]
    assert it["containers"] == [{"name": "web", "action": "restart"}] and "container" not in it and "action" not in it
    # the next save writes the new spelling
    assert c.put("/api/schedules/s1", json={"enabled": False}, headers=hdr(csrf)).status_code == 200
    with open(env.app.SCHEDULES_PATH) as f:
        saved = json.load(f)["items"][0]
    assert saved["containers"] == [{"name": "web", "action": "restart"}] and "container" not in saved and "action" not in saved


def test_create_and_edit_a_list(env, logged_in):
    c, csrf = logged_in
    for n in ("db", "app", "web"):
        env.client.add(FakeContainer(n))
    r = create(c, csrf, containers=["db", "app"])
    assert r.status_code == 201
    sid = r.get_json()["id"]
    [it] = c.get("/api/schedules").get_json()["items"]
    assert [e["name"] for e in it["containers"]] == ["db", "app"] and it["missing"] == []
    # reordering and adding are edits; only the added container is looked at
    new = [{"name": n, "action": "restart"} for n in ("app", "db", "web")]
    assert c.put(f"/api/schedules/{sid}", json={"containers": new}, headers=hdr(csrf)).status_code == 200
    assert c.get("/api/schedules").get_json()["items"][0]["containers"] == new
    bad = c.put(f"/api/schedules/{sid}", json={"containers": [{"name": "app", "action": "restart"}, {"name": "ghost", "action": "restart"}]}, headers=hdr(csrf))
    assert bad.status_code == 400 and bad.get_json()["error"] == "ghost: no such container"
    # a container that has gone since does not stop other edits to the schedule
    del env.client.by_name["db"]
    assert c.put(f"/api/schedules/{sid}", json={"time": "04:00"}, headers=hdr(csrf)).status_code == 200
    [it] = c.get("/api/schedules").get_json()["items"]
    assert it["missing"] == ["db"] and it["container_exists"] is False


def test_a_refusal_in_a_list_names_the_container(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("web"))
    r = create(c, csrf, containers=["web", "ghost"])
    assert r.status_code == 400 and r.get_json()["error"] == "ghost: no such container"
    env.client.add(FakeContainer("comp", labels=COMPOSE))
    r = create(c, csrf, containers=["comp", "web"], action="update")
    assert r.status_code == 400 and r.get_json()["error"].startswith("web: only Docker Compose")


def add_many(env, *names):
    boxes = [env.client.add(FakeContainer(n)) for n in names]
    order = []
    for box in boxes:
        box.restart = lambda timeout=None, box=box: order.append(box.name)
    return boxes, order


def test_a_list_restarts_in_order_on_its_own_thread(env, monkeypatch):
    boxes, order = add_many(env, "db", "app", "web")
    seed(env, containers=["db", "app", "web"])
    started = []
    thread = lambda target, args, daemon: SimpleNamespace(start=lambda: started.append((target, args)))
    monkeypatch.setattr(env.app, "threading", SimpleNamespace(Thread=thread))
    env.app.run_due_schedules(SLOT + 5)
    # handed to a thread, so the scheduler is free to look at the next schedule
    assert order == [] and len(started) == 1 and stored(env)["last_result"] == ""
    target, args = started[0]
    target(*args)
    assert order == ["db", "app", "web"]
    it = stored(env)
    assert it["last_result"] == "3 done" and it["last_run"] == SLOT + 5


def test_a_single_container_still_runs_in_the_scheduler_itself(env, monkeypatch):
    web = env.client.add(FakeContainer("web"))
    seed(env)
    monkeypatch.setattr(env.app, "threading", SimpleNamespace(Thread=lambda **kw: pytest.fail("no thread for one container")))
    env.app.run_due_schedules(SLOT + 5)
    assert web.calls == ["restart"]


def test_one_failure_does_not_stop_the_rest(env, monkeypatch):
    boxes, order = add_many(env, "db", "app", "web")

    def boom(timeout=None):
        raise docker.errors.APIError("daemon unhappy")

    boxes[1].restart = boom
    seed(env, containers=["db", "app", "ghost", "web"])
    run_threads_inline(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert order == ["db", "web"]
    result = stored(env)["last_result"]
    assert result.startswith("2 done, 2 failed (app: ") and "daemon unhappy" in result and "ghost: no such container" in result
    assert len(result) <= schedules.MAX_RESULT
    assert any("scheduled restart of ghost failed" in e["msg"] for e in env.app.activity.snapshot()[0])


def test_a_missed_list_is_logged_with_its_containers(env):
    boxes, order = add_many(env, "db", "app")
    seed(env, containers=["db", "app"])
    env.app.run_due_schedules(SLOT + env.app.SCHEDULE_GRACE + 1)  # a list is handed to a real thread
    for _ in range(100):
        if stored(env)["last_result"]:
            break
        env.app.time.sleep(0.05)
    assert order == [] and stored(env)["last_result"].startswith("missed")
    assert any("scheduled restart of db, app missed" in e["msg"] for e in env.app.activity.snapshot()[0])


def test_a_list_in_read_only_mode_is_skipped_once(env, monkeypatch):
    boxes, order = add_many(env, "db", "app")
    seed(env, containers=["db", "app"])
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    run_threads_inline(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert order == [] and stored(env)["last_result"] == "skipped: read-only mode"


def test_the_summary_line(env):
    summary = env.app._summary
    assert summary([("web", "restarted")]) == "restarted"
    assert summary([("web", "skipped: no update available")]) == "skipped: no update available"
    assert summary([("a", "restarted"), ("b", "update started")]) == "2 done"
    assert summary([("a", "restarted"), ("b", "failed: x"), ("c", "skipped: y")]) == "1 done, 1 skipped, 1 failed (b: x; c: y)"
    assert len(summary([(f"c{i}", "failed: " + "x" * 50) for i in range(20)])) <= schedules.MAX_RESULT


def add_two(env, local="sha256:old", latest="sha256:new"):
    env.client.add(FakeContainer("web", labels=COMPOSE, image_ref="app:1", repo_digests=[f"app@{local}"]))
    env.client.add(FakeContainer("db", labels={**COMPOSE, "com.docker.compose.service": "db"}, image_ref="db:1", repo_digests=[f"db@{local}"]))
    env.client.registry["app:1"] = latest
    env.client.registry["db:1"] = latest
    env.app._registry.clear()


def fake_compose(env, monkeypatch, fail=None):
    monkeypatch.setattr(env.app.os.path, "exists", lambda p: True)
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if fail and fail in cmd and "pull" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="pull access denied")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env.app.subprocess, "run", run)
    return calls


def test_a_list_of_updates_goes_one_after_another(env, monkeypatch):
    add_two(env)
    seed(env, action="update", containers=["db", "web"])
    calls = fake_compose(env, monkeypatch)
    run_threads_inline(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert [c[-1] for c in calls] == ["db", "db", "web", "web"]
    assert stored(env)["last_result"] == "2 done"


def test_a_failed_update_in_a_list_is_reported_and_the_next_still_runs(env, monkeypatch):
    add_two(env)
    seed(env, action="update", containers=["web", "db"])
    calls = fake_compose(env, monkeypatch, fail="web")
    run_threads_inline(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert [c[-1] for c in calls] == ["web", "db", "db"]  # web stopped after its pull
    assert stored(env)["last_result"] == "1 done, 1 failed (web: pull access denied)"


def test_an_update_with_nothing_new_is_skipped_inside_a_list(env, monkeypatch):
    add_two(env, latest="sha256:old")
    seed(env, action="update", containers=["web", "db"])
    calls = fake_compose(env, monkeypatch)
    run_threads_inline(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert calls == [] and stored(env)["last_result"] == "0 done, 2 skipped (web: no update available; db: no update available)"


def test_waiting_for_an_update_job(env, monkeypatch):
    wait = env.app._wait_for_update
    assert wait("nobody") is None  # no job: nothing running, nothing to wait for
    env.app._jobs["x"] = {"state": "error", "error": "boom"}
    assert wait("x") == "boom"
    env.app._jobs["x"] = {"state": "running", "error": None}
    ticks = []

    def tick(t):
        ticks.append(t)
        if len(ticks) == 2:
            del env.app._jobs["x"]

    monkeypatch.setattr(env.app._schedule_stop, "wait", tick)
    assert wait("x") is None and ticks == [2, 2]
    env.app._jobs["x"] = {"state": "running", "error": None}
    monkeypatch.setattr(env.app, "UPDATE_WAIT", -1)
    assert wait("x") == "timed out waiting for the update"


# ---- one action per container -------------------------------------------------


def mixed(*pairs):
    return [{"name": n, "action": a} for n, a in pairs]


def test_each_container_has_its_own_action():
    f, err = schedules.validate(base(containers=mixed(("db", "auto"), ("app", "update"), ("web", "restart"))))
    assert err is None and [e["action"] for e in f["containers"]] == ["auto", "update", "restart"]
    plain = {"freq": "daily", "time": "03:00"}  # no schedule-wide action to fall back on
    for bad in (mixed(("db", "stop")), [{"name": "db"}], ["db"], [{"action": "restart"}], mixed(("db", "restart"), ("db", "update"))):
        assert schedules.validate({**plain, "containers": bad})[0] is None, bad
    # a bare name keeps the action it has in the stored schedule
    stored = base(containers=mixed(("db", "auto"), ("web", "restart")))
    f, err = schedules.validate({"containers": ["web", "db"]}, base=stored)
    assert err is None and f["containers"] == mixed(("web", "restart"), ("db", "auto"))
    # an action sent alone sets every container's
    f, err = schedules.validate({"action": "update"}, base=stored)
    assert err is None and f["containers"] == mixed(("db", "update"), ("web", "update"))


def test_only_restart_is_possible_without_compose(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("plain"))
    env.client.add(FakeContainer("comp", labels=COMPOSE))
    for action in ("update", "auto"):
        r = create(c, csrf, containers=mixed(("comp", "restart"), ("plain", action)))
        assert r.status_code == 400 and r.get_json()["error"] == "plain: only Docker Compose containers can be updated"
    assert create(c, csrf, containers=mixed(("plain", "restart"), ("comp", "auto"))).status_code == 201


def test_update_actions_need_updates_to_be_switched_on(env, logged_in, monkeypatch):
    c, csrf = logged_in
    env.client.add(FakeContainer("comp", labels=COMPOSE))
    monkeypatch.setattr(env.app, "UPDATE_CHECK", False)
    assert create(c, csrf, containers=mixed(("comp", "restart"))).status_code == 201
    for action in ("update", "auto"):
        assert "switched off" in create(c, csrf, containers=mixed(("comp", action))).get_json()["error"]


def test_auto_updates_when_there_is_something_newer(env, monkeypatch):
    add_web(env)
    seed(env, containers=mixed(("web", "auto")))
    calls = fake_compose(env, monkeypatch)
    run_threads_inline(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert [c[-2:] for c in calls] == [["pull", "web"], ["--no-deps", "web"]]
    assert env.client.by_name["web"].calls == []  # the update recreates it; no extra restart
    assert stored(env)["last_result"] == "update started"


def test_auto_restarts_when_there_is_nothing_newer(env, monkeypatch):
    add_web(env, latest="sha256:old")
    seed(env, containers=mixed(("web", "auto")))
    calls = fake_compose(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert calls == [] and env.client.by_name["web"].calls == ["restart"]
    assert stored(env)["last_result"] == "no update, restarted"
    assert any("scheduled update/restart of web: no update, restarted" in e["msg"] for e in env.app.activity.snapshot()[0])


def test_auto_does_not_hide_an_update_that_cannot_run(env, monkeypatch):
    add_web(env)
    seed(env, containers=mixed(("web", "auto")))
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")  # but the compose file is not visible
    env.app.run_due_schedules(SLOT + 5)
    result = stored(env)["last_result"]
    assert result.startswith("skipped: compose file not visible") and env.client.by_name["web"].calls == []


def test_update_only_leaves_a_container_alone_when_nothing_is_newer(env, monkeypatch):
    add_web(env, latest="sha256:old")
    seed(env, containers=mixed(("web", "update")))
    env.app.run_due_schedules(SLOT + 5)
    assert env.client.by_name["web"].calls == [] and stored(env)["last_result"] == "skipped: no update available"


def test_auto_on_a_container_that_is_no_longer_compose_just_restarts(env):
    web = env.client.add(FakeContainer("web"))  # stored as auto, but it has lost its compose labels
    seed(env, containers=mixed(("web", "auto")))
    env.app.run_due_schedules(SLOT + 5)
    assert web.calls == ["restart"] and stored(env)["last_result"] == "no update, restarted"


def test_a_mixed_list_does_each_containers_own_action(env, monkeypatch):
    add_two(env)  # web and db, both with a newer image
    plain = env.client.add(FakeContainer("plain"))
    env.client.add(FakeContainer("quiet", labels={**COMPOSE, "com.docker.compose.service": "quiet"}, image_ref="q:1", repo_digests=["q@sha256:same"]))
    env.client.registry["q:1"] = "sha256:same"
    seed(env, containers=mixed(("db", "update"), ("plain", "restart"), ("quiet", "auto"), ("web", "update")))
    calls = fake_compose(env, monkeypatch)
    run_threads_inline(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert [c[-1] for c in calls] == ["db", "db", "web", "web"]
    assert plain.calls == ["restart"] and env.client.by_name["quiet"].calls == ["restart"]
    assert stored(env)["last_result"] == "4 done"
    assert any("scheduled actions" not in e["msg"] for e in env.app.activity.snapshot()[0])


def test_a_restart_only_list_never_asks_the_registry(env, monkeypatch):
    boxes, order = add_many(env, "db", "app")
    seed(env, containers=mixed(("db", "restart"), ("app", "restart")))
    monkeypatch.setattr(env.app, "check_registry", lambda: pytest.fail("restarts do not need the registry"))
    run_threads_inline(env, monkeypatch)
    env.app.run_due_schedules(SLOT + 5)
    assert order == ["db", "app"]
