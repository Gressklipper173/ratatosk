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
    assert schedules.valid_timezone("Europe/Oslo") and not schedules.valid_timezone("Mars/Base")
    assert schedules.get_zone("Mars/Base") is not None  # unknown falls back instead of crashing


# ---- validation ------------------------------------------------------------


def base(**kw):
    return {"container": "web", "action": "restart", "freq": "daily", "time": "03:00", **kw}


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
        {"container": ""},
        {"container": "../etc"},
        {"container": 5},
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
    stored = {"container": "web", "action": "restart", "freq": "weekly", "time": "03:00", "weekday": 1, "monthday": None, "enabled": True}
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
    assert it["id"] == sid and it["container"] == "web" and it["action"] == "restart" and it["freq"] == "weekly"
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
    assert create(c, csrf, container="ghost").status_code == 400
    assert "Ratatosk" in create(c, csrf, container="dashboard").get_json()["error"]
    assert "Compose" in create(c, csrf, action="update").get_json()["error"]  # web is not a Compose container
    monkeypatch.setattr(env.app, "UPDATE_CHECK", False)
    env.client.add(FakeContainer("comp", labels=COMPOSE))
    assert "switched off" in create(c, csrf, container="comp", action="update").get_json()["error"]
    monkeypatch.setattr(env.app, "UPDATE_CHECK", True)
    assert create(c, csrf, container="comp", action="update").status_code == 201
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
    assert it["container_exists"] is False


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
    seed(env, container="dashboard")
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
