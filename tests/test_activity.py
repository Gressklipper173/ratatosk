import io
import json
import time
import urllib.error

import pytest
from conftest import FakeContainer, post_login

import activity
import handlers
import integrations

SECRET = "goodkey123"


def lines():
    return [e["msg"] for e in activity.snapshot()[0]]


def everything():
    return json.dumps(activity.snapshot()[0])


# ---- the buffer ---------------------------------------------------------------

def test_entry_count_is_capped(monkeypatch):
    monkeypatch.setattr(activity, "MAX_ENTRIES", 5)
    for i in range(20):
        activity.log("info", "t", f"line {i}")
    assert lines() == [f"line {i}" for i in range(15, 20)]


def test_total_bytes_are_capped(monkeypatch):
    monkeypatch.setattr(activity, "MAX_BYTES", 2000)
    for i in range(200):
        activity.log("info", "t", f"{i} " + "x" * 100)
    assert activity._bytes <= 2000
    assert lines()[-1].startswith("199 ")


def test_a_long_message_and_traceback_are_cut():
    try:
        raise ValueError("boom " * 5000)
    except ValueError:
        activity.log("error", "t", "y" * 5000, exc=True)
    msg = lines()[0]
    assert len(msg) <= activity.MAX_MESSAGE + activity.MAX_TRACEBACK + 1


def test_control_characters_are_flattened():
    activity.log("info", "t", "a\x1b[31mred\r\nsecond")
    assert "\x1b" not in lines()[0] and "\r" not in lines()[0]


def test_a_repeated_line_is_counted_not_repeated():
    for _ in range(50):
        activity.log("warn", "t", "same thing")
    rows, _ = activity.snapshot()
    assert len(rows) == 1 and rows[0]["count"] == 50


def test_repeat_gets_a_new_seq_but_keeps_its_id():
    activity.log("warn", "t", "same")
    first, _ = activity.snapshot()
    activity.log("warn", "t", "same")
    second, _ = activity.snapshot(since=first[0]["seq"])
    assert second[0]["id"] == first[0]["id"] and second[0]["count"] == 2


def test_verbose_lines_only_at_verbose_level():
    activity.log("info", "t", "chatty", verbose=True)
    assert lines() == []
    activity.set_level("verbose")
    activity.log("info", "t", "chatty", verbose=True)
    assert "chatty" in lines()


def test_unknown_level_is_refused():
    with pytest.raises(ValueError):
        activity.set_level("trace")


def test_safe_url_drops_query_and_userinfo():
    assert activity.safe_url("http://u:p@10.0.0.2:8989/api/v3/x?apikey=SECRETVALUE&a=1") == "http://10.0.0.2:8989/api/v3/x"


# ---- redaction ----------------------------------------------------------------

def test_a_registered_secret_never_reaches_a_message_or_traceback():
    activity.set_secret_source(lambda: ("hunter2-password",))
    try:
        raise RuntimeError("failed with hunter2-password inside")
    except RuntimeError:
        activity.log("error", "t", "login hunter2-password failed", exc=True)
    assert "hunter2-password" not in everything()
    assert "***" in everything()


def test_python_logging_is_captured_and_redacted():
    import logging

    activity.capture_python_logging()
    activity.set_secret_source(lambda: ("hunter2-password",))
    logging.getLogger("something.else").warning("saw hunter2-password")
    assert any("saw ***" in m for m in lines())
    activity.capture_python_logging()  # twice must not double up
    n = len(lines())
    logging.getLogger("something.else").warning("once")
    assert len(lines()) == n + 1


# ---- the adapter calls themselves -----------------------------------------------

class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = io.BytesIO(body)
        self.closed = False

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        return False


def fake_network(monkeypatch, fn):
    """Every request the integrations make goes through make_opener(); `fn(req)` answers it
    (a FakeResponse) or raises what urllib would."""

    class Opener:
        def open(self, req, timeout=None):
            return fn(req)

    monkeypatch.setattr(integrations, "make_opener", lambda *extra: Opener())


def http_error(req, code):
    return urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))


def test_adapter_request_logs_status_and_time_but_not_key_or_body(monkeypatch):
    fake_network(monkeypatch, lambda req: FakeResponse(200, b'{"token": "reply-body-marker"}'))
    activity.set_level("verbose")
    integrations.http_get_json("http://10.0.0.2:8989/api/v3/x?apikey=querymarker", "X-Api-Key", SECRET)
    text = everything()
    assert "GET http://10.0.0.2:8989/api/v3/x -> HTTP 200" in text and " ms" in text
    for marker in (SECRET, "reply-body-marker", "querymarker"):
        assert marker not in text


