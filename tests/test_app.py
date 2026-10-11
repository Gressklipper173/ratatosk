import importlib
import json
import os
import re
import stat
import sys
from types import SimpleNamespace

import pytest
from conftest import FakeContainer, form_csrf, post_login

import activity

# ---- setup and login ----------------------------------------------------


def test_setup_requires_token(env):
    c = env.app.app.test_client()
    r = c.post("/setup", data={"token": "wrong", "username": "a", "password": "longenough1", "csrf_token": form_csrf(c, "/setup")})
    assert r.status_code == 200
    assert b"Invalid setup token" in r.data
    assert env.app.load_users() == {}


def test_setup_rejects_short_password(env):
    c = env.app.app.test_client()
    r = c.post("/setup", data={"token": env.app.setup_token, "username": "a", "password": "short", "csrf_token": form_csrf(c, "/setup")})
    assert b"at least 8" in r.data


def test_setup_is_one_shot(env, logged_in):
    assert env.app.app.test_client().get("/setup").status_code == 302


def test_login_redirects_to_setup_until_admin_exists(env):
    assert env.app.app.test_client().get("/login").headers["Location"].endswith("/setup")


def test_unauthenticated_requests_redirect(env, logged_in):
    c = env.app.app.test_client()
    for path in ("/", "/api/containers", "/api/vitals", "/api/containers/x/logs"):
        assert c.get(path).status_code == 302, path
    assert c.put("/api/groups", json={}).status_code == 302


def test_login_throttles_after_repeated_failures(env, logged_in):
    c = env.app.app.test_client()
    codes = [post_login(c, "admin", "bad").status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_login_works_with_right_password(env, logged_in):
    c = env.app.app.test_client()
    assert post_login(c, "admin", "correct-horse").status_code == 302


# ---- container actions --------------------------------------------------


def test_action_needs_csrf(env, logged_in):
    c, _ = logged_in
    web = env.client.add(FakeContainer("web"))
    assert c.post("/api/containers/web/stop").status_code == 403
    assert web.calls == []


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_actions_reach_the_container(env, logged_in, action):
    c, csrf = logged_in
    web = env.client.add(FakeContainer("web"))
    r = c.post(f"/api/containers/web/{action}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert web.calls == [action]


def test_unknown_action_and_missing_container(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("web"))
    h = {"X-CSRF-Token": csrf}
    assert c.post("/api/containers/web/remove", headers=h).status_code == 400
    assert c.post("/api/containers/nope/start", headers=h).status_code == 404


def test_refuses_to_control_itself(env, logged_in):
    c, csrf = logged_in
    me = env.client.add(FakeContainer("dashboard", short_id="dashboardself"))
    r = c.post("/api/containers/dashboard/stop", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400
    assert me.calls == []


def test_read_only_blocks_actions(env, logged_in, monkeypatch):
    c, csrf = logged_in
    web = env.client.add(FakeContainer("web"))
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert c.post("/api/containers/web/stop", headers={"X-CSRF-Token": csrf}).status_code == 403
    assert web.calls == []


# ---- health check and version -------------------------------------------


def test_healthz_needs_no_sign_in_and_says_nothing_else(env):
    r = env.app.app.test_client().get("/healthz")
    assert r.status_code == 200 and r.get_json() == {"ok": True}


def test_healthz_works_before_the_first_account_exists(env):
    assert env.app.setup_token is not None
    assert env.app.app.test_client().get("/healthz").status_code == 200


def test_the_version_is_shown_to_signed_in_users(env, logged_in):
    c, _ = logged_in
    version = open(env.app.os.path.join(env.app.os.path.dirname(env.app.__file__), "VERSION")).read().strip()
    assert version and c.get("/api/containers").get_json()["version"] == version
    assert f"Ratatosk {version}." in c.get("/").get_data(as_text=True)


# ---- deleting a stopped container ---------------------------------------


def _delete(c, csrf, name, volumes):
    return c.delete(f"/api/containers/{name}", json={"volumes": volumes}, headers={"X-CSRF-Token": csrf})


@pytest.mark.parametrize("volumes", [False, True])
def test_delete_removes_a_stopped_container(env, logged_in, volumes):
    c, csrf = logged_in
    web = env.client.add(FakeContainer("web", status="exited"))
    assert _delete(c, csrf, "web", volumes).status_code == 200
    assert web.calls == [("remove", volumes)]


def test_delete_refuses_running_wings_self_and_bad_requests(env, logged_in):
    c, csrf = logged_in
    running = env.client.add(FakeContainer("web"))
    wings = env.client.add(FakeContainer("a" * 8 + "-1111-2222-3333-" + "b" * 12, status="exited", labels={"ContainerType": "server_process"}))
    me = env.client.add(FakeContainer("dashboard", status="exited", short_id="dashboardself"))
    assert _delete(c, csrf, "web", False).status_code == 409
    assert _delete(c, csrf, wings.name, True).status_code == 400
    assert _delete(c, csrf, "dashboard", False).status_code == 400
    assert _delete(c, csrf, "nope", False).status_code == 404
    assert c.delete("/api/containers/web", json={"volumes": "yes"}, headers={"X-CSRF-Token": csrf}).status_code == 400
    assert c.delete("/api/containers/web", json={"volumes": False}).status_code == 403
    assert running.calls == [] and wings.calls == [] and me.calls == []


def test_delete_is_admin_only_and_off_in_read_only(env, logged_in, monkeypatch):
    from test_users import hdr, make_user, sign_in

    c, csrf = logged_in
    web = env.client.add(FakeContainer("web", status="exited"))
    o, ocsrf = sign_in(env, "op", make_user(env, c, csrf))
    assert _delete(o, ocsrf, "web", False).status_code == 403
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert _delete(c, csrf, "web", False).status_code == 403
    assert web.calls == []


# ---- listing, grouping, stats -------------------------------------------


def test_list_includes_compose_project_and_stats(env, logged_in, monkeypatch):
    c, _ = logged_in
    env.client.add(
        FakeContainer(
            "sonarr",
            labels={"com.docker.compose.project": "media", "com.docker.compose.service": "sonarr"},
            ports={"8989/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8989"}]},
        )
    )
    env.client.add(FakeContainer("loose"))
    monkeypatch.setattr(env.app, "_stats", {"sonarr": {"cpu": 1.5, "mem": 1000}})
    body = c.get("/api/containers").get_json()
    by = {i["name"]: i for i in body["items"]}
    assert by["sonarr"]["project"] == "media"
    assert by["sonarr"]["service"] == "sonarr"
    assert by["sonarr"]["ports"] == ["8989"]
    assert by["sonarr"]["cpu"] == 1.5 and by["sonarr"]["mem"] == 1000
    assert by["loose"]["project"] is None and by["loose"]["cpu"] is None
    assert body["groups"] == {"assignments": {}, "custom_groups": [], "on_demand_groups": [], "critical_groups": [], "container_priority": {}, "link_containers": [], "aliases": {}, "group_order": [], "hidden_links": [], "link_groups": [], "group_by_project": False, "disks": [], "layout": {"columns": {}, "hidden": []}}


def test_compute_stats_cpu_and_memory(env):
    raw = {
        "cpu_stats": {"cpu_usage": {"total_usage": 300}, "system_cpu_usage": 2000, "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
        "memory_stats": {"usage": 5000, "stats": {"anon": 700, "shmem": 50, "file": 4000, "active_file": 900}},
    }
    # 200/1000 of the host's cpu time across 4 cpus = 80% of one core; memory is
    # anon + shmem only, so file cache (even active) never inflates it
    assert env.app.compute_stats(raw) == {"cpu": 80.0, "mem": 750}


def test_compute_stats_memory_on_cgroup_v1(env):
    raw = {"memory_stats": {"usage": 9000, "stats": {"rss": 1234, "cache": 7000}}}
    assert env.app.compute_stats(raw)["mem"] == 1234


def test_compute_stats_handles_missing_data(env):
    assert env.app.compute_stats({}) == {"cpu": 0.0, "mem": None}


# ---- logs ---------------------------------------------------------------


def test_logs_strip_ansi_and_split_lines(env, logged_in):
    c, _ = logged_in
    env.client.add(FakeContainer("web", logs=b"2026-10-04T09:00:00Z \x1b[31mboom\x1b[0m\nsecond\n"))
    body = c.get("/api/containers/web/logs").get_json()
    assert body["lines"] == ["2026-10-04T09:00:00Z boom", "second"]


@pytest.mark.parametrize("asked,expected", [(None, 200), ("50", 50), ("999999", 1000), ("0", 1), ("-5", 1)])
def test_logs_tail_is_clamped(env, logged_in, asked, expected):
    c, _ = logged_in
    web = env.client.add(FakeContainer("web"))
    c.get("/api/containers/web/logs" + (f"?tail={asked}" if asked else ""))
    assert web.last_tail == expected


def test_logs_unknown_container_is_404(env, logged_in):
    c, _ = logged_in
    assert c.get("/api/containers/nope/logs").status_code == 404


def test_logs_available_in_read_only(env, logged_in, monkeypatch):
    c, _ = logged_in
    env.client.add(FakeContainer("web", logs=b"hi\n"))
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert c.get("/api/containers/web/logs").status_code == 200


# ---- group settings -----------------------------------------------------


def test_groups_save_and_reload(env, logged_in):
    c, csrf = logged_in
    settings = {"assignments": {"web": " Frontends "}, "custom_groups": ["Frontends", "Frontends", "Spare"]}
    r = c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    # names are trimmed and duplicates dropped
    expected = {"assignments": {"web": "Frontends"}, "custom_groups": ["Frontends", "Spare"], "on_demand_groups": [], "critical_groups": [], "container_priority": {}, "link_containers": [], "aliases": {}, "group_order": [], "hidden_links": [], "link_groups": [], "group_by_project": True, "disks": [], "layout": {"columns": {}, "hidden": []}}
    assert c.get("/api/containers").get_json()["groups"] == expected


def test_on_demand_groups_save_and_reload(env, logged_in):
    c, csrf = logged_in
    settings = {
        "assignments": {},
        "custom_groups": ["Game servers"],
        "on_demand_groups": [" Game servers ", "Game servers", "media"],
    }
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.get("/api/containers").get_json()["groups"]["on_demand_groups"] == ["Game servers", "media"]


def test_groups_file_from_before_on_demand_setting_still_loads(env, logged_in):
    import json

    c, _ = logged_in
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {"web": "A"}, "custom_groups": ["A"]}, f)
    groups = c.get("/api/containers").get_json()["groups"]
    assert groups == {"assignments": {"web": "A"}, "custom_groups": ["A"], "on_demand_groups": [], "critical_groups": [], "container_priority": {}, "link_containers": [], "aliases": {}, "group_order": [], "hidden_links": [], "link_groups": [], "group_by_project": True, "disks": [], "layout": {"columns": {}, "hidden": []}}


