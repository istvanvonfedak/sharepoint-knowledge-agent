# Setup

Run the full stack (mcp-server, sync-worker, inference-server, Postgres) with Docker Compose and connect an MCP client.

**You need:** an Entra ID tenant with SharePoint (if you don't have one, start with [Microsoft tenant setup](microsoft-tenant-setup.md)), Docker (or Python 3.11+ to run without it), and an MCP client such as Claude Code.

## Useful links

- App registrations (create the Entra apps): https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade
- Entra admin center: https://entra.microsoft.com
- Microsoft 365 admin center: https://admin.microsoft.com
- Gemini API key (embeddings): https://aistudio.google.com/apikey
- OpenAI API key (`sharepoint_ask`): https://platform.openai.com/api-keys

## 1. Configure Entra ID: two app registrations

Create both in [App registrations](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade).

The mcp-server and the sync-worker use **separate** app registrations, so a compromise of the mcp-server (the most exposed service) can't be turned into tenant-wide app access.

**mcp-server app** (users sign in through it):

1. Click on App registrations
2. Click on New registration
3. Name it mcp-server-app
4. Set Supported account types to Single tenant only
5. Set the Redirect URI platform to Web and the URI to http://localhost:8080/oauth/callback
6. Click Register
7. On the Overview page, under .env.mcp set AZURE_CLIENT_ID to the Application (client) ID and AZURE_TENANT_ID to the Directory (tenant) ID
8. Under Manage click on Certificates & secrets
9. Click on New client secret
10. Under .env.mcp set AZURE_CLIENT_SECRET to be the same as Value (not the Secret ID; the Value is only shown once)
11. Click on API permissions
12. Click on Add permission
13. Select Microsoft Graph
14. Click on Delegated permissions
15. Select Files.Read.All, Sites.Read.All and User.Read
16. Click Add permissions
17. Click Grant admin consent and confirm with Yes

**sync-worker app** (reads SharePoint into the index, no user involved):

1. Click on App registrations
2. Click on New registration
3. Name it sync-worker-app
4. Set Supported account types to Single tenant only
5. Click Register (no redirect URI needed)
6. On the Overview page, under .env.worker set SYNC_AZURE_CLIENT_ID to the Application (client) ID and AZURE_TENANT_ID to the Directory (tenant) ID
7. Generate a certificate on your machine, from the repo root (the worker signs in with it instead of a client secret: SharePoint's own API rejects tokens obtained with a secret):
    ```
    mkdir -p secrets
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=sync-worker-app" \
      -keyout secrets/sync-worker.key -out secrets/sync-worker.crt
    cat secrets/sync-worker.key secrets/sync-worker.crt > secrets/sync-worker.pem
    chmod 600 secrets/sync-worker.key secrets/sync-worker.pem
    ```
    `secrets/` is git-ignored and kept out of Docker images; Docker Compose mounts `secrets/sync-worker.pem` into the worker (on a Linux host, also run `sudo chown 999 secrets/sync-worker.pem` so the container's `app` user can read it; Docker Desktop handles this itself). The `.key` and `.pem` hold the private key: never upload or share them.
8. Under Manage click on Certificates & secrets
9. Click on the Certificates tab, then Upload certificate
10. Select `secrets/sync-worker.crt` (the public certificate only), add a description and click Add
11. Click on API permissions
12. Click on Add permission
13. Select Microsoft Graph
14. Click on Application permissions
15. Select Sites.Selected (access only to sites you grant below), User.ReadBasic.All (maps site group users to Entra ids) and GroupMember.Read.All (lists the owners of Microsoft 365 groups)
16. Click Add permissions
17. Click on Add permission again
18. Select SharePoint
19. Click on Application permissions
20. Select Sites.Selected
21. Click Add permissions
22. Click Grant admin consent and confirm with Yes
23. Navigate in a new tab to https://developer.microsoft.com/graph/graph-explorer
24. Sign in as a tenant admin
25. Click on the user profile on the top right
26. Click on Consent to permissions
27. Search for Sites.FullControl.All
28. Click on Consent and then Accept
29. Run GET `https://graph.microsoft.com/v1.0/sites/<tenant>.sharepoint.com:/sites/<site>?$select=id` and copy the `id` from the response
30. Run POST `https://graph.microsoft.com/v1.0/sites/<site id>/permissions` with this body:
    ```json
    {
      "roles": ["read"],
      "grantedToIdentities": [{"application": {"id": "<sync-worker Application (client) ID>", "displayName": "sync-worker-app"}}]
    }
    ```
    This grant covers both Microsoft Graph and SharePoint's API.
31. Under .env.worker set SYNC_SITES to the site, e.g. <tenant>.sharepoint.com:/sites/intranet. Repeat steps 29 to 30 for each site to index, comma separated

`read` is enough to crawl files and read their permissions. If `python -m sync_worker status` shows access denied on a site group's members (custom groups default to "only group members can view membership"), repeat step 30 with `"roles": ["fullcontrol"]`.

The certificate expires after a year (`-days 365`). To rotate it: generate a new one (step 7), upload it (steps 8 to 10), restart the worker, then delete the old one from Certificates & secrets. Entra accepts both while both are listed, so there is no downtime.

Most files are shared through the site's Owners/Members/Visitors groups. The worker reads their members through SharePoint's API (the certificate and SharePoint Sites.Selected), turns people into Entra ids (User.ReadBasic.All) and lists Microsoft 365 group owners (GroupMember.Read.All). Without these, such files stay `unresolved` and hidden from search; the worker log says which permission is missing.

## 2. Set up the environment

```
cp .env.example .env
cp .env.mcp.example .env.mcp
cp .env.worker.example .env.worker
```

- `.env` (shared by every service): `POSTGRES_PASSWORD`, an `INFERENCE_API_KEY` (any long random string) `GEMINI_API_KEY` for embeddings and `OPENAI_API_KEY` for `sharepoint_ask` (`openai:gpt-6-luna`). Optionally the `LANGSMITH_*` settings, to trace embedding requests with their cost.
- `.env.mcp`: `AZURE_*` of the mcp-server app and a `TOKEN_ENCRYPTION_KEY` (the command is in the file). `ASK_MODEL=none` turns the agent off. `ASK_MODEL` can also name another LangChain chat model plus its provider's key, once its `langchain-<provider>` package is installed in the mcp-server image.
- `.env.worker`: `AZURE_TENANT_ID` and `SYNC_AZURE_CLIENT_ID` of the sync-worker app (its certificate is `secrets/sync-worker.pem`, mounted by Docker Compose; set `SYNC_AZURE_CERT_PATH` when running without Docker), and `SYNC_SITES` (site ids or `contoso.sharepoint.com:/sites/HR`, comma separated).

Each Entra app's credentials stay in its own file, so neither service sees the other's. Keep `TOKEN_ENCRYPTION_KEY` out of the database and its backups: with both, someone could act as every signed-in user.

## 3. Start

```
docker compose --env-file .env -f infra/docker-compose.yml up -d --build
```

The worker discovers the sites' document libraries, crawls them and indexes every supported file (`.md .txt .csv .json .xml .log .html .docx .pptx .xlsx .pdf`). Watch it with

```
docker compose --env-file .env -f infra/docker-compose.yml exec sync-worker python -m sync_worker status
```

which shows sync lag per drive, the job queue, files by status and permission state, recent errors and Graph throttling. Sites can be managed there too: `python -m sync_worker sites add|disable|list`.

The MCP endpoint is `http://localhost:8080/api/mcp` (streamable HTTP), published on `127.0.0.1` only.

<details>
<summary>Without Docker</summary>

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
docker compose --env-file .env -f infra/docker-compose.yml up -d db   # or any Postgres with pgvector
.venv/bin/python -m sharepoint_rag.db                                  # apply the schema
.venv/bin/python -m sync_worker run &                                  # reads .env.worker and .env
.venv/bin/python -m mcp_server                                         # reads .env.mcp and .env
```

Without `INFERENCE_URL`, embedding runs in-process. Set `DATABASE_URL` and, when running `python -m inference_server`, `INFERENCE_URL` in `.env`; both services read it.

</details>

## 4. Connect a client

```
claude mcp add --transport http sharepoint http://localhost:8080/api/mcp
```

Then authenticate from the client (in Claude Code, run `/mcp` and choose *Authenticate*). Sign-ins are stored in Postgres, so they survive restarts.

Tools: `sharepoint_search` (the index, permission-checked), `sharepoint_ask` (an answer with sources; on when `OPENAI_API_KEY` is set), and live Graph tools `sharepoint_read_file`, `sharepoint_find_sites`, `sharepoint_list_drives`, `sharepoint_list_items`, `sharepoint_whoami`, `sharepoint_logout`. Until the first crawl finishes, search returns nothing; `SEARCH_BACKEND=graph` switches it to Microsoft Search instead.

## Team use (not set up)

Everything runs on localhost: the mcp-server is plain HTTP on `127.0.0.1:8080`. Serving other machines needs an HTTPS proxy in front of it (bearer tokens travel on every request, and Entra only accepts https redirect URIs off localhost). With one in place:

1. Set `PUBLIC_URL=https://mcp.example.com` in `.env.mcp` and add `https://mcp.example.com/oauth/callback` as a redirect URI of the mcp-server app.
2. If users connect from hosted clients (e.g. claude.ai), list their OAuth callbacks in `OAUTH_ALLOWED_REDIRECT_URIS` in `.env.mcp`. Loopback callbacks (desktop clients) are always allowed.
3. Turn off response buffering in the proxy: `sharepoint_ask` streams progress updates.

Per-user rate limits (`RATE_*`) and the audit log (`audit_log` table: who searched, read or asked what, with queries stored as a keyed HMAC) are on by default. Put secrets in a secret store rather than files on the host in production.

**More replicas.** The sync-worker scales with `--scale sync-worker=N` (replicas coordinate through Postgres, up to Graph's rate limits). The mcp-server keeps no state of its own except MCP sessions: behind several replicas use sticky routing per client (preferred: `sharepoint_ask` streams progress over a long-lived connection) or set `MCP_STATELESS=true`.

## Changing the embedding model

The index is versioned by [generation](architecture.md#index-generations), so a new model is filled alongside the old one, which keeps serving:

```
python -m sharepoint_rag.generations create gemini:gemini-embedding-001 768    # the worker backfills it from stored text
python -m sharepoint_rag.generations list                                     # progress
python -m sharepoint_rag.generations check 2                                  # permissions and coverage must match
python -m sharepoint_rag.generations activate 2                               # one switch; generation 1 is retired
python -m sharepoint_rag.generations drop 1
```

## Next

See [MCP OAuth sign-in](oauth.md) for how the sign-in flow works, and [Architecture](architecture.md) for the design.
