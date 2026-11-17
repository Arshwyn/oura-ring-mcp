"""Connector token store: access and refresh tokens for the MCP connector.

Pure logic with no environment access and no import-time side effects.
Persistence is injected as ``load()`` / ``save(state)`` callables so the store
can be tested without a database. Only SHA-256 digests of tokens are held or
persisted; raw tokens exist only in the value returned to the caller.
"""
import hashlib
import secrets
import threading
import time

ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60
STATE_VERSION = 1

INVALID_GRANT = {"error": "invalid_grant"}


def hash_token(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


class ConnectorTokenStore:
    def __init__(
        self,
        load=None,
        save=None,
        now=time.time,
        access_ttl=ACCESS_TOKEN_TTL_SECONDS,
        refresh_ttl=REFRESH_TOKEN_TTL_SECONDS,
        warn=print,
    ):
        self._save_fn = save
        self._now = now
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        self._warn = warn
        self._lock = threading.RLock()
        # digest -> {"client_id", "family", "expires_at"}
        self._access = {}
        # digest -> {"client_id", "family", "expires_at", "rotated"}
        self._refresh = {}
        # Persistence is switched off when the stored value cannot be understood,
        # so that value is never overwritten.
        self._persist = save is not None
        if load is not None:
            self._load(load)

    def _load(self, load):
        try:
            state = load()
        except Exception as e:
            self._warn(f"Connector token store unavailable ({type(e).__name__}); keeping tokens in memory only")
            self._persist = False
            return
        if state is None:
            return
        try:
            access = state["access"]
            refresh = state["refresh"]
            if not isinstance(access, dict) or not isinstance(refresh, dict):
                raise ValueError("bad shape")
            for e in access.values():
                e["client_id"], e["family"], float(e["expires_at"])
            for e in refresh.values():
                e["client_id"], e["family"], float(e["expires_at"]), bool(e["rotated"])
        except Exception:
            self._warn("Connector token state is unreadable; keeping tokens in memory only and leaving it untouched")
            self._persist = False
            return
        self._access = {h: dict(e) for h, e in access.items()}
        self._refresh = {h: dict(e) for h, e in refresh.items()}
        self._prune()

    def _prune(self):
        t = self._now()
        for h in [h for h, e in self._access.items() if e["expires_at"] <= t]:
            del self._access[h]
        for h in [h for h, e in self._refresh.items() if e["expires_at"] <= t]:
            del self._refresh[h]

    def _save(self):
        if not self._persist:
            return
        self._prune()
        state = {
            "version": STATE_VERSION,
            "access": {h: dict(e) for h, e in self._access.items()},
            "refresh": {h: dict(e) for h, e in self._refresh.items()},
        }
        try:
            self._save_fn(state)
        except Exception as e:
            self._warn(f"Connector token store write failed ({type(e).__name__}); continuing in memory only")

    def _mint(self, client_id, family):
        access_token = secrets.token_hex(32)
        refresh_token = secrets.token_hex(32)
        t = self._now()
        self._access[hash_token(access_token)] = {
            "client_id": client_id, "family": family, "expires_at": t + self._access_ttl,
        }
        self._refresh[hash_token(refresh_token)] = {
            "client_id": client_id, "family": family, "expires_at": t + self._refresh_ttl, "rotated": False,
        }
        return {"access_token": access_token, "refresh_token": refresh_token, "expires_in": int(self._access_ttl)}

    def _revoke_family(self, family):
        for store in (self._access, self._refresh):
            for h in [h for h, e in store.items() if e["family"] == family]:
                del store[h]

    def issue(self, client_id):
        with self._lock:
            out = self._mint(client_id, secrets.token_hex(16))
            self._save()
            return out

    def lookup_access(self, token):
        """Return the client id for a valid access token, else None (memory only)."""
        if not isinstance(token, str) or not token:
            return None
        # Lock-free on purpose: a single dict read is atomic, and this runs on
        # the event loop while a save (network I/O) may hold the lock.
        e = self._access.get(hash_token(token))
        if not e or e["expires_at"] <= self._now():
            return None
        return e["client_id"]

    def refresh(self, token, client_id):
        """Rotate a refresh token. Returns a new pair or {"error": "invalid_grant"}."""
        if not isinstance(token, str) or not token:
            return dict(INVALID_GRANT)
        with self._lock:
            h = hash_token(token)
            e = self._refresh.get(h)
            if not e or e["expires_at"] <= self._now() or e["client_id"] != client_id:
                return dict(INVALID_GRANT)
            if e["rotated"]:
                self._revoke_family(e["family"])
                self._save()
                return dict(INVALID_GRANT)
            e["rotated"] = True
            out = self._mint(client_id, e["family"])
            self._save()
            return out

    def revoke_all(self):
        with self._lock:
            self._access.clear()
            self._refresh.clear()
            self._save()