def test_unexpected_status_is_logged_at_normal_level(monkeypatch):
    def boom(req):
        raise http_error(req, 500)

    fake_network(monkeypatch, boom)
    with pytest.raises(integrations.AppError):
        integrations.http_get_json("http://10.0.0.2:8989/x", "X-Api-Key", SECRET)
    assert any("HTTP 500" in m for m in lines())


def test_unreachable_is_only_a_verbose_line(monkeypatch):
    def down(req):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    fake_network(monkeypatch, down)
    with pytest.raises(integrations.AppError):
        integrations.http_get_json("http://10.0.0.2:8989/x", "X-Api-Key", SECRET)
    assert lines() == []
    activity.set_level("verbose")
    with pytest.raises(integrations.AppError):
        integrations.http_get_json("http://10.0.0.2:8989/x", "X-Api-Key", SECRET)
    assert any("ConnectionRefusedError" in m for m in lines())


def test_all_addresses_failing_is_logged_at_normal_level():
    def fn(b):
        raise integrations.AppError("could not reach the app")

    with pytest.raises(integrations.AppError):
        integrations.first_ok(["http://a", "http://b"], fn)
    assert any("none of 2 address(es) worked" in m for m in lines())


def test_qbittorrent_send_logs_status_without_credentials():
    class Opener:
        def open(self, req, timeout=None):
            return FakeResponse(204, b"")

    import urllib.request

    req = urllib.request.Request("http://10.0.0.2:8080/api/v2/auth/login", data=b"username=u&password=pw-marker")
    activity.set_level("verbose")
    assert handlers._send(Opener(), req) == (204, b"")
    text = everything()
    assert "POST http://10.0.0.2:8080/api/v2/auth/login -> HTTP 204" in text
    assert "pw-marker" not in text


def test_qbittorrent_login_step_is_logged_at_normal_level(monkeypatch):
    monkeypatch.setattr(handlers, "_send", lambda opener, req: (204, b"") if "login" in req.full_url else (200, b"v5"))
    handlers._qbt_login("http://10.0.0.2:8080", {"username": "u", "password": "pw-marker"})
    assert any("login at http://10.0.0.2:8080: HTTP 204" in m for m in lines())
    assert "pw-marker" not in everything()


# ---- the routes ---------------------------------------------------------------

def test_debug_requires_sign_in(env):
    c = env.app.app.test_client()
    assert c.get("/api/debug").status_code == 302
    assert c.post("/api/debug/level", json={"level": "verbose"}).status_code == 302


def test_debug_returns_entries_and_only_newer_ones(env, logged_in):
    c, csrf = logged_in
    body = c.get("/api/debug").get_json()
    assert body["level"] == "normal" and body["levels"] == ["normal", "verbose"]
    assert any("first admin created" in e["msg"] for e in body["entries"])
    activity.log("info", "t", "later line")
    newer = c.get(f"/api/debug?since={body['last']}").get_json()
    assert [e["msg"] for e in newer["entries"]] == ["later line"]


def test_level_change_needs_csrf_and_a_known_level(env, logged_in):
    c, csrf = logged_in
    assert c.post("/api/debug/level", json={"level": "verbose"}).status_code == 403
    h = {"X-CSRF-Token": csrf}
    assert c.post("/api/debug/level", json={"level": "trace"}, headers=h).status_code == 400
    assert c.post("/api/debug/level", json={}, headers=h).status_code == 400
    assert c.post("/api/debug/level", json={"level": "verbose"}, headers=h).get_json()["level"] == "verbose"
    assert activity.get_level() == "verbose"


def test_debug_is_available_in_read_only_mode(env, logged_in, monkeypatch):
    c, csrf = logged_in
    monkeypatch.setattr(env.app, "READ_ONLY", True)
    assert c.get("/api/debug").status_code == 200
    assert c.post("/api/debug/level", json={"level": "verbose"}, headers={"X-CSRF-Token": csrf}).status_code == 200


def test_setup_token_never_enters_the_log(env):
    token = env.app.setup_token
    env.app.activity.log("info", "t", f"oops {token}")
    assert token not in everything()


def test_the_page_has_the_debug_button(env, logged_in):
    c, _ = logged_in
    assert b'id="debug-btn"' in c.get("/").data


