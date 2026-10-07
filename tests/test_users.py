import json

import pytest
from conftest import FakeContainer, form_csrf, post_login


def hdr(csrf):
    return {"X-CSRF-Token": csrf}


def make_user(env, c, csrf, name="op", role="operator", password="operator-pass"):
    r = c.post("/api/users", json={"username": name, "role": role, "password": password}, headers=hdr(csrf))
    assert r.status_code == 200, r.data
    return password


def sign_in(env, name, password):
    c = env.app.app.test_client()
    r = post_login(c, name, password)
    assert r.status_code == 302, r.data
    with c.session_transaction() as s:
        return c, s["csrf"]


# ---- storage ------------------------------------------------------------


def test_old_users_file_loads_as_admin(env):
    with open(env.app.USERS_PATH, "w") as f:
        json.dump({"old": env.app.generate_password_hash("old-password")}, f)
    assert env.app.load_users()["old"]["role"] == "admin"
    env.app.setup_token = None  # the app started before the file existed
    c = env.app.app.test_client()
    assert post_login(c, "old", "old-password").status_code == 302
    assert c.get("/api/users").status_code == 200


def test_setup_creates_an_admin(env, logged_in):
    assert env.app.load_users()["admin"]["role"] == "admin"


# ---- roles --------------------------------------------------------------


def test_operator_can_act_but_not_manage_users_or_keys(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("sonarr"))
    pw = make_user(env, c, csrf)
    o, ocsrf = sign_in(env, "op", pw)
    assert o.post("/api/containers/sonarr/restart", headers=hdr(ocsrf)).status_code == 200
    assert o.get("/api/users").status_code == 403
    # updates and the debug log are admin only too
    assert o.post("/api/containers/sonarr/update", headers=hdr(ocsrf)).status_code == 403
    assert o.get("/api/debug").status_code == 403
    assert o.post("/api/debug/level", json={"level": "verbose"}, headers=hdr(ocsrf)).status_code == 403
    assert env.app.activity.get_level() == "normal"
    assert o.post("/api/users", json={"username": "x", "role": "admin", "password": "long-enough"}, headers=hdr(ocsrf)).status_code == 403
    for method, path in (("post", "/api/integrations/sonarr/find-key"), ("put", "/api/integrations/sonarr/key"), ("delete", "/api/integrations/sonarr/key")):
        assert getattr(o, method)(path, json={"key": "k"}, headers=hdr(ocsrf)).status_code == 403, path
    assert o.get("/api/containers").get_json()["role"] == "operator"


def test_operator_can_still_read_logs_and_reach_the_config_editor(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("sonarr", logs=b"line\n"))
    pw = make_user(env, c, csrf)
    o, _ = sign_in(env, "op", pw)
    assert o.get("/api/containers/sonarr/logs").status_code == 200
    # the config editor is open to operators: this container is refused as not a game server, not for the role
    r = o.get("/api/containers/sonarr/config")
    assert r.status_code == 404 and "game server" in r.get_json()["error"]


def test_admin_can_use_the_debug_log_and_updates(env, logged_in):
    c, csrf = logged_in
    assert c.get("/api/debug").status_code == 200
    assert c.post("/api/debug/level", json={"level": "verbose"}, headers=hdr(csrf)).status_code == 200
    assert c.post("/api/containers/nope/update", headers=hdr(csrf)).status_code == 404


def test_operator_can_still_edit_layout_and_dismiss(env, logged_in):
    c, csrf = logged_in
    env.client.add(FakeContainer("sonarr", labels={}))
    pw = make_user(env, c, csrf)
    o, ocsrf = sign_in(env, "op", pw)
    groups = env.app.empty_groups()
    assert o.put("/api/groups", json=groups, headers=hdr(ocsrf)).status_code == 200


def test_role_change_applies_to_open_session(env, logged_in):
    c, csrf = logged_in
    pw = make_user(env, c, csrf, role="admin")
    o, ocsrf = sign_in(env, "op", pw)
    assert o.get("/api/users").status_code == 200
    assert c.put("/api/users/op", json={"role": "operator"}, headers=hdr(csrf)).status_code == 200
    assert o.get("/api/users").status_code == 403


