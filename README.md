<p align="center">
    <a href="https://www.snapwonders.com/" target="_blank">
        <img src="https://raw.githubusercontent.com/snapWONDERS/snapWONDERS-MCP/main/.github/social-preview.jpg" alt="snapWONDERS MCP Server" width="640" />
    </a>
</p>

snapWONDERS — Expose what's hidden. Hide what's yours.


# snapWONDERS MCP

A local [MCP](https://modelcontextprotocol.io) server that lets an AI assistant work on files
**on your own machine** — hide a file inside a photo, reveal hidden content, run forensic
analysis, or convert between formats.

```
Claude Desktop ──stdio──▶ snapwonders-mcp ──HTTPS──▶ snapwonders.com/api
   (your machine)          (your machine)
```

## Why this exists

snapWONDERS already has a remote MCP server at `https://snapwonders.com/mcp`, and for
orchestration it works well. But it runs on *our* servers — so it cannot see your files, and MCP
tool calls carry JSON rather than bytes. An assistant with a photo on your desktop could create a
session and then have nowhere to put the file.

This server runs **where your files are**. You give it a path, it reads the file locally and
uploads it over HTTPS alongside the conversation. That is the whole difference, and it is what
makes snapWONDERS usable from Claude Desktop, which has no terminal for an assistant to fall
back on.

Results come back the same way: a hide, reveal or convert **writes the output to your disk** and
tells the assistant where it landed — not an asset id you would need a separate HTTP client to
redeem.

## Install

You need an API key from <https://snapwonders.com/profile/api-keys> (free account, and the
address must be verified — an unverified account's keys are always rejected).

Nothing to install ahead of time if your client can run `uvx`:

```bash
uvx snapwonders-mcp
```

Or install it properly:

```bash
pip install snapwonders-mcp
```

## Configure your client

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json` on Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS:

```json
{
  "mcpServers": {
    "snapwonders": {
      "command": "uvx",
      "args": ["snapwonders-mcp"],
      "env": { "SNAPWONDERS_API_KEY": "sw_your_key_here" }
    }
  }
}
```

### Claude Code

```bash
claude mcp add snapwonders --env SNAPWONDERS_API_KEY=sw_your_key_here -- uvx snapwonders-mcp
```

### Cursor

`~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "snapwonders": {
      "command": "uvx",
      "args": ["snapwonders-mcp"],
      "env": { "SNAPWONDERS_API_KEY": "sw_your_key_here" }
    }
  }
}
```

The key is read from the environment, never taken as a tool argument — so it does not pass
through the model's context, where it would end up in transcripts and logs.

**Passwords are different, and you should know it.** `hide_file` and `reveal_file` take the
passphrase *as a tool argument*, because only the user can supply it and it changes per call.
That means it does pass through the model's context and may appear in client logs and
transcripts. There is no way around that in an assistant-driven flow — but the care taken with
the API key does not extend to the passphrase, so treat anything you hide through an assistant
accordingly.

**This server can upload any file you can read.** That is the point of it — an assistant asks
for a path and the file is sent to snapWONDERS for processing. It also means a request like
"analyse ~/.ssh/id_rsa" would do exactly that, and an assistant following instructions from a
web page or document it has read could try to use it that way. Your MCP client's tool-approval
prompt is the real gate: read the path in the tool call before approving it.

## Tools

| Tool | What it does |
|------|--------------|
| `analyse_file` | Forensic analysis of a local image or video — A–F grade, metadata exposure, manipulation evidence, hidden content. Optional face detection and OCR. |
| `hide_file` | Conceal a file inside a cover image or video, and save the result to disk. |
| `reveal_file` | Extract content hidden in an image or video, and save it to disk. |
| `convert_file` | Convert an image or video to another format, and save the output to disk. |

Four tools, each a whole task. The REST API underneath is a session → upload → job → poll →
results sequence; exposed one call per step, a model has to carry five-step state and will
sometimes get it wrong. The orchestration lives here in Python instead, where it is
deterministic.

> **Passwords for `hide_file` are not recoverable.** They are never stored, and there is no
> reset. Be sure the user knows the passphrase before hiding something with it.

## When you would use the remote server instead

`https://snapwonders.com/mcp` needs no install and is right when the files are already
somewhere the server can reach, or when you only want session and job orchestration. Use this
local server when the files are on the user's machine — which, for a desktop assistant, is
almost always.

## Which upload it uses

Whichever fits, and you never choose: the [Python SDK](https://github.com/snapWONDERS/snapWONDERS-SDK-Python)
sends anything under the server-reported cap (currently 95 MB) as a single direct upload, and
falls back to the resumable TUS protocol above it.

## Licence

MIT — see [LICENSE](LICENSE). The licence covers this server only; the snapWONDERS API it calls
is proprietary.
