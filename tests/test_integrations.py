import json
import os

import pytest
import yaml
from conftest import FakeContainer

import integrations

CONFIG_XML = "<Config><ApiKey>goodkey123</ApiKey></Config>"
SONARR_LABELS = {}


def add_sonarr(env, name="sonarr", status="running", files=None, labels=None, image_ref="lscr.io/linuxserver/sonarr:latest"):
    if files is None:
        files = {"/config/config.xml": CONFIG_XML}
    return env.client.add(FakeContainer(name, status=status, image_ref=image_ref, files=files, labels=labels))


@pytest.fixture
def fake_app(monkeypatch):
    """Stands in for Sonarr's HTTP API: only 'goodkey123' is accepted."""
    seen = []

    def fake_get(url, header, key, timeout=5):
        seen.append((url, header, key))
        if key != "goodkey123":
            raise integrations.AppError("the app rejected the key")
        if "/system/status" in url:
            return {"appName": "Sonarr"}
        if "/wanted/missing" in url:
            return {
                "totalRecords": 7,
                "records": [{"title": "Pilot", "airDate": "2024-01-01", "series": {"title": "Some Show"}}],
            }
        raise integrations.AppError("the app answered HTTP 404")

    monkeypatch.setattr(integrations, "http_get_json", fake_get)
    return seen


def items(c):
    return c.get("/api/integrations").get_json()["items"]


def post(c, csrf, url, **kw):
    return c.post(url, headers={"X-CSRF-Token": csrf}, **kw)


# ---- matching ---------------------------------------------------------------


def test_sonarr_is_found_but_nothing_is_read_yet(env, logged_in, fake_app):
    c, _ = logged_in
    add_sonarr(env)
    [it] = items(c)
    assert it["state"] == "found" and it["adapter"] == "sonarr"
    assert it["key_file"] == "/config/config.xml"
    assert it["panels"] == []
    assert fake_app == []  # no call to the app without a key


def test_unrelated_container_is_not_listed(env, logged_in):
    c, _ = logged_in
    env.client.add(FakeContainer("web", image_ref="nginx:1"))
    assert items(c) == []


def test_label_picks_an_adapter_for_any_image(env, logged_in):
    c, _ = logged_in
    add_sonarr(env, image_ref="my-custom-build", labels={"dashboard.adapter": "sonarr"})
    assert items(c)[0]["adapter"] == "sonarr"


def test_label_none_switches_the_notice_off(env, logged_in):
    c, _ = logged_in
    add_sonarr(env, labels={"dashboard.adapter": "none"})
    assert items(c) == []


def test_the_dashboard_itself_is_never_matched(env, logged_in):
    c, _ = logged_in
    env.client.add(FakeContainer("dash", image_ref="sonarr", short_id="dashboardself"))
    assert items(c) == []


# ---- finding the key (admin approved) ---------------------------------------


def test_find_key_stores_it_and_never_returns_it(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env)
    r = post(c, csrf, "/api/integrations/sonarr/find-key")
    assert r.status_code == 200 and r.get_json() == {"error": None}
    assert b"goodkey123" not in r.data
    assert env.app.load_integrations()["keys"] == {"sonarr": "goodkey123"}
    listing = c.get("/api/integrations")
    assert b"goodkey123" not in listing.data
    [it] = listing.get_json()["items"]
    assert it["state"] == "configured"
    assert it["panels"][0]["title"] == "Missing"
    assert it["panels"][0]["total"] == 7
    assert it["panels"][0]["rows"] == [{"title": "Some Show - Pilot", "detail": "2024-01-01"}]
    # the key went out in the adapter's header
    assert ("X-Api-Key", "goodkey123") in [(h, k) for _, h, k in fake_app]


def test_key_file_is_only_read_on_request(env, logged_in, fake_app):
    c, _ = logged_in
    sonarr = add_sonarr(env)
    calls = []
    original = sonarr.get_archive
    sonarr.get_archive = lambda p: calls.append(p) or original(p)
    items(c)
    items(c)
    assert calls == []


def test_find_key_reports_a_missing_file(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env, files={})
    r = post(c, csrf, "/api/integrations/sonarr/find-key")
    assert r.status_code == 404 and "not found" in r.get_json()["error"]
    assert env.app.load_integrations()["keys"] == {}


def test_find_key_with_no_key_in_the_file(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env, files={"/config/config.xml": "<Config></Config>"})
    r = post(c, csrf, "/api/integrations/sonarr/find-key")
    assert r.status_code == 404 and "enter it manually" in r.get_json()["error"]