def test_removed_user_loses_session(env, logged_in):
    c, csrf = logged_in
    pw = make_user(env, c, csrf)
    o, ocsrf = sign_in(env, "op", pw)
    assert c.delete("/api/users/op", headers=hdr(csrf)).status_code == 200
    assert o.get("/api/containers").status_code == 302


# ---- user management ----------------------------------------------------


def test_create_validates(env, logged_in):
    c, csrf = logged_in
    bad = [
        {"username": "has space", "role": "operator", "password": "long-enough"},
        {"username": "../x", "role": "operator", "password": "long-enough"},
        {"username": "ok", "role": "viewer", "password": "long-enough"},
        {"username": "ok", "role": "operator", "password": "short"},
    ]
    for body in bad:
        assert c.post("/api/users", json=body, headers=hdr(csrf)).status_code == 400, body
    make_user(env, c, csrf)
    assert c.post("/api/users", json={"username": "op", "role": "operator", "password": "long-enough"}, headers=hdr(csrf)).status_code == 409


def test_needs_csrf(env, logged_in):
    c, csrf = logged_in
    assert c.post("/api/users", json={"username": "a", "role": "operator", "password": "long-enough"}).status_code == 403
    make_user(env, c, csrf)
    assert c.put("/api/users/op", json={"role": "admin"}).status_code == 403
    assert c.delete("/api/users/op").status_code == 403


def test_last_admin_is_protected(env, logged_in):
    c, csrf = logged_in
    assert c.delete("/api/users/admin", headers=hdr(csrf)).status_code == 409
    assert c.put("/api/users/admin", json={"role": "operator"}, headers=hdr(csrf)).status_code == 409
    make_user(env, c, csrf, name="two", role="admin")
    assert c.put("/api/users/admin", json={"role": "operator"}, headers=hdr(csrf)).status_code == 200


def test_admin_resets_password(env, logged_in):
    c, csrf = logged_in
    make_user(env, c, csrf)
    assert c.put("/api/users/op", json={"password": "brand-new-pass"}, headers=hdr(csrf)).status_code == 200
    sign_in(env, "op", "brand-new-pass")
    assert post_login(env.app.app.test_client(), "op", "operator-pass").status_code == 401


def test_listing_never_includes_hashes(env, logged_in):
    c, csrf = logged_in
    make_user(env, c, csrf)
    r = c.get("/api/users")
    assert r.get_json()["items"] == [{"name": "admin", "role": "admin"}, {"name": "op", "role": "operator"}]
    assert b"hash" not in r.data and b"pbkdf2" not in r.data and b"scrypt" not in r.data


def test_read_only_blocks_user_management_but_not_own_password(env, logged_in):
    c, csrf = logged_in
    make_user(env, c, csrf)
    env.app.READ_ONLY = True
    assert c.post("/api/users", json={"username": "z", "role": "operator", "password": "long-enough"}, headers=hdr(csrf)).status_code == 403
    assert c.put("/api/users/op", json={"role": "admin"}, headers=hdr(csrf)).status_code == 403
    assert c.delete("/api/users/op", headers=hdr(csrf)).status_code == 403
    r = c.post("/api/account/password", json={"current": "correct-horse", "new": "another-long-one"}, headers=hdr(csrf))
    assert r.status_code == 200


def test_user_changes_own_password(env, logged_in):
    c, csrf = logged_in
    pw = make_user(env, c, csrf)
    o, ocsrf = sign_in(env, "op", pw)
    assert o.post("/api/account/password", json={"current": "wrong-wrong", "new": "another-long-one"}, headers=hdr(ocsrf)).status_code == 403
    assert o.post("/api/account/password", json={"current": pw, "new": "short"}, headers=hdr(ocsrf)).status_code == 400
    assert o.post("/api/account/password", json={"current": pw, "new": "another-long-one"}).status_code == 403  # no csrf
    assert o.post("/api/account/password", json={"current": pw, "new": "another-long-one"}, headers=hdr(ocsrf)).status_code == 200
    sign_in(env, "op", "another-long-one")


def test_changing_a_password_signs_out_the_other_sessions(env, logged_in):
    c, csrf = logged_in
    pw = make_user(env, c, csrf)
    phone, _ = sign_in(env, "op", pw)
    laptop, lcsrf = sign_in(env, "op", pw)
    assert phone.get("/api/containers").status_code == 200
    assert laptop.post("/api/account/password", json={"current": pw, "new": "another-long-one"}, headers=hdr(lcsrf)).status_code == 200
    assert laptop.get("/api/containers").status_code == 200  # the device that changed it stays in
    assert phone.get("/api/containers").status_code == 302  # the other one is back at the sign-in page


