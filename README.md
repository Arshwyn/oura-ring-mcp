# oura-ring-mcp

A remote MCP server that exposes your Oura Ring data (sleep, readiness,
activity, SpO2, stress, and more) as tools for Claude.

There are two separate OAuth layers:

1. **Oura OAuth** (`/auth/login`, `/auth/callback`): authorizes this server to
   call the Oura API on your behalf. Tokens are stored in Supabase and
   refreshed automatically.
2. **Claude connector OAuth** (`/oauth/*`): protects this MCP server itself.
   Claude authenticates with a pre-shared client ID and secret
   (`MCP_CLIENT_ID` / `MCP_CLIENT_SECRET`). Dynamic client registration is
   disabled.

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

Tokens are stored in a single table, `auth_store`. Run this in the Supabase
SQL editor:

```sql
create table if not exists auth_store (
  mcp_name   text primary key,
  tokens     jsonb,
  updated_at timestamptz
);
```

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
| `SUPABASE_KEY` | yes | Supabase key. A privileged secret; it must have write access to `auth_store`. |
| `PORT` | no | Host port for `docker compose` only. Default `8000`. |

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

## Connect Claude

In Claude, add a custom connector:

- Remote MCP server URL: your `BASE_URL`
- OAuth Client ID: your `MCP_CLIENT_ID`
- OAuth Client Secret: your `MCP_CLIENT_SECRET`
- Leave the callback at its default.

## First run

1. Open `<BASE_URL>/auth/login` once in a browser and approve access to link
   your Oura account.
2. `<BASE_URL>/healthz` returns a liveness response; point an uptime monitor
   at it.

## Security notes

- Never commit `.env`.
- `MCP_CLIENT_SECRET` is the only access credential for the MCP endpoint.
  Keep it secret and rotate it if exposed.
- MCP access tokens are held in memory, so after a restart the connector must
  reconnect.