def test_a_found_key_the_app_rejects_is_not_saved(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env, files={"/config/config.xml": "<ApiKey>stalekey</ApiKey>"})
    r = post(c, csrf, "/api/integrations/sonarr/find-key")
    assert r.status_code == 400 and r.get_json()["error"] == "the app rejected the key"
    assert b"stalekey" not in r.data
    assert env.app.load_integrations()["keys"] == {}


# ---- entering a key by hand -------------------------------------------------


def test_manual_key_is_tested_then_stored(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env)
    r = c.put("/api/integrations/sonarr/key", json={"key": "  goodkey123 "}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert env.app.load_integrations()["keys"]["sonarr"] == "goodkey123"


def test_wrong_manual_key_is_refused_without_echoing_it(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env)
    r = c.put("/api/integrations/sonarr/key", json={"key": "wrongkey"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400 and b"wrongkey" not in r.data
    assert env.app.load_integrations()["keys"] == {}


@pytest.mark.parametrize("bad", ["", "has space", "line\nbreak", "x" * 300, 5, None])
def test_manual_key_shape_is_checked(env, logged_in, fake_app, bad):
    c, csrf = logged_in
    add_sonarr(env)
    r = c.put("/api/integrations/sonarr/key", json={"key": bad}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400
    assert fake_app == []


def test_key_needs_a_running_container(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env, status="exited")
    r = c.put("/api/integrations/sonarr/key", json={"key": "goodkey123"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400 and "start the container" in r.get_json()["error"]


def test_remove_key_goes_back_to_found(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env)
    post(c, csrf, "/api/integrations/sonarr/find-key")
    r = c.delete("/api/integrations/sonarr/key", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert items(c)[0]["state"] == "found"
    assert env.app.load_integrations()["keys"] == {}


# ---- dismiss ----------------------------------------------------------------


def test_dismiss_and_restore(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env)
    assert post(c, csrf, "/api/integrations/sonarr/dismiss").status_code == 200
    assert items(c)[0]["state"] == "dismissed"
    assert post(c, csrf, "/api/integrations/sonarr/restore").status_code == 200
    assert items(c)[0]["state"] == "found"


def test_cannot_dismiss_while_a_key_is_stored(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env)
    post(c, csrf, "/api/integrations/sonarr/find-key")
    assert post(c, csrf, "/api/integrations/sonarr/dismiss").status_code == 400


# ---- guards -----------------------------------------------------------------


def test_everything_needs_csrf(env, logged_in, fake_app):
    c, _ = logged_in
    add_sonarr(env)
    assert c.post("/api/integrations/sonarr/find-key").status_code == 403
    assert c.put("/api/integrations/sonarr/key", json={"key": "goodkey123"}).status_code == 403
    assert c.delete("/api/integrations/sonarr/key").status_code == 403
    assert c.post("/api/integrations/sonarr/dismiss").status_code == 403
    assert env.app.load_integrations()["keys"] == {}


def test_read_only_blocks_keys_but_not_dismissing(env, logged_in, fake_app, monkeypatch):
    c, csrf = logged_in
    add_sonarr(env)
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert post(c, csrf, "/api/integrations/sonarr/find-key").status_code == 403
    assert c.put("/api/integrations/sonarr/key", json={"key": "goodkey123"}, headers={"X-CSRF-Token": csrf}).status_code == 403
    assert c.delete("/api/integrations/sonarr/key", headers={"X-CSRF-Token": csrf}).status_code == 403
    assert post(c, csrf, "/api/integrations/sonarr/dismiss").status_code == 200


def test_login_required(env):
    c = env.app.app.test_client()
    assert c.get("/api/integrations").status_code == 302
    assert c.post("/api/integrations/sonarr/find-key").status_code == 302


def test_unknown_container_and_non_integration_container(env, logged_in, fake_app):
    c, csrf = logged_in
    env.client.add(FakeContainer("web", image_ref="nginx:1"))
    assert post(c, csrf, "/api/integrations/nope/find-key").status_code == 404
    assert post(c, csrf, "/api/integrations/web/find-key").status_code == 400
    assert post(c, csrf, "/api/integrations/web/explode").status_code == 400


# ---- robustness -------------------------------------------------------------


def test_unreachable_app_is_an_error_row_not_a_crash(env, logged_in, fake_app, monkeypatch):
    c, csrf = logged_in
    add_sonarr(env)
    post(c, csrf, "/api/integrations/sonarr/find-key")

    def down(*a, **k):
        raise integrations.AppError("could not reach the app")

    monkeypatch.setattr(integrations, "http_get_json", down)
    env.app._panel_cache.clear()
    [it] = items(c)
    assert it["state"] == "configured" and it["error"] == "could not reach the app"


def test_stopped_app_shows_not_running_without_calling_it(env, logged_in, fake_app):
    c, csrf = logged_in
    sonarr = add_sonarr(env)
    post(c, csrf, "/api/integrations/sonarr/find-key")
    sonarr.status = "exited"
    env.app._panel_cache.clear()
    fake_app.clear()
    [it] = items(c)
    assert it["error"] == "the container is not running"
    assert fake_app == []


def test_panels_are_cached_between_polls(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env)
    post(c, csrf, "/api/integrations/sonarr/find-key")
    items(c)
    fake_app.clear()
    items(c)
    items(c)
    assert fake_app == []


def test_integrations_file_from_before_a_setting_still_loads(env):
    with open(env.app.INTEGRATIONS_PATH, "w") as f:
        json.dump({"keys": {"a": "b"}}, f)
    assert env.app.load_integrations() == {"keys": {"a": "b"}, "dismissed": []}
    with open(env.app.INTEGRATIONS_PATH, "w") as f:
        f.write("{not json")
    assert env.app.load_integrations() == {"keys": {}, "dismissed": []}


def test_integrations_file_is_private(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env)
    post(c, csrf, "/api/integrations/sonarr/find-key")
    assert oct(os.stat(env.app.INTEGRATIONS_PATH).st_mode & 0o777) == "0o600"


# ---- candidate addresses ----------------------------------------------------


def test_candidate_urls_use_network_ip_then_published_port():
    attrs = {
        "NetworkSettings": {
            "Networks": {"a": {"IPAddress": "172.20.0.5"}, "b": {"IPAddress": ""}},
            "Ports": {"8989/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8990"}], "9/udp": None},
        }
    }
    assert integrations.candidate_urls(attrs, 8989) == [
        "http://172.20.0.5:8989",
        "http://host.docker.internal:8990",
    ]


def test_second_address_is_tried_when_the_first_is_unreachable(monkeypatch):
    spec = integrations.validate_spec(yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "adapters", "sonarr.yml"))))
    tried = []

    def fake(url, header, key, timeout=5):
        tried.append(url)
        if "172.20" in url:
            raise integrations.AppError("could not reach the app")
        return {"ok": True}

    monkeypatch.setattr(integrations, "http_get_json", fake)
    base, data = integrations.call(["http://172.20.0.5:8989", "http://host.docker.internal:8990"], spec, "k", "/x")
    assert base == "http://host.docker.internal:8990" and len(tried) == 2


def test_a_rejected_key_is_reported_even_if_another_address_is_unreachable(monkeypatch):
    spec = integrations.validate_spec(yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "adapters", "sonarr.yml"))))

    def fake(url, header, key, timeout=5):
        if "a:1" in url:
            raise integrations.AppError("the app rejected the key")
        raise integrations.AppError("could not reach the app")

    monkeypatch.setattr(integrations, "http_get_json", fake)
    with pytest.raises(integrations.AppError, match="rejected the key"):
        integrations.call(["http://b:2", "http://a:1"], spec, "k", "/x")


