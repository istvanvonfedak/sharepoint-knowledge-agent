# sharepoint-knowledge-agent
SharePoint Q&A agent with a built-in MCP server. Entra ID delegated auth means users only get answers from content they already have access to.

## Setup
1. Entra app registration (`normabot`):
   - **Authentication** → Web redirect URI: `http://localhost:8080/oauth/callback`
   - **Certificates & secrets** → new client secret; put its *Value* in `.env` as `AZURE_CLIENT_SECRET`
   - **API permissions** → Microsoft Graph, *Delegated*: `User.Read`, `Sites.Read.All`, `Files.Read.All`, then grant admin consent
2. `cp .env.example .env` and fill in the secret
3. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
4. `.venv/bin/python -m sharepoint_mcp.server`

MCP endpoint: `http://localhost:8080/api/mcp` (streamable HTTP). Connect with e.g.
`claude mcp add --transport http sharepoint http://localhost:8080/api/mcp`, then ask it to call `sharepoint_login`.

## Security notes
- Binds to `127.0.0.1` only; intended for single-user local use.
- Sign-in links are not bound to the requesting browser: only open a link from `sharepoint_login` that *you* requested.
- Sessions are identified by the MCP session id; don't expose the port beyond localhost.
