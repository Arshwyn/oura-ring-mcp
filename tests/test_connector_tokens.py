import json

from connector_tokens import ConnectorTokenStore, hash_token


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Backend:
    """Fake persistence: stores the saved state as JSON text."""

    def __init__(self):
        self.text = None

    def load(self):
        return None if self.text is None else json.loads(self.text)

    def save(self, state):
        self.text = json.dumps(state)


def make(clock=None, backend=None, warnings=None, **kw):
    warnings = warnings if warnings is not None else []
    return ConnectorTokenStore(
        load=backend.load if backend else None,
        save=backend.save if backend else None,
        now=clock or Clock(),
        warn=warnings.append,
        **kw,
    )


def test_issue_and_lookup():
    s = make()
    out = s.issue("c1")
    assert out["expires_in"] == 3600
    assert out["access_token"] != out["refresh_token"]
    assert s.lookup_access(out["access_token"]) == "c1"
    assert s.lookup_access("nope") is None
    for v in (None, "", 5, {}):
        assert s.lookup_access(v) is None


def test_access_token_expires():
    c = Clock()
    s = make(c)
    tok = s.issue("c1")["access_token"]
    c.t += 3599
    assert s.lookup_access(tok) == "c1"
    c.t += 1
    assert s.lookup_access(tok) is None


def test_refresh_rotates():
    s = make()
    first = s.issue("c1")
    second = s.refresh(first["refresh_token"], "c1")
    assert second["refresh_token"] != first["refresh_token"]
    assert s.lookup_access(second["access_token"]) == "c1"
    assert s.refresh(first["refresh_token"], "c1") == {"error": "invalid_grant"}


def test_reuse_revokes_family():
    s = make()
    first = s.issue("c1")
    second = s.refresh(first["refresh_token"], "c1")
    assert s.refresh(first["refresh_token"], "c1")["error"] == "invalid_grant"
    assert s.lookup_access(second["access_token"]) is None
    assert s.refresh(second["refresh_token"], "c1")["error"] == "invalid_grant"


def test_reuse_does_not_touch_other_families():
    s = make()
    a = s.issue("c1")
    b = s.issue("c1")
    a2 = s.refresh(a["refresh_token"], "c1")
    s.refresh(a["refresh_token"], "c1")
    assert s.lookup_access(a2["access_token"]) is None
    assert s.lookup_access(b["access_token"]) == "c1"


def test_other_client_rejected_without_burning_token():
    s = make()
    a = s.issue("c1")
    assert s.refresh(a["refresh_token"], "c2")["error"] == "invalid_grant"
    assert s.refresh(a["refresh_token"], "c1")["refresh_token"]


def test_bad_refresh_inputs():
    s = make()
    for v in ("nope", "", None, 5):
        assert s.refresh(v, "c1")["error"] == "invalid_grant"


def test_refresh_expiry_slides():
    c = Clock(0)
    s = make(c, access_ttl=10, refresh_ttl=1000)
    cur = s.issue("c1")
    for _ in range(3):
        c.t += 900
        cur = s.refresh(cur["refresh_token"], "c1")
        assert "refresh_token" in cur
    c.t += 1000
    assert s.refresh(cur["refresh_token"], "c1")["error"] == "invalid_grant"


def test_revoke_all_persists_empty_state():
    b = Backend()
    s = make(backend=b)
    out = s.issue("c1")
    s.revoke_all()
    assert s.lookup_access(out["access_token"]) is None
    assert make(backend=b).lookup_access(out["access_token"]) is None
    assert json.loads(b.text)["access"] == {}


def test_persists_hashes_only_and_survives_restart():
    b = Backend()
    out = make(backend=b).issue("c1")
    assert out["access_token"] not in b.text
    assert out["refresh_token"] not in b.text
    assert hash_token(out["access_token"]) in b.text
    s2 = make(backend=b)
    assert s2.lookup_access(out["access_token"]) == "c1"
    assert s2.refresh(out["refresh_token"], "c1")["access_token"]


def test_expired_entries_pruned():
    b = Backend()
    c = Clock(0)
    make(c, b, access_ttl=10, refresh_ttl=20).issue("c1")
    c.t = 100
    s = make(c, b)
    s.issue("c2")
    state = json.loads(b.text)
    assert len(state["access"]) == 1 and len(state["refresh"]) == 1


def test_load_failure_warns_and_works():
    def boom():
        raise RuntimeError("secret-detail")

    warnings = []
    saved = []
    s = ConnectorTokenStore(load=boom, save=saved.append, warn=warnings.append)
    out = s.issue("c1")
    assert s.lookup_access(out["access_token"]) == "c1"
    assert len(warnings) == 1 and "secret-detail" not in warnings[0]
    assert saved == []


def test_garbage_state_disables_persistence_without_overwrite():
    saved = []
    warnings = []
    s = ConnectorTokenStore(load=lambda: {"access": "x"}, save=saved.append, warn=warnings.append)
    out = s.issue("c1")
    assert s.lookup_access(out["access_token"]) == "c1"
    assert saved == [] and len(warnings) == 1


def test_save_failure_warns_and_works():
    def boom(state):
        raise RuntimeError("secret-detail")

    warnings = []
    s = ConnectorTokenStore(load=lambda: None, save=boom, warn=warnings.append)
    out = s.issue("c1")
    assert s.lookup_access(out["access_token"]) == "c1"
    assert len(warnings) == 1 and "secret-detail" not in warnings[0]