def test_addresses_are_tried_at_the_same_time_not_one_after_another(monkeypatch):
    """An unroutable address costs a full timeout; waiting for it first made every call slow."""
    import threading
    import time

    spec = integrations.validate_spec(yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "adapters", "sonarr.yml"))))
    release = threading.Event()

    def fake(url, header, key, timeout=5):
        if "slow" in url:
            release.wait(5)
            raise integrations.AppError("could not reach the app")
        return {"ok": True}

    monkeypatch.setattr(integrations, "http_get_json", fake)
    start = time.time()
    base, data = integrations.call(["http://slow:1", "http://fast:2"], spec, "k", "/x")
    release.set()
    assert base == "http://fast:2" and time.time() - start < 2


def test_every_address_failing_reports_unreachable(monkeypatch):
    spec = integrations.validate_spec(yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "adapters", "sonarr.yml"))))

    def fake(url, header, key, timeout=5):
        raise integrations.AppError("could not reach the app")

    monkeypatch.setattr(integrations, "http_get_json", fake)
    with pytest.raises(integrations.AppError, match="could not reach"):
        integrations.call(["http://a:1", "http://b:2"], spec, "k", "/x")
    with pytest.raises(integrations.AppError, match="could not reach"):
        integrations.call([], spec, "k", "/x")


# ---- adapter files ----------------------------------------------------------

GOOD = """
id: demo
name: Demo
match: {images: [demo], port: 80}
auth: {header: X-Token}
test_path: /api/ping
panels:
  - id: items
    title: Items
    path: /api/items
    rows: data
    row_title: "{name}"
"""


def write(d, name, text):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


def test_built_in_sonarr_adapter_is_valid(env):
    assert "sonarr" in env.app.ADAPTERS and env.app.ADAPTER_PROBLEMS == []


