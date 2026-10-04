import json
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from conftest import FakeContainer

import activity
import handlers
import integrations

ADMIN = "adminkey-AAAA1111"
CLIENT = "clientkey-CCCC2222"
U1 = "ead7703d-f2d0-4c11-8064-c988a3f5ca94"
U2 = "f4fd2c40-b5f1-4725-be17-0b3d73d2855e"
PANEL_IMAGE = "ghcr.io/pelican/panel:latest"
GAME = {"ContainerType": "server_process"}


def alloc(i, ip, port, **extra):
    return {"attributes": {"id": i, "ip": ip, "port": port, **extra}}


# what the panel's API says (shapes follow the Pterodactyl-style API Pelican uses)
APP_SERVERS = [
    {"attributes": {"uuid": U1, "name": "Valheim", "allocation": 2, "limits": {"disk": 10240},
                    "relationships": {"allocations": {"data": [alloc(1, "0.0.0.0", 2000), alloc(2, "0.0.0.0", 25565)]}}}},
    {"attributes": {"uuid": U2, "name": "Palworld", "allocation": 3, "limits": {"disk": 0},
                    "relationships": {"allocations": {"data": [alloc(3, "10.0.0.7", 8211)]}}}},
]
CLIENT_SERVERS = [
    {"attributes": {"uuid": U1, "name": "Valheim (mine)", "limits": {"disk": 5120},
                    "relationships": {"allocations": {"data": [alloc(1, "0.0.0.0", 2000, is_default=False), alloc(2, "0.0.0.0", 25565, is_default=True)]}}}},
]
STATES = {U1: "running", U2: "offline"}
DISK_USED = {U1: 2 * 1024**3, U2: 123456789}  # bytes, as the panel's resources call reports them


class FakePelican:
    def __init__(self):
        self.gets = []
        self.posts = []
        self.pages = 1
        self.state_status = {}  # uuid -> AppError message to raise
        self.reject = set()  # key values the panel refuses

    def get(self, url, header, key, timeout=5):
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert header == "Authorization" and key.startswith("Bearer "), (header, key)
        token = key[len("Bearer "):]
        self.gets.append((parts.path, q, token))
        if token in self.reject or token not in (ADMIN, CLIENT):
            raise integrations.AppError("the app rejected the key")
        path = parts.path
        if path == "/api/application/servers":
            assert token == ADMIN, "application endpoint used with the client key"
            assert q.get("include") == "allocations" or q["per_page"] == "1"  # the key check needs no allocations
            return self.paged(APP_SERVERS, q)
        if path == "/api/client":
            assert token == CLIENT, "client endpoint used with the admin key"
            return self.paged(CLIENT_SERVERS, q)
        if path.startswith("/api/client/servers/") and path.endswith("/resources"):
            assert token == CLIENT
            uuid = path.split("/")[4]
            if uuid in self.state_status:
                raise integrations.AppError(self.state_status[uuid])
            return {"attributes": {"current_state": STATES[uuid], "resources": {"disk_bytes": DISK_USED[uuid], "memory_bytes": 0}}}
        raise integrations.AppError("the app answered HTTP 404")

    def paged(self, rows, q):
        page, per = int(q.get("page", 1)), int(q["per_page"])
        assert per in (1, 100)  # 1 is a key check or an address probe
        if self.pages > 1 and per == 100:  # pretend there are 2 pages, one server on each
            chunk = rows[page - 1:page]
        else:
            chunk = rows
        return {"data": chunk, "meta": {"pagination": {"current_page": page, "total_pages": self.pages}}}

    def send(self, opener, req):
        assert req.get_method() == "POST"
        self.posts.append((req.full_url, json.loads(req.data), req.headers.get("Authorization")))
        return 204, b""


@pytest.fixture
def pel(env, monkeypatch):
    fake = FakePelican()
    monkeypatch.setattr(integrations, "http_get_json", fake.get)
    monkeypatch.setattr(handlers, "_send", fake.send)
    env.client.add(FakeContainer("pelican-panel", image_ref=PANEL_IMAGE))
    env.client.add(FakeContainer(U1, labels=GAME, image_ref="ghcr.io/pelican-dev/yolks:games"))
    env.client.add(FakeContainer(U2, status="exited", labels=GAME, image_ref="ghcr.io/pelican-dev/yolks:games"))
    env.client.add(FakeContainer("web", image_ref="nginx"))
    return fake


