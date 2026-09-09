# @wow-aisuite/mcp-server

WOW AISuite — analyse your Xero backups from Claude, Cursor and other MCP
clients.

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

That is the whole configuration. There is no API key and no connection string:
the server is remote and you sign in through your browser the first time a tool
runs, so this file holds nothing secret.

Requires Node 18 or later, and an active WOW Backup & Restore subscription.

Full instructions, including clients that can connect without this package:
<https://wowbackupandrestore.com/docs/mcp>

## One-click install for Claude Desktop

Prefer not to edit a config file? The same server is packaged as a Claude Desktop
Extension. Download **[wow-aisuite.mcpb](https://wowbackupandrestore.com/downloads/wow-aisuite.mcpb)**,
then in Claude Desktop open Settings → Extensions and drag the file in (or
double-click it). It bundles this launcher, so there is nothing else to install,
and it shows the WOW mark next to its tool calls. It reaches the same endpoint —
use it *or* the config block above, not both.

## What this package is

A launcher. Claude Desktop and similar clients only accept a *command* in their
config file — a bare URL is dropped on the next save — so this bridges stdio to
the remote HTTPS endpoint.

Keeping the address here rather than in each customer's config means we can move
the endpoint by publishing a new version, instead of asking everyone to edit a
file we cannot reach.

## Overrides

For staging, or for support staff reproducing a problem:

```bash
WOW_MCP_URL=https://staging.example.com/mcp npx -y @wow-aisuite/mcp-server
# or
npx -y @wow-aisuite/mcp-server https://staging.example.com/mcp
```

## Licence

Licensed under the MIT License — see [LICENSE](LICENSE). © 2026 WOW Backup & Restore Ltd.
