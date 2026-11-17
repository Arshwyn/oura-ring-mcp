import pytest
from starlette.testclient import TestClient

import auth_link
from auth_link import COOKIE_NAME, LinkStateStore, secret_matches


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_states_are_unique_and_long():
    store = LinkStateStore()
    states = {store.issue() for _ in range(50)}
    assert len(states) == 50
    assert all(len(s) >= 43 for s in states)


def test_consume_is_single_use():
    store = LinkStateStore()
    s = store.issue()
    assert store.consume(s, s) is True
    assert store.consume(s, s) is False


def test_missing_and_mismatch_rejected():
    store = LinkStateStore()
    s = store.issue()
    assert store.consume(None, s) is False
    assert store.consume(s, None) is False
    assert store.consume("", "") is False
    assert store.consume(s, "other") is False
    assert store.consume("unknown", "unknown") is False
    assert store.consume(s, s) is True


def test_expired_rejected():
    clock = Clock()
    store = LinkStateStore(ttl=10, clock=clock)
    s = store.issue()
    clock.now = 11
    assert store.consume(s, s) is False


def test_pending_is_bounded():
    store = LinkStateStore(max_pending=3)
    first = store.issue()
    for _ in range(3):
        store.issue()
    assert store.consume(first, first) is False


def test_secret_matches():
    assert secret_matches("abc", "abc")
    assert not secret_matches("abd", "abc")
    assert not secret_matches("", "")
    assert not secret_matches(None, "abc")
    assert not secret_matches("abc", None)


@pytest.fixture()
def client(main_module, monkeypatch):
    monkeypatch.setattr(main_module, "WRONG_SECRET_DELAY_SECONDS", 0)
    monkeypatch.setattr(main_module, "LINK_STATES", LinkStateStore())
    with TestClient(main_module.app, follow_redirects=False, base_url="https://mcp.example.test") as c:
        yield c


@pytest.fixture()
def exchanges(main_module, monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"access_token": "a", "refresh_token": "r"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **kw):
            calls.append((url, data))
            return Resp()

    monkeypatch.setattr(main_module.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    return calls


def start_flow(client):
    r = client.post("/auth/login", data={"secret": "test-secret"})
    assert r.status_code == 303
    state = r.headers["location"].split("state=")[1].split("&")[0]
    return r, state


def test_login_form_has_no_side_effects(client):
    r = client.get("/auth/login")
    assert r.status_code == 200
    assert 'type="password"' in r.text
    assert "set-cookie" not in r.headers
    assert "location" not in r.headers


@pytest.mark.parametrize("data", [{}, {"secret": ""}, {"secret": "wrong"}])
def test_login_rejects_bad_secret(client, data):
    r = client.post("/auth/login", data=data)
    assert r.status_code == 403
    assert "set-cookie" not in r.headers
    assert "location" not in r.headers


def test_login_success_sets_cookie_and_state(client):
    r, state = start_flow(client)
    assert r.headers["location"].startswith("https://cloud.ouraring.com/oauth/authorize?")
    cookie = r.headers["set-cookie"]
    assert f"{COOKIE_NAME}={state}" in cookie
    for attr in ("HttpOnly", "Secure", "SameSite=lax", "Path=/auth"):
        assert attr.lower() in cookie.lower()
    _, state2 = start_flow(client)
    assert state != state2


def test_callback_success_stores_tokens(client, exchanges, fake_db):
    _, state = start_flow(client)
    r = client.get("/auth/callback", params={"code": "c1", "state": state})
    assert r.status_code == 200
    assert len(exchanges) == 1
    assert fake_db.rows["oura"] == {"access_token": "a", "refresh_token": "r"}


def test_callback_rejections_do_not_exchange(client, exchanges):
    _, state = start_flow(client)
    # missing state
    assert client.get("/auth/callback", params={"code": "c"}).status_code == 400
    # mismatched state
    assert client.get("/auth/callback", params={"code": "c", "state": "nope"}).status_code == 400
    # cookie removed (different browser)
    client.cookies.clear()
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400
    assert exchanges == []


def test_callback_replay_rejected(client, exchanges):
    _, state = start_flow(client)
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 200
    client.cookies.set(COOKIE_NAME, state, path="/auth")
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400
    assert len(exchanges) == 1


def test_callback_expired_rejected(main_module, monkeypatch, exchanges):
    clock = Clock()
    monkeypatch.setattr(main_module, "LINK_STATES", LinkStateStore(ttl=10, clock=clock))
    monkeypatch.setattr(main_module, "WRONG_SECRET_DELAY_SECONDS", 0)
    with TestClient(main_module.app, follow_redirects=False, base_url="https://mcp.example.test") as c:
        r = c.post("/auth/login", data={"secret": "test-secret"})
        state = r.headers["location"].split("state=")[1].split("&")[0]
        clock.now = 11
        assert c.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400
    assert exchanges == []