def test_groups_need_csrf(env, logged_in):
    c, _ = logged_in
    assert c.put("/api/groups", json={"assignments": {}, "custom_groups": []}).status_code == 403


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"assignments": [], "custom_groups": []},
        {"assignments": {}, "custom_groups": {}},
        {"assignments": {"web": ""}, "custom_groups": []},
        {"assignments": {"web": 5}, "custom_groups": []},
        {"assignments": {"web": "x" * 65}, "custom_groups": []},
        {"assignments": {}, "custom_groups": ["   "]},
        {"assignments": {}, "custom_groups": [], "on_demand_groups": "media"},
        {"assignments": {}, "custom_groups": [], "on_demand_groups": [""]},
        {"assignments": {}, "custom_groups": [], "on_demand_groups": [5]},
        {"assignments": {}, "custom_groups": [], "group_order": "media"},
        {"assignments": {}, "custom_groups": [], "group_order": [""]},
        {"assignments": {}, "custom_groups": [], "group_order": [5]},
        {"assignments": {}, "custom_groups": [], "group_order": [f"g{i}" for i in range(201)]},
        {"assignments": {}, "custom_groups": [], "aliases": []},
        {"assignments": {}, "custom_groups": [], "aliases": {"web": ""}},
        {"assignments": {}, "custom_groups": [], "aliases": {"web": 5}},
        {"assignments": {}, "custom_groups": [], "aliases": {"web": "x" * 65}},
        {"assignments": {}, "custom_groups": [], "aliases": {f"c{i}": "n" for i in range(501)}},
        {"assignments": {}, "custom_groups": [], "on_demand_groups": [f"g{i}" for i in range(101)]},
        {"assignments": {}, "custom_groups": [f"g{i}" for i in range(101)]},
        {"assignments": {}, "custom_groups": [], "hidden_links": "web"},
        {"assignments": {}, "custom_groups": [], "hidden_links": [""]},
        {"assignments": {}, "custom_groups": [], "hidden_links": [5]},
        {"assignments": {}, "custom_groups": [], "link_groups": "Games"},
        {"assignments": {}, "custom_groups": [], "link_groups": [""]},
        {"assignments": {}, "custom_groups": [], "link_groups": [f"g{i}" for i in range(101)]},
        {"assignments": {}, "custom_groups": [], "hidden_links": [f"c{i}" for i in range(501)]},
        {"assignments": {}, "custom_groups": [], "layout": []},
        {"assignments": {}, "custom_groups": [], "layout": {"columns": []}},
        {"assignments": {}, "custom_groups": [], "layout": {"columns": {"6": [[], [], [], [], [], []]}}},
        {"assignments": {}, "custom_groups": [], "layout": {"columns": {"4": [[], [], []]}}},
        {"assignments": {}, "custom_groups": [], "layout": {"columns": {"2": [["host"]]}}},
        {"assignments": {}, "custom_groups": [], "layout": {"columns": {"1": ["host"]}}},
        {"assignments": {}, "custom_groups": [], "layout": {"columns": {"1": [[5]]}}},
        {"assignments": {}, "custom_groups": [], "layout": {"columns": {"1": [[""]]}}},
        {"assignments": {}, "custom_groups": [], "layout": {"columns": {"1": [["x" * 301]]}}},
        {"assignments": {}, "custom_groups": [], "layout": {"columns": {"1": [[f"p{i}" for i in range(201)]]}}},
        {"assignments": {}, "custom_groups": [], "layout": {"hidden": "links"}},
        {"assignments": {}, "custom_groups": [], "layout": {"hidden": [5]}},
        {"assignments": {}, "custom_groups": [], "layout": {"hidden": [f"p{i}" for i in range(201)]}},
    ],
)
def test_groups_reject_malformed_settings(env, logged_in, bad):
    c, csrf = logged_in
    r = c.put("/api/groups", json=bad, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400


def test_groups_editable_in_read_only(env, logged_in, monkeypatch):
    c, csrf = logged_in
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    r = c.put("/api/groups", json={"assignments": {}, "custom_groups": ["A"]}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200


# ---- vitals -------------------------------------------------------------


def put_disks(c, csrf, disks):
    r = c.get("/api/containers").get_json()["groups"]
    r["disks"] = disks
    return c.put("/api/groups", json=r, headers={"X-CSRF-Token": csrf})


def test_vitals_shape(env, logged_in, monkeypatch):
    c, _ = logged_in
    monkeypatch.setattr(env.app, "DEFAULT_DISKS", [{"label": "Storage", "path": env.data}])
    body = c.get("/api/vitals").get_json()
    assert set(body) == {"cpu_percent", "cpu_count", "mem_used", "mem_total", "disks"}
    assert 0 < body["mem_used"] <= body["mem_total"]
    [disk] = body["disks"]
    assert set(disk) == {"label", "path", "used", "total", "free", "percent"} and disk["label"] == "Storage"
    assert 0 < disk["used"] <= disk["total"]


def test_configured_disks_replace_the_default_and_keep_their_order(env, logged_in, monkeypatch, tmp_path):
    c, csrf = logged_in
    seen = []
    real = env.app.psutil.disk_usage
    monkeypatch.setattr(env.app.psutil, "disk_usage", lambda path: seen.append(path) or real(path))
    other = tmp_path / "home"
    other.mkdir()
    assert put_disks(c, csrf, [{"label": " Home ", "path": str(other)}, {"label": "Root", "path": env.data}]).status_code == 200
    disks = c.get("/api/vitals").get_json()["disks"]
    assert [(d["label"], d["path"]) for d in disks] == [("Home", str(other)), ("Root", env.data)]
    assert seen == [str(other), env.data]  # the default /data was not measured as well


def test_removing_every_disk_goes_back_to_the_default(env, logged_in, monkeypatch):
    c, csrf = logged_in
    monkeypatch.setattr(env.app, "DEFAULT_DISKS", [{"label": "Storage", "path": env.data}])
    put_disks(c, csrf, [{"label": "Root", "path": env.data}])
    put_disks(c, csrf, [])
    assert [d["label"] for d in c.get("/api/vitals").get_json()["disks"]] == ["Storage"]


def test_a_missing_disk_path_leaves_the_rest_of_the_vitals_working(env, logged_in):
    c, csrf = logged_in
    put_disks(c, csrf, [{"label": "Gone", "path": "/no/such/place"}, {"label": "Here", "path": env.data}])
    body = c.get("/api/vitals").get_json()
    assert body["disks"][0] == {"label": "Gone", "path": "/no/such/place", "error": "not available"}
    assert body["disks"][1]["label"] == "Here" and body["mem_total"] > 0


@pytest.mark.parametrize("disks", [
    "x", [1], [{"label": "A"}], [{"path": "/x"}], [{"label": "", "path": "/x"}], [{"label": "  ", "path": "/x"}],
    [{"label": "A", "path": "relative"}], [{"label": "A", "path": "/x\x00y"}], [{"label": "A" * 33, "path": "/x"}],
    [{"label": "A", "path": "/" + "x" * 200}], [{"label": "A", "path": 5}],
    [{"label": "A", "path": "/x"}, {"label": "B", "path": "/x"}],  # the same path twice
    [{"label": str(i), "path": f"/p{i}"} for i in range(9)],
])
def test_bad_disk_lists_are_refused(env, logged_in, disks):
    c, csrf = logged_in
    assert put_disks(c, csrf, disks).status_code == 400
    assert c.get("/api/containers").get_json()["groups"]["disks"] == []


def test_disks_need_csrf_and_are_allowed_in_read_only_mode(env, logged_in, monkeypatch):
    c, csrf = logged_in
    groups = c.get("/api/containers").get_json()["groups"]
    groups["disks"] = [{"label": "Root", "path": "/"}]
    assert c.put("/api/groups", json=groups).status_code == 403
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert c.put("/api/groups", json=groups, headers={"X-CSRF-Token": csrf}).status_code == 200  # display-only, like the layout


def test_a_groups_file_from_before_disks_still_loads(env, logged_in):
    c, _ = logged_in
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": []}, f)
    assert c.get("/api/containers").get_json()["groups"]["disks"] == []


def test_disk_picker_lists_one_real_directory_per_device(env, logged_in, monkeypatch, tmp_path):
    c, _ = logged_in
    folder = tmp_path / "home"
    folder.mkdir()
    afile = tmp_path / "hosts"
    afile.write_text("x")
    P = lambda dev, mp, fs="ext4": type("P", (), {"device": dev, "mountpoint": mp, "fstype": fs})()
    parts = [
        P("/dev/md1", "/data"), P("/dev/md1", str(afile)), P("/dev/md1", str(tmp_path)),
        P("/dev/md3", str(folder)), P("proc", "/proc/x", "proc"), P("/dev/sdz", "/dev/shm"),
    ]
    monkeypatch.setattr(env.app.psutil, "disk_partitions", lambda all=False: parts)
    real = env.app.psutil.disk_usage
    monkeypatch.setattr(env.app.psutil, "disk_usage", lambda p: real(env.data))
    monkeypatch.setattr(env.app.os.path, "isdir", lambda p: p in ("/data", str(tmp_path), str(folder)))
    items = c.get("/api/disks").get_json()["items"]
    assert [(i["device"], i["path"]) for i in items] == [("/dev/md1", "/data"), ("/dev/md3", str(folder))]
    assert set(items[0]) == {"path", "device", "fstype", "used", "total", "free", "percent"}


def test_disk_picker_needs_sign_in(env):
    assert env.app.app.test_client().get("/api/disks").status_code == 302


# ---- container display names --------------------------------------------


def test_aliases_save_and_reload_without_touching_the_container(env, logged_in):
    c, csrf = logged_in
    web = env.client.add(FakeContainer("ead7703d-f2d0-4c11-8064-c988a3f5ca94"))
    settings = {
        "assignments": {},
        "custom_groups": [],
        "aliases": {"ead7703d-f2d0-4c11-8064-c988a3f5ca94": "  Valheim  "},
    }
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    body = c.get("/api/containers").get_json()
    assert body["groups"]["aliases"] == {"ead7703d-f2d0-4c11-8064-c988a3f5ca94": "Valheim"}
    # display-only: the real name is still what the API lists and what actions use
    assert [i["name"] for i in body["items"]] == ["ead7703d-f2d0-4c11-8064-c988a3f5ca94"]
    assert web.name == "ead7703d-f2d0-4c11-8064-c988a3f5ca94"


def test_groups_file_from_before_aliases_still_loads(env, logged_in):
    import json

    c, _ = logged_in
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": [], "on_demand_groups": ["A"]}, f)
    assert c.get("/api/containers").get_json()["groups"]["aliases"] == {}


# ---- group order --------------------------------------------------------


def test_group_order_saves_trimmed_and_deduplicated(env, logged_in):
    c, csrf = logged_in
    settings = {"assignments": {}, "custom_groups": [], "group_order": [" pelican ", "media", "pelican"]}
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.get("/api/containers").get_json()["groups"]["group_order"] == ["pelican", "media"]


def test_groups_file_from_before_group_order_still_loads(env, logged_in):
    import json

    c, _ = logged_in
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": ["A"]}, f)
    assert c.get("/api/containers").get_json()["groups"]["group_order"] == []


# ---- link tiles ---------------------------------------------------------


def tcp(port, host_ip="0.0.0.0"):
    return {f"{port}/tcp": [{"HostIp": host_ip, "HostPort": str(port)}]}


def link_of(env, c, **kw):
    env.client.add(FakeContainer("app", **kw))
    items = c.get("/api/containers").get_json()["items"]
    return items[0]["link"]


def test_link_uses_the_published_port(env, logged_in):
    c, _ = logged_in
    assert link_of(env, c, ports=tcp(8989)) == {"url": None, "port": "8989", "https": False}


def test_link_prefers_a_web_port_over_a_lower_one(env, logged_in):
    c, _ = logged_in
    ports = {**tcp(22), **tcp(8080)}
    assert link_of(env, c, ports=ports)["port"] == "8080"


def test_link_falls_back_to_the_lowest_tcp_port(env, logged_in):
    c, _ = logged_in
    ports = {**tcp(9999), **tcp(7777)}
    assert link_of(env, c, ports=ports)["port"] == "7777"


def test_link_is_https_for_tls_ports(env, logged_in):
    c, _ = logged_in
    assert link_of(env, c, ports=tcp(443))["https"] is True


def test_no_link_for_udp_loopback_or_unpublished_ports(env, logged_in):
    c, _ = logged_in
    udp = {"27015/udp": [{"HostIp": "0.0.0.0", "HostPort": "27015"}]}
    assert link_of(env, c, ports=udp) is None
    env.client.by_name.clear()
    assert link_of(env, c, ports=tcp(8080, host_ip="127.0.0.1")) is None
    env.client.by_name.clear()
    assert link_of(env, c) is None


def test_dashboard_url_label_overrides_the_port(env, logged_in):
    c, _ = logged_in
    labels = {"dashboard.url": "https://media.example.com/"}
    link = link_of(env, c, labels=labels, ports=tcp(8080))
    assert link == {"url": "https://media.example.com/", "port": None, "https": False}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "ftp://host/", "not a url", "http://"])
