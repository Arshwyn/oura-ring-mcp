"""Single-use, short-lived state for the Oura account link flow."""

import hmac
import secrets
import time

STATE_TTL_SECONDS = 10 * 60
MAX_PENDING = 100
COOKIE_NAME = "oura_link_state"
WRONG_SECRET_DELAY_SECONDS = 1.0


class LinkStateStore:
    def __init__(self, ttl=STATE_TTL_SECONDS, max_pending=MAX_PENDING, clock=time.monotonic):
        self._ttl = ttl
        self._max = max_pending
        self._clock = clock
        self._pending = {}

    def _purge(self):
        now = self._clock()
        for key in [k for k, exp in self._pending.items() if exp <= now]:
            del self._pending[key]

    def issue(self):
        self._purge()
        while len(self._pending) >= self._max:
            del self._pending[next(iter(self._pending))]
        state = secrets.token_urlsafe(32)
        self._pending[state] = self._clock() + self._ttl
        return state

    def consume(self, query_state, cookie_state):
        """True only when both values match a stored, unexpired, unused state."""
        self._purge()
        if not query_state or not cookie_state:
            return False
        if not hmac.compare_digest(query_state.encode(), cookie_state.encode()):
            return False
        found = None
        for key in self._pending:
            if hmac.compare_digest(key.encode(), query_state.encode()):
                found = key
        if found is None:
            return False
        del self._pending[found]
        return True


def secret_matches(supplied, expected):
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())
