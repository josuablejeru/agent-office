# Agent Office

AI coworkers on your Mac. Each agent has its own persistent Linux computer (a
QEMU/HVF virtual machine with a desktop, Chrome, a shell and files), a model of
your choice, and a seat in shared channels where agents and you talk to each
other. Everything runs locally; model requests go only to the provider you pick.

## Status

| Area | State |
| --- | --- |
| Agents with their own persistent computer (Debian desktop, Chrome) | working |
| Tools: web search, browser (Playwright), shell, files, screenshots | working |
| Shared folder for handing files to an agent and collecting results | working |
| Long-term memory (facts with links, recalled automatically) and SQLite databases per agent | working |
| Chat, run limits, stop button, conversation clearing | working |
| Safety rules with approval dialog (allow once / reject) | working |
| Shared channels, `@mention` hand-offs between agents | working |
| Characters with drawn avatars, or a photo of your own | working |
| Desktop view (noVNC) with take control / return control | working |
| Providers: any OpenAI-compatible server (Ollama, LM Studio, vLLM, xAI, OpenRouter...) | verified with a local Ollama |
| Providers: OpenAI, Anthropic, Google Vertex AI (Gemini and Claude) | implemented and unit-tested; not called live |
| Jev decision provider | adapter against an assumed HTTP contract; not verified against a real Jev |
| macOS app with bundled Python, Keychain key storage, in-app setup | working |

## Requirements

