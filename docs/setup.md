# Setup

Run the SharePoint MCP server locally and connect an MCP client to it.

**You need:** an Entra ID tenant with SharePoint (if you don't have one, start with [Microsoft tenant setup](microsoft-tenant-setup.md)), Docker (or Python 3.11+ to run without it), and an MCP client such as Claude Code.

## 1. Configure Entra ID

In your Entra app registration:

- **Authentication**: add a Web redirect URI `http://localhost:8080/oauth/callback`.
- **Certificates & secrets**: create a client secret and copy its *Value*.
- **API permissions**: add Microsoft Graph *Delegated* permissions `User.Read`, `Sites.Read.All` and `Files.Read.All`, then grant admin consent.

## 2. Set up the environment

```
cp .env.example .env
```

Fill in `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` in `.env`.

## 3. Start the dev server

```
docker compose -f infra/docker-compose.yml up -d --build mcp-server
```

The MCP endpoint is `http://localhost:8080/api/mcp` (streamable HTTP). The port is published on `127.0.0.1` only, and the container always uses port 8080, so keep the redirect URI as is. Logs: `docker compose -f infra/docker-compose.yml logs -f mcp-server`. Stop with `docker compose -f infra/docker-compose.yml stop mcp-server`.

<details>
<summary>Without Docker</summary>

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m mcp_server
```

`PORT` in `.env` defaults to `8080`; if you change it, update the redirect URI to match.

</details>

## 4. Connect a client

Point any MCP client that supports HTTP transport and OAuth at the endpoint. For example, with Claude Code:

```
claude mcp add --transport http sharepoint http://localhost:8080/api/mcp
```

Then authenticate from the client (in Claude Code, run `/mcp` and choose *Authenticate*). Your browser opens the Microsoft login page and returns to the client once you sign in.

Tokens are held in memory, so restarting the server means authenticating again.

## Next

See [MCP OAuth sign-in](oauth.md) for how the sign-in flow works and its limits. The server is for single-user local use; see the security notes in the [README](../README.md#security-notes) before exposing it anywhere.
