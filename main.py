import os
import json
import httpx
import time
import secrets
import hashlib
import base64
import hmac
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse
from urllib.parse import urlencode
from fastmcp import FastMCP
from fastmcp.server.auth import TokenVerifier, AccessToken
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from datetime import datetime, timezone

# --- CONFIGURATION ---

# Oura's own OAuth -- authenticates *this server* to the real Oura API.
# Unrelated to the Claude-connector OAuth below; don't conflate the two.
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

OURA_AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"
OURA_BASE_URL = "https://api.ouraring.com/v2/usercollection"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

MCP_NAME = "oura"

# The public URL this server is reachable at, exactly as configured in
# Claude's "Remote MCP server URL" field (bare origin, no path -- the
# convention that works with the connector). Everything below (issuer,
# endpoints, the protected-resource "resource" field) is derived from this
# static value rather than request headers, so there's no reverse-proxy
# "trust proxy" step some frameworks require --
# it's never wrong regardless of what X-Forwarded-Proto says.
BASE_URL = os.getenv("BASE_URL")

# The Claude-connector-facing OAuth client. Distinct env vars from Oura's
# CLIENT_ID/CLIENT_SECRET above -- this is a single, pre-shared client_id
# (typed into Claude's "OAuth Client ID" field) and client_secret (typed
# into "OAuth Client Secret"), not a per-user credential. Dynamic client
# registration is disabled below, so these fields double as the actual
# access credential -- see /oauth/register.
MCP_CLIENT_ID = os.getenv("MCP_CLIENT_ID")
MCP_CLIENT_SECRET = os.getenv("MCP_CLIENT_SECRET")
# Claude's OAuth callback is a fixed, documented URL (not deployment-
# specific like BASE_URL), so a default here is appropriate.
MCP_REDIRECT_URI = os.getenv("MCP_REDIRECT_URI", "https://claude.ai/api/mcp/auth_callback")

AUTH_CODE_TTL_SECONDS = 5 * 60
ACCESS_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days -- bounded, not a static year-long token

# In-memory by design: this is a personal, single-tenant, restart-tolerant
# service. Losing codes/tokens on restart just means re-authorizing.
AUTH_CODES = {}
ACCESS_TOKENS = {}


def save_tokens(tokens):
    try:
        data = {
            "mcp_name": MCP_NAME,
            "tokens": tokens,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        supabase.table("auth_store").upsert(data).execute()
        print(f"Tokens saved to Supabase for {MCP_NAME}.")
    except Exception as e:
        print(f"Error saving tokens to Supabase: {e}")

def load_tokens():
    try:
        response = supabase.table("auth_store").select("tokens").eq("mcp_name", MCP_NAME).execute()
        if response.data and len(response.data) > 0:
            return response.data[0].get("tokens")
        return None
    except Exception as e:
        print(f"Error loading tokens from Supabase: {e}")
        return None

async def refresh_access_token(refresh_token):
    async with httpx.AsyncClient() as client:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        }
        resp = await client.post(OURA_TOKEN_URL, data=payload)
        if resp.status_code != 200:
            raise Exception(f"Refresh failed ({resp.status_code}): {resp.text}")

        new_tokens = resp.json()

        if "refresh_token" not in new_tokens:
            new_tokens["refresh_token"] = refresh_token

        save_tokens(new_tokens)
        return new_tokens.get("access_token")

async def get_valid_token():
    tokens = load_tokens()
    if not tokens or not tokens.get("access_token"):
        return None
    return tokens.get("access_token")

