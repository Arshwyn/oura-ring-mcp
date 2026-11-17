import asyncio

import pytest
from starlette.testclient import TestClient

from conftest import pkce_pair

EXPECTED_TOOLS = {
    "get_personal_info", "get_daily_sleep", "get_sleep_periods", "get_daily_activity",
    "get_daily_readiness", "get_daily_spo2", "get_daily_stress", "get_daily_resilience",
    "get_daily_cardiovascular_age", "get_vo2_max", "get_ring_configuration", "get_enhanced_tags",
}
CLIENT = {"client_id": "test-client", "client_secret": "test-secret"}
REDIRECT = "https://claude.example.test/callback"


@pytest.fixture()
def client(main_module):
    with TestClient(main_module.app, follow_redirects=False) as c:
        yield c


def test_tool_list(main_module):
    tools = asyncio.run(main_module.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert len(tools) == 12


def test_unauthenticated_request_gets_challenge(client):
    r = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]


def test_protected_resource_metadata(client):
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    assert r.json() == {"resource": "https://mcp.example.test", "authorization_servers": ["https://mcp.example.test"]}


def test_authorization_server_metadata(client):
    body = client.get("/.well-known/oauth-authorization-server").json()
    assert body["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert body["code_challenge_methods_supported"] == ["S256"]


def get_code(client, challenge):
    r = client.get("/oauth/authorize", params={
        "client_id": "test-client", "redirect_uri": REDIRECT, "state": "s",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    assert r.status_code in (302, 307)
    return r.headers["location"].split("code=")[1].split("&")[0]


def test_code_then_refresh_flow(client, fake_db):
    verifier, challenge = pkce_pair()
    code = get_code(client, challenge)
    r = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "code_verifier": verifier, **CLIENT,
    })
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    first = r.json()
    assert first["expires_in"] == 3600 and first["refresh_token"]

    # access token works against the MCP endpoint's auth layer
    r = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Authorization": f"Bearer {first['access_token']}",
                             "Accept": "application/json, text/event-stream"})
    assert r.status_code != 401

    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"], **CLIENT})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]

    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"], **CLIENT})
    assert r.status_code == 400 and r.json() == {"error": "invalid_grant"}
    assert r.headers["cache-control"] == "no-store"
    # reuse revoked the family
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": second["refresh_token"], **CLIENT})
    assert r.status_code == 400

    # persisted state holds hashes only
    stored = str(fake_db.rows.get("oura_connector_tokens"))
    assert first["access_token"] not in stored and first["refresh_token"] not in stored


def test_token_endpoint_errors(client):
    r = client.post("/oauth/token", data={"grant_type": "password", **CLIENT})
    assert (r.status_code, r.json()) == (400, {"error": "unsupported_grant_type"})
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x",
                                          "client_id": "test-client", "client_secret": "bad"})
    assert (r.status_code, r.json()) == (401, {"error": "invalid_client"})
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", **CLIENT})
    assert (r.status_code, r.json()) == (400, {"error": "invalid_request"})
    r = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": "nope",
                                          "redirect_uri": REDIRECT, **CLIENT})
    assert (r.status_code, r.json()) == (400, {"error": "invalid_grant"})


def test_refresh_with_basic_auth(client):
    import base64
    code = get_code(client, None)
    first = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                              "redirect_uri": REDIRECT, **CLIENT}).json()
    basic = base64.b64encode(b"test-client:test-secret").decode()
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
                    headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200
