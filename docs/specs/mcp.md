# MCP servers

> **Status (21 Oct):** built. Checked:
> - Unit tests:
>   - Remote servers, using a stand-in MCP server over Streamable HTTP behind a bearer token. Covered: listing, calls, a failing tool, a refused token, an unreachable server.
>   - Command servers. Covered: environment from the Keychain, one process kept across calls, a restart after a crash, a missing command, stopping when the app quits, environment edits that keep untouched values, arguments kept out of logs and error messages, no retry at every task after a failure, and the login shell's network settings winning over the app's defaults.
>   - Import from Claude Desktop.
>   - Agent runs: ask by default, run when allowed, a rejected call never reaches the server, "Not allowed" hides the tools, and "always ask" asks even where MCP tools are allowed.
> - In the app's window type, with a real agent computer:
>   - A remote server and a command server, each added and tested in Settings and ticked for an agent. The approval dialog named server, tool and arguments.
>   - Two separate tasks used the same command-server process; saving an edit restarted it and kept the stored value.
>   - Import from a stand-in Claude Desktop config, then Test of an imported server.
>   - No process left after quitting, and no token or environment value in config.yaml, the logs or the database.
> - Real servers started from a Finder-like environment: `npx -y @modelcontextprotocol/server-everything` and `uvx mcp-server-time`. The latter is found only through fish's PATH on the development Mac.
>
> Not checked:
> - The user's own Claude Desktop config (deliberately not read in tests).
> - Company MCP servers, npx/uvx through a company proxy or Artifactory, and servers that need OAuth sign-in.
> - Gemini's handling of MCP tool schemas.

**For:** you, wanting agents to use the tools you already use in Claude Desktop (GitHub, tickets,
wiki, databases) without handing tokens to every agent's computer.
**Outcome:** set a server up once (or import it from Claude Desktop), tick it per agent, and its
tools appear in that agent's toolbox under your approval rules.

## What is in
- **Settings › MCP servers.** There are two kinds, as in Claude Desktop:
  - **Command:** `npx -y …`, `uvx …`, or a local program, with environment variables and an optional working folder. It runs on this Mac with your rights.
  - **Remote (HTTP):** an address, a token, and the header the token goes in.

  Each server has **Test** (lists its tools), **Remove**, and **Always ask** (on by default for command servers).
- **Import from Claude Desktop** reads `~/Library/Application Support/Claude/claude_desktop_config.json`. Environment values and tokens go into the Keychain.
- **Per agent:** on the Permissions tab, which servers it may use, and an **MCP tools** level, **Ask me first** unless you choose otherwise. A server set to always ask only ever tightens that level.
- **Command servers:**
  - Started on first use and kept running.
  - Restarted after a crash or an edit, and stopped when the app quits.
  - Their output goes to `~/.config/agent-office/logs/mcp-<name>.log`, which records the command but not its arguments.
  - They get PATH and network settings from your terminal shells: the account's shell, plus fish if you use it. That covers the proxy variables, CA variables, `npm_config_*`, `PIP_*` and `UV_*`.
  - They also get your `~/.npmrc` and pip/uv config through HOME, `UV_NATIVE_TLS=1`, and the company root certificates as `NODE_EXTRA_CA_CERTS`.
- **The app is the MCP client.** Tokens never reach an agent's computer. Tool lists are read at the start of a task and cached for five minutes. A server that can't be reached is retried after two minutes or when you save it. At most 40 tools per server. Results are capped text and treated as untrusted, like web pages.

## What is out (on purpose)
- **Running command servers inside the agent's own computer.** That would isolate them better, but
  it would also break the Claude Desktop setups people already have. Instead, they are labelled
  "runs on your Mac" and set to always ask.
- **Secrets in arguments.** Arguments are stored as written in `config.yaml`. Put tokens in
  environment variables, which go to the Keychain.
- OAuth sign-in flows, resources and prompts, and images in tool results (left out with a note).