async def make_oura_request(endpoint: str, params: dict = None):
    token = await get_valid_token()
    if not token:
        return {"error": "Authentication required. Please visit /auth/login"}

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{OURA_BASE_URL}/{endpoint}"

    if params:
        params = {k: v for k, v in params.items() if v is not None}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers, params=params)

            if resp.status_code == 401:
                print("Token expired, refreshing...")
                tokens = load_tokens()
                if tokens and "refresh_token" in tokens:
                    try:
                        new_token = await refresh_access_token(tokens["refresh_token"])
                        headers["Authorization"] = f"Bearer {new_token}"
                        resp = await client.get(url, headers=headers, params=params)
                    except Exception as e:
                        return {"error": f"Your Oura connection has expired. Please visit /auth/login to reconnect."}
                else:
                    return {"error": "No refresh token available. Please visit /auth/login to authenticate again."}

            if resp.status_code != 200:
                return {"error": f"API Error {resp.status_code}: {resp.text}"}

            return resp.json()
        except Exception as e:
            return {"error": str(e)}


# --- CLAUDE CONNECTOR AUTH (bearer-token verification for the MCP transport) ---

class BearerTokenVerifier(TokenVerifier):
    """Verifies access tokens issued by /oauth/token against ACCESS_TOKENS.

    Wired into FastMCP's `auth=` below, this makes FastMCP wrap the MCP
    transport endpoint (every tool call and the transport itself) in a real
    bearer-token check -- a 401 with the correct WWW-Authenticate header on
    failure, handled by the framework rather than hand-rolled per route.
    """

    async def verify_token(self, token: str):
        entry = ACCESS_TOKENS.get(token)
        if not entry or entry["expires_at"] < time.time():
            return None
        return AccessToken(token=token, client_id=MCP_CLIENT_ID, scopes=[])


mcp = FastMCP("oura-cloud", auth=BearerTokenVerifier(base_url=BASE_URL))

# --- MCP TOOLS ---

@mcp.tool()
async def get_personal_info() -> str:
    """Get user profile (Age, Weight, Height, Email)."""
    return json.dumps(await make_oura_request("personal_info"), indent=2)

@mcp.tool()
async def get_daily_sleep(start_date: str, end_date: str) -> str:
    """Get daily sleep summaries. Dates in YYYY-MM-DD."""
    return json.dumps(await make_oura_request("daily_sleep", {"start_date": start_date, "end_date": end_date}), indent=2)

@mcp.tool()
async def get_sleep_periods(start_date: str, end_date: str) -> str:
    """Get detailed sleep periods (hypnograms, phases)."""
    return json.dumps(await make_oura_request("sleep", {"start_date": start_date, "end_date": end_date}), indent=2)

@mcp.tool()
async def get_daily_activity(start_date: str, end_date: str) -> str:
    """Get daily activity summaries."""
    return json.dumps(await make_oura_request("daily_activity", {"start_date": start_date, "end_date": end_date}), indent=2)

@mcp.tool()
async def get_daily_readiness(start_date: str, end_date: str) -> str:
    """Get readiness scores."""
    return json.dumps(await make_oura_request("daily_readiness", {"start_date": start_date, "end_date": end_date}), indent=2)

@mcp.tool()
async def get_daily_spo2(start_date: str, end_date: str) -> str:
    """Get daily SpO2 averages."""
    return json.dumps(await make_oura_request("daily_spo2", {"start_date": start_date, "end_date": end_date}), indent=2)

@mcp.tool()
async def get_daily_stress(start_date: str, end_date: str) -> str:
    """Get daily stress summaries."""
    return json.dumps(await make_oura_request("daily_stress", {"start_date": start_date, "end_date": end_date}), indent=2)

@mcp.tool()
async def get_daily_resilience(start_date: str, end_date: str) -> str:
    """Get resilience score and contributors."""
    return json.dumps(await make_oura_request("daily_resilience", {"start_date": start_date, "end_date": end_date}), indent=2)

@mcp.tool()
async def get_daily_cardiovascular_age(start_date: str, end_date: str) -> str:
    """Get cardiovascular age data."""
    return json.dumps(await make_oura_request("daily_cardiovascular_age", {"start_date": start_date, "end_date": end_date}), indent=2)

@mcp.tool()
async def get_vo2_max(start_date: str, end_date: str) -> str:
    """Get VO2 Max estimates."""
    return json.dumps(await make_oura_request("vO2_max", {"start_date": start_date, "end_date": end_date}), indent=2)

