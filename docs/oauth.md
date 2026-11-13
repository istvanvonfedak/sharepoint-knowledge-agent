# MCP OAuth sign-in

The MCP server signs users in the same way hosted MCP servers (e.g. Figma) do: the client opens the browser, the user signs in, and the browser returns to the client. No tool call or copy-pasted link is involved.

## How it works

Entra ID doesn't support dynamic client registration, which MCP clients expect. So the server acts as its own OAuth authorization server for MCP clients and proxies the actual sign-in to Entra:

```
Claude Code            MCP server (localhost:8080)             Entra ID
    |  POST /api/mcp  ->  401 + metadata URL                      |
    |  POST /register (dynamic client registration)               |
    |  GET  /authorize (PKCE)  ------------------------------->   |  redirect to Microsoft login
    |                                                             |
    |                   GET /oauth/callback  <--------------------|  user signs in
    |  <- redirect to client's redirect_uri with our own code     |
    |  POST /token (code + PKCE verifier)                         |
    |  <- opaque access + refresh tokens                          |
    |  POST /api/mcp  (Authorization: Bearer ...)                 |
```

The server never hands Entra's tokens to the client. Entra tokens stay in MSAL's cache, keyed by the user's account (`home_account_id`). The client gets opaque tokens issued by this server, which map to that account. Each tool call looks up the user from the bearer token and gets a Graph token for them.

## Where the code is

- `apps/mcp-server/mcp_server/oauth_provider.py`: the OAuth provider (client registration, codes, tokens).
- `apps/mcp-server/mcp_server/server.py`: wires it into FastMCP and handles `/oauth/callback`.
- `packages/sharepoint/sharepoint_client/auth.py`: Entra sign-in via MSAL, one account per user.

## Usage

```
claude mcp add --transport http sharepoint http://localhost:8080/api/mcp
```

Then run `/mcp` in Claude Code and choose *Authenticate*. The Entra app registration needs no changes (redirect URI `http://localhost:8080/oauth/callback`).

## Limits

- State is in memory. Restarting the server drops all clients and tokens, so users must authenticate again.
- Access tokens last 1 hour; refresh tokens last 30 days and rotate on use.
- Intended for single-user local use: bound to `127.0.0.1`, and any local process can register as a client (it still needs the user to approve the sign-in in the browser).
