"""Images pinned to a version tag: finding a later tag, the notice on the row, the choices (rewrite
the compose file, or ignore) and the compose rewrite itself. Tag lists come from FakeClient.tags;
nothing here reaches a registry."""
import json
import os
from types import SimpleNamespace

import pytest

import pinned
from conftest import FakeContainer

REF = "ghcr.io/autobrr/autobrr:v1.86.0"
TAGS = ["v1.85.0", "v1.86.0", "v1.87.0", "v1.87.0-rc1", "latest", "nightly", "v1.9.0"]

COMPOSE_TEXT = """\
# media stack
services:
  db:
    image: postgres:16   # keep in step with the dump

  autobrr:
    image: "ghcr.io/autobrr/autobrr:v1.86.0"  # pinned
    container_name: autobrr
    restart: unless-stopped

volumes:
  data: {}
"""


# ---- pure helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "ref, expected",
    [
        ("ghcr.io/autobrr/autobrr:v1.86.0", ("ghcr.io", "autobrr/autobrr", "v1.86.0")),
        ("postgres:16", ("registry-1.docker.io", "library/postgres", "16")),
        ("linuxserver/sonarr:4.0", ("registry-1.docker.io", "linuxserver/sonarr", "4.0")),
        ("localhost:5000/app:1", ("localhost:5000", "app", "1")),
        ("app", None),
        ("app@sha256:abc", None),
        ("ghcr.io/x/y:1@sha256:abc", None),
        ("", None),
    ],
)
def test_split_ref(ref, expected):
    assert pinned.split_ref(ref) == expected


@pytest.mark.parametrize(
    "current, tags, expected",
    [
        ("v1.86.0", TAGS, "v1.87.0"),
        ("v1.87.0", TAGS, None),
        ("v1.88.0", TAGS, None),
        ("v1.86.0", ["v2.0.0", "v1.86.1"], "v2.0.0"),
        ("1.2", ["1.3", "1.2.1", "2"], "1.3"),  # same number of dotted parts only
        ("16-alpine", ["15-alpine", "17-alpine", "18", "18-slim"], "17-alpine"),
        ("16", ["16-alpine", "17", "17-rc1"], "17"),
        ("latest", ["latest", "v1.0.0"], None),
        ("v1.86.0", ["1.99.0", "V2.0.0"], None),  # another prefix
        ("v1.9.0", ["v1.10.0"], "v1.10.0"),  # numbers, not text
        ("v1.86.0", [], None),
    ],
)
def test_newest_tag(current, tags, expected):
    assert pinned.newest_tag(current, tags) == expected


def test_is_newer_and_with_tag():
    assert pinned.is_newer("v1.88.0", "v1.87.0")
    assert not pinned.is_newer("v1.87.0", "v1.87.0")
    assert not pinned.is_newer("v1.86.0", "v1.87.0")
    assert not pinned.is_newer("latest", "v1.87.0")
    assert pinned.with_tag(REF, "v1.87.0") == "ghcr.io/autobrr/autobrr:v1.87.0"
    assert pinned.with_tag("localhost:5000/app:1", "latest") == "localhost:5000/app:latest"


def test_list_tags_follows_the_token_handshake_and_pages():
    seen = []

    class Opener:
        def open(self, req, timeout=None):
            import io
            import urllib.error

            url, auth = req.full_url, req.get_header("Authorization")
            seen.append((url, auth))
            if url.startswith("https://auth.example/token"):
                return Resp(b'{"token": "tok"}')
            if not auth:
                hdrs = {"WWW-Authenticate": 'Bearer realm="https://auth.example/token",service="reg",scope="repository:x/y:pull"'}
                raise urllib.error.HTTPError(url, 401, "no", hdrs, io.BytesIO())
            if "last=" in url:
                return Resp(b'{"tags": ["c"]}')
            return Resp(b'{"tags": ["a", "b"]}', {"Link": '</v2/x/y/tags/list?n=1000&last=b>; rel="next"'})

    class Resp:
        def __init__(self, body, headers=None):
            self.status, self.headers, self._body = 200, headers or {}, body

        def read(self, n=None):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    assert pinned.list_tags("reg.example", "x/y", Opener()) == ["a", "b", "c"]
    assert seen[1][0].startswith("https://auth.example/token?scope=repository%3Ax%2Fy%3Apull&service=reg")
    assert seen[-1][1] == "Bearer tok"


def test_list_tags_refuses_a_login_realm_that_is_not_https():
    import io
    import urllib.error

    class Opener:
        def open(self, req, timeout=None):
            hdrs = {"WWW-Authenticate": 'Bearer realm="http://auth.example/token"'}
            raise urllib.error.HTTPError(req.full_url, 401, "no", hdrs, io.BytesIO())

    with pytest.raises(ValueError):
        pinned.list_tags("reg.example", "x/y", Opener())