def test_user_adapter_loads_and_matches(tmp_path):
    write(tmp_path, "demo.yml", GOOD)
    adapters, problems = integrations.load_adapters(str(tmp_path))
    assert problems == [] and adapters["demo"]["panels"][0]["rows"] == "data"
    assert integrations.match_adapter(adapters, {}, "ghcr.io/me/demo:1")["id"] == "demo"
    assert integrations.match_adapter(adapters, {}, "other") is None


def test_user_adapter_overrides_a_built_in_by_id(tmp_path):
    builtin, user = tmp_path / "b", tmp_path / "u"
    write(builtin, "demo.yml", GOOD)
    write(user, "mine.yml", GOOD.replace("name: Demo", "name: My Demo"))
    adapters, _ = integrations.load_adapters(str(builtin), str(user))
    assert adapters["demo"]["name"] == "My Demo"


def test_bad_adapter_is_skipped_and_reported_without_blocking_others(tmp_path):
    write(tmp_path, "good.yml", GOOD)
    write(tmp_path, "bad.yml", GOOD.replace("port: 80", "port: 99999"))
    write(tmp_path, "garbage.yml", "id: [unclosed")
    adapters, problems = integrations.load_adapters(str(tmp_path))
    assert list(adapters) == ["demo"]
    assert len(problems) == 2
    assert any(p.startswith("bad.yml:") and "port" in p for p in problems)


def test_missing_user_directory_is_fine(tmp_path):
    assert integrations.load_adapters(str(tmp_path / "nope")) == ({}, [])


def test_problems_are_returned_by_the_api(env, logged_in):
    c, _ = logged_in
    env.app.ADAPTER_PROBLEMS.append("bad.yml: oops")
    assert c.get("/api/integrations").get_json()["problems"] == ["bad.yml: oops"]


@pytest.mark.parametrize(
    "change",
    [
        ("id: demo", "id: Demo!"),
        ("header: X-Token", "header: 'X Token'"),
        ("test_path: /api/ping", "test_path: http://evil.example/x"),
        ("path: /api/items", "path: /api/../../etc/passwd"),
        ("images: [demo]", "images: []"),
        ("rows: data", "rows: ''"),
    ],
)
def test_spec_validation_rejects(tmp_path, change):
    write(tmp_path, "x.yml", GOOD.replace(*change))
    adapters, problems = integrations.load_adapters(str(tmp_path))
    assert adapters == {} and len(problems) == 1


def test_key_file_regex_needs_one_group(tmp_path):
    extra = "key_file: {path: /c.xml, regex: 'Key=.*'}\n"
    write(tmp_path, "x.yml", GOOD + extra)
    assert integrations.load_adapters(str(tmp_path))[0] == {}
    write(tmp_path, "x.yml", GOOD + "key_file: {path: /c.xml, regex: 'Key=(.*)'}\n")
    assert "demo" in integrations.load_adapters(str(tmp_path))[0]


def test_build_panel_shapes_rows_and_survives_odd_data():
    panel = {"id": "p", "title": "P", "rows": "a.b", "row_title": "{x.y} - {z}", "row_detail": "{d}", "total": "n"}
    out = integrations.build_panel(panel, {"a": {"b": [{"x": {"y": "S"}, "z": "T", "d": 3}, {"z": "only"}]}, "n": 9})
    assert out["rows"] == [{"title": "S - T", "detail": "3"}, {"title": "- only", "detail": ""}]
    assert out["total"] == 9
    with pytest.raises(integrations.AppError):
        integrations.build_panel(panel, {"a": "nope"})


def test_worked_example_in_the_docs_is_a_valid_adapter():
    """The example in docs/ADAPTERS.md must keep working, so the docs can't rot."""
    docs = open(os.path.join(os.path.dirname(__file__), "..", "docs", "ADAPTERS.md")).read()
    block = docs.split("<!-- worked-example -->", 1)[1].split("```yaml\n", 1)[1].split("```", 1)[0]
    spec = integrations.validate_spec(yaml.safe_load(block))
    assert spec["id"] == "pantry" and "key_file" not in spec
    answer = {"data": {"items": [{"name": "Rice", "left": 1, "unit": "kg"}], "count": 4}}
    out = integrations.build_panel(spec["panels"][0], answer)
    assert out["rows"] == [{"title": "Rice", "detail": "1 kg"}] and out["total"] == 4
    assert integrations.match_adapter({"pantry": spec}, {}, "ghcr.io/me/pantry-tracker:2")["id"] == "pantry"


# ---- Radarr, Seerr, qBittorrent ---------------------------------------------

import handlers  # noqa: E402


