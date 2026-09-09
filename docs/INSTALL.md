# Connecting to WOW AISuite

Ask questions about your Xero backups from Claude, Cursor, or any other MCP
client. You sign in with your normal WOW Backup & Restore account; the server
only ever shows you the organisations your subscription covers.

**Endpoint**

```
https://mcp.wowbackupandrestore.com
```

There is no API key to copy and no connection string to paste. A local server
like MongoDB's takes a credential in its config file because it connects to your
database directly; this one is remote, so you sign in through your browser and
the config file holds nothing secret. It is safe to commit or share.

---

## Claude Code

```bash
claude mcp add --transport http wow-aisuite https://mcp.wowbackupandrestore.com
```

Then run `/mcp` inside Claude Code and choose **Authenticate**. A browser opens,
you sign in, and you are done.

## Claude Desktop and claude.ai

Settings → **Connectors** → **Add custom connector**, and paste the endpoint.
Claude discovers the login automatically and opens a browser the first time.

This is the better route where it is available: no config file to edit, and
Claude manages the sign-in for you.

## Claude Desktop extension (one-click, branded)

If you would rather install a file than paste a URL, WOW AISuite is also packaged
as a Claude Desktop Extension. It installs in one click and shows the WOW mark
next to its tool calls.

1. Download **[wow-aisuite.mcpb](https://wowbackupandrestore.com/downloads/wow-aisuite.mcpb)**.
2. Open Claude Desktop → Settings → **Extensions**.
3. Drag the file onto that panel, or double-click it, and confirm **Install**.

The first tool call opens a browser to sign in, the same as every other route.
It reaches the same remote server as the connector above, so choose one or the
other — you do not need both. There is nothing secret in the file; the endpoint
lives inside it and moves when we publish a new version.

## Config-file clients

Some clients only accept a command in their config file — Claude Desktop's
`claude_desktop_config.json` validates stdio servers and silently drops a bare
URL on the next save. For those, a small launcher bridges the two:

```json
{
  "mcpServers": {
    "wow-aisuite": {
      "command": "npx",
      "args": ["-y", "@wow-aisuite/mcp-server"]
    }
  }
}
```

Config file locations:

| Client | Path |
| --- | --- |
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor | `~/.cursor/mcp.json` |

Restart the client after editing. The first tool call opens a browser to sign in.

### Clients that speak HTTP directly

If your client supports remote servers natively, skip the launcher and point it
at the endpoint:

```json
{
  "mcpServers": {
    "wow-aisuite": {
      "url": "https://mcp.wowbackupandrestore.com"
    }
  }
}
```

---

## What you can ask

Once connected, ask in plain language — Claude picks the right tool:

- *"Which organisations can I see, and when was each last backed up?"*
- *"Show me the invoices in Acme Ltd's latest backup over £10,000."*
- *"What changed between my last two backups of Acme Ltd?"*
- *"Across all my organisations, what's the total outstanding receivable?"*

Seven tools are available: listing organisations, backups and files; describing
and previewing a file; aggregating one file or sweeping every organisation;
comparing two backups; and signing out.

## Signing out

Ask Claude to run **wow-aisuite logout**. That ends the session on the server, so
the next request sends you back through sign-in — useful for switching accounts.

---

## Troubleshooting

**"Needs authentication" and it never clears.** The browser step did not
complete. Run `/mcp` in Claude Code, or remove and re-add the connector.

**You sign in successfully but see "subscription required".** The account you
signed in with is not the one that owns the backups. Sign out with the logout
tool and sign in with the email you use on the portal.

**"Could not verify your subscription right now."** A problem on our side rather
than yours — the server refuses rather than guessing at your entitlements. Retry
shortly.

**Too many requests.** Each account has a request budget, and a client stuck in
a loop can reach it. The response says how long to wait.