# ---- compose rewrite --------------------------------------------------------


def write(tmp_path, text=COMPOSE_TEXT, name="compose.yml"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_rewrite_changes_only_that_services_image_line(tmp_path):
    f = write(tmp_path)
    path, original = pinned.rewrite_image([f], "autobrr", REF, "ghcr.io/autobrr/autobrr:v1.87.0")
    assert path == f and original == COMPOSE_TEXT
    assert open(f).read() == COMPOSE_TEXT.replace("autobrr:v1.86.0", "autobrr:v1.87.0")


def test_rewrite_keeps_the_file_mode_and_restore_puts_it_back(tmp_path):
    f = write(tmp_path)
    os.chmod(f, 0o640)
    path, original = pinned.rewrite_image([f], "autobrr", REF, "ghcr.io/autobrr/autobrr:latest")
    assert os.stat(f).st_mode & 0o777 == 0o640
    assert pinned.restore(path, original) is None
    assert open(f).read() == COMPOSE_TEXT


def test_rewrite_keeps_windows_line_endings(tmp_path):
    f = write(tmp_path, COMPOSE_TEXT.replace("\n", "\r\n"))
    pinned.rewrite_image([f], "autobrr", REF, "ghcr.io/autobrr/autobrr:v1.87.0")
    with open(f, newline="") as fh:
        text = fh.read()
    assert text.count("\r\n") == COMPOSE_TEXT.count("\n") and "v1.87.0\"  # pinned" in text


def test_rewrite_picks_the_service_when_two_share_an_image(tmp_path):
    text = "services:\n  a:\n    image: app:1\n  b:\n    image: app:1\n"
    f = write(tmp_path, text)
    pinned.rewrite_image([f], "b", "app:1", "app:2")
    assert open(f).read() == "services:\n  a:\n    image: app:1\n  b:\n    image: app:2\n"


@pytest.mark.parametrize(
    "text",
    [
        "services:\n  web:\n    image: ${IMG}\n",  # the running image is app:1, the file says a variable
        "services:\n  web:\n    <<: *base\n",  # no image of its own
        "services:\n  other:\n    image: app:1\n",  # no such service
        "services:\n  web: {image: 'app:1'}\n",  # flow style: no plain line
    ],
)
def test_rewrite_refuses_what_it_cannot_change_safely(tmp_path, text):
    f = write(tmp_path, text)
    before = open(f).read()
    with pytest.raises(pinned.RewriteError):
        pinned.rewrite_image([f], "web", "app:1", "app:2")
    assert open(f).read() == before


def test_a_later_compose_file_decides_the_image(tmp_path):
    base = write(tmp_path, "services:\n  web:\n    image: app:1\n", "a.yml")
    over = write(tmp_path, "services:\n  web:\n    image: app:1\n", "b.yml")
    assert pinned.rewrite_image([base, over], "web", "app:1", "app:2")[0] == over
    assert open(base).read().endswith("app:1\n")


def test_check_rewrite_says_why_not(tmp_path, monkeypatch):
    f = write(tmp_path)
    assert pinned.check_rewrite([f], "autobrr", REF) is None
    assert "differs" in pinned.check_rewrite([f], "autobrr", "ghcr.io/autobrr/autobrr:v9")
    monkeypatch.setattr(pinned.os, "access", lambda p, mode: False)
    assert "read-only" in pinned.check_rewrite([f], "autobrr", REF)


# ---- the notice and the choices ----------------------------------------------


@pytest.fixture
def stack(env, logged_in, tmp_path, monkeypatch):
    """autobrr pinned to v1.86.0 in a real compose file, the registry listing TAGS, docker faked."""
    f = write(tmp_path)
    labels = {
        "com.docker.compose.project": "media",
        "com.docker.compose.service": "autobrr",
        "com.docker.compose.project.config_files": f,
        "com.docker.compose.project.working_dir": str(tmp_path),
    }
    env.client.add(FakeContainer("autobrr", labels=labels, image_ref=REF, repo_digests=["x@sha256:same"]))
    env.client.registry[REF] = "sha256:same"
    env.client.tags["autobrr/autobrr"] = list(TAGS)
    monkeypatch.setattr(env.app.shutil, "which", lambda n: "/usr/bin/docker")
    calls = []
    result = SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(env.app.subprocess, "run", lambda cmd, **kw: calls.append((cmd, open(f).read())) or result)
    thread = lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args))
    monkeypatch.setattr(env.app, "threading", SimpleNamespace(Thread=thread))
    env.app.check_registry()
    c, csrf = logged_in
    return SimpleNamespace(env=env, c=c, csrf=csrf, file=f, calls=calls, result=result)