@pytest.fixture(autouse=True)
def clean_handler_state():
    handlers._titles.clear()
    handlers._sessions.clear()
    yield
    handlers._titles.clear()
    handlers._sessions.clear()


def add_app(env, name, image, files=None, labels=None, status="running"):
    return env.client.add(FakeContainer(name, status=status, image_ref=image, files=files or {}, labels=labels))


def test_all_built_in_adapters_load_without_problems(env):
    assert {"sonarr", "radarr", "seerr", "qbittorrent"} <= set(env.app.ADAPTERS)
    assert env.app.ADAPTER_PROBLEMS == []


def test_radarr_panel_shows_missing_movies(env, logged_in, monkeypatch):
    c, csrf = logged_in
    add_app(env, "radarr", "lscr.io/linuxserver/radarr:latest", files={"/config/config.xml": CONFIG_XML})

    def fake(url, header, key, timeout=5):
        if key != "goodkey123":
            raise integrations.AppError("the app rejected the key")
        if "/wanted/missing" in url:
            return {"totalRecords": 3, "records": [{"title": "Heat", "year": 1995}]}
        return {}

    monkeypatch.setattr(integrations, "http_get_json", fake)
    assert post(c, csrf, "/api/integrations/radarr/find-key").status_code == 200
    [it] = items(c)
    assert it["adapter"] == "radarr"
    assert it["panels"][0]["title"] == "Missing" and it["panels"][0]["total"] == 3
    assert it["panels"][0]["rows"] == [{"title": "Heat", "detail": "1995"}]


# -- Seerr

SEERR_SETTINGS = json.dumps(
    {
        "radarr": [{"apiKey": "radarr-key-first"}],
        "main": {"apiKey": "goodkey123"},
        "sonarr": [{"apiKey": "sonarr-key"}],
    }
)


def seerr_http(monkeypatch, lookups=None):
    lookups = lookups if lookups is not None else []

    def fake(url, header, key, timeout=5):
        if key != "goodkey123":
            raise integrations.AppError("the app rejected the key")
        if "/settings/main" in url:
            return {}
        if "/api/v1/request" in url:
            if "filter=pending" in url:
                return {
                    "pageInfo": {"results": 1},
                    "results": [{"type": "movie", "media": {"tmdbId": 11}, "requestedBy": {"displayName": "Sam"}}],
                }
            return {
                "pageInfo": {"results": 2},
                "results": [
                    {"type": "movie", "media": {"tmdbId": 11}, "requestedBy": {"displayName": "Sam"}},
                    {"type": "tv", "media": {"tmdbId": 22}, "requestedBy": {}},
                    {"type": "tv", "media": {"tmdbId": 33}, "requestedBy": {"displayName": "Kim"}},
                ],
            }
        lookups.append(url)
        if "/movie/11" in url:
            return {"title": "Heat"}
        if "/tv/22" in url:
            return {"name": "Slow Horses"}
        raise integrations.AppError("the app answered HTTP 404")

    monkeypatch.setattr(integrations, "http_get_json", fake)
    return lookups


def test_seerr_key_comes_from_main_api_key_even_when_another_key_is_first(env, logged_in, monkeypatch):
    c, csrf = logged_in
    seerr_http(monkeypatch)
    add_app(env, "jellyseerr", "ghcr.io/seerr-team/seerr:latest", files={"/app/config/settings.json": SEERR_SETTINGS})
    assert post(c, csrf, "/api/integrations/jellyseerr/find-key").status_code == 200
    assert env.app.load_integrations()["keys"] == {"jellyseerr": "goodkey123"}


def test_seerr_shows_both_lists_with_titles(env, logged_in, monkeypatch):
    c, csrf = logged_in
    seerr_http(monkeypatch)
    add_app(env, "seerr", "ghcr.io/seerr-team/seerr:latest", files={"/app/config/settings.json": SEERR_SETTINGS})
    post(c, csrf, "/api/integrations/seerr/find-key")
    [it] = items(c)
    pending, unavailable = it["panels"]
    assert (pending["title"], pending["total"]) == ("Awaiting approval", 1)
    assert pending["rows"] == [{"title": "Heat", "detail": "movie · requested by Sam"}]
    assert (unavailable["title"], unavailable["total"]) == ("Not yet available", 2)
    assert [r["title"] for r in unavailable["rows"]] == ["Heat", "Slow Horses", "Unknown"]  # 33 failed to look up
    assert unavailable["rows"][1]["detail"] == "tv · requested by unknown"


