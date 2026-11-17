from connector_tokens import ConnectorTokenStore
from oauth_token import NO_STORE_HEADERS, client_authenticated, handle_refresh_grant, token_response

ID, SECRET = "c1", "s1"


def call(store, refresh_token, client_id=ID, secret=SECRET):
    return handle_refresh_grant(store, ID, SECRET, client_id, secret, refresh_token)


def test_client_authenticated():
    assert client_authenticated(ID, SECRET, ID, SECRET)
    for cid, sec in [(ID, "wrong"), ("other", SECRET), (ID, None), (ID, ""), (None, SECRET), (ID, "é")]:
        assert not client_authenticated(cid, sec, ID, SECRET)
    assert not client_authenticated(ID, SECRET, ID, None)


def test_refresh_success_shape():
    store = ConnectorTokenStore()
    first = store.issue(ID)
    status, body, reason = call(store, first["refresh_token"])
    assert status == 200 and reason is None
    assert set(body) == {"access_token", "token_type", "expires_in", "refresh_token"}
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600
    assert body["refresh_token"] != first["refresh_token"]


def test_invalid_client():
    store = ConnectorTokenStore()
    rt = store.issue(ID)["refresh_token"]
    for cid, sec in [(ID, "wrong"), ("nope", SECRET), (ID, None)]:
        status, body, _ = call(store, rt, cid, sec)
        assert (status, body) == (401, {"error": "invalid_client"})


def test_invalid_request():
    status, body, _ = call(ConnectorTokenStore(), None)
    assert (status, body) == (400, {"error": "invalid_request"})


def test_invalid_grant_cases():
    store = ConnectorTokenStore()
    first = store.issue(ID)
    assert call(store, "nope")[1] == {"error": "invalid_grant"}
    assert call(store, first["refresh_token"])[0] == 200
    status, body, _ = call(store, first["refresh_token"])
    assert (status, body) == (400, {"error": "invalid_grant"})


def test_token_response_includes_refresh_token():
    body = token_response(ConnectorTokenStore().issue(ID))
    assert set(body) == {"access_token", "token_type", "expires_in", "refresh_token"}


def test_no_store_headers():
    assert NO_STORE_HEADERS["Cache-Control"] == "no-store"