def put_keys(c, csrf, keys):
    return c.put("/api/integrations/pelican-panel/key", json={"keys": keys}, headers={"X-CSRF-Token": csrf})


def card(c):
    return next(i for i in c.get("/api/integrations").get_json()["items"] if i["container"] == "pelican-panel")


def row(c, name):
    return next(i for i in c.get("/api/containers").get_json()["items"] if i["name"] == name)


def everything():
    return json.dumps(activity.snapshot()[0])


# ---- the adapter and its spec -------------------------------------------------

def test_the_shipped_adapter_loads_and_keys_kind_needs_a_handler(env):
    spec = env.app.ADAPTERS["pelican"]
    assert spec["auth"]["kind"] == "keys" and [k["id"] for k in spec["auth"]["keys"]] == ["application", "client"]
    assert env.app.ADAPTER_PROBLEMS == []
    raw = yaml.safe_load(open(env.app.os.path.join(env.app.BUILTIN_ADAPTERS, "pelican.yml")))
    del raw["handler"]
    with pytest.raises(integrations.SpecError, match="needs a handler"):
        integrations.validate_spec(raw, trusted=True)


def test_a_user_adapter_cannot_use_keys_to_reach_a_handler(env):
    raw = yaml.safe_load(open(env.app.os.path.join(env.app.BUILTIN_ADAPTERS, "pelican.yml")))
    with pytest.raises(integrations.SpecError, match="only available to adapters shipped"):
        integrations.validate_spec(raw, trusted=False, handlers=handlers.HANDLERS)


@pytest.mark.parametrize("keys, msg", [
    ([], "1-4 keys"),
    ([{"id": "a", "label": "A"}, {"id": "a", "label": "B"}], "unique"),
    ([{"id": "Bad Id", "label": "A"}], "lowercase"),
    (["nope"], "mapping"),
])
def test_key_set_rules(env, keys, msg):
    raw = yaml.safe_load(open(env.app.os.path.join(env.app.BUILTIN_ADAPTERS, "pelican.yml")))
    raw["auth"]["keys"] = keys
    with pytest.raises(integrations.SpecError, match=msg):
        integrations.validate_spec(raw, trusted=True)


@pytest.mark.parametrize("ref, hit", [
    ("ghcr.io/pelican/panel:latest", True),
    ("ghcr.io/pelican-dev/panel:v1", True),
    ("pelican/panel", True),
    ("localhost:5000/pelican/panel:1", True),
    ("ghcr.io/pterodactyl/panel:latest", False),
    ("panel", False),
    ("ghcr.io/someone/notpelican/panel", False),
])
def test_matching_uses_the_repository_for_slash_entries(env, ref, hit):
    found = integrations.match_adapter(env.app.ADAPTERS, {}, ref)
    assert (found is not None and found["id"] == "pelican") is hit


# ---- keys: entered by hand, each optional, checked before they are stored ---------

def test_card_asks_for_keys_and_never_offers_a_search(env, logged_in, pel):
    c, _ = logged_in
    it = card(c)
    assert it["state"] == "found" and it["auth_kind"] == "keys" and it["key_file"] is None
    assert [(k["id"], k["set"]) for k in it["auth_keys"]] == [("application", False), ("client", False)]
    assert pel.gets == []  # nothing was called or read


def test_admin_key_alone_is_checked_and_stored(env, logged_in, pel):
    c, csrf = logged_in
    assert put_keys(c, csrf, {"application": ADMIN, "client": ""}).status_code == 200
    assert [p for p, _, t in pel.gets if t == CLIENT] == []  # the blank one was not tested
    it = card(c)
    assert it["state"] == "configured" and [k["set"] for k in it["auth_keys"]] == [True, False]
    assert [r["title"] for r in it["panels"][0]["rows"]] == ["Palworld", "Valheim"]