- Apple Silicon Mac, macOS 13 or later
- QEMU: `brew install qemu`
- To build from source: [uv](https://docs.astral.sh/uv/) and Node.js 20+
- A model: a local server such as [Ollama](https://ollama.com), a server on
  your network, or a hosted provider

## The app

    scripts/build-app.sh

builds `Agent Office.app` and installs it into `/Applications`. Open it like
any other app. It starts the backend itself and shows the UI in its own window.

- **First start on a new Mac:** the window offers to build the system image
  agents boot from (about 1 GB of downloads, several minutes).
- **Quitting** shuts every agent's computer down cleanly; disks are kept, so
  each agent continues where it left off. Settings has a switch to keep them
  running in the background instead.
- **Self-contained:** the bundle carries the backend, the UI, its Python
  packages and its own Python interpreter. Only QEMU comes from the Mac.
- **Logs:** `~/Library/Logs/agent-office.log` (rotated at 5 MB).
- Signed ad hoc for this Mac; not notarized for other machines. Run
  `scripts/build-app.sh` again after changing the code.

## Run from source

    scripts/create-base-image.sh     # once; or use "Build now" in the UI
    scripts/start-dev.sh             # backend on :8000, UI on :5173, prints a sign-in link

    uv run python -m backend.app     # the app's window without building the bundle
    uv run pytest
    uvx ruff check backend guest tests scripts
    uv run python -m backend.vm.capabilities   # exits 0 when QEMU, HVF and firmware are present

Only one backend may use a data directory at a time; a second one refuses to
start. From source, agent computers keep running when the backend stops and are
found again on the next start.

## Using it

1. **New agent.** Pick a character (or upload a photo), a provider and a
   model, and adjust the job description. Under "More options": a fallback
   model, memory and CPUs, and which tools the agent may use.
2. **Turn on its computer.** The first start creates the agent's disk; later
   starts boot the same disk, so files, installed software and browser logins
   persist.
3. **Chat.** The agent searches the web, uses its browser, runs shell commands
   and reads and writes files. Every action is listed in the conversation and
   can be expanded; screenshots appear inline. The square button stops a run.
4. **Files.** Each agent has a `~/Shared` folder. The Files panel on the agent
   page sends files into it (up to 25 MB each) and saves files from it to your
   Downloads folder. Agents are told to look there for what you give them and
   to put results there.
5. **Memory.** An agent saves what is worth knowing later (people,
   preferences, decisions) and is shown the relevant entries at the start of
   every task, so it still knows them after you clear the conversation or
   restart its computer. Entries can link things ("Dana - works at - Acme"),
   and asking about one thing also brings up what it is linked to. The Memory
   panel shows, searches, adds and removes entries. Agents can also keep their
   own SQLite databases (under `~/Databases` on their computer) for lists and
   records; the panel shows their tables and row counts.
6. **Approvals.** A destructive action pauses the run and shows the exact
   command, the risk and the reason. The sidebar marks agents that are waiting
   for you.
7. **Open computer.** Watch the agent's desktop. **Take control** gives you
   mouse and keyboard and pauses the agent, for logins, 2FA and CAPTCHAs.
8. **Channels.** A channel is a room everyone in the office can read. Write
   `@name` to ask an agent to act; its answer is posted there. Agents can read
   and post to channels themselves and hand work to each other with `@name`.
   A chain of hand-offs stops after three steps so agents cannot keep each
   other busy indefinitely. An agent needs its computer on to respond.

### Characters and photos

The built-in characters are original drawings of generic office roles (the
boss, the accountant, the intern...). For anything else, use "Your photo" in
the agent's settings to upload an image; it is stored only on this Mac. Pictures
of real people or characters from TV shows are your own to supply and use.

## Models

Providers are listed in `~/.config/agent-office/config.yaml` and in Settings.
Nothing in the app is tied to a particular model.

    providers:
      ollama:                       # a local server
        type: openai-compatible
        base_url: http://localhost:11434/v1
        api_key: none
      gpu-box:                      # a server on your network
        type: openai-compatible
        base_url: http://192.168.1.50:8000/v1
        api_key: none
      anthropic:
        type: anthropic
        api_key_env: ANTHROPIC_API_KEY
      vertex:                       # Gemini and other models on Vertex AI
        type: vertex
        project: my-gcp-project
        region: global
      vertex-claude:                # Claude on Vertex AI
        type: vertex-anthropic
        project: my-gcp-project
        region: global

- Setting a provider to `null` in `config.yaml` (for example `openai: null`)
  hides a built-in one you do not use.
- **OpenAI-compatible servers** can be added from Settings ("Add a model
  server") without editing the file. Optional per-provider fields:
  `context_chars` (shorten old tool results to fit a small context window) and
  `extra_body` (extra fields sent with every request, for server-specific
  switches).
- **API keys.** `api_key_env` names the key. It is read from an environment
  variable of that name, or from the macOS Keychain, where the Settings dialog
  stores it. Keys are never written to this app's files, never logged and never
  sent to an agent's computer.
- **Test button.** Settings and the agent form have a Test button that sends
  one small request and says whether the model is reachable and whether it
  makes tool calls, which agents depend on. Failures show the reason (no key,
  not signed in, model not found, server not reachable).
- **Vertex AI** uses no API key. Set the project and region in Settings and
  sign in once with `gcloud auth application-default login`. Model ids are
  what Vertex calls them, for example `google/gemini-2.5-pro` for the `vertex`
  type and a Claude model id for `vertex-anthropic`.
- Screenshots are stored on the Mac and shown to you; they are not sent to the
  model.

Models differ a great deal in how reliably they use tools. The action list in
each conversation shows what an agent actually did, independent of what it
says it did.

Run limits (time waiting for your approval or while you hold control does not
count):

    limits:
      max_tool_calls_per_run: 50
      max_runtime_seconds: 900
      max_repeated_identical_calls: 3

## Policy

Every tool call passes the policy engine (`backend/policy/`) before it runs.

- **Hardcoded rules decide first and cannot be overridden.** Read-only
  commands are allowed. These need your approval: recursive `rm`, deleting or
  overwriting system paths, `find -delete`/`-exec`, writing to disk devices,
  formatting, shutdown and reboot, removing packages, changing users or
  passwords, and piping a download into a shell. In an agent's databases:
  dropping a table, and deleting or updating every row. The rules look through
  `sudo`, `env`, `bash -c`, pipes, `&&`, `;` and command substitution.
- **Everything else** follows `policy.default_action`: `allow` (default; the
  agent acts inside its own computer) or `require_approval`.
- **Jev**, when configured (`jev: {base_url: ..., api_key_env: ...}`) and
  enabled for an agent, is asked only about calls no rule decides. The adapter
  posts `{"state", "questions"}` to `{base_url}/decide` and expects
  `{"answers"}`; that contract is an assumption.

Not covered: what an agent does inside web pages, and scripts it writes and
runs when the default action is `allow`.

## Security model

- **Host API token.** A guest can reach the Mac's loopback through QEMU's
  user networking, so every API request needs the token in
  `secrets/api-token`. Requests addressed to other hostnames are refused.
- **Guest daemon.** Reachable only through a loopback port forward and only
  with the per-agent secret.
- **VNC and QMP** are unix sockets, not TCP ports. The UI reaches the desktop
  through an authenticated WebSocket bridge.
- **Other services on your Mac's loopback** that have no authentication of
  their own, such as a local model server, are reachable from inside an
  agent's computer.
- **Memory** lives on the agent's own disk and is deleted with the agent. The
  entries recalled for a task are part of the request sent to the model
  provider; with a hosted provider, avoid having agents remember secrets.
- **Web content and channel messages are untrusted input** to an agent. The
  approval rules apply to whatever it then attempts.

## Data

Everything lives in `~/.config/agent-office/` (or `$XDG_CONFIG_HOME`; override
with `AGENT_OFFICE_HOME`):

    config.yaml          providers, limits, policy, app settings
    agent-office.db      agents, conversations, runs, channels
    images/              debian-desktop.qcow2, download cache
    agents/<name>/       agent.yaml, disk.qcow2 (incl. the agent's memory and databases),
                         avatar, screenshots/, logs/
    run/                 control sockets
    secrets/             API token, guest secrets, image login password

Agent disks are large; exclude `images/` and `agents/*/disk.qcow2` if you back
up `~/.config`. A computer is not started when less than 5 GB of disk space is
free. The database is upgraded in place when the app gains new fields.

To uninstall: delete `/Applications/Agent Office.app`,
`~/.config/agent-office` and `~/Library/Logs/agent-office.log`, and remove the
`agent-office` entries from Keychain Access.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| "This Mac is not ready to run agent computers" | `brew install qemu`, then reopen the app |
| The model cannot be reached | Is the model server running and the URL in Settings right? The agent page says what is missing. |
| An agent's computer stays on "Starting up…" | `agents/<name>/logs/console.log`; restart it from the agent page |
| "paused because the Mac's disk is almost full" | Free up space, then turn the computer off and on |
| "This agent's computer is running an older version of its software" | Turn the computer off and on; it picks up the current tools at start |
| A website shows the agent a CAPTCHA | Open computer, Take control, pass the check, Return control. Agents are told not to try themselves. |
| An agent does not answer in a channel | Its computer must be on, and it must not be busy with another task |
| A model server on your network works from Terminal but not from the app | System Settings › Privacy & Security › Local Network: allow Agent Office. macOS may ask again after the app is rebuilt. |
| The app says it is already running | Quit the other copy, or a `start-dev.sh` session |

API reference: run from source and open http://127.0.0.1:8000/docs.
