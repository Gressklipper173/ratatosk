import json
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import FakeContainer
from test_pelican import ADMIN, CLIENT, GAME, PANEL_IMAGE, U1, U2, everything, put_keys

import activity
import editor
import handlers
import integrations

PALWORLD = (
    "[/Script/Pal.PalGameWorldSettings]\r\n"
    'OptionSettings=(ExpRate=1.000000,ServerName="Secret Name",Pvp=False)\r\n'
)
INI = "; settings\n[General]\nName = My Server\nPort=2456\n"


class FakeFiles:
    """A Pelican panel with one file tree per server (listing, reading, writing, power state)."""

    def __init__(self):
        self.tree = {
            "/": {"dirs": [".config", "steamapps", "linux64", "evil_Data"], "files": {"run.sh": "x" * 50, "big.ini": "x" * (300 * 1024), "tiny.ini": "x"}},
            "/.config": {"dirs": [], "files": {"Game.ini": INI, "PalWorldSettings.ini": PALWORLD, "notes.bin": "x" * 40}},
            "/steamapps": {"dirs": [], "files": {"hidden.ini": INI}},
            "/evil_Data": {"dirs": [], "files": {"hidden.ini": INI}},
            "/linux64": {"dirs": [], "files": {}},
        }
        self.state = {U1: "offline", U2: "offline"}
        self.gets, self.posts, self.reads = [], [], []
        self.write_status = 204
        self.read_status = 200

    def get(self, url, header, key, timeout=5):
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        token = key[len("Bearer "):]
        self.gets.append((parts.path, q, token))
        if token not in (ADMIN, CLIENT):
            raise integrations.AppError("the app rejected the key")
        path = parts.path
        if path in ("/api/application/servers", "/api/client"):
            return {"data": [], "meta": {"pagination": {"total_pages": 1}}}
        if path.endswith("/resources"):
            return {"attributes": {"current_state": self.state[path.split("/")[4]]}}
        if path.endswith("/files/list"):
            d = self.tree.get(q["directory"])
            if d is None:
                raise integrations.AppError("the app answered HTTP 404")
            rows = [{"attributes": {"name": n, "is_file": False, "is_symlink": False, "size": 4096}} for n in d["dirs"]]
            rows += [{"attributes": {"name": n, "is_file": True, "is_symlink": False, "size": len(t.encode())}} for n, t in d["files"].items()]
            return {"object": "list", "data": rows}
        raise integrations.AppError("the app answered HTTP 404")

    def send(self, opener, req):
        parts = urlsplit(req.full_url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        directory, _, name = q["file"].rpartition("/")
        files = self.tree[directory or "/"]["files"]
        if req.get_method() == "GET":
            assert parts.path.endswith("/files/contents")
            self.reads.append(q["file"])
            if self.read_status != 200 or name not in files:
                return (self.read_status if self.read_status != 200 else 500), b""
            return 200, files[name].encode()
        assert req.get_method() == "POST" and parts.path.endswith("/files/write")
        self.posts.append((req.full_url, q["file"], req.data.decode(), req.headers.get("Authorization")))
        if self.write_status in (200, 204):
            files[name] = req.data.decode()
        return self.write_status, b""


@pytest.fixture
def fs(env, logged_in, monkeypatch):
    fake = FakeFiles()
    monkeypatch.setattr(integrations, "http_get_json", fake.get)
    monkeypatch.setattr(handlers, "_send", fake.send)
    env.client.add(FakeContainer("pelican-panel", image_ref=PANEL_IMAGE))
    env.client.add(FakeContainer(U1, labels=GAME, image_ref="ghcr.io/pelican-dev/yolks:games"))
    env.client.add(FakeContainer(U2, status="exited", labels=GAME, image_ref="ghcr.io/pelican-dev/yolks:games"))
    env.client.add(FakeContainer("web", image_ref="nginx"))
    c, csrf = logged_in
    assert put_keys(c, csrf, {"application": ADMIN, "client": CLIENT}).status_code == 200
    fake.gets.clear()
    return fake


def url(name, suffix="config"):
    return f"/api/containers/{name}/{suffix}"


def open_file(c, name, path):
    return c.get(url(name, "config/file"), query_string={"path": path})


def save(c, csrf, name, **body):
    return c.put(url(name, "config/file"), json=body, headers={"X-CSRF-Token": csrf})


# ---- listing -------------------------------------------------------------------

def test_lists_only_config_looking_files_and_skips_asset_trees(fs, logged_in):
    c, _ = logged_in
    r = c.get(url(U1))
    assert r.status_code == 200
    body = r.get_json()
    assert [f["path"] for f in body["files"]] == ["/.config/Game.ini", "/.config/PalWorldSettings.ini"]
    assert body["truncated"] is False
    listed = {q["directory"] for p, q, _ in fs.gets if p.endswith("/files/list")}
    assert "/steamapps" not in listed and "/evil_Data" not in listed and "/.config" in listed


def test_listing_is_capped(fs, logged_in, monkeypatch):
    c, _ = logged_in
    monkeypatch.setattr(editor, "MAX_ENTRIES", 3)
    assert c.get(url(U1)).get_json()["truncated"] is True


def test_depth_is_capped(fs, logged_in, monkeypatch):
    c, _ = logged_in
    monkeypatch.setattr(editor, "MAX_DEPTH", 0)
    assert c.get(url(U1)).get_json()["files"] == []


# ---- reading -------------------------------------------------------------------

def test_open_returns_text_hash_and_fields(fs, logged_in):
    c, _ = logged_in
    r = open_file(c, U1, "/.config/Game.ini")
    body = r.get_json()
    assert r.status_code == 200 and body["raw"] == INI and body["hash"] == editor.digest(INI)
    assert body["header"] == ["settings"]
    assert body["format"] == "keyvalue" and {e["key"] for e in body["entries"]} == {"Name", "Port"}


@pytest.mark.parametrize("path", ["/../etc/passwd", "relative.ini", "/a/../b.ini", "", "/x\\y.ini"])
def test_unsafe_paths_never_reach_the_panel(fs, logged_in, path):
    c, _ = logged_in
    assert open_file(c, U1, path).status_code == 400
    assert fs.reads == []


@pytest.mark.parametrize("path", ["/run.sh", "/.config/notes.bin", "/big.ini", "/tiny.ini", "/steamapps/nothere.ini", "/.config/gone.ini"])
def test_only_listed_candidate_files_open(fs, logged_in, path):
    c, _ = logged_in
    r = open_file(c, U1, path)
    assert r.status_code == 502 and "doesn't list" in r.get_json()["error"]
    assert fs.reads == []


def test_a_panel_read_failure_is_a_safe_message(fs, logged_in):
    c, _ = logged_in
    fs.read_status = 403
    r = open_file(c, U1, "/.config/Game.ini")
    assert r.status_code == 502 and "user key can't read" in r.get_json()["error"]


# ---- the offline rule, read-only mode, keys ---------------------------------------

@pytest.mark.parametrize("state", ["running", "starting", "stopping", None])
def test_every_call_needs_the_server_offline(fs, logged_in, state):
    c, csrf = logged_in
    fs.state[U1] = state
    for r in (c.get(url(U1)), open_file(c, U1, "/.config/Game.ini"), save(c, csrf, U1, path="/.config/Game.ini", hash="x", raw="x")):
        assert r.status_code == 409 and "Stop the server" in r.get_json()["error"]
    assert fs.posts == [] and fs.reads == []


def test_read_only_mode_blocks_everything_including_reading(fs, logged_in, env):
    c, csrf = logged_in
    env.app.READ_ONLY = True
    for r in (c.get(url(U1)), open_file(c, U1, "/.config/Game.ini"), save(c, csrf, U1, path="/.config/Game.ini", hash="x", raw="x")):
        assert r.status_code == 403
    assert fs.gets == [] and fs.reads == [] and fs.posts == []


def test_a_user_key_is_required(env, logged_in, monkeypatch):
    c, csrf = logged_in
    fake = FakeFiles()
    monkeypatch.setattr(integrations, "http_get_json", fake.get)
    monkeypatch.setattr(handlers, "_send", fake.send)
    env.client.add(FakeContainer("pelican-panel", image_ref=PANEL_IMAGE))
    env.client.add(FakeContainer(U1, labels=GAME))
    assert put_keys(c, csrf, {"application": ADMIN}).status_code == 200
    r = c.get(url(U1))
    assert r.status_code == 409 and "user key" in r.get_json()["error"]


@pytest.mark.parametrize("name", ["web", "pelican-panel", "not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_only_pelican_game_servers(fs, logged_in, name):
    c, _ = logged_in
    assert c.get(url(name)).status_code == 404


def test_signed_out_is_refused(fs, env):
    r = env.app.app.test_client().get(url(U1))
    assert r.status_code in (302, 401)


# ---- saving --------------------------------------------------------------------

def test_save_changes_only_the_edited_value_and_returns_the_new_state(fs, logged_in):
    c, csrf = logged_in
    opened = open_file(c, U1, "/.config/Game.ini").get_json()
    port = next(e for e in opened["entries"] if e["key"] == "Port")
    r = save(c, csrf, U1, path="/.config/Game.ini", hash=opened["hash"], edits={str(port["line"]): "2457"})
    body = r.get_json()
    assert r.status_code == 200
    assert fs.tree["/.config"]["files"]["Game.ini"] == INI.replace("2456", "2457")
    assert body["hash"] == editor.digest(INI.replace("2456", "2457"))
    assert len(fs.posts) == 1 and fs.posts[0][1] == "/.config/Game.ini" and fs.posts[0][3] == f"Bearer {CLIENT}"


def test_save_a_packed_setting_keeps_crlf(fs, logged_in):
    c, csrf = logged_in
    opened = open_file(c, U1, "/.config/PalWorldSettings.ini").get_json()
    line = next(e for e in opened["entries"] if e["key"] == "OptionSettings")["line"]
    r = save(c, csrf, U1, path="/.config/PalWorldSettings.ini", hash=opened["hash"], edits={str(line): {"ExpRate": "2.000000"}})
    assert r.status_code == 200
    assert fs.tree["/.config"]["files"]["PalWorldSettings.ini"] == PALWORLD.replace("1.000000", "2.000000")


def test_save_raw_text(fs, logged_in):
    c, csrf = logged_in
    opened = open_file(c, U1, "/.config/Game.ini").get_json()
    r = save(c, csrf, U1, path="/.config/Game.ini", hash=opened["hash"], raw="Port=1\n")
    assert r.status_code == 200 and fs.tree["/.config"]["files"]["Game.ini"] == "Port=1\n"


def test_nothing_is_written_when_nothing_changed(fs, logged_in):
    c, csrf = logged_in
    opened = open_file(c, U1, "/.config/Game.ini").get_json()
    assert save(c, csrf, U1, path="/.config/Game.ini", hash=opened["hash"], edits={}).status_code == 200
    assert fs.posts == []


def test_a_file_changed_since_it_was_opened_is_refused(fs, logged_in):
    c, csrf = logged_in
    opened = open_file(c, U1, "/.config/Game.ini").get_json()
    fs.tree["/.config"]["files"]["Game.ini"] = INI + "Extra=1\n"
    r = save(c, csrf, U1, path="/.config/Game.ini", hash=opened["hash"], raw="Port=1\n")
    assert r.status_code == 409 and "changed on disk" in r.get_json()["error"]
    assert fs.posts == []


@pytest.mark.parametrize("body", [
    {"path": "/.config/Game.ini", "hash": "x"},
    {"path": "/.config/Game.ini", "hash": "x", "edits": "no"},
    {"path": "/../x", "hash": "x", "raw": "x"},
])
def test_bad_save_requests(fs, logged_in, body):
    c, csrf = logged_in
    opened = open_file(c, U1, "/.config/Game.ini").get_json()
    if body.get("hash") == "x":
        body = {**body, "hash": opened["hash"]}
    assert save(c, csrf, U1, **body).status_code == 400
    assert fs.posts == []


def test_an_edit_that_would_break_the_file_is_refused(fs, logged_in):
    c, csrf = logged_in
    opened = open_file(c, U1, "/.config/Game.ini").get_json()
    r = save(c, csrf, U1, path="/.config/Game.ini", hash=opened["hash"], edits={"3": "1\nInjected=1"})
    assert r.status_code == 400 and fs.posts == []


def test_save_needs_csrf(fs, logged_in):
    c, csrf = logged_in
    r = c.put(url(U1, "config/file"), json={"path": "/.config/Game.ini", "hash": "x", "raw": "x"})
    assert r.status_code == 403 and fs.posts == []


def test_a_panel_write_failure_is_reported(fs, logged_in):
    c, csrf = logged_in
    opened = open_file(c, U1, "/.config/Game.ini").get_json()
    fs.write_status = 500
    r = save(c, csrf, U1, path="/.config/Game.ini", hash=opened["hash"], raw="Port=1\n")
    assert r.status_code == 502 and "HTTP 500" in r.get_json()["error"]


def test_the_write_goes_to_one_address_only(fs, logged_in, env, monkeypatch):
    c, csrf = logged_in
    monkeypatch.setattr(integrations, "candidate_urls", lambda attrs, port: ["http://a:80", "http://b:80", "http://c:80"])
    opened = open_file(c, U1, "/.config/Game.ini").get_json()
    assert save(c, csrf, U1, path="/.config/Game.ini", hash=opened["hash"], raw="Port=1\n").status_code == 200
    assert len(fs.posts) == 1


# ---- nothing secret leaks ---------------------------------------------------------

def test_file_contents_and_paths_never_reach_the_debug_log(fs, logged_in, env):
    c, csrf = logged_in
    activity.set_level("verbose")  # the most the log ever records
    opened = open_file(c, U1, "/.config/PalWorldSettings.ini").get_json()
    line = next(e for e in opened["entries"] if e["key"] == "OptionSettings")["line"]
    save(c, csrf, U1, path="/.config/PalWorldSettings.ini", hash=opened["hash"], edits={str(line): {"ServerName": "Brand New Name"}})
    log = everything()
    for secret in ("Secret Name", "Brand New Name", "OptionSettings", "PalWorldSettings", CLIENT):
        assert secret not in log
    assert "Secret Name" not in c.get("/api/debug").get_data(as_text=True)
    assert "editor" in log  # it did log that it opened and saved
