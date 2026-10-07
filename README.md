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
| Permissions per agent: allowed, ask first or not allowed for each kind of tool | working |
| Split DNS, so agents resolve VPN-internal names like the Mac does | working with a stand-in server; not tried on a real VPN |
| Shared channels, `@mention` hand-offs between agents | working |
| Characters with drawn avatars, or a photo of your own | working |
| Desktop view (noVNC) with take control / return control | working |
| Providers: any OpenAI-compatible server (Ollama, LM Studio, vLLM, xAI, OpenRouter...) | verified with a local Ollama |
| Providers: OpenAI, Anthropic, Google Vertex AI (Gemini and Claude) | implemented and unit-tested; not called live |
| Jev decision provider | adapter against an assumed HTTP contract; not verified against a real Jev |
| macOS app with bundled Python, Keychain key storage, in-app setup | working |

## Install

Download the latest `Agent-Office-…-macos-arm64.zip` from
[Releases](https://github.com/josuablejeru/agent-office/releases), unzip it and
move *Agent Office* to Applications. You also need QEMU: `brew install qemu`.

Each release says how it was signed. A release that is not signed with an
Apple Developer ID and notarized is blocked by macOS when downloaded; build it
yourself instead (`scripts/build-app.sh`), or remove the download mark with
`xattr -dr com.apple.quarantine "/Applications/Agent Office.app"`.

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
   model, memory and CPUs. The Permissions tab sets what the agent may do
   (see [Policy](#policy)).
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
   every task (unless Memory is not set to "Allowed" in its permissions), so
   it still knows them after you clear the conversation or restart its
   computer. Entries can link things ("Dana - works at - Acme"),
   and asking about one thing also brings up what it is linked to. The Memory
   panel shows, searches, adds and removes entries. Agents can also keep their
   own SQLite databases (under `~/Databases` on their computer) for lists and
   records; the panel shows their tables and row counts.
6. **Approvals.** A destructive action, or any action of a kind you set to
   "Ask me first", pauses the run and shows the exact command, the risk and
   the reason. The sidebar marks agents that are waiting
   for you, the Dock icon shows how many are waiting, and it bounces if the
   app is in the background. It bounces once when a task finishes.
7. **Open computer.** Watch the agent's desktop. **Take control** gives you
   mouse and keyboard and pauses the agent, for logins, 2FA and CAPTCHAs.
8. **Channels.** A channel is a room everyone in the office can read. Write
   `@name` to ask an agent to act; its answer is posted there. Agents can read
   and post to channels themselves and hand work to each other with `@name`.
   A chain of hand-offs stops after three steps so agents cannot keep each
   other busy indefinitely. An agent needs its computer on to respond, and
   Channels must not be switched off in its permissions.

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

- Saving from the Settings dialog rewrites `config.yaml`: values are kept,
  comments in the file are not.
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

## Working behind a VPN

An agent's computer reaches the network through this Mac, so anything the Mac
can reach, including hosts behind a VPN, the agent can reach too.

Names need one more step. A VPN usually tells macOS "ask our servers about
`corp.example`" (split DNS), and a VM would not learn that. The app therefore
runs a small DNS forwarder on the Mac: each agent's computer sends its lookups
there, and every lookup is passed to whichever server macOS would use for that
name. It follows the Mac live: for full names (`jira.corp.example`),
connecting or disconnecting the VPN takes effect within seconds. Short names
(`jira`) depend on search domains, which an agent's computer only learns when
it is turned on, so restart it after connecting the VPN if you rely on those.
Settings shows the internal domains currently in effect.

- Extra rules, if your VPN client does not register its domains with macOS:

      network:
        dns_rules:
          corp.example: ["10.20.0.53"]

- `network.split_dns: false` turns the forwarder off; agents then use basic
  DNS and internal names will not resolve.
- An agent's computer picks the forwarder up when it is turned on. Its name
  lookups then depend on the app: while the app is closed, a computer that was
  left running ("keep running after I quit") cannot resolve names.
- **Signing in is separate.** The agent's Chrome has none of your sessions or
  certificates. Use Take control to log in once; tools that require a
  company-managed device or a client certificate will refuse it.
- **Mind what you connect.** An agent that can reach internal tools can be
  steered to them by a web page it reads, and what it reads is sent to its
  model provider. For work use, consider `policy.default_action:
  require_approval`.

## Policy

Each agent has a **Permissions** tab (also a button on the agent page). Per
kind of tool (run commands, files, browser, web search, databases, memory,
channels) choose:

- **Allowed**: runs by itself, except where a built-in rule asks for approval.
- **Ask me first**: every action of that kind waits for your approval.
- **Not allowed**: the agent is not offered the tool, and a call to it is
  refused. For Memory this also stops the automatic recall; for Channels it
  also stops `@mentions` from reaching the agent.

These can only restrict: the built-in rules below still ask for approval
whatever is chosen. A change applies to the agent's next action, even in the
middle of a task. With "Run commands" allowed an agent can do most things
through the shell (read files, fetch pages), so restrict that first to confine
one. The settings govern the agent's tools; they do not limit what programs on
its computer can reach on your network.

Every tool call passes the policy engine (`backend/policy/`) before it runs.

- **Hardcoded rules decide first and cannot be overridden.** Read-only
  commands are allowed. These need your approval: recursive `rm`, deleting or
  overwriting system paths, `find -delete`/`-exec`, writing to disk devices,
  formatting, shutdown and reboot, removing packages, changing users or
  passwords, and piping a download into a shell. In an agent's databases:
  dropping a table, and deleting or updating every row. The rules look through
  `sudo`, `env`, `bash -c`, pipes, `&&`, `;` and command substitution.
- **Everything else** follows the agent's own setting on its Permissions tab
  ("Anything the safety rules do not recognise"), or else the app-wide
  `policy.default_action`: `allow` (default; the agent acts inside its own
  computer) or `require_approval`.
- **Jev**, when configured (`jev: {base_url: ..., api_key_env: ...}`) and
  enabled for an agent, is asked only about calls no rule decides. The adapter
  posts `{"state", "questions"}` to `{base_url}/decide` and expects
  `{"answers"}`; that contract is an assumption.

Not covered by the built-in rules: what an agent does inside web pages (set
Browser to "Ask me first" to confirm each step), and scripts it writes and
runs when unrecognised actions are allowed.

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
- **Images in an agent's replies are not loaded.** They are shown as
  `[image: ...]`, so a manipulated agent cannot leak text through an image
  address. Text typed into password fields is never shown to the model.
- **Agents are separate.** Each has its own computer, disk, browser profile
  and memory; nothing is shared between them except channels.

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
| An agent does not answer in a channel | Its computer must be on, it must not be busy with another task, and Channels must not be "Not allowed" in its permissions |
| An agent keeps asking for approval for harmless things | Its Permissions tab has that kind of action on "Ask me first" |
| A task stalls for a long time and then fails | The Mac went to sleep: agents and their computers pause with it. Keep the Mac awake (plugged in, lid open) for long tasks. |
| A model server on your network works from Terminal but not from the app | System Settings › Privacy & Security › Local Network: allow Agent Office. macOS may ask again after the app is rebuilt. |
| An internal hostname does not resolve on an agent's computer | Settings › Network should list the domain while the VPN is connected. If not, add it under `network.dns_rules`. Turn the agent's computer off and on once after updating the app. |
| The app says it is already running | Quit the other copy, or a `start-dev.sh` session |

API reference: run from source and open http://127.0.0.1:8000/docs.

## Releases

Pushing a tag publishes a release with the app attached
(`.github/workflows/release.yml`):

    git tag v0.2.0 && git push origin v0.2.0

A tag with a suffix, such as `v0.2.0-rc1`, becomes a pre-release. The app is
signed and notarized when these repository secrets exist, and ad-hoc signed
otherwise:

| Secret | What it is |
| --- | --- |
| `MACOS_CERTIFICATE_P12` | A *Developer ID Application* certificate with its private key, exported as .p12 and base64-encoded (`base64 -i cert.p12 \| pbcopy`) |
| `MACOS_CERTIFICATE_PASSWORD` | The password chosen when exporting the .p12 |
| `NOTARY_KEY_P8` | The contents of an App Store Connect API key file (`AuthKey_….p8`) |
| `NOTARY_KEY_ID` | That key's ID |
| `NOTARY_ISSUER_ID` | The issuer ID shown above the key list in App Store Connect |

Set each with `gh secret set NAME`. To sign a local build the same way:
`SIGN_IDENTITY="Developer ID Application: …" scripts/build-app.sh`
(see `scripts/sign-and-notarize.sh`).

## Tests

```sh
uv run pytest                                   # unit tests, seconds
AGENT_OFFICE_E2E=1 uv run pytest tests/e2e -v   # real agent computers, about two minutes
AGENT_OFFICE_E2E=1 AGENT_OFFICE_E2E_MODEL=ollama/qwen3:8b \
  uv run pytest tests/e2e/test_live_model.py -v  # everyday tasks with a real model
```

The end-to-end tests start their own backend and agent computers in a throwaway
directory, with a scripted stand-in for the model. They need the base image and
do not touch your agents.

## License

[GNU Affero General Public License v3.0](LICENSE).
