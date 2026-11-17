"""Pure helpers for the connector token endpoint (no env access, no side effects)."""
import hmac

NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def client_authenticated(client_id, client_secret, expected_id, expected_secret):
    """Constant-time check of the pre-shared connector client credentials."""
    if not expected_id or not expected_secret or client_id != expected_id:
        return False
    if not isinstance(client_secret, str) or not client_secret:
        return False
    return hmac.compare_digest(client_secret.encode(), str(expected_secret).encode())


def token_response(issued):
    return {
        "access_token": issued["access_token"],
        "token_type": "Bearer",
        "expires_in": issued["expires_in"],
        "refresh_token": issued["refresh_token"],
    }


def handle_refresh_grant(store, expected_id, expected_secret, client_id, client_secret, refresh_token):
    """Handle grant_type=refresh_token. Returns (status, body, reason)."""
    if not client_authenticated(client_id, client_secret, expected_id, expected_secret):
        return 401, {"error": "invalid_client"}, "invalid_client"
    if not isinstance(refresh_token, str) or not refresh_token:
        return 400, {"error": "invalid_request"}, "missing refresh_token"
    result = store.refresh(refresh_token, client_id)
    if "error" in result:
        return 400, {"error": "invalid_grant"}, "invalid_grant"
    return 200, token_response(result), None