@mcp.tool()
async def get_ring_configuration() -> str:
    """Get ring details (color, model, size)."""
    return json.dumps(await make_oura_request("ring_configuration"), indent=2)

@mcp.tool()
async def get_enhanced_tags(start_date: str, end_date: str) -> str:
    """Get tags (lifestyle, mood, symptoms)."""
    return json.dumps(await make_oura_request("enhanced_tag", {"start_date": start_date, "end_date": end_date}), indent=2)

# --- SERVER & OURA AUTH ROUTES ---

@mcp.custom_route("/auth/login", methods=["GET"])
async def login(request: Request) -> RedirectResponse:
    params = urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "email personal daily heartrate workout enhanced_tag session spo2Daily",
    })
    return RedirectResponse(f"{OURA_AUTH_URL}?{params}")

@mcp.custom_route("/auth/callback", methods=["GET"])
async def callback(request: Request) -> JSONResponse:
    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "No code received"}, status_code=400)

    async with httpx.AsyncClient() as client:
        try:
            payload = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET
            }
            resp = await client.post(OURA_TOKEN_URL, data=payload)
            if resp.status_code != 200:
                return JSONResponse(
                    {"error": f"Token exchange failed ({resp.status_code})", "detail": resp.text},
                    status_code=resp.status_code
                )
            tokens = resp.json()
            save_tokens(tokens)
            return JSONResponse({"message": "Login Successful!"})
        except Exception as e:
            return JSONResponse({"error": f"Auth failed: {str(e)}"}, status_code=500)

# --- CLAUDE CONNECTOR OAUTH ROUTES ---
# Real OAuth 2.1 + PKCE for the Claude-connector-facing flow. Dynamic client
# registration is disabled (single pre-shared client below); every rejection
# reason is logged, since that was the single biggest lever for diagnosing
# connector failures.

