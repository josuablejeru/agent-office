# MCP servers

> **Status (21 Oct):** built. Checked: unit tests for the registry, tool names and schemas, a
> stand-in MCP server over Streamable HTTP behind a bearer token (listing, calls, a failing tool,
> a refused token, an unreachable server), and agent runs where an MCP tool asks first by
> default, runs when allowed, never reaches the server when rejected, and is not offered when
> not allowed. In the app's window type with a real agent computer: a server set up and tested
> under Settings › MCP servers, ticked on the agent's Permissions tab, the approval dialog naming
> server, tool and arguments, and the answer after "Allow once"; the token was in neither
> config.yaml nor the database.
>
> Not checked: company MCP servers, servers that need OAuth sign-in, and Gemini's handling of
> MCP tool schemas.

**For:** you, wanting agents to use tools the company already exposes through MCP (tickets,
wiki, CRM) without handing tokens to every agent's computer.
**Outcome:** set a server up once, tick it per agent, and its tools appear in that agent's
toolbox under your approval rules.

## What is in
- **Settings › MCP servers:** name, address, token (kept in the Keychain) and the header it goes
  in, with Test (lists the tools) and Remove. Config in `config.yaml` under `mcp.servers`.
- **Per agent:** on the Permissions tab, which servers it may use, and an **MCP tools** level,
  **Ask me first** unless you choose otherwise.
- The app is the MCP client: tokens never reach an agent's computer. Tool lists are read at the
  start of a task (cached for five minutes), at most 40 tools per server; tool names become
  `mcp_<server>_<tool>`; results are text, capped, and treated as untrusted like web pages.

## What is out (on purpose)
- **stdio servers.** Started on the Mac they would run with your full rights, outside the agent's
  own computer, which is the boundary the product rests on. They could later run inside the
  agent's computer (that needs Node in the image, through Artifactory at work).
- OAuth sign-in flows, resources and prompts, and images in tool results (left out with a note).