def test_seerr_title_lookups_are_cached(env, logged_in, monkeypatch):
    c, csrf = logged_in
    lookups = seerr_http(monkeypatch)
    add_app(env, "seerr", "ghcr.io/seerr-team/seerr:latest", files={"/app/config/settings.json": SEERR_SETTINGS})
    post(c, csrf, "/api/integrations/seerr/find-key")
    items(c)
    first = len(lookups)
    env.app._panel_cache.clear()
    items(c)
    # Heat and Slow Horses are remembered; only the failed one is asked about again
    assert len(lookups) - first == 1 and sum("/movie/11" in u for u in lookups) == 1


def test_old_jellyseerr_image_name_still_matches(env, logged_in):
    c, _ = logged_in
    add_app(env, "js", "fallenbagel/jellyseerr:latest")
    assert items(c)[0]["adapter"] == "seerr"


# -- qBittorrent

TORRENTS = [
    {"name": "Done.iso", "progress": 1, "state": "uploading", "dlspeed": 0, "eta": 8640000},
    {"name": "Big.Show.S01", "progress": 0.456, "state": "downloading", "dlspeed": 3355443, "eta": 780},
    {"name": "Paused.Thing", "progress": 0.1, "state": "pausedDL", "dlspeed": 0, "eta": 8640000},
    {"name": "Slow.One", "progress": 0.9, "state": "downloading", "dlspeed": 0, "eta": 8640000},
]


class FakeQbt:
    """Stands in for qBittorrent's web API behind handlers._send. `new_style` answers like
    newer versions (204 on success, 401 on failure) instead of 200 "Ok." / 200 "Fails."."""

    def __init__(self, user="admin", password="s3cret pass", new_style=False):
        self.user, self.password, self.new_style = user, password, new_style
        self.logins = 0
        self.expire_next = False
        self.version_status = 200
        self.seen = []

    def __call__(self, opener, req):
        url = req.full_url
        self.seen.append((url, req.data, dict(req.header_items())))
        if url.endswith("/api/v2/auth/login"):
            from urllib.parse import parse_qs

            form = parse_qs(req.data.decode())
            self.logins += 1
            ok = form["username"] == [self.user] and form["password"] == [self.password]
            if self.new_style:
                return (204, b"") if ok else (401, b"")
            return 200, b"Ok." if ok else b"Fails."
        if url.endswith("/app/version"):
            return self.version_status, b"v5.0.0"
        if self.expire_next:
            self.expire_next = False
            return 403, b""
        if url.endswith("/torrents/info"):
            return 200, json.dumps(TORRENTS).encode()
        if url.endswith("/transfer/info"):
            return 200, json.dumps({"dl_info_speed": 5 * 1024 * 1024, "up_info_speed": 2048}).encode()
        return 404, b""


@pytest.fixture
def qbt(monkeypatch):
    fake = FakeQbt()
    monkeypatch.setattr(handlers, "_send", fake)
    return fake


def put_login(c, csrf, name, user="admin", password="s3cret pass"):
    return c.put(
        f"/api/integrations/{name}/key", json={"username": user, "password": password}, headers={"X-CSRF-Token": csrf}
    )


def test_qbittorrent_card_asks_for_a_login_and_offers_no_key_search(env, logged_in, qbt):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    [it] = items(c)
    assert it["auth_kind"] == "login" and it["key_file"] is None and it["state"] == "found"
    assert post(c, csrf, "/api/integrations/qbittorrent/find-key").status_code == 400
    assert qbt.seen == []


def test_qbittorrent_login_is_checked_stored_and_never_returned(env, logged_in, qbt):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    r = put_login(c, csrf, "qbittorrent")
    assert r.status_code == 200 and b"s3cret" not in r.data
    assert env.app.load_integrations()["keys"]["qbittorrent"] == {"username": "admin", "password": "s3cret pass"}
    listing = c.get("/api/integrations")
    assert b"s3cret" not in listing.data and b"password" not in listing.data and b'"username"' not in listing.data
    assert oct(os.stat(env.app.INTEGRATIONS_PATH).st_mode & 0o777) == "0o600"
    # login went out as form data with a Referer on the app's own address
    url, data, headers = qbt.seen[0]
    assert headers["Referer"] == url.rsplit("/api/v2/auth/login", 1)[0]


def test_newer_qbittorrent_answers_204_on_success_and_401_on_failure(env, logged_in, monkeypatch):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    fake = FakeQbt(new_style=True)
    monkeypatch.setattr(handlers, "_send", fake)
    bad = put_login(c, csrf, "qbittorrent", password="nope")
    assert bad.status_code == 400 and bad.get_json()["error"] == "the app rejected the login"
    assert put_login(c, csrf, "qbittorrent").status_code == 200
    [it] = items(c)
    assert it["error"] is None and it["panels"][0]["total"] == 3


