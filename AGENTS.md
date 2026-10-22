# Notes for coding agents

Agent Office is a macOS app (Apple Silicon) that gives AI agents their own QEMU
virtual machines. Read `README.md` for what it does; this file is about working
on it.

## Layout

| Path | What |
| --- | --- |
| `backend/` | FastAPI backend, agent runs, policy, VM management (Python 3.12, uv) |
| `guest/` | The daemon inside each agent's VM (stdlib + websockets + playwright), delivered on every VM start |
| `frontend/` | React + TypeScript UI (Vite) |
| `scripts/` | `build-app.sh` (the .app), `sign-and-notarize.sh`, `create-base-image.sh` (the VM image) |
| `tests/` | Unit tests; `tests/e2e/` runs real VMs |
| `docs/specs/` | One-page specs of features, with what was and was not checked |

## Checks before you say something works

```sh
uv run pytest -q                                  # unit tests, seconds
uv run ruff check backend guest tests scripts     # lint (ruff is pinned in the dev group)
(cd frontend && npx tsc --noEmit && npm run build)
AGENT_OFFICE_E2E=1 uv run pytest tests/e2e -q     # real agent VMs, about two minutes
AGENT_OFFICE_E2E=1 AGENT_OFFICE_E2E_MODEL=ollama/qwen3:8b uv run pytest tests/e2e/test_live_model.py -v
```

- The e2e suite needs QEMU and the base image; it uses a throwaway data
  directory and a scripted model. Keep the Mac awake (`caffeinate -dimsu`):
  runs that stall usually mean the Mac slept.
- Keep "checked" and "not checked" apart when reporting. Real VPNs, hosted
  model providers and the user's work network can only be checked by the user.

## The user's data and machine

- Never touch `~/.config/agent-office` or the user's agents in tests. Use
  `AGENT_OFFICE_HOME=<scratch dir>` and symlink the base image into it.
- Never write test certificates into the user's keychain, and never use the
  user's Google Cloud credentials.
  `tests/conftest.py` hides them from every test. A backend started by hand
  for a check (a scratch server for a UI walk-through) needs the same:
  `GOOGLE_APPLICATION_CREDENTIALS=/nonexistent CLOUDSDK_CONFIG=<empty dir>`.
  Opening Settings lists every provider's models, which for Vertex AI asks
  Google.
- `scripts/build-app.sh` installs into `/Applications`; with `INSTALL_DIR=` it
  only builds into `dist/`. Check that the app is not running first.
- Write test runs to their own logs, not `~/Library/Logs/agent-office.log`.

## Releases

Releases are built, signed, notarized and published by
`.github/workflows/release.yml` when a tag `v*` is pushed. It runs one job per
stage (Python tests · Lint · UI → Build & sign → Notarize → Publish → Verify
download). After changing it, start it by hand from the Actions tab (Run
workflow): that dry run goes up to Notarize and publishes nothing.

**Ask the user before tagging.** Commit and push to `main` freely and let CI
pass; publishing a release is the user's decision.

1. Set `version` in `pyproject.toml` (plain `X.Y.Z`) and run `uv lock`.
2. Commit, push, and wait for CI to pass on `main`.
3. Tag and push:
   - `vX.Y.Z-rcN` for a **release candidate**: published as a GitHub
     pre-release, offered only to Macs on the **beta** update channel;
   - `vX.Y.Z` for a **stable** release: offered to everyone.
4. Verify the download: checksum, `spctl --assess --type execute` says
   "Notarized Developer ID", and `Contents/Resources/app/VERSION` holds the tag.

Usual path: `v0.5.0-rc1` → (fixes) `-rc2` … → `v0.5.0`.

### What the in-app updater relies on

`backend/updates.py` updates installed copies from GitHub Releases. Changing
any of these breaks updating for everyone already installed:

- **Asset names:** `Agent-Office-<tag without v>-macos-arm64.zip` and the same
  name plus `.sha256`, both attached to the release.
- **Version file:** the release build writes the tag into
  `Contents/Resources/app/VERSION` (`APP_VERSION_LABEL`); the updater refuses
  a download whose version does not match its tag.
- **Signature:** bundle identifier `com.josuablejeru.agent-office`, signed with
  the Developer ID Application certificate of team `X82QNBM4C4`, notarized.
  The updater checks all of these (`REQUIREMENT` in `backend/updates.py`).
- **Tags:** `vX.Y.Z` or `vX.Y.Z-rcN` only; a candidate sorts before its
  release, and the updater never offers an older version.

Only release builds update themselves; local builds (version from
`git describe`, e.g. `v0.4.0-3-gabc1234-dirty`) never do.

### Signing secrets

The workflow signs and notarizes when these repository secrets exist:
`MACOS_CERTIFICATE_P12`, `MACOS_CERTIFICATE_PASSWORD`, `NOTARY_KEY_P8`,
`NOTARY_KEY_ID`, `NOTARY_ISSUER_ID`. Never print, commit or log them.

## Conventions

- Safety rules (`backend/policy/`) and per-agent permissions only ever tighten:
  nothing may let a setting or Jev loosen a built-in rule.
- Nothing the model says or a web page contains is trusted: tool arguments
  come from the model; guest replies are validated on the host.
- Images and credentials never go into the database or logs.
- Specs live in `docs/specs/` with a status block saying what was checked.
- Model routing (`backend/agents/routing.py`): the router only ever sees the
  user's own chat messages, never tool results, web pages or channel posts.
  When a task moves to another model (escalation, fallback), earlier tool
  turns go through `flatten_tool_turns` (one report from the user's side):
  provider-specific data such as Claude thinking blocks or Gemini thought
  signatures must never reach another model, and written as the model's own
  turns, models imitate them instead of calling tools.
- MCP command servers run on the Mac with the user's rights: their "always
  ask" only ever tightens an agent's MCP level, and their arguments and
  environment values never go into logs.