def test_an_admin_reset_signs_the_user_out(env, logged_in):
    c, csrf = logged_in
    pw = make_user(env, c, csrf)
    o, _ = sign_in(env, "op", pw)
    assert o.get("/api/containers").status_code == 200
    assert c.put("/api/users/op", json={"password": "brand-new-pass"}, headers=hdr(csrf)).status_code == 200
    assert o.get("/api/containers").status_code == 302
    assert c.get("/api/containers").status_code == 200  # the admin's own session is untouched


def test_a_role_change_keeps_the_session(env, logged_in):
    c, csrf = logged_in
    pw = make_user(env, c, csrf)
    o, _ = sign_in(env, "op", pw)
    assert c.put("/api/users/op", json={"role": "admin"}, headers=hdr(csrf)).status_code == 200
    assert o.get("/api/containers").status_code == 200


def test_password_guessing_through_change_is_throttled(env, logged_in):
    c, csrf = logged_in
    for _ in range(env.app.MAX_FAILURES):
        c.post("/api/account/password", json={"current": "nope-nope", "new": "another-long-one"}, headers=hdr(csrf))
    r = c.post("/api/account/password", json={"current": "correct-horse", "new": "another-long-one"}, headers=hdr(csrf))
    assert r.status_code == 429


def test_passwords_never_reach_the_debug_log(env, logged_in):
    c, csrf = logged_in
    make_user(env, c, csrf, password="very-secret-pw1")
    c.put("/api/users/op", json={"password": "reset-secret-pw2"}, headers=hdr(csrf))
    c.post("/api/account/password", json={"current": "correct-horse", "new": "own-secret-pw3"}, headers=hdr(csrf))
    log = c.get("/api/debug").get_data(as_text=True)
    for secret in ("very-secret-pw1", "reset-secret-pw2", "own-secret-pw3", "correct-horse"):
        assert secret not in log
    assert "created operator op" in log


# ---- per-user layouts ---------------------------------------------------


def test_layouts_are_per_user(env, logged_in):
    c, csrf = logged_in
    pw = make_user(env, c, csrf)
    o, ocsrf = sign_in(env, "op", pw)
    mine = env.app.empty_groups()
    mine["custom_groups"] = ["Mine"]
    assert c.put("/api/groups", json=mine, headers=hdr(csrf)).status_code == 200
    assert c.get("/api/containers").get_json()["groups"]["custom_groups"] == ["Mine"]
    assert o.get("/api/containers").get_json()["groups"]["custom_groups"] == []
    theirs = env.app.empty_groups()
    theirs["custom_groups"] = ["Theirs"]
    o.put("/api/groups", json=theirs, headers=hdr(ocsrf))
    assert c.get("/api/containers").get_json()["groups"]["custom_groups"] == ["Mine"]
    assert o.get("/api/containers").get_json()["groups"]["custom_groups"] == ["Theirs"]


def test_new_users_start_from_the_shared_file_until_they_save(env, logged_in):
    c, csrf = logged_in
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": ["Old"]}, f)
    pw = make_user(env, c, csrf)
    o, ocsrf = sign_in(env, "op", pw)
    assert o.get("/api/containers").get_json()["groups"]["custom_groups"] == ["Old"]
    # saving writes the user's own file and leaves the shared one alone
    g = env.app.empty_groups()
    o.put("/api/groups", json=g, headers=hdr(ocsrf))
    assert o.get("/api/containers").get_json()["groups"]["custom_groups"] == []
    assert json.load(open(env.app.GROUPS_PATH))["custom_groups"] == ["Old"]


def test_removing_a_user_removes_their_layout_file(env, logged_in):
    import os

    c, csrf = logged_in
    pw = make_user(env, c, csrf)
    o, ocsrf = sign_in(env, "op", pw)
    o.put("/api/groups", json=env.app.empty_groups(), headers=hdr(ocsrf))
    path = env.app.groups_path("op")
    assert os.path.exists(path)
    c.delete("/api/users/op", headers=hdr(csrf))
    assert not os.path.exists(path)