def test_a_204_that_does_not_give_a_working_session_is_not_a_success(env, logged_in, monkeypatch):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    fake = FakeQbt(new_style=True)
    fake.version_status = 403
    monkeypatch.setattr(handlers, "_send", fake)
    r = put_login(c, csrf, "qbittorrent")
    assert r.status_code == 400 and r.get_json()["error"] == "the app rejected the login"
    assert env.app.load_integrations()["keys"] == {}


def test_wrong_qbittorrent_login_is_refused_without_echo(env, logged_in, qbt):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    r = put_login(c, csrf, "qbittorrent", password="nope")
    assert r.status_code == 400 and r.get_json()["error"] == "the app rejected the login"
    assert b"nope" not in r.data and env.app.load_integrations()["keys"] == {}


@pytest.mark.parametrize("body", [{}, {"username": "a"}, {"username": "a", "password": 5}, {"key": "x"}, {"username": "a", "password": "bad\nline"}])
def test_login_shape_is_checked(env, logged_in, qbt, body):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    r = c.put("/api/integrations/qbittorrent/key", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400 and qbt.seen == []


def test_qbittorrent_panel_shows_only_unfinished_torrents(env, logged_in, qbt):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    put_login(c, csrf, "qbittorrent")
    [it] = items(c)
    [panel] = it["panels"]
    assert panel["title"] == "Downloading" and panel["total"] == 3
    assert panel["summary"] == "↓ 5.0 MB/s   ↑ 2.0 KB/s"
    rows = {r["title"]: r for r in panel["rows"]}
    assert "Done.iso" not in rows
    assert rows["Big.Show.S01"]["detail"] == "45% · 3.2 MB/s · ETA 13 min"
    assert rows["Big.Show.S01"]["progress"] == 45.6
    assert rows["Paused.Thing"]["detail"] == "10% · paused"
    assert rows["Slow.One"]["detail"] == "90%"  # no speed, and 8640000 means "never"
    assert [r["title"] for r in panel["rows"]] == ["Paused.Thing", "Big.Show.S01", "Slow.One"]  # by progress


def test_expired_qbittorrent_session_logs_in_again_once(env, logged_in, qbt):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    put_login(c, csrf, "qbittorrent")
    items(c)
    logins = qbt.logins
    qbt.expire_next = True
    env.app._panel_cache.clear()
    [it] = items(c)
    assert it["error"] is None and qbt.logins == logins + 1


def test_session_is_reused_between_polls(env, logged_in, qbt):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    put_login(c, csrf, "qbittorrent")
    items(c)
    logins = qbt.logins
    env.app._panel_cache.clear()
    items(c)
    assert qbt.logins == logins


def test_login_endpoints_respect_read_only_and_csrf(env, logged_in, qbt, monkeypatch):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    assert c.put("/api/integrations/qbittorrent/key", json={"username": "admin", "password": "x"}).status_code == 403
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert put_login(c, csrf, "qbittorrent").status_code == 403
    assert qbt.seen == []


def test_remove_login_goes_back_to_found(env, logged_in, qbt):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    put_login(c, csrf, "qbittorrent")
    assert c.delete("/api/integrations/qbittorrent/key", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert items(c)[0]["state"] == "found" and env.app.load_integrations()["keys"] == {}


def test_a_stored_key_string_does_not_count_as_a_login(env, logged_in, qbt):
    c, _ = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")
    with open(env.app.INTEGRATIONS_PATH, "w") as f:
        json.dump({"keys": {"qbittorrent": "leftover-key"}}, f)
    assert items(c)[0]["state"] == "found"


def test_unreachable_qbittorrent_says_so(env, logged_in, monkeypatch):
    c, csrf = logged_in
    add_app(env, "qbittorrent", "lscr.io/linuxserver/qbittorrent:latest")

    def down(opener, req):
        raise integrations.AppError("could not reach the app")

    monkeypatch.setattr(handlers, "_send", down)
    r = put_login(c, csrf, "qbittorrent")
    assert r.status_code == 400 and r.get_json()["error"] == "could not reach the app"


def test_speed_and_eta_formatting():
    assert handlers.fmt_speed(900) == "900 B/s"
    assert handlers.fmt_speed(1536) == "1.5 KB/s"
    assert handlers.fmt_speed(3 * 1024**3) == "3.0 GB/s"
    assert handlers.fmt_eta(45) == "45 s"
    assert handlers.fmt_eta(780) == "13 min"
    assert handlers.fmt_eta(3 * 3600 + 5 * 60) == "3 h 5 min"
    assert handlers.fmt_eta(2 * 86400 + 4 * 3600) == "2 d 4 h"


# -- spec rules for handlers, login and key files

HANDLER_SPEC = GOOD.replace("id: demo", "id: demo\nhandler: seerr")


def test_user_adapters_cannot_name_a_handler(tmp_path):
    write(tmp_path, "x.yml", HANDLER_SPEC)
    adapters, problems = integrations.load_adapters(str(tmp_path))
    assert adapters == {} and "only available to adapters shipped" in problems[0]


def test_built_in_adapters_can_name_a_handler(tmp_path):
    write(tmp_path, "x.yml", HANDLER_SPEC)
    adapters, problems = integrations.load_adapters(str(tmp_path), trusted=(str(tmp_path),), handlers=handlers.HANDLERS)
    assert problems == [] and adapters["demo"]["handler"] == "seerr"


def test_unknown_handler_name_is_rejected(tmp_path):
    write(tmp_path, "x.yml", GOOD.replace("id: demo", "id: demo\nhandler: nothere"))
    adapters, problems = integrations.load_adapters(str(tmp_path), trusted=(str(tmp_path),), handlers=handlers.HANDLERS)
    assert adapters == {} and "unknown handler" in problems[0]


def test_an_override_of_a_built_in_in_the_user_folder_cannot_add_a_handler(env, tmp_path):
    user = tmp_path / "u"
    write(user, "seerr.yml", HANDLER_SPEC.replace("id: demo", "id: seerr"))
    adapters, problems = integrations.load_adapters(
        env.app.BUILTIN_ADAPTERS, str(user), trusted=(env.app.BUILTIN_ADAPTERS,), handlers=handlers.HANDLERS
    )
    assert len(problems) == 1 and adapters["seerr"]["handler"] == "seerr"  # the built-in stays


def test_login_auth_needs_a_handler_and_has_no_key_file(tmp_path):
    login = GOOD.replace("auth: {header: X-Token}", "auth: {kind: login}")
    write(tmp_path, "a.yml", login)
    assert integrations.load_adapters(str(tmp_path))[0] == {}  # no handler
    both = login.replace("id: demo", "id: demo\nhandler: seerr") + "key_file: {path: /c, json_path: a.b}\n"
    write(tmp_path, "a.yml", both)
    adapters, problems = integrations.load_adapters(str(tmp_path), trusted=(str(tmp_path),))
    assert adapters == {} and "key_file only applies to header auth" in problems[0]


@pytest.mark.parametrize(
    "key_file",
    [
        "key_file: {path: /c, regex: 'a(b)', json_path: a.b}\n",  # both
        "key_file: {path: /c}\n",  # neither
        "key_file: {path: /c, json_path: 'a..b'}\n",
        "key_file: {path: /c, json_path: 'a b'}\n",
    ],
)
def test_key_file_needs_exactly_one_valid_locator(tmp_path, key_file):
    write(tmp_path, "x.yml", GOOD + key_file)
    assert integrations.load_adapters(str(tmp_path))[0] == {}


def test_json_path_key_extraction_picks_the_named_field():
    import io
    import tarfile

    def archive(text):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("settings.json")
            info.size = len(text.encode())
            tar.addfile(info, io.BytesIO(text.encode()))
        return [buf.getvalue()]

    kf = {"path": "/x", "json_path": "main.apiKey"}
    assert integrations.key_from_archive(archive(SEERR_SETTINGS), kf) == "goodkey123"
    assert integrations.key_from_archive(archive("{not json"), kf) is None
    assert integrations.key_from_archive(archive('{"main": {"apiKey": 5}}'), kf) is None
    assert integrations.key_from_archive(archive('{"main": {}}'), kf) is None


# ---- the page's refresh rate ----------------------------------------------------


def panel_calls(fake_app):
    return [u for u, _, _ in fake_app if "/system/status" not in u]


def test_panel_cache_follows_the_age_the_page_asks_for(env, logged_in, fake_app):
    c, csrf = logged_in
    add_sonarr(env)
    post(c, csrf, "/api/integrations/sonarr/find-key")
    env.app._panel_cache.clear()
    c.get("/api/integrations")  # fills the cache
    fake_app.clear()
    c.get("/api/integrations")  # default 30 s: answered from the cache
    assert fake_app == []
    # the cached answer is 20 s old: fine for a 30 s page, too old for a page refreshing every 10 s
    name, (at, result) = next(iter(env.app._panel_cache.items()))
    env.app._panel_cache[name] = (at - 20, result)
    c.get("/api/integrations?age=30")
    assert fake_app == []
    c.get("/api/integrations?age=10")
    assert fake_app != []


@pytest.mark.parametrize("value, expected", [("10", 10), ("1", 5), ("99999", 300), ("abc", 30), ("", 30), ("-3", 5)])
def test_age_is_clamped(env, value, expected):
    with env.app.app.test_request_context(f"/?age={value}"):
        assert env.app.age_arg(30, 5, 300) == expected