def row(s, name="autobrr"):
    return {i["name"]: i for i in s.c.get("/api/containers").get_json()["items"]}[name]


def choose(s, action, version="v1.87.0", csrf=True, client=None):
    headers = {"X-CSRF-Token": s.csrf} if csrf else {}
    return (client or s.c).post("/api/containers/autobrr/tag", json={"action": action, "version": version}, headers=headers)


def test_a_pinned_image_with_a_later_tag_gets_a_notice_not_an_update(stack):
    r = row(stack)
    assert r["update"] is None
    assert r["pinned"] == {"current": "v1.86.0", "newer": "v1.87.0", "latest": True, "reason": None, "state": None, "error": None}


def test_no_later_tag_no_notice(stack):
    stack.env.client.tags["autobrr/autobrr"] = ["v1.85.0", "v1.86.0", "latest"]
    stack.env.app.check_registry()
    assert row(stack)["pinned"] is None


def test_a_digest_update_wins_over_the_notice(stack):
    stack.env.client.registry[REF] = "sha256:new"
    stack.env.app.check_registry()
    r = row(stack)
    assert r["update"]["available"] is True and r["pinned"] is None


def test_tag_listing_errors_are_reported_and_keep_the_last_answer(stack):
    stack.env.client.tags_error = OSError("no route")
    body = stack.env.app.check_registry()
    assert [e["image"] for e in body["errors"]] == [REF]
    assert row(stack)["pinned"]["newer"] == "v1.87.0"


def test_the_check_counts_pinned_notices_and_not_ignored_ones(stack):
    assert stack.env.app.check_registry()["updates"] == 1
    assert choose(stack, "ignore").status_code == 200
    assert stack.env.app.check_registry()["updates"] == 0


def test_plain_tags_and_non_compose_containers_get_no_notice(stack):
    stack.env.client.add(FakeContainer("bare", image_ref=REF, repo_digests=["x@sha256:same"]))
    stack.env.client.add(FakeContainer("rolling", labels=stack.env.client.by_name["autobrr"].attrs["Config"]["Labels"], image_ref="ghcr.io/autobrr/autobrr:latest", repo_digests=["y@sha256:s"]))
    stack.env.client.registry["ghcr.io/autobrr/autobrr:latest"] = "sha256:s"
    stack.env.app.check_registry()
    assert row(stack, "bare")["pinned"] is None and row(stack, "rolling")["pinned"] is None


def test_updates_off_hides_the_notice(stack, monkeypatch):
    monkeypatch.setattr(stack.env.app, "UPDATE_CHECK", False)
    assert row(stack)["pinned"] is None


def test_newer_rewrites_the_compose_file_then_pulls_and_recreates(stack):
    r = choose(stack, "newer")
    assert r.status_code == 202
    steps = [c[0][-2:] for c in stack.calls]
    assert steps == [["pull", "autobrr"], ["--no-deps", "autobrr"]]
    assert all("autobrr:v1.87.0" in seen for _, seen in stack.calls)  # the file was already changed for both steps
    assert open(stack.file).read() == COMPOSE_TEXT.replace("v1.86.0", "v1.87.0")
    assert "autobrr" not in stack.env.app._jobs


def test_latest_sets_the_tag_to_latest(stack):
    assert choose(stack, "latest").status_code == 202
    assert "image: \"ghcr.io/autobrr/autobrr:latest\"" in open(stack.file).read()


def test_a_failed_update_puts_the_compose_file_back_and_shows_the_error(stack):
    stack.result.returncode, stack.result.stderr = 1, "boom\npull denied"
    assert choose(stack, "newer").status_code == 202
    assert open(stack.file).read() == COMPOSE_TEXT
    p = row(stack)["pinned"]
    assert p["state"] == "error" and "pull denied" in p["error"] and "put back" in p["error"]
    assert row(stack)["update"] is None
    # trying again after fixing things works, and clears the error
    stack.result.returncode = 0
    assert choose(stack, "newer").status_code == 202
    assert row(stack)["pinned"]["error"] is None and "autobrr" not in stack.env.app._jobs
    assert "autobrr:v1.87.0" in open(stack.file).read()  # (the fake docker never recreates the container, so the row still shows it)


def test_a_running_job_shows_as_running_and_refuses_a_second(stack, monkeypatch):
    thread = lambda target, args, daemon: SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(stack.env.app, "threading", SimpleNamespace(Thread=thread))
    assert choose(stack, "newer").status_code == 202
    assert row(stack)["pinned"]["state"] == "running"
    assert choose(stack, "newer").status_code == 409
    assert stack.env.app.start_update("autobrr")[1] == 400  # and the plain update path stays out of it


