# Per-agent permissions

> **Status (6 Oct):** built. Checked: unit tests for every level against every kind of rule
> verdict; on real agent computers, "Ask me first" stopped a harmless `echo` for approval, a
> switched-off browser was neither offered to the model nor run when called anyway, and a second
> agent was unaffected; the Permissions tab was clicked through in a headless Chrome (change,
> save, reload, values kept); a copy of the real database migrated with the existing agent
> unchanged (everything allowed).
>
> Not checked: the tab inside the app's own window, and the approval dialog raised by
> "Ask me first" as seen in the UI (it is the same dialog as for risky commands).

**For:** you, giving different agents different amounts of trust: a research agent that may
browse but not run commands, a new agent that asks before everything.
**Outcome:** on an agent's Permissions tab you choose, per kind of tool, Allowed / Ask me first /
Not allowed, and the agent's very next action follows it.
**Appetite:** half a day.

## What is in
- A **Permissions** tab in each agent's settings (and a Permissions button on the agent page).
- Seven kinds of tool: run commands, files, browser, web search, databases, memory, channels.
- Three levels each:
  - **Allowed**: runs by itself, except where a built-in safety rule asks for approval.
  - **Ask me first**: every action of that kind waits for your approval.
  - **Not allowed**: the agent is not offered the tool, and a call to it is refused.
- **Anything the safety rules do not recognise**: per agent, allowed or ask, overriding the
  app-wide `policy.default_action`.
- The list of things that always ask, whatever is chosen.
- Changes apply to the agent's next action, even in the middle of a task.

## How the decisions combine
Your settings can only make an agent more restricted:
1. Not allowed: refused.
2. A built-in rule's verdict stands. "Allowed" never skips the approval for `rm -rf`.
3. Ask me first: anything that would have run by itself becomes an approval.
4. Jev, if enabled, is asked only about actions no rule covers, and cannot loosen 1 to 3.
5. Otherwise the agent's setting for unrecognised actions, or the app default.

## What is out (on purpose)
- Allow and block lists for websites. Checked only when opening a page, they would miss clicks,
  redirects and `curl`, and so would promise more than they enforce. Needs network-level
  filtering, which is its own piece of work.
- "Always allow this" from the approval dialog, and custom rules in free text.
- Limits on what programs on the agent's computer can reach on the network: these settings
  govern the agent's tools only.

## Known limits
- A tool switched back on in the middle of a task is offered to the model from the next task.
- With "Run commands" allowed, an agent can do in the shell most of what other tools do (read
  files, fetch pages). To really confine an agent, restrict "Run commands" first.
