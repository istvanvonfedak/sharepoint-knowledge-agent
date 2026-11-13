# sharepoint-knowledge-agent
SharePoint Q&A agent with a built-in MCP server. Entra ID delegated auth means users only get answers from content they already have access to.

## Docs
- [Setup](docs/setup.md): configure Entra ID, run the MCP server (Docker or local) and connect a client.
- [Microsoft tenant setup](docs/microsoft-tenant-setup.md): step-by-step walkthrough from a new Azure/Microsoft 365 tenant and Entra app registration to a validated working server.
- [MCP OAuth sign-in](docs/oauth.md): how the sign-in flow works, and its limits.

## Layout
```
apps/mcp-server/      MCP server and tool definitions (python -m mcp_server)
packages/sharepoint/  Entra ID auth + Microsoft Graph client (sharepoint_client)
packages/rag/         pgvector schema, DB access, Gemini embeddings (sharepoint_rag)
evals/                Inspect AI regression + capability evals (see evals/README.md)
infra/                docker-compose.yml (MCP server + Postgres/pgvector)
```
One shared `.venv` at the repo root; each package is installed editable by `requirements.txt`. Config comes from the root `.env`.

## Security notes
- Binds to `127.0.0.1` only; intended for single-user local use.
- Clients register dynamically, but only loopback redirect URIs are accepted. Don't expose the port beyond localhost.
- Tokens issued by this server are opaque, bound to the user's Entra account, and expire after 1 hour (refresh tokens: 30 days, rotated).

## Database (RAG)
`docker compose -f infra/docker-compose.yml up -d db`, then `.venv/bin/python -m sharepoint_rag.db` to apply the schema.
Embeddings use the Gemini API: set `GEMINI_API_KEY` (https://aistudio.google.com/apikey).

## Indexer permissions (needed for the RAG crawler)
The background indexer uses an app-only token, which needs Graph **application** permissions (separate from the delegated ones in [Setup](docs/setup.md)):
`Sites.Selected` (grant per site) or `Sites.Read.All` + `Files.Read.All`, then grant admin consent. Without them Graph returns 401/403.
If you change `EMBEDDING_DIMENSIONS` after the first migration, `sharepoint_rag.db` will refuse to run until the table is recreated.