def test_dashboard_url_label_must_be_http_or_https(env, logged_in, url):
    c, _ = logged_in
    # an unusable URL is ignored and the published port is used instead
    link = link_of(env, c, labels={"dashboard.url": url}, ports=tcp(8080))
    assert link["url"] is None and link["port"] == "8080"


def test_dashboard_hide_label_removes_the_link(env, logged_in):
    c, _ = logged_in
    assert link_of(env, c, labels={"dashboard.hide": "true"}, ports=tcp(8080)) is None


def test_ratatosk_labels_work_and_win_over_the_older_prefix(env, logged_in):
    c, _ = logged_in
    both = {"ratatosk.url": "https://new.example.com/", "dashboard.url": "https://old.example.com/"}
    assert link_of(env, c, labels=both, ports=tcp(8080))["url"] == "https://new.example.com/"
    assert link_of(env, c, labels={"ratatosk.hide": "true"}, ports=tcp(8080)) is None
    env.client.add(FakeContainer("abc123", labels={"ratatosk.name": "New", "dashboard.name": "Old"}))
    by = {i["name"]: i for i in c.get("/api/containers").get_json()["items"]}
    assert by["abc123"]["label_name"] == "New"


def test_dashboard_name_label_is_listed(env, logged_in):
    c, _ = logged_in
    env.client.add(FakeContainer("abc123", labels={"dashboard.name": "Sonarr"}))
    env.client.add(FakeContainer("plain"))
    by = {i["name"]: i for i in c.get("/api/containers").get_json()["items"]}
    assert by["abc123"]["label_name"] == "Sonarr" and by["plain"]["label_name"] is None