def test_unhandled_error_is_logged_with_traceback_and_hides_details(env, logged_in, monkeypatch):
    c, _ = logged_in

    def broken():
        raise RuntimeError("kaboom-detail")

    monkeypatch.setattr(env.app, "empty_groups", broken)
    env.app.app.config["TESTING"] = False
    r = c.get("/api/containers")
    assert r.status_code == 500
    assert "kaboom-detail" not in r.get_data(as_text=True)
    assert any("RuntimeError: kaboom-detail" in m and "Traceback" in m for m in lines())


def test_sign_in_failures_and_successes_are_logged(env, logged_in):
    c = env.app.app.test_client()
    post_login(c, "admin", "wrong-password-marker")
    post_login(c, "admin", "correct-horse")
    text = everything()
    assert "failed sign-in" in text and "admin signed in" in text
    assert "wrong-password-marker" not in text and "correct-horse" not in text


# ---- end to end: stored credentials never show up -----------------------------

def test_storing_and_using_a_key_leaves_no_trace_of_it(env, logged_in, monkeypatch):
    c, csrf = logged_in
    env.client.add(FakeContainer("sonarr", image_ref="lscr.io/linuxserver/sonarr:latest"))

    def fake_open(req):
        if req.headers.get("X-api-key") != SECRET:
            raise http_error(req, 401)
        if "/system/status" in req.full_url:
            return FakeResponse(200, b'{"appName": "Sonarr"}')
        return FakeResponse(200, b'{"totalRecords": 0, "records": []}')

    fake_network(monkeypatch, fake_open)
    activity.set_level("verbose")
    h = {"X-CSRF-Token": csrf}
    c.put("/api/integrations/sonarr/key", json={"key": "wrongkey999"}, headers=h)
    assert c.put("/api/integrations/sonarr/key", json={"key": SECRET}, headers=h).status_code == 200
    c.get("/api/integrations")
    c.delete("/api/integrations/sonarr/key", headers=h)
    text = everything()
    assert "credentials for sonarr tested and stored" in text and "credentials for sonarr removed" in text
    assert SECRET not in text and "wrongkey999" not in text


# ---- redaction, hardened ---------------------------------------------------------------

def test_url_encoded_and_json_escaped_secrets_are_masked_too():
    import json as _json
    from urllib.parse import quote, quote_plus

    secret = 's3cret pass&"x"/é'
    activity.set_secret_source(lambda: (secret,))
    for form in (secret, quote_plus(secret), quote(secret), _json.dumps(secret)[1:-1], _json.dumps(secret, ensure_ascii=False)[1:-1]):
        assert activity.redact(f"a {form} b") == "a *** b"
    activity.log("info", "t", "username=admin&password=" + quote_plus(secret))
    assert "s3cret" not in everything()


def test_a_secret_inside_another_does_not_leave_the_tail_of_the_longer_one():
    for order in (("abcd", "abcdefgh"), ("abcdefgh", "abcd")):
        activity.set_secret_source(lambda order=order: order)
        assert activity.redact("x abcdefgh y") == "x *** y"


def test_a_secret_source_that_fails_does_not_break_logging_and_keeps_the_last_known_secrets():
    state = {"fail": False}

    def source():
        if state["fail"]:
            raise OSError("unreadable store")
        return ("hunter2-password",)

    activity.set_secret_source(source)
    activity.log("info", "t", "first")
    state["fail"] = True
    activity.log("info", "t", "again hunter2-password")  # must not raise
    assert "hunter2-password" not in everything() and "again ***" in lines()[-1]
    activity.set_secret_source(lambda: None)  # not even iterable
    activity.log("info", "t", "still works")


def test_stderr_gets_one_flat_line_per_entry(capsys):
    activity.log("error", "adapter", "bad file\n[auth] info: admin signed in\r\x1b[2J")
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "\x1b" not in err and "\r" not in err


def test_snapshot_with_a_limit_of_zero_is_empty():
    activity.log("info", "t", "one")
    assert activity.snapshot(limit=0)[0] == []
    assert len(activity.snapshot(limit=1)[0]) == 1


def test_a_boolean_total_is_not_a_count():
    panel = {"id": "p", "title": "P", "rows": "data", "row_title": "{n}", "total": "n"}
    assert integrations.build_panel(panel, {"data": [{}, {}], "n": True})["total"] == 2
    assert integrations.build_panel(panel, {"data": [{}, {}], "n": 9})["total"] == 9


def test_a_boolean_total_from_seerr_is_not_a_count(monkeypatch):
    monkeypatch.setattr(integrations, "http_get_json", lambda *a, **k: {"results": [], "pageInfo": {"results": True}})
    panels = handlers.seerr_fetch(["http://s:1"], {"auth": {"header": "X-Api-Key"}}, "k")
    assert [p["total"] for p in panels] == [0, 0]