def test_second_key_can_be_added_later_and_is_merged(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN})
    assert put_keys(c, csrf, {"client": CLIENT}).status_code == 200
    stored = json.load(open(env.app.INTEGRATIONS_PATH))["keys"]["pelican-panel"]
    assert stored == {"application": ADMIN, "client": CLIENT}
    assert all(k["set"] for k in card(c)["auth_keys"])


def test_a_refused_key_is_named_and_nothing_is_stored(env, logged_in, pel):
    c, csrf = logged_in
    r = put_keys(c, csrf, {"application": ADMIN, "client": "wrong-client-key"})
    assert r.status_code == 400 and r.get_json()["error"] == "user key: the app rejected the key"
    assert card(c)["state"] == "found"
    assert "wrong-client-key" not in json.dumps(r.get_json()) and "wrong-client-key" not in everything()


@pytest.mark.parametrize("keys", [{}, {"application": "  "}, {"nonsense": ADMIN}, "x", None])
def test_at_least_one_known_key_is_needed(env, logged_in, pel, keys):
    c, csrf = logged_in
    assert put_keys(c, csrf, keys).status_code == 400
    assert card(c)["state"] == "found"


def test_keys_are_never_sent_to_the_browser_or_logged(env, logged_in, pel):
    c, csrf = logged_in
    activity.set_level("verbose")
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    body = c.get("/api/integrations").get_data(as_text=True) + c.get("/api/containers").get_data(as_text=True)
    assert ADMIN not in body and CLIENT not in body
    assert ADMIN not in everything() and CLIENT not in everything()


def test_removing_forgets_both_keys(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    assert c.delete("/api/integrations/pelican-panel/key", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert card(c)["state"] == "found"
    assert row(c, U1)["game"]["name"] is None  # the cached names went with the keys


def test_key_endpoints_respect_read_only_and_csrf(env, logged_in, pel, monkeypatch):
    c, csrf = logged_in
    assert c.put("/api/integrations/pelican-panel/key", json={"keys": {"client": CLIENT}}).status_code == 403
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert put_keys(c, csrf, {"client": CLIENT}).status_code == 403


# ---- the panel ----------------------------------------------------------------------

def test_panel_lists_servers_with_ports_and_wings_state(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    panel = card(c)["panels"][0]
    assert panel["title"] == "Game servers" and panel["total"] == 2 and panel["summary"] == "1 running"
    assert panel["power"] is True
    assert panel["rows"] == [
        {"title": "Palworld", "detail": "10.0.0.7:8211 · offline", "server": U2, "state": "offline", "disk": {"used": 123456789, "limit": None}},
        {"title": "Valheim", "detail": "port 25565 · running", "server": U1, "state": "running", "disk": {"used": 2 * 1024**3, "limit": 10240 * 1024**2}},
    ]


def test_disk_use_needs_the_user_key_and_the_allocation_comes_with_either(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN})
    rows = {r["title"]: r for r in card(c)["panels"][0]["rows"]}
    assert rows["Valheim"]["disk"] == {"used": None, "limit": 10240 * 1024**2}
    assert "disk" not in rows["Palworld"]  # unlimited, and nothing known about use


def test_the_user_keys_own_list_gives_its_allocation(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"client": CLIENT})
    row_ = card(c)["panels"][0]["rows"][0]
    assert row_["disk"] == {"used": 2 * 1024**3, "limit": 5120 * 1024**2}


@pytest.mark.parametrize("bad", [-5, "10", True, None, [1]])
def test_odd_disk_values_are_ignored(bad):
    assert handlers._disk_limit({"limits": {"disk": bad}}) is None
    assert handlers._number(bad) is None


def test_admin_key_only_has_names_and_ports_but_no_state(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN})
    panel = card(c)["panels"][0]
    assert panel["rows"][1] == {"title": "Valheim", "detail": "port 25565", "server": U1, "state": None, "disk": {"used": None, "limit": 10240 * 1024**2}}
    assert "summary" not in panel and panel["power"] is False  # no user key: nothing to send power with
    assert not [g for g in pel.gets if g[0].endswith("/resources")]