def test_username_cannot_escape_the_groups_folder(env):
    import os

    p = env.app.groups_path("../../etc/passwd")
    assert os.path.dirname(p) == env.app.GROUPS_DIR


# ---- behind a proxy ---------------------------------------------------------


def fail_logins(env, headers, n):
    c = env.app.app.test_client()
    for _ in range(n):
        post_login(c, "admin", "wrong-wrong", headers=headers, environ_overrides={"REMOTE_ADDR": "10.0.0.2"})
    return c


def test_without_the_setting_a_forged_header_changes_nothing(env, logged_in):
    for i in range(env.app.MAX_FAILURES):
        fail_logins(env, {"CF-Connecting-IP": f"1.1.1.{i}"}, 1)
    c = env.app.app.test_client()
    r = post_login(c, "admin", "correct-horse", headers={"CF-Connecting-IP": "9.9.9.9"}, environ_overrides={"REMOTE_ADDR": "10.0.0.2"})
    assert r.status_code == 429  # all counted against the socket's address


def test_behind_proxy_each_visitor_has_their_own_throttle(env, logged_in):
    env.app.BEHIND_PROXY = True
    env.app.PROXY_IP_HEADER = "cf"
    fail_logins(env, {"CF-Connecting-IP": "1.1.1.1"}, env.app.MAX_FAILURES)
    c = env.app.app.test_client()
    hdr_attacker = {"CF-Connecting-IP": "1.1.1.1"}
    hdr_me = {"CF-Connecting-IP": "2.2.2.2"}
    kw = {"environ_overrides": {"REMOTE_ADDR": "10.0.0.2"}}
    assert post_login(c, "admin", "correct-horse", headers=hdr_attacker, **kw).status_code == 429
    assert post_login(c, "admin", "correct-horse", headers=hdr_me, **kw).status_code == 302


def test_a_forged_cf_connecting_ip_is_ignored_in_xff_mode(env, logged_in):
    """Behind Caddy or nginx the visitor can set CF-Connecting-IP to anything; only
    X-Forwarded-For (the proxy's own entry) counts, so rotating it must not reset the throttle."""
    env.app.BEHIND_PROXY = True
    assert env.app.PROXY_IP_HEADER == "xff"
    kw = {"environ_overrides": {"REMOTE_ADDR": "10.0.0.2"}}
    c = env.app.app.test_client()
    for i in range(env.app.MAX_FAILURES):
        post_login(c, "admin", "wrong-wrong", headers={"X-Forwarded-For": "5.5.5.5", "CF-Connecting-IP": f"1.1.1.{i}"}, **kw)
    r = post_login(c, "admin", "correct-horse", headers={"X-Forwarded-For": "5.5.5.5", "CF-Connecting-IP": "9.9.9.9"}, **kw)
    assert r.status_code == 429
    # a different visitor (a different forwarded address) is unaffected
    r = post_login(c, "admin", "correct-horse", headers={"X-Forwarded-For": "6.6.6.6", "CF-Connecting-IP": "5.5.5.5"}, **kw)
    assert r.status_code == 302


def test_xff_mode_takes_the_last_entry_and_ignores_cf(env):
    env.app.BEHIND_PROXY = True
    with env.app.app.test_request_context("/", headers={"X-Forwarded-For": "6.6.6.6, 3.3.3.3", "CF-Connecting-IP": "1.1.1.1"}):
        assert env.app.client_ip() == "3.3.3.3"


