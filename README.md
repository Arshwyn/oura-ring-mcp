# oura-ring-mcp

A remote MCP server that exposes your Oura Ring data (sleep, readiness,
activity, SpO2, stress, and more) as tools for Claude.

There are two separate OAuth layers:

1. **Oura OAuth** (`/auth/login`, `/auth/callback`): authorizes this server to
   call the Oura API on your behalf. Tokens are stored in Supabase and
   refreshed automatically.
2. **Claude connector OAuth** (`/oauth/*`): protects this MCP server itself.
   Claude authenticates at the token endpoint with a pre-shared client ID and
   secret (`MCP_CLIENT_ID` / `MCP_CLIENT_SECRET`) and then uses the issued
   bearer tokens. Dynamic client registration is disabled.

## Prerequisites

- An Oura developer application (https://cloud.ouraring.com/oauth/applications).
- A Supabase project.
- A public HTTPS URL that reaches this server (used as `BASE_URL`).
- Python 3.11+ with [uv](https://docs.astral.sh/uv/), or Docker.

## Oura setup

1. Create an Oura application.
2. Register the redirect URI `<BASE_URL>/auth/callback`. It must exactly equal
   the `REDIRECT_URI` environment variable.
3. The scopes requested are `email personal daily heartrate workout
   enhanced_tag session spo2Daily` (see `login()` in `main.py`).

Only the OAuth flow is supported; Oura Personal Access Tokens are not.

## Supabase setup

Tokens are stored in a single table, `auth_store`. It holds one row for the
Oura tokens and one row (`oura_connector_tokens`) for the hashed connector
tokens described under [Connector tokens and persistence](#connector-tokens-and-persistence);
no extra SQL is needed for the second row. Run this in the Supabase SQL editor:

```sql
create table if not exists auth_store (
  mcp_name   text primary key,
  tokens     jsonb,
  updated_at timestamptz
);
```

Then enable row level security on the table:

```sql
alter table auth_store enable row level security;
```

The table holds Oura OAuth tokens, and tables created through SQL are
reachable through Supabase's API unless row level security is enabled. Do not
create any policies: with RLS enabled and no policies, only the service-role
(secret) key used by the server can access the table, and the `anon` and
`authenticated` roles are denied.

`SUPABASE_KEY` must be the project's secret key (`sb_secret_...`) or the legacy
`service_role` key. Keep it server-side and never use the anon/publishable key
or expose it to clients. If it is ever exposed, rotate it in the Supabase
dashboard and update `.env`.

How to verify: after running the SQL, the table shows RLS enabled in the
Supabase dashboard (Table Editor, or Authentication > Policies). The Supabase
Security Advisor may show an informational "RLS enabled, no policy" notice for
`auth_store`; this is expected, because the server uses the service-role key
and no policies are intended.

`mcp_name` must be the primary key (or unique) so the upsert resolves to one
row. Supabase is the only supported token store; the server will not start
without `SUPABASE_URL` and `SUPABASE_KEY`.

## Environment variables

Copy `.env.example` to `.env` and fill it in.

| Variable | Required | Description |
|----------|----------|-------------|
| `BASE_URL` | yes | Public HTTPS origin of this server, no path (also the connector's Remote MCP server URL). |
| `CLIENT_ID` | yes | Oura application client ID. |
| `CLIENT_SECRET` | yes | Oura application client secret. |
| `REDIRECT_URI` | yes | Oura OAuth redirect, `<BASE_URL>/auth/callback`. |
| `MCP_CLIENT_ID` | yes | Client ID you enter in Claude's connector form (any string). |
| `MCP_CLIENT_SECRET` | yes | Client secret you enter in Claude. Generate with `openssl rand -hex 32`. |
| `MCP_REDIRECT_URI` | no | Claude's OAuth callback. Default `https://claude.ai/api/mcp/auth_callback`. |
| `SUPABASE_URL` | yes | Supabase project URL. |
| `SUPABASE_KEY` | yes | Supabase secret key (`sb_secret_...`) or the legacy `service_role` key. Server-side only; never the anon or publishable key. Rotate it if exposed. |
| `PORT` | no | Host port for `docker compose` only. Default `8000`. |
| `BIND_ADDR` | no | Host interface the `docker compose` port is published on. Default unset: all interfaces (IPv4 and IPv6), same as before. Compose only; does not change the address the app listens on inside the container. |
| `IMAGE_TAG` | no | Local image tag for `docker compose` only. Default `latest`. |

## Run locally

```sh
cp .env.example .env   # then edit
uv venv && . .venv/bin/activate
uv pip install -r pyproject.toml
set -a; . ./.env; set +a
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Run with Docker

```sh
cp .env.example .env   # then edit
docker compose up --build
```

The base image is pinned to `python:3.11.18-slim-trixie` in the `Dockerfile`, and
the `uv` image to `ghcr.io/astral-sh/uv:0.13.6`. Bumping either is a deliberate
edit of the corresponding line followed by a rebuild.

The runtime image does not include `setuptools` or `wheel`; they are removed
after the dependencies are installed.

The container runs as a non-root user (UID 1001) with a read-only root
filesystem, a `/tmp` tmpfs, all capabilities dropped, `no-new-privileges`, and a
`/healthz` healthcheck. Any future write path must use `/tmp` or a new, declared
volume.

`BIND_ADDR` sets the host interface the published port is bound to. It does not
change the address the app listens on inside the container. Unset (or empty), the
port is published on all interfaces (IPv4 and IPv6), exactly as before. Set it to a specific LAN address
(for example `192.0.2.10`) to allow LAN-only access to the host port. Set it to
`127.0.0.1` when a TLS reverse proxy or tunnel runs on the same host and reaches
the container through the host port on loopback. A loopback bind breaks any proxy
or tunnel that reaches the container through the server's LAN address, because
that address is no longer published. An IPv6 address must be written in brackets
in compose (for example `[::1]`), or left unset; a `::` value is not recommended
because it publishes on all IPv6 interfaces.

## Connect Claude

In Claude, add a custom connector:

- Remote MCP server URL: your `BASE_URL`
- OAuth Client ID: your `MCP_CLIENT_ID`
- OAuth Client Secret: your `MCP_CLIENT_SECRET`
- Leave the callback at its default.

## First run

1. Open `<BASE_URL>/auth/login` once in a browser and approve access to link
   your Oura account. Then restrict `/auth/*` as described under
   [Security notes](#security-notes).
2. `<BASE_URL>/healthz` returns a liveness response; point an uptime monitor
   at it.

## Connector tokens and persistence

The token endpoint (`POST /oauth/token`) supports the `authorization_code` and
`refresh_token` grants, both advertised in the discovery metadata. A code
exchange returns an access token (valid 1 hour) and a refresh token (valid 90
days). Presenting the refresh token with the same client credentials returns a
new access token and a new refresh token; the old refresh token stops working
(rotation) and the 90-day lifetime restarts on every refresh. Reusing an
already-rotated refresh token is rejected and ends that session. A refresh
token only works for the client it was issued to. Errors are `invalid_client`,
`invalid_request`, `invalid_grant` and `unsupported_grant_type`, and token
responses are sent with `Cache-Control: no-store`. A connector that supports
refresh therefore stays signed in without manual reconnects.

Only SHA-256 hashes of access and refresh tokens are stored, in the
`oura_connector_tokens` row of the existing `auth_store` table, so restarts and
redeploys keep sessions. Raw tokens are never stored or logged. The state is
read once at startup; token checks use memory. Authorization codes stay in
memory (5 minutes). If Supabase cannot be read or written, the server logs a
warning and keeps working with in-memory tokens, so a restart then requires
reconnecting. A stored value that cannot be parsed is left untouched.

Why Supabase and not a file: the container has a read-only root filesystem and
no volume, and Supabase is already required, so reusing `auth_store` needs no
new volume, table or setting.

To sign every connector session out, delete that row and restart the container:

```sql
delete from auth_store where mcp_name = 'oura_connector_tokens';
```

Rotating `MCP_CLIENT_SECRET` also blocks all refreshes; existing access tokens
then lapse within an hour.

## Tests

```sh
uv run --group dev pytest
```

The tests cover the token store, the token endpoint, and a no-network app
smoke check (401 challenge, tool list, code and refresh flow).

## Security notes

- Never commit `.env`.
- The container is hardened: non-root user, read-only root filesystem, no
  capabilities, and `no-new-privileges`.
- `MCP_CLIENT_SECRET` authenticates the connector client at `/oauth/token`
  (code exchange and refresh). Issued access and refresh tokens are also bearer
  credentials for the MCP endpoint. Keep the secret private and rotate it if
  exposed; rotation blocks new sign-ins and refreshes, and existing access
  tokens lapse within an hour.
- `/auth/login` and `/auth/callback` are not authenticated and carry no
  `state` check. Restrict `/auth/*` at the reverse proxy or tunnel (for example
  with an access policy or an IP allowlist), or block it once your Oura account
  is linked.
- Connector access and refresh tokens are stored only as SHA-256 hashes and
  survive restarts; see "Connector tokens and persistence".
