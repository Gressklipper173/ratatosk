import json
from types import SimpleNamespace

import pytest
from conftest import FakeContainer

# ---- setup and login ----------------------------------------------------


def test_setup_requires_token(env):
    c = env.app.app.test_client()
    r = c.post("/setup", data={"token": "wrong", "username": "a", "password": "longenough1"})
    assert r.status_code == 200
    assert b"Invalid setup token" in r.data
    assert env.app.load_users() == {}


def test_setup_rejects_short_password(env):
    c = env.app.app.test_client()
    r = c.post("/setup", data={"token": env.app.setup_token, "username": "a", "password": "short"})
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
    codes = [c.post("/login", data={"username": "admin", "password": "bad"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_login_works_with_right_password(env, logged_in):
    c = env.app.app.test_client()
    assert c.post("/login", data={"username": "admin", "password": "correct-horse"}).status_code == 302


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
    assert body["groups"] == {"assignments": {}, "custom_groups": [], "on_demand_groups": [], "critical_groups": [], "container_priority": {}, "link_containers": [], "aliases": {}, "group_order": [], "hidden_links": [], "link_groups": [], "disks": [], "layout": {"columns": {}, "hidden": []}}


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
    expected = {"assignments": {"web": "Frontends"}, "custom_groups": ["Frontends", "Spare"], "on_demand_groups": [], "critical_groups": [], "container_priority": {}, "link_containers": [], "aliases": {}, "group_order": [], "hidden_links": [], "link_groups": [], "disks": [], "layout": {"columns": {}, "hidden": []}}
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
    assert groups == {"assignments": {"web": "A"}, "custom_groups": ["A"], "on_demand_groups": [], "critical_groups": [], "container_priority": {}, "link_containers": [], "aliases": {}, "group_order": [], "hidden_links": [], "link_groups": [], "disks": [], "layout": {"columns": {}, "hidden": []}}


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
    monkeypatch.setattr(env.app.threading, "Thread", lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)))
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
    monkeypatch.setattr(env.app.threading, "Thread", lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)))
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
    monkeypatch.setattr(env.app.threading, "Thread", lambda target, args, daemon: SimpleNamespace(start=lambda: None))
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