@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_metadata(request: Request):
    return JSONResponse({
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "scopes_supported": ["oura_mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    })

@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def resource_metadata(request: Request):
    # Must exactly match the connector's configured "Remote MCP server URL",
    # including path -- an Anthropic-specific requirement, not just an
    # MCP-spec nicety. If the connector ends up configured with anything
    # other than the bare BASE_URL (e.g. a trailing slash), this needs to
    # be updated to match the real, user-confirmed value.
    return JSONResponse({
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL],
    })

@mcp.custom_route("/oauth/register", methods=["POST"])
async def register_client(request: Request):
    print("POST /oauth/register rejected: dynamic client registration is disabled")
    return JSONResponse(
        {
            "error": "invalid_request",
            "error_description": "Dynamic client registration is disabled. Configure a Client ID and Client Secret manually in the connector's Advanced settings.",
        },
        status_code=400,
    )

@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize(request: Request):
    client_id = request.query_params.get("client_id")
    redirect_uri = request.query_params.get("redirect_uri")
    state = request.query_params.get("state")
    code_challenge = request.query_params.get("code_challenge")
    code_challenge_method = request.query_params.get("code_challenge_method")

    if client_id != MCP_CLIENT_ID:
        print(f"/oauth/authorize rejected: unknown client_id={client_id}")
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    if not redirect_uri or redirect_uri != MCP_REDIRECT_URI:
        # client_id isn't secret, so a mismatch here is expected during
        # initial setup (or from a scanner) -- log it, but never redirect
        # to a redirect_uri that isn't the one pre-configured above.
        print(f"/oauth/authorize rejected: redirect_uri mismatch, received={redirect_uri}")
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri does not match the configured value"},
            status_code=400,
        )

    if code_challenge and code_challenge_method != "S256":
        print(f"/oauth/authorize rejected: unsupported code_challenge_method={code_challenge_method}")
        return JSONResponse(
            {"error": "invalid_request", "error_description": "only the S256 code_challenge_method is supported"},
            status_code=400,
        )

    code = secrets.token_hex(32)
    AUTH_CODES[code] = {
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "expires_at": time.time() + AUTH_CODE_TTL_SECONDS,
    }

    redirect_params = {"code": code}
    if state is not None:
        redirect_params["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(redirect_params)}")

def _basic_auth_credentials(request: Request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return None, None
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        client_id, _, client_secret = decoded.partition(":")
        return client_id, client_secret
    except Exception:
        return None, None

def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(computed, code_challenge)

@mcp.custom_route("/oauth/token", methods=["POST"])
async def oauth_token(request: Request):
    # Starlette's request.form() parses application/x-www-form-urlencoded
    # (what Claude sends for both the initial exchange and any retry)
    # without needing a separate parser registered, unlike some frameworks.
    form = await request.form()
    grant_type = form.get("grant_type")
    code = form.get("code")
    redirect_uri = form.get("redirect_uri")
    code_verifier = form.get("code_verifier")

    # Accept credentials via both client_secret_basic (Authorization header)
    # and client_secret_post (form body) -- supporting only one of these
    # was what cost a full failed round with the real connector.
    basic_client_id, basic_client_secret = _basic_auth_credentials(request)
    client_id = basic_client_id or form.get("client_id")
    client_secret = basic_client_secret if basic_client_id else form.get("client_secret")

    if grant_type != "authorization_code":
        print(f"/oauth/token rejected: unsupported grant_type={grant_type}")
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    if client_id != MCP_CLIENT_ID or not client_secret or not hmac.compare_digest(str(client_secret), MCP_CLIENT_SECRET):
        print(f"/oauth/token rejected: invalid_client for client_id={client_id}")
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    auth_code = AUTH_CODES.get(code)
    if not auth_code or auth_code["expires_at"] < time.time() or auth_code["redirect_uri"] != redirect_uri:
        print(f"/oauth/token rejected: invalid_grant for client_id={client_id} (missing/expired/reused code or redirect_uri mismatch)")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    del AUTH_CODES[code]

    if auth_code["code_challenge"]:
        if not code_verifier or not _verify_pkce(code_verifier, auth_code["code_challenge"]):
            print(f"/oauth/token rejected: PKCE verification failed for client_id={client_id}")
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

    access_token = secrets.token_hex(32)
    ACCESS_TOKENS[access_token] = {"expires_at": time.time() + ACCESS_TOKEN_TTL_SECONDS}

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
    })

# Kept off the bare origin ("/") on purpose -- that path is now the MCP
# Streamable HTTP endpoint itself (GET/POST/DELETE, bearer-token gated).
# Point any uptime monitor at /healthz for liveness.
@mcp.custom_route("/healthz", methods=["GET"])
async def health_check(request: Request):
    return JSONResponse({
        "status": "online",
        "mcp_server": "oura-cloud",
        "message": "Server is active"
    })


class RequestLoggingMiddleware:
    """Blanket method+path request trace, from the start rather than added
    as a later debugging escalation. Deliberately logs nothing else --
    several routes above carry secrets (client_secret, code_verifier,
    Authorization headers) that must never end up in logs. Plain ASGI
    (not BaseHTTPMiddleware) so it never touches the response body, which
    matters for the MCP transport's streaming responses.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            print(f"{datetime.now(timezone.utc).isoformat()} {scope['method']} {scope['path']}")
        await self.app(scope, receive, send)


# Streamable HTTP transport (MCP protocol 2025-03-26+): a single endpoint
# accepting POST with a JSON-RPC body, session-scoped via the
# Mcp-Session-Id header -- what Claude's connector actually uses, not the
# legacy separate GET-a-stream-then-POST-messages pattern the old
# transport="sse" call provided. Mounted at the bare origin ("/") per the
# convention that works with the Claude connector: it should
# be configured with just BASE_URL, no path. `auth=` above (BearerTokenVerifier)
# makes FastMCP wrap this route in a real bearer-token check on every
# request, returning 401 + a WWW-Authenticate header pointing at
# /.well-known/oauth-protected-resource on failure.
app = mcp.http_app(path="/", transport="http")

app.add_middleware(RequestLoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
