import importlib
import io
import os
import sys
import tarfile
import tempfile
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
        self.images = SimpleNamespace(get_registry_data=self._registry_data)
        self.containers = SimpleNamespace(get=self._get, list=lambda all=False: list(self.by_name.values()))

    def add(self, c):
        self.by_name[c.name] = c
        return c

    def _registry_data(self, ref):
        if ref not in self.registry:
            raise RuntimeError("registry down")
        return SimpleNamespace(id=self.registry[ref])

    def _get(self, name):
        import docker

        if name not in self.by_name:
            raise docker.errors.NotFound("nope")
        return self.by_name[name]


@pytest.fixture
def env(monkeypatch):
    """Fresh app module per test: temp data dir, no sampler thread, fake Docker."""
    import docker

    data = tempfile.mkdtemp()
    monkeypatch.setenv("DATA_DIR", data)
    monkeypatch.setenv("DISABLE_SAMPLER", "1")
    monkeypatch.setenv("HOSTNAME", "dashboardself")
    monkeypatch.delenv("READ_ONLY", raising=False)
    client = FakeClient()
    monkeypatch.setattr(docker, "from_env", lambda: client)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    module.app.config["TESTING"] = True
    return SimpleNamespace(app=module, client=client, data=data)


@pytest.fixture
def logged_in(env):
    """A test client with the admin created and signed in; returns (client, csrf)."""
    c = env.app.app.test_client()
    r = c.post(
        "/setup",
        data={"token": env.app.setup_token, "username": "admin", "password": "correct-horse"},
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