def test_hidden_links_save_and_reload(env, logged_in):
    c, csrf = logged_in
    settings = {"assignments": {}, "custom_groups": [], "hidden_links": ["web", "db", "web"]}
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.get("/api/containers").get_json()["groups"]["hidden_links"] == ["web", "db"]


def test_groups_file_from_before_hidden_links_still_loads(env, logged_in):
    import json

    c, _ = logged_in
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": ["A"]}, f)
    assert c.get("/api/containers").get_json()["groups"]["hidden_links"] == []


def test_link_groups_save_trimmed_and_reload(env, logged_in):
    c, csrf = logged_in
    settings = {"assignments": {}, "custom_groups": [], "link_groups": [" Games ", "Games"]}
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.get("/api/containers").get_json()["groups"]["link_groups"] == ["Games"]


def test_groups_file_from_before_link_groups_still_loads(env, logged_in):
    import json

    c, _ = logged_in
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": ["A"], "hidden_links": ["web"]}, f)
    assert c.get("/api/containers").get_json()["groups"]["link_groups"] == []


# ---- container updates ----------------------------------------------------

COMPOSE = {
    "com.docker.compose.project": "media",
    "com.docker.compose.service": "web",
    "com.docker.compose.project.config_files": "/srv/media/compose.yml",
    "com.docker.compose.project.working_dir": "/srv/media",
}


def add_web(env, local="sha256:old", latest="sha256:new", labels=COMPOSE, **kw):
    env.client.add(FakeContainer("web", labels=labels, image_ref="app:1", repo_digests=[f"app@{local}"], **kw))
    env.client.registry["app:1"] = latest
    env.app._registry.clear()
    env.app.check_registry()


def update_of(c, name="web"):
    return {i["name"]: i for i in c.get("/api/containers").get_json()["items"]}[name]["update"]


def test_newer_registry_digest_shows_an_update(env, logged_in):
    c, _ = logged_in
    add_web(env)
    assert update_of(c)["available"] is True


def test_same_digest_shows_no_update(env, logged_in):
    c, _ = logged_in
    add_web(env, latest="sha256:old")
    assert update_of(c) is None


def test_non_compose_and_unpulled_images_show_no_update(env, logged_in):
    c, _ = logged_in
    add_web(env, labels={})
    assert update_of(c) is None
    env.client.by_name.clear()
    add_web(env, local="")  # built locally: "app@" has no digest
    env.client.by_name["web"].image.attrs["RepoDigests"] = []
    assert update_of(c) is None


def test_registry_error_keeps_the_last_answer(env, logged_in):
    c, _ = logged_in
    add_web(env)
    env.client.registry.clear()
    env.app.check_registry()
    assert update_of(c)["available"] is True


def test_update_check_off_hides_updates(env, logged_in, monkeypatch):
    c, _ = logged_in
    add_web(env)
    monkeypatch.setattr(env.app, "UPDATE_CHECK", False)
    assert update_of(c) is None


def test_update_button_blocked_when_compose_file_not_visible(env, logged_in, monkeypatch):
    c, _ = logged_in
    add_web(env)
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    assert "compose file not visible" in update_of(c)["blocked"]


