import base64
import hashlib
import os
import sys
import types

import pytest

# Placeholder configuration so `import main` works without a real deployment.
os.environ.update({
    "BASE_URL": "https://mcp.example.test",
    "CLIENT_ID": "oura-client",
    "CLIENT_SECRET": "oura-secret",
    "REDIRECT_URI": "https://mcp.example.test/auth/callback",
    "MCP_CLIENT_ID": "test-client",
    "MCP_CLIENT_SECRET": "test-secret",
    "MCP_REDIRECT_URI": "https://claude.example.test/callback",
    "SUPABASE_URL": "https://project.example.test",
    "SUPABASE_KEY": "test-key",
})


class FakeSupabase:
    """Minimal in-memory stand-in for the auth_store table."""

    def __init__(self):
        self.rows = {}

    def table(self, name):
        return FakeTable(self)


class FakeTable:
    def __init__(self, db):
        self.db = db
        self._op = None
        self._name = None

    def select(self, *_):
        self._op = "select"
        return self

    def eq(self, _col, value):
        self._name = value
        return self

    def upsert(self, row):
        self._op = "upsert"
        self._row = row
        return self

    def execute(self):
        if self._op == "upsert":
            self.db.rows[self._row["mcp_name"]] = self._row["tokens"]
            return types.SimpleNamespace(data=[])
        tokens = self.db.rows.get(self._name)
        return types.SimpleNamespace(data=[] if tokens is None else [{"tokens": tokens}])


@pytest.fixture(scope="session")
def fake_db():
    return FakeSupabase()


@pytest.fixture(scope="session")
def main_module(fake_db):
    import supabase

    supabase.create_client = lambda *a, **k: fake_db
    sys.modules.pop("main", None)
    import main

    return main


def pkce_pair():
    verifier = "v" * 50
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")
