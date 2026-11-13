# sharepoint-knowledge-agent
SharePoint Q&A agent with a built-in MCP server. Entra ID delegated auth means users only get answers from content they already have access to.

## Layout
```
apps/mcp-server/      MCP server and tool definitions (python -m mcp_server)
packages/sharepoint/  Entra ID auth + Microsoft Graph client (sharepoint_client)
packages/rag/         pgvector schema, DB access, Gemini embeddings (sharepoint_rag)
evals/                Inspect AI regression + capability evals (see evals/README.md)
infra/                docker-compose.yml (Postgres + pgvector)
```
One shared `.venv` at the repo root; each package is installed editable by `requirements.txt`. Config comes from the root `.env`.

## Setup
1. Entra app registration (`normabot`):
   - **Authentication** → Web redirect URI: `http://localhost:8080/oauth/callback`
   - **Certificates & secrets** → new client secret; put its *Value* in `.env` as `AZURE_CLIENT_SECRET`
   - **API permissions** → Microsoft Graph, *Delegated*: `User.Read`, `Sites.Read.All`, `Files.Read.All`, then grant admin consent
2. `cp .env.example .env` and fill in the secret
3. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
4. `.venv/bin/python -m mcp_server`

MCP endpoint: `http://localhost:8080/api/mcp` (streamable HTTP). Connect with e.g.
`claude mcp add --transport http sharepoint http://localhost:8080/api/mcp`, then run `/mcp` in Claude Code and choose *Authenticate*: it opens the Microsoft sign-in in your browser and returns to the client automatically (MCP OAuth; the server proxies to Entra). Tokens are held in memory, so restarting the server requires authenticating again.

## Security notes
- Binds to `127.0.0.1` only; intended for single-user local use.
- Clients register dynamically and are not pre-approved: any local process can register and request sign-in, which still needs you to approve it in the browser. Don't expose the port beyond localhost.
- Tokens issued by this server are opaque, bound to the user's Entra account, and expire after 1 hour (refresh tokens: 30 days, rotated).

## Database (RAG)
`docker compose -f infra/docker-compose.yml up -d`, then `.venv/bin/python -m sharepoint_rag.db` to apply the schema.
Embeddings use the Gemini API: set `GEMINI_API_KEY` (https://aistudio.google.com/apikey).

## Indexer permissions (needed for the RAG crawler)
The background indexer uses an app-only token, which needs Graph **application** permissions (separate from the delegated ones above):
`Sites.Selected` (grant per site) or `Sites.Read.All` + `Files.Read.All`, then grant admin consent. Without them Graph returns 401/403.
If you change `EMBEDDING_DIMENSIONS` after the first migration, `sharepoint_rag.db` will refuse to run until the table is recreated.