def test_cf_mode_takes_cf_and_ignores_forwarded_for(env):
    env.app.BEHIND_PROXY = True
    env.app.PROXY_IP_HEADER = "cf"
    with env.app.app.test_request_context("/", headers={"X-Forwarded-For": "6.6.6.6", "CF-Connecting-IP": "1.1.1.1"}):
        assert env.app.client_ip() == "1.1.1.1"
    with env.app.app.test_request_context("/", headers={"X-Forwarded-For": "6.6.6.6"}, environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert env.app.client_ip() == "127.0.0.1"  # no CF header: the socket, never a forgeable one


def test_behind_proxy_falls_back_to_last_forwarded_for(env):
    env.app.BEHIND_PROXY = True
    with env.app.app.test_request_context("/", headers={"X-Forwarded-For": "6.6.6.6, 3.3.3.3"}):
        assert env.app.client_ip() == "3.3.3.3"
    with env.app.app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        assert env.app.client_ip() == "127.0.0.1"


def test_cookie_is_secure_only_over_https(env):
    def cookie(**kw):
        c = env.app.app.test_client()
        r = c.post("/setup", data={"token": env.app.setup_token, "username": "a", "password": "correct-horse", "csrf_token": form_csrf(c, "/setup", **kw)}, **kw)
        return r.headers.get("Set-Cookie", "")

    assert "Secure" not in cookie()


def test_cookie_secure_over_https_and_behind_proxy(env):
    c = env.app.app.test_client()
    r = c.post("/setup", data={"token": env.app.setup_token, "username": "a", "password": "correct-horse", "csrf_token": form_csrf(c, "/setup", base_url="https://x")}, base_url="https://x")
    assert "Secure" in r.headers["Set-Cookie"]


def test_forwarded_proto_counts_only_behind_proxy(env):
    with env.app.app.test_request_context("/", headers={"X-Forwarded-Proto": "https"}):
        assert env.app.is_https() is False
    env.app.BEHIND_PROXY = True
    with env.app.app.test_request_context("/", headers={"X-Forwarded-Proto": "https"}):
        assert env.app.is_https() is True


# ---- priorities and link overrides ------------------------------------------


def test_priority_settings_round_trip_and_old_files_load(env, logged_in):
    c, csrf = logged_in
    g = env.app.empty_groups()
    g.update(critical_groups=[" Media ", "Media"], container_priority={"web": "critical", "db": "on_demand"}, link_containers=["web"])
    r = c.put("/api/groups", json=g, headers=hdr(csrf))
    assert r.status_code == 200
    saved = c.get("/api/containers").get_json()["groups"]
    assert saved["critical_groups"] == ["Media"]
    assert saved["container_priority"] == {"web": "critical", "db": "on_demand"}
    assert saved["link_containers"] == ["web"]
    with open(env.app.GROUPS_PATH, "w") as f:
        json.dump({"assignments": {}, "custom_groups": []}, f)
    old = env.app.load_groups()
    assert old["critical_groups"] == [] and old["container_priority"] == {} and old["link_containers"] == []


def test_a_group_is_never_both_critical_and_on_demand_and_a_link_never_both_on_and_hidden(env, logged_in):
    c, csrf = logged_in
    g = env.app.empty_groups()
    g.update(critical_groups=["A", "B"], on_demand_groups=["A"], link_containers=["x", "y"], hidden_links=["y"])
    c.put("/api/groups", json=g, headers=hdr(csrf))
    saved = c.get("/api/containers").get_json()["groups"]
    assert saved["critical_groups"] == ["B"]
    assert saved["link_containers"] == ["x"]


@pytest.mark.parametrize("field,value", [
    ("critical_groups", "A"), ("critical_groups", [""]), ("critical_groups", [5]),
    ("container_priority", {"web": "urgent"}), ("container_priority", {"": "critical"}), ("container_priority", ["web"]),
    ("link_containers", "web"), ("link_containers", [""]), ("link_containers", [3]),
])
def test_bad_priority_settings_are_refused(env, logged_in, field, value):
    c, csrf = logged_in
    g = env.app.empty_groups()
    g[field] = value
    assert c.put("/api/groups", json=g, headers=hdr(csrf)).status_code == 400


# ---- concurrent writers --------------------------------------------------------------------


def test_simultaneous_user_creations_are_all_kept(env, logged_in):
    from concurrent.futures import ThreadPoolExecutor

    c, csrf = logged_in
    names = [f"user{i}" for i in range(12)]

    def create(name):
        client = env.app.app.test_client()
        with client.session_transaction() as s:
            with c.session_transaction() as mine:
                s.update(dict(mine))
        return client.post("/api/users", json={"username": name, "role": "operator", "password": "operator-pass"}, headers=hdr(csrf)).status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(create, names))
    assert codes == [200] * 12
    assert set(env.app.load_users()) == {"admin", *names}


def test_simultaneous_key_saves_are_all_kept(env, logged_in):
    from concurrent.futures import ThreadPoolExecutor

    store = env.app.load_integrations()

    def save(i):
        with env.app._data_lock:
            s = env.app.load_integrations()
            s["keys"][f"c{i}"] = "k"
            env.app.save_integrations(s)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(24)))
    assert len(env.app.load_integrations()["keys"]) == 24 and store["keys"] == {}
