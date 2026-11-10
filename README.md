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
`claude mcp add --transport http sharepoint http://localhost:8080/api/mcp`, then ask it to call `sharepoint_login`.

## Security notes
- Binds to `127.0.0.1` only; intended for single-user local use.
- Sign-in links are not bound to the requesting browser: only open a link from `sharepoint_login` that *you* requested.
- Sessions are identified by the MCP session id; don't expose the port beyond localhost.

## Database (RAG)
`docker compose -f infra/docker-compose.yml up -d`, then `.venv/bin/python -m sharepoint_rag.db` to apply the schema.
Embeddings use the Gemini API: set `GEMINI_API_KEY` (https://aistudio.google.com/apikey).

## Indexer permissions (needed for the RAG crawler)
The background indexer uses an app-only token, which needs Graph **application** permissions (separate from the delegated ones above):
`Sites.Selected` (grant per site) or `Sites.Read.All` + `Files.Read.All`, then grant admin consent. Without them Graph returns 401/403.
If you change `EMBEDDING_DIMENSIONS` after the first migration, `sharepoint_rag.db` will refuse to run until the table is recreated.
