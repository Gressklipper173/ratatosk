import importlib
import io
import os
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class FakeImage:
    def __init__(self, tags=("img:1",), repo_digests=()):
        self.tags = list(tags)
        self.attrs = {"RepoDigests": list(repo_digests)}
        self.short_id = "sha256:abc"


class FakeContainer:
    def __init__(self, name, status="running", labels=None, short_id=None, logs=b"", ports=None, image_ref="img:1", repo_digests=(), files=None):
        self.name = name
        self.status = status
        self.short_id = short_id or (name.ljust(12, "0"))[:12]
        self.image = FakeImage(repo_digests=repo_digests)
        self.attrs = {
            "State": {},
            "Config": {"Labels": labels or {}, "Image": image_ref},
            "NetworkSettings": {"Ports": ports or {}, "Networks": {"net": {"IPAddress": "172.20.0.5"}}},
        }
        self._logs = logs
        self._files = files or {}
        self.calls = []

    def get_archive(self, path):
        import docker

        if path not in self._files:
            raise docker.errors.NotFound("no such file")
        buf = io.BytesIO()
        data = self._files[path].encode()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(os.path.basename(path))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return iter([buf.getvalue()]), {}

    def start(self):
        self.calls.append("start")

    def stop(self, timeout=None):
        self.calls.append("stop")

    def restart(self, timeout=None):
        self.calls.append("restart")

    def remove(self, v=False, force=False):
        self.calls.append(("remove", v))

    def logs(self, tail=None, timestamps=False):
        self.last_tail = tail
        return self._logs


class FakeClient:
    def __init__(self):
        self.by_name = {}
        self.registry = {}
        self.tags = {}  # repository -> the tags the registry lists for it (a missing one lists none)
        self.tags_error = None  # set to an exception to make listing tags fail
        self.images = SimpleNamespace(get_registry_data=self._registry_data)
        self.containers = SimpleNamespace(get=self._get, list=self._list)
        self.ping_error = None  # set to an exception to make the daemon look unreachable

    def ping(self):
        if self.ping_error:
            raise self.ping_error
        return True

    def add(self, c):
        self.by_name[c.name] = c
        return c

    def _registry_data(self, ref):
        if ref not in self.registry:
            raise RuntimeError("registry down")
        return SimpleNamespace(id=self.registry[ref])

    def _list(self, all=False):
        # like Docker: without all=True, only running containers
        return [c for c in self.by_name.values() if all or c.status == "running"]

    def _get(self, name):
        import docker

        if name not in self.by_name:
            raise docker.errors.NotFound("nope")
        return self.by_name[name]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fresh app module per test: temp data dir, no sampler thread, fake Docker. The settings
    the app reads from the environment are cleared first, so a developer's own don't leak in."""
    import docker

    data = str(tmp_path)
    monkeypatch.setenv("DATA_DIR", data)
    monkeypatch.setenv("DISABLE_SAMPLER", "1")
    monkeypatch.setenv("HOSTNAME", "dashboardself")
    for name in ("READ_ONLY", "BEHIND_PROXY", "UPDATE_CHECK", "PROXY_IP_HEADER"):
        monkeypatch.delenv(name, raising=False)
    client = FakeClient()
    monkeypatch.setattr(docker, "from_env", lambda: client)
    import pinned

    def list_tags(registry, repo, opener=None):
        if client.tags_error:
            raise client.tags_error
        return client.tags.get(repo, [])

    monkeypatch.setattr(pinned, "list_tags", list_tags)  # no test reaches a real registry
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    module.app.config["TESTING"] = True
    return SimpleNamespace(app=module, client=client, data=data)


def form_csrf(client, path="/login", **kw):
    """The hidden token the login and setup forms carry (fetched the way a browser would)."""
    client.get(path, **kw)
    with client.session_transaction(**kw) as s:
        return s["csrf_pre"]


def post_login(client, username, password, **kw):
    """Sign in the way the form does: with the token its page carried."""
    return client.post("/login", data={"username": username, "password": password, "csrf_token": form_csrf(client)}, **kw)


@pytest.fixture
def logged_in(env):
    """A test client with the admin created and signed in; returns (client, csrf)."""
    c = env.app.app.test_client()
    r = c.post(
        "/setup",
        data={"token": env.app.setup_token, "username": "admin", "password": "correct-horse", "csrf_token": form_csrf(c, "/setup")},
    )
    assert r.status_code == 302
    with c.session_transaction() as s:
        csrf = s["csrf"]
    return c, csrf


@pytest.fixture(autouse=True)
def fresh_activity_log():
    """The activity log is process-wide state; every test starts with it empty and quiet."""
    import activity

    import handlers

    handlers.pelican_forget()
    activity.clear()
    activity._level = "normal"
    activity.set_secret_source(lambda: ())
    yield