def test_client_key_only_uses_its_own_server_list(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"client": CLIENT})
    panel = card(c)["panels"][0]
    assert panel["rows"] == [{"title": "Valheim (mine)", "detail": "port 25565 · running", "server": U1, "state": "running", "disk": {"used": 2 * 1024**3, "limit": 5120 * 1024**2}}]
    assert not [g for g in pel.gets if g[0].startswith("/api/application")]


def test_every_page_is_read_by_number(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN})
    pel.pages = 2
    pel.gets.clear()
    env.app._panel_cache.clear()
    assert card(c)["panels"][0]["total"] == 2
    assert [q["page"] for p, q, _ in pel.gets if p == "/api/application/servers"] == ["1", "2"]


def test_a_server_the_client_key_cannot_see_is_only_missing_its_state(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    pel.state_status[U2] = "the app answered HTTP 404"
    env.app._panel_cache.clear()
    handlers.pelican_forget()
    panel = card(c)["panels"][0]
    assert panel["rows"][0] == {"title": "Palworld", "detail": "10.0.0.7:8211", "server": U2, "state": None}
    assert panel["summary"] == "1 running"


def test_when_every_state_call_fails_the_panel_says_so(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    pel.state_status.update({U1: "the app rejected the key", U2: "the app rejected the key"})
    env.app._panel_cache.clear()
    assert card(c)["panels"][0]["summary"] == "state unavailable"


# ---- the container rows -------------------------------------------------------------

def test_game_server_rows_get_name_port_and_state_from_pelican(env, logged_in, pel):
    c, csrf = logged_in
    assert row(c, U1)["game"] == {"name": None, "connect": None, "state": None}  # a game server, but no Pelican yet
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    handlers.pelican_forget()
    assert row(c, U1)["game"] == {"name": "Valheim", "connect": "port 25565", "state": "running", "disk_limit": 10240 * 1024**2, "disk_used": 2 * 1024**3}
    assert row(c, U2)["game"]["state"] == "offline"
    assert row(c, "web")["game"] is None and row(c, "pelican-panel")["game"] is None


def test_the_list_never_waits_for_pelican_after_the_first_answer(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    row(c, U1)
    pel.gets.clear()
    row(c, U1)
    row(c, U2)
    assert pel.gets == []  # fresh enough: served from the last answer


def test_pelican_is_asked_again_when_the_answer_is_stale(env, logged_in, pel, monkeypatch):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    row(c, U1)
    pel.gets.clear()
    handlers._pelican["at"] -= env.app.PELICAN_STALE + 1
    row(c, U1)
    assert pel.gets


def test_an_answer_that_is_too_old_is_not_shown(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    row(c, U1)
    handlers._pelican["at"] -= 500
    pel.reject.update({ADMIN, CLIENT})  # the panel has gone bad
    assert row(c, U1)["game"]["name"] is None


def test_an_unreachable_panel_does_not_break_the_container_list(env, logged_in, pel, monkeypatch):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    handlers.pelican_forget()

    def down(url, header, key, timeout=5):
        raise integrations.AppError("could not reach the app")

    monkeypatch.setattr(integrations, "http_get_json", down)
    r = c.get("/api/containers")
    assert r.status_code == 200 and len(r.get_json()["items"]) == 4


def test_no_pelican_calls_without_game_servers_or_keys(env, logged_in, pel):
    c, csrf = logged_in
    row(c, "web")
    assert pel.gets == []
    put_keys(c, csrf, {"application": ADMIN})
    env.client.by_name.pop(U1)
    env.client.by_name.pop(U2)
    handlers.pelican_forget()
    pel.gets.clear()
    c.get("/api/containers")
    assert pel.gets == []  # no game server, so no reason to ask


# ---- power goes through the panel ------------------------------------------------------

def act(c, csrf, name, action):
    return c.post(f"/api/containers/{name}/{action}", headers={"X-CSRF-Token": csrf})


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_power_goes_through_pelican_with_the_client_key(env, logged_in, pel, action):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    assert act(c, csrf, U1, action).status_code == 200
    assert pel.posts == [(f"http://172.20.0.5:80/api/client/servers/{U1}/power", {"signal": action}, f"Bearer {CLIENT}")]
    assert env.client.by_name[U1].calls == []  # Docker was not touched
    assert any(f"{action} {U1} through Pelican" in m["msg"] for m in activity.snapshot()[0])


def test_power_posts_to_one_address_only(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"client": CLIENT})
    panel = env.client.by_name["pelican-panel"]
    panel.attrs["NetworkSettings"]["Networks"] = {"a": {"IPAddress": "172.20.0.5"}, "b": {"IPAddress": "172.21.0.5"}}
    act(c, csrf, U1, "restart")
    assert len(pel.posts) == 1


def test_power_state_is_refreshed_after_an_action(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    row(c, U1)
    act(c, csrf, U1, "stop")
    pel.gets.clear()
    row(c, U1)
    assert pel.gets  # asked again straight away, not after the usual wait


def test_without_a_client_key_power_falls_back_to_docker_and_says_so(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN})
    assert act(c, csrf, U1, "stop").status_code == 200
    assert pel.posts == [] and env.client.by_name[U1].calls == ["stop"]
    assert any("no Pelican user key" in m["msg"] for m in activity.snapshot()[0])


def test_without_pelican_at_all_a_game_server_is_controlled_through_docker(env, logged_in, pel):
    c, csrf = logged_in
    assert act(c, csrf, U2, "start").status_code == 200
    assert env.client.by_name[U2].calls == ["start"]


def test_ordinary_containers_never_go_through_pelican(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    act(c, csrf, "web", "restart")
    assert pel.posts == [] and env.client.by_name["web"].calls == ["restart"]


def test_a_game_container_not_named_by_uuid_falls_back_to_docker(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    env.client.add(FakeContainer("not-a-uuid", labels=GAME))
    act(c, csrf, "not-a-uuid", "stop")
    assert pel.posts == [] and env.client.by_name["not-a-uuid"].calls == ["stop"]


@pytest.mark.parametrize("status, msg", [
    (403, "Pelican refused: the user key can't control this server"),
    (404, "Pelican doesn't know this server for that user key"),
    (500, "Pelican answered HTTP 500"),
])
def test_a_refusal_from_pelican_is_reported_without_touching_docker(env, logged_in, pel, monkeypatch, status, msg):
    c, csrf = logged_in
    put_keys(c, csrf, {"client": CLIENT})
    monkeypatch.setattr(handlers, "_send", lambda opener, req: (status, b""))
    r = act(c, csrf, U1, "stop")
    assert r.status_code == 502 and r.get_json()["error"] == msg
    assert env.client.by_name[U1].calls == []
    assert CLIENT not in r.get_data(as_text=True) and CLIENT not in everything()


def test_power_needs_sign_in_csrf_and_is_blocked_in_read_only(env, logged_in, pel, monkeypatch):
    c, csrf = logged_in
    put_keys(c, csrf, {"client": CLIENT})
    assert c.post(f"/api/containers/{U1}/stop").status_code == 403
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert act(c, csrf, U1, "stop").status_code == 403
    assert pel.posts == []


def test_pelican_power_refuses_odd_input(env):
    creds = {"client": CLIENT}
    for uuid, signal in ((U1, "kill"), (U1, "../x"), ("../../etc", "stop"), ("not-a-uuid", "stop")):
        with pytest.raises(integrations.AppError):
            handlers.pelican_power(["http://x"], creds, uuid, signal)
    with pytest.raises(integrations.AppError, match="no Pelican user key"):
        handlers.pelican_power(["http://x"], {"application": ADMIN}, U1, "stop")


def test_the_panel_shows_the_new_state_right_after_a_power_action(env, logged_in, pel):
    c, csrf = logged_in
    put_keys(c, csrf, {"application": ADMIN, "client": CLIENT})
    assert next(r for r in card(c)["panels"][0]["rows"] if r["server"] == U1)["state"] == "running"
    handlers.pelican_forget()
    STATES[U1] = "stopping"
    try:
        assert next(r for r in card(c)["panels"][0]["rows"] if r["server"] == U1)["state"] == "running"  # cached for 30 s
        act(c, csrf, U1, "stop")
        assert next(r for r in card(c)["panels"][0]["rows"] if r["server"] == U1)["state"] == "stopping"
    finally:
        STATES[U1] = "running"