def run_threads_inline(env, monkeypatch, run=True):
    """The app's own background threads run on the spot (or not at all). Only the app module's name
    for threading is replaced, so the thread pool the registry check uses still works."""
    thread = lambda target, args, daemon: SimpleNamespace(start=(lambda: target(*args)) if run else (lambda: None))
    monkeypatch.setattr(env.app, "threading", SimpleNamespace(Thread=thread))


def test_update_runs_pull_then_up(env, logged_in, monkeypatch):
    c, csrf = logged_in
    add_web(env)
    monkeypatch.setattr(env.app.os.path, "exists", lambda p: True)
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env.app.subprocess, "run", fake_run)
    run_threads_inline(env, monkeypatch)
    r = c.post("/api/containers/web/update", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 202
    assert [x[-2:] for x in calls] == [["pull", "web"], ["--no-deps", "web"]]
    assert calls[1][-4] == "up"
    assert calls[0][:6] == ["docker", "compose", "-p", "media", "--project-directory", "/srv/media"]
    assert "-f" in calls[0] and "/srv/media/compose.yml" in calls[0]
    assert "web" not in env.app._jobs


def test_failed_update_is_reported_on_the_row(env, logged_in, monkeypatch):
    c, csrf = logged_in
    add_web(env)
    monkeypatch.setattr(env.app.os.path, "exists", lambda p: True)
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    monkeypatch.setattr(
        env.app.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom\npull denied")
    )
    run_threads_inline(env, monkeypatch)
    assert c.post("/api/containers/web/update", headers={"X-CSRF-Token": csrf}).status_code == 202
    u = update_of(c)
    assert u["state"] == "error" and u["error"] == "pull denied"


def test_update_refusals(env, logged_in, monkeypatch):
    c, csrf = logged_in
    add_web(env)
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    h = {"X-CSRF-Token": csrf}
    assert c.post("/api/containers/web/update").status_code == 403  # no csrf
    assert c.post("/api/containers/web/update", headers=h).status_code == 400  # compose file missing
    assert c.post("/api/containers/nope/update", headers=h).status_code == 404
    env.client.add(FakeContainer("plain"))
    assert c.post("/api/containers/plain/update", headers=h).status_code == 400  # nothing to update
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert c.post("/api/containers/web/update", headers=h).status_code == 403


def test_second_update_while_running_is_refused(env, logged_in, monkeypatch):
    c, csrf = logged_in
    add_web(env)
    monkeypatch.setattr(env.app.os.path, "exists", lambda p: True)
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    run_threads_inline(env, monkeypatch, run=False)
    h = {"X-CSRF-Token": csrf}
    assert c.post("/api/containers/web/update", headers=h).status_code == 202
    assert c.post("/api/containers/web/update", headers=h).status_code == 409


# ---- panel layout -------------------------------------------------------


def test_layout_saves_and_reloads(env, logged_in):
    c, csrf = logged_in
    layout = {
        "columns": {"3": [["host", "int:sonarr"], ["group:BE"], ["links"]], "1": [["host", "group:BE"]]},
        "hidden": ["links"],
    }
    settings = {"assignments": {}, "custom_groups": [], "layout": layout}
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.get("/api/containers").get_json()["groups"]["layout"] == layout


def test_layout_saves_four_and_five_columns(env, logged_in):
    c, csrf = logged_in
    layout = {
        "columns": {"4": [["host"], ["links"], [], ["group:BE"]], "5": [["host"], [], [], [], ["links"]]},
        "hidden": [],
    }
    settings = {"assignments": {}, "custom_groups": [], "layout": layout}
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.get("/api/containers").get_json()["groups"]["layout"] == layout


def test_layout_drops_a_repeated_panel_and_duplicate_hidden(env, logged_in):
    c, csrf = logged_in
    layout = {"columns": {"2": [["host", "links"], ["links", "group:BE", "host"]]}, "hidden": ["links", "links"]}
    settings = {"assignments": {}, "custom_groups": [], "layout": layout}
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    saved = c.get("/api/containers").get_json()["groups"]["layout"]
    assert saved == {"columns": {"2": [["host", "links"], ["group:BE"]]}, "hidden": ["links"]}


def test_groups_file_from_before_layout_still_loads(env, logged_in):
    import json

    c, _ = logged_in
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": ["A"], "group_order": ["A"]}, f)
    groups = c.get("/api/containers").get_json()["groups"]
    assert groups["layout"] == {"columns": {}, "hidden": []}
    assert groups["group_order"] == ["A"]


def test_layout_is_optional_when_saving(env, logged_in):
    c, csrf = logged_in
    r = c.put("/api/groups", json={"assignments": {}, "custom_groups": []}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.get_json()["groups"]["layout"] == {"columns": {}, "hidden": []}


def test_group_by_project_starts_off_on_a_new_account_and_on_for_old_files(env, logged_in):
    import json

    c, _ = logged_in
    assert c.get("/api/containers").get_json()["groups"]["group_by_project"] is False
    # a file saved before the setting existed keeps the old behaviour
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": []}, f)
    assert c.get("/api/containers").get_json()["groups"]["group_by_project"] is True
    # a damaged value is treated as on, not as off
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": [], "group_by_project": "no"}, f)
    assert c.get("/api/containers").get_json()["groups"]["group_by_project"] is True


def test_group_by_project_off_saves_and_reloads(env, logged_in):
    c, csrf = logged_in
    settings = {"assignments": {}, "custom_groups": [], "group_by_project": False}
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.get("/api/containers").get_json()["groups"]["group_by_project"] is False
    settings["group_by_project"] = True
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.get("/api/containers").get_json()["groups"]["group_by_project"] is True


@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_group_by_project_must_be_a_boolean(env, logged_in, value):
    c, csrf = logged_in
    settings = {"assignments": {}, "custom_groups": [], "group_by_project": value}
    assert c.put("/api/groups", json=settings, headers={"X-CSRF-Token": csrf}).status_code == 400


def test_creating_the_admin_opens_the_page_on_the_searching_screen(env):
    c = env.app.app.test_client()
    r = c.post("/setup", data={"token": env.app.setup_token, "username": "admin", "password": "correct-horse", "csrf_token": form_csrf(c, "/setup")})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/?welcome=1")
    shown = c.get("/?welcome=1").get_data(as_text=True)
    tag = re.search(r'<div class="welcome" id="welcome"[^>]*>', shown).group(0)
    assert " hidden" not in tag
    assert "Searching for containers" in shown


def test_the_searching_screen_is_hidden_on_a_normal_visit(env, logged_in):
    c, _ = logged_in
    page = c.get("/").get_data(as_text=True)
    tag = re.search(r'<div class="welcome" id="welcome"[^>]*>', page).group(0)
    assert " hidden" in tag
    # anything but welcome=1 leaves it hidden
    tag = re.search(r'<div class="welcome" id="welcome"[^>]*>', c.get("/?welcome=0").get_data(as_text=True)).group(0)
    assert " hidden" in tag


# ---- every route needs a sign-in ----------------------------------------------

OPEN_ROUTES = {"static", "healthz", "login", "setup"}


def test_every_route_but_the_open_ones_redirects_to_sign_in(env, logged_in):
    """A route added later without @login_required fails here."""
    c = env.app.app.test_client()
    checked = 0
    for rule in env.app.app.url_map.iter_rules():
        if rule.endpoint in OPEN_ROUTES:
            continue
        path = re.sub(r"<[^>]+>", "x", rule.rule)
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            r = c.open(path, method=method, json={} if method in ("POST", "PUT") else None)
            assert r.status_code == 302 and r.headers["Location"].endswith("/login"), (method, rule.rule, r.status_code)
            checked += 1
    assert checked > 20
    assert {r.endpoint for r in env.app.app.url_map.iter_rules()} >= OPEN_ROUTES


# ---- environment settings -------------------------------------------------------


@pytest.fixture
def polluted_environment(monkeypatch):
    for name in ("BEHIND_PROXY", "READ_ONLY"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("UPDATE_CHECK", "false")
    monkeypatch.setenv("PROXY_IP_HEADER", "cf")


def test_the_test_app_ignores_the_developers_own_environment(polluted_environment, env, tmp_path):
    assert env.app.BEHIND_PROXY is False and env.app.READ_ONLY is False
    assert env.app.UPDATE_CHECK is True and env.app.PROXY_IP_HEADER == "xff"
    assert env.app.DATA_DIR == str(tmp_path)


def reload_app(monkeypatch, **variables):
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    sys.modules.pop("app", None)
    return importlib.import_module("app")


@pytest.mark.parametrize("value,expected", [("cf", "cf"), ("XFF", "xff"), (" cf ", "cf")])
def test_proxy_ip_header_setting(env, monkeypatch, value, expected):
    assert reload_app(monkeypatch, PROXY_IP_HEADER=value).PROXY_IP_HEADER == expected


def test_an_unknown_proxy_ip_header_falls_back_to_xff_with_a_warning(env, monkeypatch):
    assert reload_app(monkeypatch, PROXY_IP_HEADER="x-real-ip").PROXY_IP_HEADER == "xff"
    assert any("PROXY_IP_HEADER" in e["msg"] and e["level"] == "warn" for e in activity.snapshot()[0])


def test_env_flags_are_read_at_import(env, monkeypatch):
    module = reload_app(monkeypatch, BEHIND_PROXY="yes", READ_ONLY="1", UPDATE_CHECK="off")
    assert module.BEHIND_PROXY is True and module.READ_ONLY is True and module.UPDATE_CHECK is False


# ---- logout -----------------------------------------------------------------------


def test_logout_is_post_only(env, logged_in):
    c, _ = logged_in
    assert c.get("/logout").status_code == 405
    assert c.get("/api/containers").status_code == 200  # still signed in


def test_logout_needs_the_csrf_token(env, logged_in):
    c, _ = logged_in
    assert c.post("/logout").status_code == 403
    assert c.post("/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert c.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert c.get("/api/containers").status_code == 200


def test_logout_with_the_header_or_the_form_field_signs_out(env, logged_in):
    c, csrf = logged_in
    r = c.post("/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert c.get("/api/containers").status_code == 302
    c2 = env.app.app.test_client()
    post_login(c2, "admin", "correct-horse")
    with c2.session_transaction() as s:
        token = s["csrf"]
    assert c2.post("/logout", data={"csrf_token": token}).status_code == 302
    assert c2.get("/api/containers").status_code == 302


def test_logout_when_already_signed_out_lands_on_the_login_page(env, logged_in):
    r = env.app.app.test_client().post("/logout")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")


# ---- login and setup carry a token before there is a session ---------------------


def test_the_login_and_setup_pages_carry_a_token(env):
    c = env.app.app.test_client()
    page = c.get("/setup").get_data(as_text=True)
    with c.session_transaction() as s:
        assert re.search(r'name="csrf_token" value="%s"' % s["csrf_pre"], page)
    env.app.setup_token = None
    env.app.save_users({"a": {"hash": env.app.generate_password_hash("long-enough"), "role": "admin"}})
    page = c.get("/login").get_data(as_text=True)
    with c.session_transaction() as s:
        assert re.search(r'name="csrf_token" value="%s"' % s["csrf_pre"], page)


@pytest.mark.parametrize("token", [None, "", "guess", "\u00e9"])
def test_login_without_the_right_token_is_refused_and_not_counted(env, logged_in, token):
    c = env.app.app.test_client()
    c.get("/login")
    data = {"username": "admin", "password": "correct-horse"}
    if token is not None:
        data["csrf_token"] = token
    assert c.post("/login", data=data).status_code == 403
    assert c.get("/api/containers").status_code == 302
    assert env.app._failures == {}


def test_a_token_from_another_session_is_no_good(env, logged_in):
    other = env.app.app.test_client()
    token = form_csrf(other)
    c = env.app.app.test_client()
    c.get("/login")
    assert c.post("/login", data={"username": "admin", "password": "correct-horse", "csrf_token": token}).status_code == 403


@pytest.mark.parametrize("token", [None, "guess"])
def test_setup_without_the_right_token_creates_nothing(env, token):
    c = env.app.app.test_client()
    c.get("/setup")
    data = {"token": env.app.setup_token, "username": "admin", "password": "correct-horse"}
    if token is not None:
        data["csrf_token"] = token
    assert c.post("/setup", data=data).status_code == 403
    assert env.app.setup_token is not None and not os.path.exists(env.app.USERS_PATH)


def test_the_form_token_is_replaced_by_the_session_token_after_sign_in(env, logged_in):
    c, _ = logged_in
    with c.session_transaction() as s:
        assert "csrf_pre" not in s and s["csrf"]


# ---- /setup input rules --------------------------------------------------------------


@pytest.mark.parametrize("username", ["a/b", "", "x" * 33, "white space", "\u00e9"])
def test_setup_applies_the_username_rules(env, username):
    c = env.app.app.test_client()
    r = c.post("/setup", data={"token": env.app.setup_token, "username": username, "password": "correct-horse", "csrf_token": form_csrf(c, "/setup")})
    assert r.status_code == 200 and b"Username:" in r.data
    assert env.app.setup_token is not None


def test_setup_refuses_a_password_over_256_characters(env):
    c = env.app.app.test_client()
    r = c.post("/setup", data={"token": env.app.setup_token, "username": "a", "password": "p" * 257, "csrf_token": form_csrf(c, "/setup")})
    assert r.status_code == 200 and b"256" in r.data and env.app.setup_token is not None


def test_non_ascii_setup_token_is_refused_not_a_500(env):
    c = env.app.app.test_client()
    r = c.post("/setup", data={"token": "t\u00f6ken", "username": "a", "password": "correct-horse", "csrf_token": form_csrf(c, "/setup")})
    assert r.status_code == 200 and b"Invalid setup token" in r.data


def test_non_ascii_csrf_header_is_a_403_not_a_500(env, logged_in):
    c, _ = logged_in
    env.client.add(FakeContainer("web"))
    r = c.post("/api/containers/web/stop", headers={"X-CSRF-Token": "t\u00f6ken".encode("latin-1")})
    assert r.status_code == 403


def test_safe_eq(env):
    eq = env.app._safe_eq
    assert eq("abc", "abc") and not eq("abc", "abd") and not eq("t\u00f6", "to")
    assert eq("t\u00f6", "t\u00f6")


# ---- sessions ----------------------------------------------------------------------------


def test_sessions_last_twelve_hours(env, logged_in):
    from datetime import timedelta

    assert env.app.SESSION_LIFETIME == timedelta(hours=12)
    assert env.app.app.permanent_session_lifetime == timedelta(hours=12)
    c = env.app.app.test_client()
    r = post_login(c, "admin", "correct-horse")
    cookie = r.headers["Set-Cookie"]
    assert "Expires=" in cookie and "HttpOnly" in cookie
    with c.session_transaction() as s:
        assert s.permanent


def test_an_expired_session_cookie_is_signed_out(env, logged_in, monkeypatch):
    from datetime import timedelta

    c, _ = logged_in
    assert c.get("/api/containers").status_code == 200
    monkeypatch.setitem(env.app.app.config, "PERMANENT_SESSION_LIFETIME", timedelta(seconds=-1))
    assert c.get("/api/containers").status_code == 302


def test_unknown_user_still_costs_a_password_hash_check(env, logged_in, monkeypatch):
    seen = []
    real = env.app.check_password_hash
    monkeypatch.setattr(env.app, "check_password_hash", lambda h, p: seen.append(h) or real(h, p))
    c = env.app.app.test_client()
    assert post_login(c, "nobody", "whatever-it-is").status_code == 401
    assert seen == [env.app._DUMMY_HASH]


def test_login_failures_for_a_quiet_address_are_forgotten(env, logged_in, monkeypatch):
    env.app.record_failure("9.9.9.9")
    assert "9.9.9.9" in env.app._failures
    monkeypatch.setattr(env.app.time, "time", lambda: 10**10)
    assert env.app.throttled("9.9.9.9") is False
    assert "9.9.9.9" not in env.app._failures


# ---- response headers ---------------------------------------------------------------------


CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; frame-ancestors 'none'"


@pytest.mark.parametrize("path", ["/", "/api/containers", "/api/vitals", "/api/debug"])
def test_signed_in_responses_carry_the_security_headers(env, logged_in, path):
    c, _ = logged_in
    r = c.get(path)
    assert r.status_code == 200
    assert r.headers["Content-Security-Policy"] == CSP
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"


def test_pages_and_redirects_carry_the_headers_too(env):
    c = env.app.app.test_client()
    for r in (c.get("/setup"), c.get("/login"), c.get("/nowhere")):
        assert r.headers["X-Frame-Options"] == "DENY" and r.headers["Content-Security-Policy"] == CSP
    assert c.get("/setup").headers["Cache-Control"] == "no-store"  # the form carries a token
    assert c.get("/api/containers").headers["Cache-Control"] == "no-store"  # also when it only redirects
    assert c.post("/api/containers/x/stop").headers["X-Content-Type-Options"] == "nosniff"


def test_static_files_get_the_headers_but_stay_cacheable(env):
    r = env.app.app.test_client().get("/static/app.js")
    assert r.status_code == 200 and r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Content-Security-Policy"] == CSP
    assert "no-store" not in r.headers.get("Cache-Control", "")


def test_a_header_set_by_a_view_is_not_overwritten(env, logged_in):
    c, _ = logged_in
    with env.app.app.test_request_context("/"):
        resp = env.app.app.make_response(("x", 200, {"X-Frame-Options": "SAMEORIGIN"}))
        assert env.app.security_headers(resp).headers["X-Frame-Options"] == "SAMEORIGIN"


def test_the_page_has_no_inline_script_or_style_that_the_policy_would_block(env, logged_in):
    c, _ = logged_in
    for path in ("/", "/login"):
        html = c.get(path).get_data(as_text=True)
        assert not re.search(r"<style|\sstyle=|\sonclick=|<script(?![^>]*\ssrc=)", html), path


# ---- health check --------------------------------------------------------------------------


def test_healthz_is_503_when_docker_cannot_be_reached(env, logged_in):
    env.client.ping_error = RuntimeError("no socket")
    r = env.app.app.test_client().get("/healthz")
    assert r.status_code == 503 and r.get_json() == {"ok": False}
    env.client.ping_error = None
    assert env.app.app.test_client().get("/healthz").status_code == 200


def test_healthz_is_503_when_ping_says_no(env, logged_in, monkeypatch):
    monkeypatch.setattr(env.client, "ping", lambda: False)
    assert env.app.app.test_client().get("/healthz").status_code == 503


def test_healthz_is_ok_before_setup_even_without_docker(env):
    env.client.ping_error = RuntimeError("no socket")
    assert env.app.setup_token is not None
    assert env.app.app.test_client().get("/healthz").status_code == 200


# ---- files and secrets ---------------------------------------------------------------------


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_secret_key_is_created_once_and_kept(env, monkeypatch):
    assert mode(env.app.SECRET_PATH) == 0o600
    first = env.app.app.secret_key
    assert len(first) == 64 and env.app.load_secret_key() == first
    assert reload_app(monkeypatch).app.secret_key == first  # a restart keeps the sessions valid


def test_data_files_are_private(env, logged_in):
    c, csrf = logged_in
    assert mode(env.app.USERS_PATH) == 0o600
    assert c.put("/api/groups", json=env.app.empty_groups(), headers={"X-CSRF-Token": csrf}).status_code == 200
    assert mode(env.app.groups_path("admin")) == 0o600
    env.app.write_json(env.app.GROUPS_PATH, {})
    assert mode(env.app.GROUPS_PATH) == 0o600
    leftovers = [n for d in (env.data, env.app.GROUPS_DIR) for n in os.listdir(d) if n.endswith(".tmp")]
    assert leftovers == []


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(env, logged_in):
    path = env.app.USERS_PATH
    before = open(path).read()
    with pytest.raises(TypeError):
        env.app.write_json(path, {"x": object()})
    assert open(path).read() == before
    assert [n for n in os.listdir(env.data) if n.endswith(".tmp")] == []


# ---- damaged data files --------------------------------------------------------------------


@pytest.mark.parametrize("content", ["{not json", "[]", '"text"', "null", '{"a": 5}', b"\xff\xfe".decode("latin-1")])
def test_a_damaged_users_file_is_an_error_never_an_empty_list(env, logged_in, content):
    with open(env.app.USERS_PATH, "w", encoding="latin-1") as f:
        f.write(content)
    with pytest.raises(env.app.DataFileError, match="users.json"):
        env.app.load_users()


def test_a_missing_users_file_is_still_no_users(env):
    assert env.app.load_users() == {}


def test_the_app_refuses_to_start_on_a_damaged_users_file(env, logged_in, monkeypatch):
    with open(env.app.USERS_PATH, "w") as f:
        f.write("{oops")
    with pytest.raises(Exception, match="users.json"):
        reload_app(monkeypatch)


def test_a_users_file_damaged_while_running_answers_503_and_logs_it(env, logged_in):
    c, _ = logged_in
    with open(env.app.USERS_PATH, "w") as f:
        f.write("[]")
    r = c.get("/api/containers")
    assert r.status_code == 503 and "error" in r.get_json()
    assert any("users.json" in e["msg"] and e["level"] == "error" for e in activity.snapshot()[0])
    assert env.app.app.test_client().get("/setup").status_code == 302  # /setup stays closed
    assert env.app.setup_token is None


@pytest.mark.parametrize("content", ["{oops", "[]", "5", '"x"'])
def test_a_damaged_groups_file_falls_back_to_defaults_with_one_warning(env, logged_in, content):
    c, _ = logged_in
    os.makedirs(env.app.GROUPS_DIR, exist_ok=True)
    with open(env.app.groups_path("admin"), "w") as f:
        f.write(content)
    for _ in range(3):
        r = c.get("/api/containers")
        assert r.status_code == 200 and r.get_json()["groups"] == env.app.empty_groups()
    warnings = [e for e in activity.snapshot()[0] if "was ignored" in e["msg"]]
    assert len(warnings) == 1 and warnings[0]["level"] == "warn"


def test_a_damaged_personal_groups_file_falls_through_to_the_shared_one(env, logged_in):
    os.makedirs(env.app.GROUPS_DIR, exist_ok=True)
    with open(env.app.groups_path("admin"), "w") as f:
        f.write("[1]")
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {"web": "Media"}, "custom_groups": ["Media"]}, f)
    assert env.app.load_groups("admin")["assignments"] == {"web": "Media"}


@pytest.mark.parametrize("content", ["[]", "5", "{oops", '"x"'])
def test_a_damaged_integrations_file_reads_as_empty(env, content):
    with open(env.app.INTEGRATIONS_PATH, "w") as f:
        f.write(content)
    assert env.app.load_integrations() == {"keys": {}, "dismissed": []}


# ---- updates and housekeeping --------------------------------------------------------------


def test_a_failed_update_is_forgotten_once_the_container_is_current(env, logged_in):
    c, _ = logged_in
    add_web(env)
    env.app._jobs["web"] = {"state": "error", "error": "pull denied"}
    assert update_of(c)["state"] == "error"  # still behind: the error stays
    env.client.registry["app:1"] = "sha256:old"
    env.app.check_registry()
    assert update_of(c) is None
    assert "web" not in env.app._jobs


def test_registry_failures_are_logged(env, logged_in):
    add_web(env)
    env.client.registry.clear()
    env.app.check_registry()
    assert any("registry check for app:1 failed" in e["msg"] for e in activity.snapshot()[0])


def test_the_own_container_check_is_one_function(env):
    own = FakeContainer("dash", short_id="dashboardself")
    assert env.app.is_self(own) and not env.app.is_self(FakeContainer("web"))
    assert env.app.describe(own)["self"] is True


# ---- "Check for updates now" and the checker's timing ---------------------


def check_now(c, csrf):
    return c.post("/api/updates/check", headers={"X-CSRF-Token": csrf})


@pytest.fixture
def no_cooldown(env, monkeypatch):
    monkeypatch.setattr(env.app, "CHECK_COOLDOWN", 0)


def test_the_check_covers_stopped_containers_too(env, logged_in, no_cooldown):
    c, csrf = logged_in
    add_web(env, status="exited")
    env.app._registry.clear()
    r = check_now(c, csrf)
    assert r.status_code == 200
    assert r.get_json() == {"error": None, "checked": 1, "updates": 1, "errors": []}
    assert update_of(c)["available"] is True


def test_the_check_reports_images_the_registry_would_not_answer_for(env, logged_in, no_cooldown):
    c, csrf = logged_in
    add_web(env)
    env.client.registry.clear()
    body = check_now(c, csrf).get_json()
    assert body["checked"] == 1 and body["updates"] == 1  # the earlier answer is kept
    assert [e["image"] for e in body["errors"]] == ["app:1"]
    assert "registry down" in body["errors"][0]["error"]


def test_the_check_skips_local_builds_and_this_container(env, logged_in, no_cooldown):
    c, csrf = logged_in
    env.client.add(FakeContainer("built", labels=COMPOSE, image_ref="built:1", repo_digests=[]))
    env.client.add(FakeContainer("dashboardself", labels=COMPOSE, image_ref="ratatosk:1", repo_digests=["ratatosk@sha256:a"], short_id="dashboardself"))
    body = check_now(c, csrf).get_json()
    assert body["checked"] == 0 and body["errors"] == []
    assert env.app._attempted == {"built:1"}  # a local build is noted so it never asks again; this container is ignored
    env.app._check_wake.clear()
    c.get("/api/containers")
    assert not env.app._check_wake.is_set()


def test_the_check_is_admin_only_and_needs_the_csrf_token(env, logged_in, no_cooldown):
    from test_users import make_user, sign_in

    c, csrf = logged_in
    assert c.post("/api/updates/check").status_code == 403
    make_user(env, c, csrf)
    o, ocsrf = sign_in(env, "op", "operator-pass")
    assert check_now(o, ocsrf).status_code == 403
    assert env.app.app.test_client().post("/api/updates/check").status_code in (302, 401)


def test_the_check_is_refused_when_updates_are_off_and_allowed_in_read_only(env, logged_in, no_cooldown, monkeypatch):
    c, csrf = logged_in
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert check_now(c, csrf).status_code == 200
    monkeypatch.setattr(env.app, "UPDATE_CHECK", False)
    assert check_now(c, csrf).status_code == 400


def test_the_check_has_a_cooldown_and_never_runs_twice_at_once(env, logged_in):
    c, csrf = logged_in
    assert check_now(c, csrf).status_code == 200
    assert check_now(c, csrf).status_code == 429
    env.app._last_manual = 0.0
    assert env.app._check_lock.acquire(blocking=False)
    try:
        assert check_now(c, csrf).status_code == 409
    finally:
        env.app._check_lock.release()


def test_settings_show_the_button_to_admins_when_checks_are_on(env, logged_in, monkeypatch):
    from test_users import make_user, sign_in

    c, csrf = logged_in
    assert b'id="check-updates"' in c.get("/").data
    make_user(env, c, csrf)
    o, _ = sign_in(env, "op", "operator-pass")
    assert b'id="check-updates"' not in o.get("/").data
    monkeypatch.setattr(env.app, "UPDATE_CHECK", False)
    assert b'id="check-updates"' not in c.get("/").data


def test_a_container_with_an_image_nobody_asked_about_wakes_the_checker(env, logged_in):
    c, _ = logged_in
    env.app._check_wake.clear()
    add_web(env)  # add_web runs a pass, so web's image is known
    env.app._check_wake.clear()
    c.get("/api/containers")
    assert not env.app._check_wake.is_set()
    env.client.add(FakeContainer("other", labels={**COMPOSE, "com.docker.compose.service": "other"}, image_ref="new:1", repo_digests=["new@sha256:a"]))
    c.get("/api/containers")
    assert env.app._check_wake.is_set()


def test_the_checker_retries_once_soon_after_a_failed_first_pass(env, monkeypatch):
    waits = []

    class Stop(Exception):
        pass

    def wait(delay):
        waits.append(delay)
        if len(waits) == 3:
            raise Stop

    results = iter([{"checked": 1, "updates": 0, "errors": [{"image": "a", "error": "down"}]}] * 3)
    monkeypatch.setattr(env.app, "check_registry", lambda: next(results))
    monkeypatch.setattr(env.app._check_wake, "wait", wait)
    with pytest.raises(Stop):
        env.app._update_checker()
    assert waits == [env.app.CHECK_RETRY, env.app.UPDATE_INTERVAL, env.app.UPDATE_INTERVAL]