def test_a_read_only_mount_disables_the_rewrite_choices(stack, monkeypatch):
    monkeypatch.setattr(pinned.os, "access", lambda p, mode: False)
    assert "read-only" in row(stack)["pinned"]["reason"]
    r = choose(stack, "newer")
    assert r.status_code == 400 and "read-only" in r.get_json()["error"]
    assert open(stack.file).read() == COMPOSE_TEXT
    assert choose(stack, "ignore").status_code == 200  # ignoring still works


def test_the_page_must_name_the_version_it_showed(stack):
    r = choose(stack, "newer", version="v1.80.0")
    assert r.status_code == 409 and "v1.87.0" in r.get_json()["error"]
    assert open(stack.file).read() == COMPOSE_TEXT


def test_bad_requests(stack):
    assert choose(stack, "newer", csrf=False).status_code == 403
    assert choose(stack, "explode").status_code == 400
    assert stack.c.post("/api/containers/nothing/tag", json={"action": "ignore", "version": "x"}, headers={"X-CSRF-Token": stack.csrf}).status_code == 404
    stack.env.client.tags["autobrr/autobrr"] = []
    stack.env.app.check_registry()
    assert choose(stack, "newer").status_code == 400  # nothing to move to


def test_operators_cannot_choose(stack):
    from test_users import make_user, sign_in

    make_user(stack.env, stack.c, stack.csrf)
    o, ocsrf = sign_in(stack.env, "op", "operator-pass")
    r = o.post("/api/containers/autobrr/tag", json={"action": "ignore", "version": "v1.87.0"}, headers={"X-CSRF-Token": ocsrf})
    assert r.status_code == 403
    assert o.get("/api/containers").get_json()["ignored_updates"] == []


def test_read_only_blocks_the_rewrite_but_not_ignoring(stack, monkeypatch):
    monkeypatch.setattr(stack.env.app, "READ_ONLY", True)
    assert choose(stack, "newer").status_code == 403
    assert choose(stack, "latest").status_code == 403
    assert open(stack.file).read() == COMPOSE_TEXT
    assert choose(stack, "ignore").status_code == 200
    assert stack.c.post("/api/updates/ignored/autobrr/restore", headers={"X-CSRF-Token": stack.csrf}).status_code == 200


def test_ignore_hides_the_notice_until_a_newer_version_and_can_be_undone(stack):
    assert choose(stack, "ignore").status_code == 200
    assert row(stack)["pinned"] is None
    body = stack.c.get("/api/containers").get_json()
    assert body["ignored_updates"] == [{"name": "autobrr", "version": "v1.87.0"}]
    with open(os.path.join(stack.env.data, "ignored_updates.json")) as f:
        assert json.load(f) == {"autobrr": "v1.87.0"}
    # a later release brings the notice back
    stack.env.client.tags["autobrr/autobrr"] = TAGS + ["v1.88.0"]
    stack.env.app.check_registry()
    assert row(stack)["pinned"]["newer"] == "v1.88.0"
    # ... and un-ignoring brings it back too
    stack.env.client.tags["autobrr/autobrr"] = TAGS
    stack.env.app.check_registry()
    assert row(stack)["pinned"] is None
    assert stack.c.post("/api/updates/ignored/autobrr/restore", headers={"X-CSRF-Token": stack.csrf}).status_code == 200
    assert row(stack)["pinned"]["newer"] == "v1.87.0"
    assert stack.c.get("/api/containers").get_json()["ignored_updates"] == []


def test_unignore_needs_csrf_and_an_ignored_container(stack):
    assert stack.c.post("/api/updates/ignored/autobrr/restore").status_code == 403
    assert stack.c.post("/api/updates/ignored/autobrr/restore", headers={"X-CSRF-Token": stack.csrf}).status_code == 404


def test_a_damaged_ignore_file_reads_as_empty(stack):
    with open(os.path.join(stack.env.data, "ignored_updates.json"), "w") as f:
        f.write("{nope")
    assert stack.env.app.load_ignored() == {}
    assert row(stack)["pinned"] is not None


def test_a_schedule_never_changes_the_tag(stack):
    from test_schedules import SLOT, seed

    seed(stack.env, containers=["autobrr"], action="update")
    stack.env.app.run_due_schedules(SLOT + 5)
    assert stack.calls == []
    assert open(stack.file).read() == COMPOSE_TEXT


def test_settings_has_the_ignored_list_for_admins_only(stack):
    from test_users import make_user, sign_in

    assert b'id="ignored-updates"' in stack.c.get("/").data
    make_user(stack.env, stack.c, stack.csrf)
    o, _ = sign_in(stack.env, "op", "operator-pass")
    assert b'id="ignored-updates"' not in o.get("/").data
