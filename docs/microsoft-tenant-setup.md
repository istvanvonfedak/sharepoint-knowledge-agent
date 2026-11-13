# Microsoft tenant setup: from zero to a working SharePoint MCP server

By the end you'll have your own Microsoft tenant with a SharePoint site, the MCP server running locally, and Claude Code answering questions from documents you uploaded.

**You need:** a personal email address, a phone number, a credit card (for identity checks and the trial; nothing is charged if you cancel in time), [Docker](https://docs.docker.com/get-docker/), [Claude Code](https://claude.com/claude-code), and this repo cloned.

**Time:** about 60 minutes. Microsoft's portals change their layout often, so menu names may differ slightly.

## 1. Sign up for Azure

1. Go to [azure.microsoft.com/free](https://azure.microsoft.com/free) and choose *Start free*.
2. Sign in with a Microsoft account (or create one), then complete the phone and card verification.

This creates your **tenant** (a "Default Directory"). You'll use Entra ID, which is free, and won't create any Azure resources.

## 2. Create an admin user with MFA

Your sign-up account is a personal account that was added to the tenant. Create a dedicated cloud admin to work with instead.

1. Open the [Entra admin center](https://entra.microsoft.com) → **Users** → **New user** → *Create new user*.
2. Fill in the user principal name (e.g. `admin`, on your `*.onmicrosoft.com` domain), display name and a strong password. Copy the full UPN; you'll need it to sign in.
3. On the **Assignments** tab, add the **Global Administrator** role.
4. Open a private browser window and sign in as the new user. Change the password when prompted.
5. When asked to set up MFA, install Microsoft Authenticator on your phone, scan the QR code and approve the test prompt.

Back in your main window, check that MFA is enforced: **Entra ID** → **Overview** → **Properties** → **Manage security defaults** should be *Enabled*. New tenants have this on by default, and it requires every user to register for MFA.

> From here on, use the admin account for everything. Keep the personal account as a fallback only.

## 3. Start the Microsoft 365 Business Basic trial

SharePoint Online comes with a Microsoft 365 license, not with Azure.

1. As the admin, open the [Microsoft 365 admin center](https://admin.microsoft.com) → **Billing** → **Purchase services**.
2. Find **Microsoft 365 Business Basic** and choose *Start free trial*. It asks for a payment method; check the trial length and terms on screen, and set a reminder to cancel before it ends.
3. Assign the license to yourself: **Users** → **Active users** → your admin → **Licenses and apps** → tick *Microsoft 365 Business Basic* → **Save**.

Provisioning can take a few minutes. Your SharePoint address will be `https://<tenant>.sharepoint.com`.

## 4. Register the app in Entra ID

1. In the Entra admin center: **App registrations** → **New registration**.
2. Name it (e.g. `sharepoint-mcp`), keep *Single tenant*, and set the redirect URI to platform **Web** and `http://localhost:8080/oauth/callback`. Choose **Register**.
3. On the overview page, copy the **Application (client) ID** and **Directory (tenant) ID**.

**Add permissions.** Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**, and add `Sites.Read.All` and `Files.Read.All` (`User.Read` is already there). Then choose **Grant admin consent for \<tenant\>**. All three should show a green check.

**Create the client secret.** Go to **Certificates & secrets** → **New client secret**, give it a name and choose **Add**. Copy the **Value** now (not the Secret ID); it's only shown once.

## 5. Create a SharePoint site and add documents

1. Open `https://<tenant>.sharepoint.com` → **Create site** → **Team site**. Name it `Knowledge Base`.
2. Create two small files locally and upload them to the site's **Documents** library:

`onboarding.md`
```
# Onboarding
New hires get a laptop on day one. The office wifi password rotates monthly.
```

`expenses.txt`
```
Expense policy: meals up to 40 EUR per day are reimbursed without a receipt.
Travel must be booked at least 14 days in advance.
```

These contain facts only your tenant knows, so you can tell when Claude is really reading them.

## 6. Set up and start the MCP server

From the repo root:

```
cp .env.example .env
```

Fill in `.env` with the values from step 4:

```
AZURE_TENANT_ID=<Directory (tenant) ID>
AZURE_CLIENT_ID=<Application (client) ID>
AZURE_CLIENT_SECRET=<secret Value>
```

Start the server:

```
docker compose -f infra/docker-compose.yml up -d --build mcp-server
```

It listens on `http://localhost:8080/api/mcp`, published on `127.0.0.1` only. Check that it's up:

```
curl -i -X POST http://localhost:8080/api/mcp
```

A `401` response is correct: the server is running and asking for authentication. If it doesn't respond, check `docker compose -f infra/docker-compose.yml logs mcp-server`.

Prefer not to use Docker? See the *Without Docker* note in the [setup guide](setup.md).

## 7. Connect Claude Code and sign in

```
claude mcp add --transport http sharepoint http://localhost:8080/api/mcp
```

Start `claude`, run `/mcp`, select **sharepoint** and choose **Authenticate**. Your browser opens the Microsoft sign-in: use the admin account and approve the Authenticator prompt. The browser then returns to Claude Code, which should show the server as connected.

## 8. Validate

Ask Claude Code these, one at a time:

| Ask | Expect |
|---|---|
| "Who am I on SharePoint?" | Your admin's name and email (`sharepoint_whoami`) |
| "Find SharePoint sites" | The `Knowledge Base` site (`sharepoint_find_sites`) |
| "List the files in the Knowledge Base site" | `onboarding.md` and `expenses.txt` (`sharepoint_list_drives`, `sharepoint_list_items`) |
| "How often does the office wifi password rotate?" | Claude reads `onboarding.md` and answers *monthly* (`sharepoint_read_file`) |
| "Search SharePoint for expense policy" | `expenses.txt` in the results (`sharepoint_search`) |

> Search relies on Microsoft's index, which can take a while (sometimes much longer than a few minutes) to pick up new files. If it comes back empty, the other checks still prove the setup works; try again later.

**Sign-out check.** Ask Claude to sign out of SharePoint, then make another request. It should prompt you to authenticate again.


## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Server exits with `Missing required env var` | `.env` isn't filled in (re-run the `up` command after editing it) |
| `port is already allocated` or `address already in use` | Something else is using port 8080; stop it (the redirect URI is fixed to 8080) |
| `AADSTS50011` redirect URI mismatch | The redirect URI in Entra must be exactly `http://localhost:8080/oauth/callback`, platform *Web* |
| `AADSTS7000215` invalid client secret | You copied the Secret ID instead of the Value, or the secret expired |
| `AADSTS65001` consent required | Admin consent wasn't granted in step 4 |
| Sign-in works but there are no sites | The admin has no license assigned, or the site is still provisioning |
| Tools fail with an auth error after a restart | Tokens are in memory; authenticate again from `/mcp` |

## Clean up

Stop the server with `docker compose -f infra/docker-compose.yml down`. Cancel the Microsoft 365 trial under **Billing** → **Your products** before it ends, and delete the client secret if you won't keep using it.

## Next

[Setup](setup.md) is the short version of steps 6 and 7, for when your tenant is already configured. [MCP OAuth sign-in](oauth.md) explains how the sign-in flow works.
